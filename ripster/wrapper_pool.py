"""Local Apple-wrapper POOL — run several `ripster-wrapper:premium` containers so
Apple (zhaarey) downloads can run in parallel instead of serialising on one
wrapper.

Design:
  * Image `ripster-wrapper:premium` = the owner's premium account baked in
    (committed from the logged-in amd-wrapper). Each instance must start with
    `args=-H 0.0.0.0 -L <apple-id>:<password>` so it logs in and serves.
  * Slot i → host ports decrypt 10020+i, m3u8 20020+i; container `rip-wrapper-i`.
  * AUTOSCALE: only as many instances as Apple demand needs (lazy start on
    acquire, scale down idle ones after a cooldown). Cap = pool_size.
  * acquire()/release() hand a free slot's ports to a task; the zhaarey engine
    writes those into its per-run config so concurrent tasks hit different
    wrappers.

Pure infra: nothing here is imported by app.py yet — wiring into the engine is
the next step. Safe to import/run standalone.

Docker access: local image run/stop/start needs NO registry → the Docker
credential-helper (broken in this env) is never invoked. Uses the Engine API
over the Windows named pipe via the docker SDK.
"""
from __future__ import annotations

import threading
import time

try:
    import docker
except Exception:  # pragma: no cover
    docker = None

IMAGE = "ripster-wrapper:premium"
NAME_PREFIX = "rip-wrapper-"
DECRYPT_BASE = 10020
M3U8_BASE = 20020
IDLE_COOLDOWN = 300.0   # stop an instance after this many seconds idle


def _client():
    if docker is None:
        raise RuntimeError("docker SDK not available")
    return docker.DockerClient(base_url="npipe:////./pipe/docker_engine")


