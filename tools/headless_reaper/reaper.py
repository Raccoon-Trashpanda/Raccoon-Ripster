"""Shared headless-Chrome reaper — Tracker #44.

Every verification harness in C:\\dev that spawns a throwaway headless Chrome must
go through this module (or its Node twin, reaper.js/mjs) so the browser never
outlives its launcher. The three properties that stop the leak:

1. OWNERSHIP BY MARKER. We launch Chrome with a UNIQUE --user-data-dir whose
   basename starts with a fixed prefix plus our pid and a timestamp
   (`ripster-verify-<pid>-<YYYYmmddHHMMSS>-<nonce>`). Ownership is therefore never
   in doubt: a process is ours iff its command line names a user-data-dir whose
   basename starts with our prefix (or it is a descendant of such a process, or
   of our own launcher). We NEVER kill chrome.exe by image name — the owner keeps
   a real browser open at all times; killing it is a standing ban.

2. TREE KILL ON EVERY EXIT. `owned_browser()` is a context manager: teardown
   runs on success, on failure, on exception, and on Ctrl-C / SIGTERM / SIGBREAK
   (signal handlers raise so the finally block executes). We kill the whole
   process TREE (taskkill /T /F), not just the launcher PID — killing the parent
   alone is what orphaned renderers before.

3. STARTUP SWEEP. If a run was killed outright (crash, timeout, OOM) its finally
   never ran. `sweep_stale()` runs at START and reaps any leftover process whose
   marker dir is older than a grace window — so the next run cleans up after the
   last one.

A locked profile directory must never mean a surviving process: kill first, then
attempt removal; if removal still fails, leave the dir and move on. The directory
is scratch; the process is the memory leak.

`is_owned_cmdline` / `select_owned` are pure functions over fake process data so
the safety-critical predicate can be unit-tested: a process that is NOT ours must
never be selected.
"""
from __future__ import annotations

import contextlib
import datetime as _dt
import os
import re
import secrets
import signal
import subprocess
import sys
import tempfile
import time
from typing import Dict, Iterable, Iterator, List, NamedTuple, Optional, Tuple

# Console is cp1251 on the owner's box; a report that can't encode a char must
# not crash *after* it already succeeded.
try:
    sys.stdout.reconfigure(errors="replace")
    sys.stderr.reconfigure(errors="replace")
except Exception:
    pass

try:
    import psutil  # optional fast path; PowerShell is the fallback
except ImportError:
    psutil = None

# Fixed marker. Every owned profile dir starts with this; nothing else on the
# machine does. Changing it deliberately invalidates older orphans.
PREFIX = os.environ.get("HEADLESS_VERIFY_PREFIX", "ripster-verify")

# Grace window for profiles we cannot prove dead another way (playwright dirs,
# unparseable names). 5 min: longer than any normal verification launch,
# shorter than "leftover from a run that already died".
STALE_GRACE_SEC = int(os.environ.get("HEADLESS_VERIFY_STALE_GRACE", "300"))

# Playwright's own temp automation profiles (never a human profile: the owner's
# Chrome uses "User Data", playwright uses this ephemeral %TEMP% pattern).
EXTRA_MARKERS = tuple(filter(None, os.environ.get(
    "HEADLESS_VERIFY_EXTRA_MARKERS", "playwright_chromiumdev_profile-"
).split(",")))

# Owned profile basenames look exactly like this; a process arg merely containing
# the prefix word in some other shape is NOT ownership proof.
_DIR_RE = re.compile(rf"^{re.escape(PREFIX)}-(\d+)-(\d{{14}})(?:-[A-Za-z0-9._-]+)*$")

_UDD_RE = re.compile(r"--user-data-dir[= ]\"?([^\"]+?)\"?(?:\s|$)", re.IGNORECASE)

_BROWSER_FAMILIES = ("chrome.exe", "msedge.exe", "chromium.exe")

_CHROME_CANDIDATES = (
    os.environ.get("CHROME_PATH"),
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    os.path.join(os.environ.get("LOCALAPPDATA", ""),
                 r"Google\Chrome\Application\chrome.exe"),
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
)


def find_chrome() -> Optional[str]:
    for c in _CHROME_CANDIDATES:
        if c and os.path.isfile(c):
            return c
    return None


# ────────────────────────── pure ownership logic ────────────────────────────

class Proc(NamedTuple):
    pid: int
    ppid: int
    name: str
    cmdline: str
    rss: int = 0


def extract_user_data_dir(cmdline: str) -> Optional[str]:
    m = _UDD_RE.search(cmdline or "")
    return m.group(1).strip() if m else None


def is_owned_cmdline(cmdline: str, prefix: str = PREFIX) -> bool:
    """True ONLY if the command line names a --user-data-dir whose basename
    starts with `<prefix>-` (or an extra ephemeral-automation marker). The
    owner's real browser (default profile, no marker) and any lookalike
    argument must return False."""
    udd = extract_user_data_dir(cmdline)
    if not udd:
        return False
    udd = udd.rstrip("\\/\"'")
    base = os.path.basename(udd.replace("\\", "/").rstrip("/"))
    return base.startswith(f"{prefix}-") or any(base.startswith(m)
                                                for m in EXTRA_MARKERS)


def launcher_alive(d: str) -> Optional[bool]:
    """For our marker dirs: is the launcher pid baked into the name still alive
    (and old enough to be it)? None = not our naming scheme / unknown.
    pid dead ⇒ the run was killed outright ⇒ its chrome is an ORPHAN at any age;
    pid alive ⇒ a verification in flight; grace-window rules must not kill it."""
    base = os.path.basename((d or "").replace("\\", "/").rstrip("/"))
    m = _DIR_RE.match(base)
    if not m:
        return None
    pid, ts = int(m.group(1)), m.group(2)
    if psutil is not None:
        if not psutil.pid_exists(pid):
            return False
        try:
            started = psutil.Process(pid).create_time()
        except Exception:
            return True  # exists but unreadable (zombie-ish) — assume alive
        try:
            made = _dt.datetime.strptime(ts, "%Y%m%d%H%M%S").timestamp()
        except ValueError:
            return True
        return started >= made - 2.0  # reused pid ⇒ different, NEWER process
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    except Exception:
        return None
    return True


def select_owned(procs: Iterable[Proc], launcher_pid: Optional[int] = None,
                 prefix: str = PREFIX) -> List[Proc]:
    """Processes provably ours: cmdline marker, or descendant of a marked
    process (covers crashpad / helpers without the flag), or a browser-named
    descendant of our own launcher (a harness may have OTHER children — a dev
    server — that are not browsers and must not be selected). Self is never
    selected. Pure — testable on fake graphs."""
    procs = list(procs)
    by_parent: Dict[int, List[Proc]] = {}
    for p in procs:
        by_parent.setdefault(p.ppid, []).append(p)
    marked = {p.pid for p in procs if is_owned_cmdline(p.cmdline, prefix)}
    ours: set[int] = set()
    stack = list(marked)
    while stack:
        for child in by_parent.get(stack.pop(), ()):
            if child.pid not in ours:
                ours.add(child.pid)
                stack.append(child.pid)
    ours |= marked
    if launcher_pid is not None:
        # BFS from launcher entering only browser-named children (or children
        # already claimed above); once under a marked root everything is ours.
        lstack = [launcher_pid]
        while lstack:
            for child in by_parent.get(lstack.pop(), ()):
                if child.pid in ours or child.pid == launcher_pid:
                    continue
                if (child.name.lower() in _BROWSER_FAMILIES
                        or child.pid in marked):
                    ours.add(child.pid)
                    lstack.append(child.pid)
    ours.discard(os.getpid())
    ours.discard(launcher_pid)
    return [p for p in procs if p.pid in ours]