class WrapperPool:
    def __init__(self, accounts: list[dict], size: int | None = None):
        """accounts: [{"id": str, "password": str, "label": str}, ...] — ONE
        DISTINCT Apple account per slot. accounts[0] is the primary
        (wrapper-apple-id/wrapper-password), accounts[1:] come from the
        wrapper-accounts config list.

        A single account cannot sustain 2+ concurrent wrapper sessions —
        Apple's own device-lease limit (see the ripster-apple-wrapper skill's
        "#1 killer"), so `size` is capped at len(accounts): there is no
        elastic "spin up more instances of the same account" mode any more.
        That elastic behavior was the pool's original design but was never
        actually safe — confirmed 2026-07-22 when even a FRESH device
        identity under the same account would have re-collided with itself.
        """
        self.accounts = accounts
        self.size = max(1, min(int(size or len(accounts)), len(accounts)))
        self._lock = threading.Lock()
        # slot -> {"busy": bool, "last_used": ts}
        self._slots: dict[int, dict] = {}

    # ── container lifecycle ────────────────────────────────────────────────────
    def _ports(self, i: int) -> tuple[int, int]:
        return DECRYPT_BASE + i, M3U8_BASE + i

    def _name(self, i: int) -> str:
        # Slot 0 reuses the already-running `amd-wrapper` (it holds 10020/20020);
        # never recreate/rebind it. New instances are rip-wrapper-1..N.
        return "amd-wrapper" if i == 0 else f"{NAME_PREFIX}{i}"

    def _data_dir(self, i: int) -> str:
        """Per-slot PERSISTENT Apple device-identity directory.

        `ripster-wrapper:premium` ships a BAKED-IN device identity (adi.pb +
        account/cookie sqlite DBs) — every container from the stock image
        presents the SAME Apple "device" to Apple's servers regardless of
        which -L account logs in through it. Reusing that shared identity
        (or one slot's identity for another slot) collides with Apple's
        per-device concurrent-session limit ("device limit", lease 3062) —
        found 2026-07-22 when two DIFFERENT accounts both hit that error
        identically through the unmodified image (see
        project_service_gating_2026-07-22 memory). Each slot gets its own
        directory, generated fresh on first boot and then reused (not wiped)
        on restart so the wrapper doesn't have to burn a fresh device lease
        every time it restarts."""
        from pathlib import Path as _P
        import os as _os
        base = _P(_os.environ.get("RIPSTER_BASE_DIR") or _P(__file__).resolve().parent.parent)
        d = base / "dist" / "docker" / "rootfs_pool" / f"acct{i}" / "data"
        d.mkdir(parents=True, exist_ok=True)
        return str(d)

    def _running(self, c, i: int) -> bool:
        try:
            ct = c.containers.get(self._name(i))
            ct.reload()
            return ct.status == "running"
        except Exception:
            return False

    def _start(self, c, i: int) -> tuple[int, int]:
        """Ensure instance i is up; return its (decrypt, m3u8) host ports.
        Slot 0 (amd-wrapper) is only ever started if stopped — never recreated
        (it already runs with its own proven-working fresh identity mount,
        set up manually 2026-07-22 at dist/docker/rootfs_working/data)."""
        dec, m3u = self._ports(i)
        name = self._name(i)
        try:
            ct = c.containers.get(name)
            ct.reload()
            if ct.status != "running":
                ct.start()
            return dec, m3u
        except Exception:
            if i == 0:
                # amd-wrapper must already exist; don't synthesize slot 0.
                raise RuntimeError("slot 0 (amd-wrapper) container not found")
            if i >= len(self.accounts):
                raise RuntimeError(f"no account configured for slot {i}")
            acct = self.accounts[i]
            c.containers.run(
                IMAGE, detach=True, name=name,
                environment={"args": f"-H 0.0.0.0 -L {acct['id']}:{acct['password']}"},
                ports={"10020/tcp": ("127.0.0.1", dec), "20020/tcp": ("127.0.0.1", m3u)},
                volumes={self._data_dir(i): {"bind": "/app/rootfs/data", "mode": "rw"}},
                restart_policy={"Name": "unless-stopped"},
            )
        return dec, m3u

    def _wait_listening(self, c, i: int, timeout: float = 25.0) -> bool:
        """Block until the instance logged in + is serving (account cached)."""
        name = self._name(i)
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                ct = c.containers.get(name)
                logs = ct.logs(tail=20).decode("utf-8", "replace")
                if "account info cached successfully" in logs and "listening" in logs:
                    return True
            except Exception:
                pass
            time.sleep(1.5)
        return False

    # ── public API ─────────────────────────────────────────────────────────────
    def ensure(self, n: int) -> int:
        """Make sure at least n (<=size) instances are running. Returns count up."""
        n = min(self.size, max(0, n))
        c = _client()
        up = 0
        with self._lock:
            for i in range(self.size):
                if up >= n:
                    break
                self._start(c, i)
                self._slots.setdefault(i, {"busy": False, "last_used": time.time()})
                up += 1
        return up

    def acquire(self) -> tuple[int, int, int] | None:
        """Reserve a free instance, starting one if needed (up to size).
        Returns (slot, decrypt_port, m3u8_port) or None if all busy at cap."""
        c = _client()
        with self._lock:
            # free running slot first
            for i in range(self.size):
                s = self._slots.get(i)
                if s and not s["busy"] and self._running(c, i):
                    s["busy"] = True
                    s["last_used"] = time.time()
                    dec, m3u = self._ports(i)
                    return i, dec, m3u
            # else start a new slot
            for i in range(self.size):
                if i not in self._slots or not self._slots[i]["busy"]:
                    dec, m3u = self._start(c, i)
                    self._wait_listening(c, i)
                    self._slots[i] = {"busy": True, "last_used": time.time()}
                    return i, dec, m3u
        return None

    def release(self, slot: int) -> None:
        with self._lock:
            if slot in self._slots:
                self._slots[slot]["busy"] = False
                self._slots[slot]["last_used"] = time.time()

    def scale_down_idle(self) -> int:
        """Stop instances idle longer than IDLE_COOLDOWN (keep slot 0 warm).
        Returns number stopped."""
        c = _client()
        stopped = 0
        now = time.time()
        with self._lock:
            for i in range(1, self.size):  # keep slot 0 always available
                s = self._slots.get(i)
                if s and not s["busy"] and now - s["last_used"] > IDLE_COOLDOWN:
                    try:
                        c.containers.get(self._name(i)).stop(timeout=5)
                        self._slots.pop(i, None)
                        stopped += 1
                    except Exception:
                        pass
        return stopped

    def status(self) -> list[dict]:
        c = _client()
        out = []
        for i in range(self.size):
            s = self._slots.get(i, {})
            dec, m3u = self._ports(i)
            out.append({"slot": i, "decrypt": dec, "m3u8": m3u,
                        "running": self._running(c, i),
                        "busy": bool(s.get("busy"))})
        return out


# ─── Module singleton + engine glue ──────────────────────────────────────────
# Everything below wires the pool into the live zhaarey download path. It is
# DEMAND-DRIVEN (smart): the singleton is created lazily on first use, acquire()
# starts a container only when a task needs one (slot 0 reuses the always-on
# amd-wrapper, so a lone Apple download spins up NOTHING new), and a daemon
# reaper scales idle extras back down. Any failure here must fall back to the
# old single-wrapper path — never break a download.

import re as _re
from pathlib import Path as _Path

_POOL: "WrapperPool | None" = None
_POOL_LOCK = threading.Lock()
_REAPER_STARTED = False

DEFAULT_POOL_SIZE = 3   # capped at len(accounts) regardless — see WrapperPool.__init__


def _configured_accounts(config: dict) -> list[dict]:
    """accounts[0] = the primary wrapper-apple-id/wrapper-password (same keys
    the single-wrapper UI/API already write). accounts[1:] = the
    wrapper-accounts list (added via Settings → Apple → "+ add account").
    Each entry needs its own real Apple ID — see WrapperPool's docstring for
    why one account can't be split across multiple slots."""
    accounts: list[dict] = []
    aid, pw = config.get("wrapper-apple-id"), config.get("wrapper-password")
    if aid and pw:
        accounts.append({"id": aid, "password": pw, "label": config.get("wrapper-apple-id", "")})
    for extra in (config.get("wrapper-accounts") or []):
        if isinstance(extra, dict) and extra.get("id") and extra.get("password"):
            accounts.append({"id": extra["id"], "password": extra["password"],
                             "label": extra.get("label") or extra["id"]})
    return accounts


def pool_enabled(config: dict) -> bool:
    """The pool only governs the LOCAL premium wrapper path. Disabled when the
    docker SDK is missing, when Apple is forced to the public wrapper, when no
    wrapper credentials are configured, or when fewer than 2 DISTINCT accounts
    are configured (a single account gains nothing from "pool mode" — see
    WrapperPool's docstring for why 1 account can't run 2+ sessions)."""
    if docker is None:
        return False
    if config.get("apple-pool") not in (True, "on", "1", 1):
        return False
    mode = (config.get("apple-wrapper") or "auto").strip().lower()
    if mode == "public":
        return False
    return len(_configured_accounts(config)) >= 2


def get_pool(config: dict) -> "WrapperPool | None":
    """Lazily build (once) the shared pool, or None if the pool is disabled."""
    global _POOL, _REAPER_STARTED
    if not pool_enabled(config):
        return None
    with _POOL_LOCK:
        if _POOL is None:
            accounts = _configured_accounts(config)
            try:
                size = int(config.get("apple-pool-size", DEFAULT_POOL_SIZE) or DEFAULT_POOL_SIZE)
            except Exception:
                size = DEFAULT_POOL_SIZE
            _POOL = WrapperPool(accounts, size=size)
        if not _REAPER_STARTED:
            _REAPER_STARTED = True
            threading.Thread(target=_reaper_loop, args=(_POOL,),
                             daemon=True, name="wrapper-pool-reaper").start()
        return _POOL


def pool_size(config: dict) -> int:
    """Concurrency cap for the Apple-local lane (1 when the pool is off)."""
    p = get_pool(config)
    return p.size if p else 1