def profile_dir_age_sec(path: str, now: Optional[float] = None) -> float:
    """Age from the timestamp baked into the dir name (authoritative: dir
    mtimes drift). Falls back to filesystem ctime/mtime; missing dir → inf."""
    base = os.path.basename(path.replace("\\", "/").rstrip("/"))
    m = _DIR_RE.match(base)
    if m:
        try:
            made = _dt.datetime.strptime(m.group(2), "%Y%m%d%H%M%S").timestamp()
            return (time.time() if now is None else now) - made
        except ValueError:
            pass
    try:
        st = os.stat(path)
        return (time.time() if now is None else now) - max(st.st_ctime, st.st_mtime)
    except OSError:
        return float("inf")


# ─────────────────────────── process enumeration ────────────────────────────

def _owned_procs_live() -> List[Tuple[Proc, Optional[str]]]:
    """All processes provably ours (marker or marked-ancestor/descendant), with
    their marker dir. Batched: one psutil sweep or one PowerShell query."""
    if psutil is not None:
        procs = []
        for p in psutil.process_iter(["pid", "ppid", "name", "cmdline"]):
            try:
                cl = " ".join(p.info["cmdline"] or [])
                procs.append(Proc(p.info["pid"], p.info["ppid"],
                                  (p.info["name"] or "").lower(), cl,
                                  _rss(p)))
            except Exception:
                continue
        by_pid = {p.pid: p for p in procs}
        out = []
        for p in select_owned(procs):
            out.append((p, extract_user_data_dir(p.cmdline)))
        return out
    ps = (
        "$OutputEncoding=[Console]::OutputEncoding=[Text.UTF8Encoding]::UTF8;"
        "Get-CimInstance Win32_Process | Where-Object { $_.CommandLine } | "
        "ForEach-Object { "
        "$cl=$_.CommandLine -replace \"`t\",' ';"
        "\"{0}`t{1}`t{2}`t{3}\" -f $_.ProcessId,$_.ParentProcessId,"
        "$_.WorkingSetSize,$cl }"
    )
    try:
        r = subprocess.run(["powershell", "-NoProfile", "-Command", ps],
                           capture_output=True, text=True, timeout=60)
    except subprocess.TimeoutExpired:
        return []
    procs = []
    for line in (r.stdout or "").splitlines():
        parts = line.split("\t", 3)
        if len(parts) != 4:
            continue
        try:
            procs.append(Proc(int(parts[0]), int(parts[1]),
                              "", parts[3], int(parts[2] or 0)))
        except ValueError:
            continue
    return [(p, extract_user_data_dir(p.cmdline)) for p in select_owned(procs)]


def _rss(p) -> int:
    try:
        return p.memory_info().rss
    except Exception:
        return 0


def browser_family_report() -> Tuple[int, int]:
    """(count, total RSS bytes) of ALL chrome/msedge/chromium processes — a
    read-only counter for before/after verification output. Counts, never kills."""
    if psutil is not None:
        n = mem = 0
        for p in psutil.process_iter(["name"]):
            if (p.info["name"] or "").lower() in _BROWSER_FAMILIES:
                n += 1
                mem += _rss(p)
        return n, mem
    ps = ("Get-CimInstance Win32_Process -Filter \"Name='chrome.exe' or "
          "Name='msedge.exe' or Name='chromium.exe'\" | "
          "Measure-Object WorkingSetSize -Sum | "
          "ForEach-Object { \"$($_.Count)`t$($_.Sum)\" }")
    r = subprocess.run(["powershell", "-NoProfile", "-Command", ps],
                       capture_output=True, text=True)
    line = (r.stdout or "0\t0").strip().splitlines()[-1]
    c, _, s = line.partition("\t")
    return int(c or 0), int(float(s or 0))


# ───────────────────────────────── kill ──────────────────────────────────────

def kill_tree(pid: int) -> None:
    """Kill pid AND all its descendants (the /T flag is mandatory: killing only
    the launcher is exactly what leaked renderers)."""
    try:
        subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"],
                       capture_output=True, text=True, timeout=30)
    except Exception:
        pass