def live_status() -> dict | None:
    """Cheap, NO-DOCKER snapshot of the live pool singleton — for the admin
    console and real-time `pool_update` WS broadcasts. Reads the in-memory slot
    map only (never touches docker, never creates the pool). Returns None when
    no Apple task has spun a wrapper up yet this session."""
    p = _POOL
    if p is None:
        return None
    with p._lock:
        slots = {i: dict(s) for i, s in p._slots.items()}
    now = time.time()
    busy = sum(1 for s in slots.values() if s.get("busy"))
    return {
        "size":         p.size,             # cap = max wrapper containers
        "active_slots": len(slots),         # containers touched this session
        "busy":         busy,               # wrappers downloading right now
        "free":         len(slots) - busy,
        "slots": [
            {
                "slot":    i,
                "busy":    bool(s.get("busy")),
                "decrypt": DECRYPT_BASE + i,
                "m3u8":    M3U8_BASE + i,
                "idle_s":  round(now - s.get("last_used", now), 1),
                "name":    "amd-wrapper" if i == 0 else f"{NAME_PREFIX}{i}",
            }
            for i, s in sorted(slots.items())
        ],
    }


def _reaper_loop(pool: "WrapperPool") -> None:
    while True:
        time.sleep(60)
        try:
            pool.scale_down_idle()
        except Exception:
            pass


# Keys the zhaarey Go binary + the Python wrapper-manager read for the wrapper
# address. We rewrite all four so the per-slot config is internally consistent.
_DEC_KEYS = ("decrypt-m3u8-port", "decrypt-port")
_M3U_KEYS = ("get-m3u8-port", "m3u8-port")


def ensure_all_decrypt_ports(config: dict) -> list[str]:
    """Start every pool container and return their decrypt endpoints, so a single
    album can fan its tracks across the WHOLE pool (apple-parallel-tracks) — each
    concurrent track decrypts through its own container, avoiding one wrapper's
    CKC serialisation. Returns [] when the pool is disabled."""
    p = get_pool(config)
    if p is None:
        return []
    try:
        n = p.ensure(p.size)
    except Exception:
        n = 0
    n = max(1, n)
    return [f"127.0.0.1:{DECRYPT_BASE + i}" for i in range(n)]


def slot_cwd(slot: int, decrypt_port: int, m3u8_port: int, base_dir,
             decrypt_ports_csv: str = "") -> str:
    """Write `<base>/.pool_cwd/slot{N}/config.yaml` — a byte-for-byte copy of the
    root config.yaml with ONLY the wrapper-port lines repointed at this slot's
    container. The zhaarey binary reads config.yaml from its cwd (every other
    path in the file is absolute), so running it here sends its decrypt/m3u8
    traffic to the slot's wrapper instead of the global one.

    ``decrypt_ports_csv`` (optional) lists ALL pool decrypt endpoints; when set,
    the Go tool spreads parallel tracks across them (apple-parallel-tracks)."""
    base = _Path(base_dir)
    text = (base / "config.yaml").read_text(encoding="utf-8")
    dec_v, m3u_v = f"127.0.0.1:{decrypt_port}", f"127.0.0.1:{m3u8_port}"

    def _set(t: str, key: str, val: str) -> str:
        pat = _re.compile(rf"^{_re.escape(key)}:.*$", _re.M)
        return pat.sub(f"{key}: {val}", t) if pat.search(t) else t + f"\n{key}: {val}\n"

    for k in _DEC_KEYS:
        text = _set(text, k, dec_v)
    for k in _M3U_KEYS:
        text = _set(text, k, m3u_v)
    if decrypt_ports_csv:
        text = _set(text, "decrypt-ports", f'"{decrypt_ports_csv}"')

    d = base / ".pool_cwd" / f"slot{slot}"
    d.mkdir(parents=True, exist_ok=True)
    (d / "config.yaml").write_text(text, encoding="utf-8")
    return str(d)