def _free_port() -> int:
    import socket
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _marker_dirs() -> List[str]:
    root = tempfile.gettempdir()
    try:
        entries = os.listdir(root)
    except OSError:
        return []
    return [os.path.join(root, e) for e in entries
            if (_DIR_RE.match(e) or any(e.startswith(m) for m in EXTRA_MARKERS))
            and os.path.isdir(os.path.join(root, e))]


def remove_dir_tolerant(path: str, retries: int = 8, delay: float = 0.4) -> bool:
    """Remove a profile dir, retrying while Chrome releases file handles.
    Returns True if gone; False on a persistent lock — never raises, because a
    locked directory must never mean a surviving process (already killed) or a
    failed verification run. Stale dirs are re-attempted by the next sweep."""
    import shutil
    for _ in range(retries):
        if not os.path.isdir(path):
            return True
        shutil.rmtree(path, ignore_errors=True)
        if not os.path.isdir(path):
            return True
        time.sleep(delay)
    return False


# ───────────────────────── context manager for harnesses ─────────────────────

@contextlib.contextmanager
def owned_browser(*, headless: bool = True, port: Optional[int] = None,
                  extra_args: Iterable[str] = (),
                  initial_url: str = "about:blank",
                  profile_suffix: str = "",
                  sweep: bool = True) -> Iterator[dict]:
    """Launch a uniquely-marked Chrome; guarantee the whole tree dies on EVERY
    exit path — success, exception, or signal.

        from headless_reaper.reaper import owned_browser
        with owned_browser(port=9333) as br:
            ...drive CDP against br["port"]...
    """
    chrome = find_chrome()
    if not chrome:
        raise RuntimeError("No Chrome/Edge found — set CHROME_PATH")

    if sweep:
        with contextlib.suppress(Exception):
            sweep_stale(reap=True)

    port = port or _free_port()
    udd = new_profile_dir(suffix=profile_suffix)
    args = [chrome, f"--user-data-dir={udd}", f"--remote-debugging-port={port}",
            "--no-first-run", "--no-default-browser-check", "--disable-gpu"]
    if headless:
        args.insert(1, "--headless=new")
    args += list(extra_args)
    args.append(initial_url)

    proc = subprocess.Popen(args, stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL)

    def _on_sig(signum, frame):
        raise KeyboardInterrupt(f"signal {signum}")  # make `finally` run

    prev: Dict[int, object] = {}
    for sig in (signal.SIGINT, signal.SIGTERM,
                getattr(signal, "SIGBREAK", None)):
        if sig is not None:
            try:
                prev[sig] = signal.signal(sig, _on_sig)
            except (ValueError, OSError, RuntimeError):
                pass  # non-main thread: finally still covers normal errors

    try:
        yield {"pid": proc.pid, "port": port, "user_data_dir": udd,
               "proc": proc, "cmdline": subprocess.list2cmdline(args)}
    finally:
        # 1. Tree kill first — even if the dir stays locked, the process dies.
        if proc.poll() is None:
            kill_tree(proc.pid)
        with contextlib.suppress(Exception):
            proc.wait(timeout=10)
        # Stragglers that outlived their parent (no reparenting on Windows, but
        # crashpad can linger): reap by marker dir — provably ours.
        for p, d in _owned_procs_live():
            if d and os.path.normcase(d) == os.path.normcase(udd):
                kill_tree(p.pid)
        for sig, h in prev.items():
            with contextlib.suppress(Exception):
                signal.signal(sig, h)
        # 2. Dir removal is best-effort; a lock is reported, never fatal.
        remove_dir_tolerant(udd)


def new_profile_dir(pid: Optional[int] = None, suffix: str = "",
                    now: Optional[float] = None) -> str:
    """Unique identifiable user-data-dir path under %TEMP% (created empty)."""
    pid = os.getpid() if pid is None else pid
    ts = _dt.datetime.fromtimestamp(time.time() if now is None else now)
    base = f"{PREFIX}-{pid}-{ts.strftime('%Y%m%d%H%M%S')}"
    if suffix:
        base += "-" + re.sub(r"[^A-Za-z0-9._-]", "-", suffix)
    base += "-" + secrets.token_hex(3)
    path = os.path.join(tempfile.gettempdir(), base)
    os.makedirs(path, exist_ok=True)
    return path


# ───────────────────────────── startup sweep ─────────────────────────────────

def sweep_stale(*, grace_sec: int = STALE_GRACE_SEC,
                reap: bool = True,
                now: Optional[float] = None) -> dict:
    """Find (and, unless reap=False, kill) leftovers of previously-killed runs.

    A process is only touched if its marker-dir is older than `grace_sec`;
    younger ones belong to runs in flight. Returns a summary dict.
    """
    now = time.time() if now is None else now
    stale_procs: List[Tuple[Proc, Optional[str]]] = []
    fresh_pids: set[int] = set()
    live_dirs: set[str] = set()
    for p, d in _owned_procs_live():
        alive = launcher_alive(d) if d else None
        if alive is False:
            orphan = True                    # launcher died outright → orphans
        elif alive is True:
            orphan = False                   # verification in flight — hands off
        else:
            age = profile_dir_age_sec(d, now) if d else float("inf")
            orphan = age >= grace_sec
        if orphan:
            stale_procs.append((p, d))
        else:
            fresh_pids.add(p.pid)
            if d:
                live_dirs.add(os.path.normcase(d))

    killed: List[dict] = []
    if reap:
        for p, _d in stale_procs:
            kill_tree(p.pid)
            killed.append({"pid": p.pid, "rss": p.rss})

    removed: List[str] = []
    locked: List[str] = []
    if reap:
        for path in _marker_dirs():
            if os.path.normcase(path) in live_dirs:
                continue
            if launcher_alive(path) is True:      # dir of a run still starting
                continue
            if not stale_dirs_only(path, now, grace_sec):
                continue
            if remove_dir_tolerant(path, retries=3, delay=0.2):
                removed.append(path)
            else:
                locked.append(path)
    return {"stale_procs": [{"pid": p.pid, "rss": p.rss, "dir": d}
                            for p, d in stale_procs],
            "killed": killed if reap else [],
            "removed_dirs": removed, "locked_dirs": locked,
            "fresh_skipped": len(fresh_pids)}


def stale_dirs_only(path: str, now: float, grace_sec: int) -> bool:
    return profile_dir_age_sec(path, now) >= grace_sec


def reap_by_cmdline_pure(cmdlines: Dict[int, str], *,
                         prefix: str = PREFIX) -> List[int]:
    """Testable core of orphan selection: given {pid: cmdline}, return PIDs that
    are provably ours (marker in user-data-dir). No OS calls — feed it a fake
    owner command line and assert it is rejected."""
    return [pid for pid, cl in cmdlines.items() if is_owned_cmdline(cl, prefix)]


# ─────────────────────────────── playwright ──────────────────────────────────

@contextlib.contextmanager
def owned_playwright_context(chromium, *, headless: bool = True,
                             sweep: bool = True, **launch_kwargs):
    """Playwright twin of owned_browser: a persistent context whose user_data_dir
    carries the marker, closed + tree-killed + dir-removed on every exit path."""
    if sweep:
        with contextlib.suppress(Exception):
            sweep_stale(reap=True)
    udd = new_profile_dir(suffix="pw")
    # Playwright sets --user-data-dir itself and REJECTS a duplicate in `args`
    # ("Pass user_data_dir parameter ... instead of specifying '--user-data-dir'
    # argument"), so the marker reaches the command line via the keyword. The
    # ownership predicate reads the live command line, not our intent, so the
    # proof of ownership survives unchanged.
    args = list(launch_kwargs.pop("args", ()))
    ctx = chromium.launch_persistent_context(user_data_dir=udd,
                                             headless=headless,
                                             args=args, **launch_kwargs)
    try:
        yield ctx
    finally:
        with contextlib.suppress(Exception):
            ctx.close()
        # Playwright closes its own tree; sweep-by-dir catches stragglers.
        for p, d in _owned_procs_live():
            if d and os.path.normcase(d) == os.path.normcase(udd):
                kill_tree(p.pid)
        remove_dir_tolerant(udd)
