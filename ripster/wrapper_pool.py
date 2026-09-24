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


def _bound_to_loopback(ct) -> bool:
    """Все опубликованные порты контейнера смотрят только в петлю?

    Docker Desktop теряет HostIp при перезапуске контейнера, поэтому проверять
    надо ФАКТ, а не то, с какими ключами он когда-то создавался.
    """
    try:
        binds = (ct.attrs.get("HostConfig") or {}).get("PortBindings") or {}
    except Exception:                                   # noqa: BLE001
        return False
    if not binds:
        return False
    for _port, rows in binds.items():
        for row in (rows or []):
            host = (row or {}).get("HostIp") or ""
            if host not in ("127.0.0.1", "::1"):
                return False
    return True


# ── Пауза входа после отказа Apple ─────────────────────────────────────────
# Отказ «лимит устройств» / «логин отклонён» за минуты не проходит, а каждая
# новая попытка входа сжигает ещё один слот устройства. Метка живёт на диске
# (перезапуск приложения её не стирает) и привязана к учётке + отпечатку
# пароля: владелец сменил пароль в настройках — пауза снялась сама.
# «account is disabled» — тоже: учётку Apple заблокировал, и каждый новый `-L`
# вход к этой же учётке жжёт слот устройства впустую (24.09.2026 три учётки
# владельца получали именно этот диалог).
_HARD_LOGIN_REASONS = ("device_limit", "login_failed", "account_disabled")
LOGIN_PAUSE_S = 12 * 3600


def _blocks_path():
    from pathlib import Path as _P
    import os as _os
    base = _P(_os.environ.get("RIPSTER_BASE_DIR") or _P(__file__).resolve().parent.parent)
    return base / "dist" / "docker" / "login_blocks.json"


def _acct_key(acct: dict) -> str:
    import hashlib as _h
    a = acct or {}
    pw = _h.sha256(str(a.get("password") or "").encode()).hexdigest()[:12]
    return f"{str(a.get('id') or '').lower()}|{pw}"


def _blocks_load() -> dict:
    import json as _j
    try:
        return _j.loads(_blocks_path().read_text(encoding="utf-8"))
    except Exception:
        return {}


def mark_login_blocked(acct: dict, reason: str, now: float | None = None) -> None:
    import json as _j
    if not (acct or {}).get("id"):
        return
    now = time.time() if now is None else now
    d = _blocks_load()
    d[_acct_key(acct)] = {"reason": reason, "until": now + LOGIN_PAUSE_S}
    try:
        p = _blocks_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(".tmp")
        tmp.write_text(_j.dumps(d, ensure_ascii=False, indent=1), encoding="utf-8")
        tmp.replace(p)
    except Exception as e:                              # noqa: BLE001
        print(f"[pool] метку паузы входа не записал: {e}", flush=True)


def login_pause(acct: dict, now: float | None = None) -> dict | None:
    """{'reason', 'until'} если вход этой учётки сейчас на паузе, иначе None."""
    if not (acct or {}).get("id"):
        return None
    now = time.time() if now is None else now
    p = _blocks_load().get(_acct_key(acct))
    if p and float(p.get("until") or 0) > now:
        return p
    return None


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
        logins = [a for a in accounts if a.get("kind", "login") == "login"]
        self.size = max(1, min(int(size or len(logins)), max(1, len(logins))))
        self._lock = threading.Lock()
        # slot -> {"busy": bool, "last_used": ts}
        self._slots: dict[int, dict] = {}
        # контейнер -> (когда спрашивали, страна учётки): `docker exec` не бесплатно
        self._session_memo: dict[str, tuple[float, str]] = {}

    # ── container lifecycle ────────────────────────────────────────────────────
    def reach(self) -> int:
        """Сколько слотов пул МОЖЕТ поднять: по одному на каждую настроенную
        учётку. Это НЕ параллельность (та остаётся в `self.size`): веер треков
        и лимит дорожек считаются от `size`, а перебор учёток при отказе обязан
        дотянуть до ПОСЛЕДНЕЙ учётки, иначе «перебрать все свои аккаунты»
        означает «перебрать первые три»."""
        return max(1, len(self.accounts))

    def is_login_slot(self, i: int) -> bool:
        """Можно ли этому слоту вообще доверить логин: учётка есть и это не
        media-user-token (тот врапперу не годится — см. `all_accounts`)."""
        return 0 <= i < len(self.accounts) and \
            self.accounts[i].get("kind", "login") == "login"

    def login_slots(self) -> list[int]:
        return [i for i in range(self.reach()) if self.is_login_slot(i)]

    def _parallel_slots(self) -> list[int]:
        """Слоты, которые пул держит поднятыми для ВЕЕРА треков: не первые
        `size` номеров, а `size` годных (login) учёток. Токен-запись в начале
        списка не должна съедать место параллельности."""
        return self.login_slots()[: self.size]

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
        every time it restarts.

        Каталог именуется по АККАУНТУ, а не по номеру слота (09.08.2026).
        Раньше было `rootfs_pool/acct{i}` — привязка к позиции в списке. Стоило
        переставить аккаунты местами, и два разных аккаунта получали одну и ту
        же identity, а это ровно тот самый device-limit. Владелец просил
        возможность менять приоритет аккаунтов вручную; с привязкой к номеру
        такая возможность была бы миной. По имени учётки перестановка
        безопасна: каждый аккаунт носит свою identity с собой.

        Старые каталоги `rootfs_pool/acct{i}` переносятся под новое имя при
        первом обращении — терять уже прогретую identity нельзя, её повторное
        создание стоит слота устройства."""
        from pathlib import Path as _P
        import os as _os, re as _re, shutil as _sh
        base = _P(_os.environ.get("RIPSTER_BASE_DIR") or _P(__file__).resolve().parent.parent)
        acct = (self.accounts[i].get("id") if i < len(self.accounts) else "") or f"slot{i}"
        tag = _re.sub(r"[^A-Za-z0-9]+", "", acct.split("@")[0])[:24] or f"slot{i}"
        d = base / "dist" / "docker" / "rootfs_id" / tag / "data"
        if not d.exists():
            old = base / "dist" / "docker" / "rootfs_pool" / f"acct{i}" / "data"
            if old.exists() and any(old.iterdir()):
                d.parent.mkdir(parents=True, exist_ok=True)
                try:
                    _sh.move(str(old), str(d))
                    print(f"[pool] identity migrated: acct{i} → {tag}", flush=True)
                except Exception as e:
                    print(f"[pool] identity migration acct{i}->{tag} failed: {e}", flush=True)
        d.mkdir(parents=True, exist_ok=True)
        return str(d)

    def _running(self, c, i: int) -> bool:
        try:
            ct = c.containers.get(self._name(i))
            ct.reload()
            return ct.status == "running"
        except Exception:
            return False

    def _create(self, c, i: int) -> tuple[int, int]:
        """Создать контейнер слота i с ПЕТЛЕВОЙ привязкой портов.

        Единственный рецепт создания: и путь «контейнера нет», и путь «мёртв или
        открыт наружу» (см. `_start`) обязаны приходить сюда. Разрыв между ними и
        есть баг 22.09.2026: `remove()` стоял в `try`, а `run()` — только в
        `except`, поэтому успешное удаление остановленного слота НЕ приводило к
        созданию. Сборщик гасит простые слоты через 5 минут, следующая задача их
        удаляла, и пул оставался с портами, которым нечего слушать.
        """
        if i >= len(self.accounts):
            raise RuntimeError(f"no account configured for slot {i}")
        dec, m3u = self._ports(i)
        acct = self.accounts[i]
        self._forget_session(i)      # старый ответ о сессии этого слота больше не факт
        try:
            # имя может занимать полумёртвый контейнер — снять, если что
            c.containers.get(self._name(i)).remove(force=True)
        except Exception:
            pass
        c.containers.run(
            IMAGE, detach=True, name=self._name(i),
            environment={"args": f"-H 0.0.0.0 -L {acct['id']}:{acct['password']}"},
            ports={"10020/tcp": ("127.0.0.1", dec), "20020/tcp": ("127.0.0.1", m3u)},
            volumes={self._data_dir(i): {"bind": "/app/rootfs/data", "mode": "rw"}},
            # БЕЗ restart_policy: автоподъём докером при старте системы —
            # это и есть путь, на котором теряется привязка к 127.0.0.1.
            # Пул сам поднимает слот, когда он нужен.
        )
        return dec, m3u

    def _refuse_if_paused(self, i: int) -> None:
        """Не логинить учётку, которой Apple только что отказал.

        Каждый новый контейнер — это новый `-L` вход, а каждый неудачный вход
        сжигает слот устройства (скилл ripster-apple-wrapper, «убийца №1»).
        23.09.2026: британские учётки в «лимите устройств», сборщик простоя
        гасит слоты через 5 минут, а лестница на каждой задаче с Invalid CKC
        пересоздавала их заново — то есть сама добивала учётку."""
        acct = self.accounts[i] if 0 <= i < len(self.accounts) else {}
        p = login_pause(acct)
        if p:
            raise RuntimeError(
                f"slot {i}: вход учётки на паузе до "
                f"{time.strftime('%d.%m %H:%M', time.localtime(p['until']))} "
                f"({p['reason']}) — повторный логин сжёг бы ещё одно устройство")

    def _start(self, c, i: int) -> tuple[int, int]:
        """Ensure instance i is up; return its (decrypt, m3u8) host ports.
        Slot 0 (amd-wrapper) is only ever started if stopped — never recreated
        (it already runs with its own proven-working fresh identity mount,
        set up manually 2026-07-22 at dist/docker/rootfs_working/data)."""
        dec, m3u = self._ports(i)
        if not self.is_login_slot(i):
            raise RuntimeError(
                f"slot {i} — media-user-token, а не логин: враппер его не поднимает")
        name = self._name(i)
        try:
            ct = c.containers.get(name)
            ct.reload()
        except Exception:
            if i == 0:
                # amd-wrapper must already exist; don't synthesize slot 0.
                raise RuntimeError("slot 0 (amd-wrapper) container not found")
            self._refuse_if_paused(i)
            return self._create(c, i)
        # Переиспользуем слот, только если он ЖИВ и привязан к петле.
        # `ct.start()` у остановленного контейнера обнуляет HostIp (Docker
        # Desktop 4.83), и порт расшифровки уходит на 0.0.0.0 — на машине с
        # несколькими учётками так открывался каждый слот пула. Поэтому
        # остановленный или неправильно привязанный слот не оживляем, а
        # пересоздаём: `_create` задаёт привязку заново.
        #
        # И ещё одно условие: в контейнере должна быть ТА учётка, которой слот
        # значится сегодня. Номера слотов — это порядок `wrapper-accounts`, а
        # контейнер помнит `-L`, с которым его создали: после правки списка
        # живой rip-wrapper-2 мог оказаться сессией чужого аккаунта, и пул
        # отправил бы задачу в другую страну, честней считая её своей.
        if ct.status == "running" and _bound_to_loopback(ct) and self._holds(ct, i):
            return dec, m3u
        if i == 0:
            raise RuntimeError(
                "slot 0 (amd-wrapper) остановлен или открыт наружу — "
                "его пересоздаёт ripster/amd.py, пул этого не делает")
        self._refuse_if_paused(i)       # до снятия: иначе слот пропадёт зря
        try:
            ct.remove(force=True)
        except Exception as e:
            print(f"[pool] slot {i} remove before recreate failed: {e}", flush=True)
        return self._create(c, i)

    def _holds(self, ct, i: int) -> bool:
        """Контейнер слота i создан под ту учётку, которая в списке сейчас."""
        try:
            env = (ct.attrs.get("Config") or {}).get("Env") or []
            id_ = str((self.accounts[i] or {}).get("id") or "")
            if not id_:
                return False
            return any(f"-L {id_}:" in str(e) or f"{id_}:" in str(e) for e in env)
        except Exception:
            return False

    def _serving(self, i: int, timeout: float = 1.5) -> bool:
        """Порт расшифровки слота ОТВЕЧАЕТ. Контейнер может числиться running,
        а Go внутри него ещё не слушает порт — тогда задача получит
        «connection refused», который со стороны выглядит как «нет прав в
        регионе». Отдавать наружу можно только проверенный порт."""
        import socket
        dec = self._ports(i)[0]
        s = socket.socket(); s.settimeout(timeout)
        try:
            s.connect(("127.0.0.1", dec))
            return True
        except Exception:
            return False
        finally:
            try:
                s.close()
            except Exception:
                pass

    # Порт ОТВЕЧАЕТ ≠ сессия ЕСТЬ. 22.09.2026 rip-wrapper-1 был запущен, порт
    # 10021 открыт, а внутри — `response type 6` (device limit) и `playback
    # error`: аккаунт не закеширован, и каждый запрос ключа через этот порт
    # возвращает Invalid CKC. Веер треков уходил в такой порт, а движок
    # объяснял отказ «нет прав в регионе», потому что его единственная проба
    # (`local_wrapper_session_alive`) спрашивает 30020 у СЛОТА НОЛЬ. Отсюда
    # правило: годится только слот, который сам называет свою учётку.
    def _session_country(self, i: int, max_age: float = 15.0) -> str:
        """Страна ЗАЛОГИНЕННОЙ учётки слота; '' — сессии нет (или не спросили).
        Дорого стоит (`docker exec`), поэтому короткие повторные пробы берутся
        из памяти; после пересоздания слота память чистится (`_forget_session`)."""
        now = time.time()
        name = self._name(i)
        hit = self._session_memo.get(name)
        if hit and now - hit[0] < max_age:
            return hit[1]
        cc = ""
        try:
            from ripster import apple_accounts
            cc = apple_accounts.container_storefront(name) or ""
        except Exception:
            cc = ""
        self._session_memo[name] = (now, cc)
        return cc

    def _forget_session(self, i: int) -> None:
        """Контейнер пересоздан — прежний ответ о его сессии больше не факт."""
        self._session_memo.pop(self._name(i), None)
        try:
            from ripster import apple_accounts
            apple_accounts.forget_container(self._name(i))
        except Exception:
            pass

    def _usable(self, i: int) -> bool:
        """Слот годится к отдаче задаче: порт жив И учётка закеширована."""
        return self._serving(i) and bool(self._session_country(i))

    def blocked_reason(self, i: int) -> str:
        """ПОЧЕМУ слот не отдаёт ключ — по журналу самого контейнера. Пустая
        строка значит «не выяснили»: выдумывать причину вместо этого нельзя.
        Реализация одна — в `apple_accounts`, иначе два описания одного
        «response type 6» рано или поздно разбегутся."""
        try:
            from ripster import apple_accounts
            return apple_accounts.container_block_reason(self._name(i))
        except Exception:
            return ""

    def usable_slots(self) -> list[int]:
        """Номера слотов с живым портом И закешированной учёткой (по факту)."""
        return [i for i in range(self.reach()) if self._usable(i)]

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
        """Make sure at least n (<=size) instances are running and SERVING.
        Returns count up.

        Слот, который не поднялся, НЕ считается поднявшимся и не блокирует
        остальных: раньше исключение из `_start` вылетало наружу и
        `ensure_all_decrypt_ports` получало нуль, а значит и весь перебор
        пула рассыпался на одном битом аккаунте."""
        n = min(self.size, max(0, n))
        c = _client()
        up = 0
        with self._lock:
            todo = list(self._parallel_slots())
        for i in todo:
            if up >= n:
                break
            if self.up_slot(i, client=c):
                up += 1
        return up

    def up_slot(self, i: int, client=None, wait: float = 25.0) -> bool:
        """Один слот: создать/поднять и дождаться живого порта. Публично —
        чтобы перебор учёток мог поднять КОНКРЕТНЫЙ слот, а не «любой
        свободный» (иначе ступень уезжала бы в другую страну)."""
        if not 0 <= i < self.reach():
            return False
        c = client or _client()
        with self._lock:
            try:
                self._start(c, i)
            except Exception as e:
                print(f"[pool] slot {i} did not start: {e}", flush=True)
                return False
            fresh = not self._serving(i)
            self._slots.setdefault(i, {"busy": False, "last_used": time.time()})
        if fresh:
            # ждём ЛОГИНА, а не голого порта: Go поднимает порт раньше, чем
            # аккаунт закеширован, и первая же расшифровка дала бы Invalid CKC
            self._wait_listening(c, i, timeout=wait)
        ok = self._usable(i)
        if not ok:
            why = self.blocked_reason(i)
            print(f"[pool] slot {i} непригоден: порт {self._ports(i)[0]} "
                  f"{'открыт' if self._serving(i) else 'мёртв'}, сессии учётки нет"
                  f"{'' if not why else ' (' + why + ')'}", flush=True)
            self._note(i, ready=False, reason=why)
            if why in _HARD_LOGIN_REASONS and 0 <= i < len(self.accounts):
                mark_login_blocked(self.accounts[i], why)
        return ok

    def _note(self, i: int, **kw) -> None:
        """Причина непригодности слота — в память слота, чтобы её увидел и
        `live_status` (консоль владельца), и итоговый отчёт перебора."""
        with self._lock:
            self._slots.setdefault(i, {"busy": False, "last_used": time.time()})
            self._slots[i].update(kw)

    def acquire(self) -> tuple[int, int, int] | None:
        """Reserve a free instance, starting one if needed (up to size).
        Returns (slot, decrypt_port, m3u8_port) or None if all busy at cap."""
        c = _client()
        with self._lock:
            parallel = self._parallel_slots()
            # free running slot first — но ТОЛЬКО с закешированной учёткой:
            # запущенный контейнер без сессии (device limit) открывает порт и
            # отдаёт Invalid CKC на каждом треке (22.09.2026, rip-wrapper-1).
            for i in parallel:
                s = self._slots.get(i)
                if (s and not s["busy"] and self._running(c, i)
                        and self._usable(i)):
                    s["busy"] = True
                    s["last_used"] = time.time()
                    s.pop("ready", None); s.pop("reason", None)
                    dec, m3u = self._ports(i)
                    return i, dec, m3u
            # else start a new slot — слот, который не удалось поднять,
            # пропускаем, а не роняем весь acquire: один битый аккаунт не должен
            # лишать задачу остальных.
            for i in parallel:
                if i in self._slots and self._slots[i]["busy"]:
                    continue
                try:
                    dec, m3u = self._start(c, i)
                except Exception as e:
                    print(f"[pool] acquire: slot {i} skipped: {e}", flush=True)
                    continue
                if not self._serving(i):
                    self._wait_listening(c, i)
                if not self._usable(i):
                    why = self.blocked_reason(i)
                    if self._serving(i):
                        print(f"[pool] acquire: slot {i} skipped — порт {dec} жив, "
                              f"а учётка не закеширована"
                              f"{'' if not why else ' (' + why + ')'}", flush=True)
                    else:
                        print(f"[pool] acquire: slot {i} not serving on {dec}, "
                              f"skipped", flush=True)
                    self._slots[i] = {"busy": False, "last_used": time.time(),
                                      "ready": False, "reason": why}
                    continue
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
        Returns number stopped.

        Перебирает ВСЕ поднятые слоты (`reach`), а не только первые `size`:
        перебор учёток поднимает и четвёртый, и пятый слот — и если сборщик
        до них не дотягивается, они висят запущенными после задачи навсегда."""
        c = _client()
        stopped = 0
        now = time.time()
        with self._lock:
            for i in range(1, self.reach()):  # keep slot 0 always available
                s = self._slots.get(i)
                if s and not s["busy"] and now - s["last_used"] > IDLE_COOLDOWN:
                    try:
                        c.containers.get(self._name(i)).stop(timeout=5)
                        self._slots.pop(i, None)
                        self._session_memo.pop(self._name(i), None)
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

    def health(self) -> list[dict]:
        """Честное «что с каждым слотом» для отчёта перебора и настроек:
        запущен, открыт ли порт, ЗАЛОГИНЕНА ли учётка, и почему нет.
        `country` пустой при живом порте — это и есть тот слот, который раньше
        молча отдавал Invalid CKC."""
        c = _client()
        out = []
        for i in range(self.reach()):
            run = self._running(c, i)
            open_ = run and self._serving(i)
            cc = self._session_country(i) if open_ else ""
            out.append({"slot": i, "decrypt": self._ports(i)[0],
                        "country": cc, "running": run,
                        "port_open": bool(open_), "ready": bool(open_ and cc),
                        "reason": ("" if (open_ and cc) else self.blocked_reason(i))})
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


def _looks_like_media_user_token(value: str) -> bool:
    """Media-user-token, вклеенный в поле «Apple ID». Владелец до появления
    отдельного поля для токена записывал его именно так (22.09.2026 в
    `wrapper-accounts` лежит запись с id длиной 246 символа, начинающимся с
    `0.`). For such an entry `-L 0.AsA5…:<password>` would be handed to the
    wrapper as an Apple ID, and every failed login burns a device lease
    (see skill #2) — so it must never be logged in."""
    v = str(value or "").strip()
    return v.startswith("0.") and len(v) > 150 and "@" not in v


def all_accounts(config: dict) -> list[dict]:
    """Каждая запись в порядке слота: {"id","password","label","kind"}, где
    kind = "login" (годится врапперу) или "token" (media-user-token: каталог,
    тексты, AAC — расшифровку НЕ делает).

    🔴 Порядок здесь = номер слота = каталог identity (`rootfs_id/<аккаунт>`) и
    номер контейнера. Запись НЕ ВЫЧЕРКИВАЕТСЯ из списка, даже бесполезная для
    враппера: вычеркнув середину, сдвинешь остальные аккаунты, а запущенный
    контейнер помнит учётку, с которой его создали, — два аккаунта на минуту
    окажутся одной identity. Бесполезная для расшифровки запись помечается
    kind="token" и остаётся на своём месте."""
    out: list[dict] = []
    aid, pw = config.get("wrapper-apple-id"), config.get("wrapper-password")
    if aid and pw:
        out.append({"id": aid, "password": pw,
                    "label": str(config.get("wrapper-apple-id", "")), "kind": "login"})
    for extra in (config.get("wrapper-accounts") or []):
        if not isinstance(extra, dict):
            continue
        tok = str(extra.get("token") or "").strip()
        eid = str(extra.get("id") or "").strip()
        if not tok and _looks_like_media_user_token(eid):
            tok = eid
        kind = "token" if tok else "login"
        cc = str(extra.get("country") or "").lower()
        # Метка уходит в JSON настроек и в журналы, поэтому токен-подобная —
        # не метка. До появления отдельного поля токен вставляли в «Apple ID», а
        # метку брали из него: в config у владельца до сих пор лежит 246-
        # символьный blob в `label`, и ручка `/api/wrapper/accounts` отдавала
        # его браузеру как есть.
        lab = str(extra.get("label") or "").strip()
        if _looks_like_media_user_token(lab):
            lab = ""
        if not lab:
            lab = (f"token · {cc}" if kind == "token" and cc
                   else "token" if kind == "token" else eid)
        out.append({"id": eid, "password": str(extra.get("password") or ""),
                    "label": lab,
                    "token": tok,
                    "country": cc,
                    "priority": extra.get("priority"),
                    "enabled": extra.get("enabled", True),
                    "kind": kind})
    return out


def account_identity(acct: dict) -> str:
    """Чем опознаётся учётка в реестре снятых и в счётчике неудач. Для логина —
    связка id+хеш пароля (то же, что ключ паузы входа), для токена — сам токен.

    Значение СЕКРЕТНО: наружу уходит только через `_ident`/`_mask`."""
    a = acct or {}
    tok = str(a.get("token") or "").strip()
    if a.get("kind") == "token" or (tok and not a.get("id")):
        return tok or str(a.get("label") or "")
    return _acct_key(a)


def display_label(acct: dict) -> str:
    """Метка учётки ДЛЯ ПОКАЗА: в JSON настроек, в журнал, в сообщение владельцу.

    🔴 У token-записей поле `label` в старых конфигах — сам media-user-token (его
    вклеивали в «Apple ID», а метку брали оттуда). Ручка removal до 24.09.2026
    отвечала этим значением браузеру дословно: «Аккаунт 0.AsA5… убран», то есть
    пароль по сути уехал в HTTP-ответ и в интерфейс. Поэтому показываем хвост по
    тому же правилу, что и остальные сервисы (`credential_health._mask`), а не
    метку как её завёл человек."""
    from .credential_health import _mask
    a = acct or {}
    cc = str(a.get("country") or "").lower()
    tok = str(a.get("token") or "").strip()
    if not tok and _looks_like_media_user_token(a.get("id") or ""):
        tok = str(a.get("id") or "").strip()
    lab = str(a.get("label") or "").strip()
    kind = a.get("kind") or ("token" if tok else "login")
    if kind == "token":
        # человеческая метка у token-записи — это либо заглушка («token»,
        # «token · es»), либо утёкший токен; показываем только настоящую
        safe = lab if lab and "token" not in lab.lower() \
            and not _looks_like_media_user_token(lab) else ""
        shown = safe or "token"
        if cc and cc not in shown:
            shown = f"{shown} · {cc}"
        return f"{shown} {_mask(tok)}" if tok else shown
    if _looks_like_media_user_token(lab):
        # логин-запись, в метке которой на самом деле токен
        return f"токен {_mask(lab)}"
    shown = lab or str(a.get("id") or "")
    # Apple ID — это почта владельца. Как метка она годится только та, что
    # человек вписал сам («roger987»); всё, что похоже на адрес, маскируется
    # так же, как секреты остальных сервисов: отчёт сторожа и список настроек
    # читаются вслух, а `label` по умолчанию и есть id.
    if "@" in shown:
        from .credential_health import _ident
        return _ident(account_identity(a))
    return shown


def login_accounts(config: dict) -> list[dict]:
    """Только те же учётки, что умеет поднять враппер."""
    return [a for a in all_accounts(config) if a.get("kind") == "login"]


def token_accounts(config: dict) -> list[dict]:
    """Учётки, заданные media-user-token'ом: каталог, тексты, AAC-путь — но НЕ
    расшифровка. Враппер их не поднимает.

    Токен отсюда уходит вызывающему в поле `token` — наружу его не печатать ни
    в логах, ни в `live_status`, ни в тестах."""
    return [a for a in all_accounts(config) if a.get("kind") == "token"]


def _configured_accounts(config: dict) -> list[dict]:
    """accounts[0] = the primary wrapper-apple-id/wrapper-password (same keys
    the single-wrapper UI/API already write). accounts[1:] = the
    wrapper-accounts list (added via Settings → Apple → "+ add account").
    Each entry needs its own real Apple ID — see WrapperPool's docstring for
    why one account can't be split across multiple slots.

    Токен-записи остаются на СВОИХ местах (см. `all_accounts`), но помечены
    kind="token": пул отказывается такой слот создавать, и лестница не зовёт его
    на расшифровку."""
    return all_accounts(config)


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
    return len(login_accounts(config)) >= 2


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


def managed_slot(config: dict, slot: int) -> bool:
    """Может ли пул создать/поднять слот с таким номером. Перебор учёток не
    должен звать создателя там, где у него нет ни рецепта, ни учётки.
    Граница — `reach` (все настроенные учётки), а НЕ `size`: параллельность
    ограничена `size`, но дотянуться при отказе надо до каждой учётки."""
    p = get_pool(config)
    return bool(p) and p.is_login_slot(int(slot))


def pool_health(config: dict) -> list[dict]:
    """Честное состояние каждого слота (запущен / порт открыт / учётка
    закеширована / почему нет). Для отчёта перебора и для настроек — чтобы
    «слот не поднялся» и «слот поднят, но ключей не даст» были РАЗНЫМИ
    строками, а не одной «нет прав в регионе»."""
    p = get_pool(config)
    try:
        return p.health() if p else []
    except Exception as e:
        print(f"[pool] health не снята: {e}", flush=True)
        return []


def ensure_slot(config: dict, slot: int, wait: float = 25.0) -> bool:
    """Поднять слот пула, СОЗДАВ контейнер, если его вообще нет.

    Нужен потому, что `apple_accounts.ensure_slot_up` умел только `docker start`
    над существующим контейнером. Контейнер, удалённый сборщиком binding-гейтом
    или потерянный из-за бага с пересозданием, был для него вечным «не поднялся»,
    и страна этого аккаунта выпадала из перебора молча. Здесь — единственный
    рецепт создания с петлевой привязкой (`WrapperPool._create`), поэтому
    безопасность не ослабевает: наружу слот не публикуется ни в одном пути.
    """
    p = get_pool(config)
    if p is None:
        print(f"[pool] slot {slot} не поднят: пул выключен "
              f"(нужны apple-pool и ≥2 разных учётки)", flush=True)
        return False
    return p.up_slot(int(slot), wait=wait)


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
                # почему слот молчал в последнем прогоне (device_limit и т.п.);
                # пусто — либо годен, либо причину не выясняли
                "ready":   s.get("ready", True),
                "reason":  s.get("reason", ""),
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
    CKC serialisation. Returns [] when the pool is disabled.

    🔴 В список попадают ТОЛЬКО порты, которые реально отвечают. Раньше список
    строился по номерам `range(n)` — «n слотов поднято, значит это слоты
    0..n-1», — и в него попадали мёртвые порты: Go веером гнал треки в
    `connection refused`, а наружу выходило «Invalid CKC / нет прав в регионе».
    """
    p = get_pool(config)
    if p is None:
        return []
    try:
        p.ensure(p.size)
        up = p.usable_slots()
    except Exception as e:
        print(f"[pool] ensure_all_decrypt_ports: {e}", flush=True)
        up = []
    return [f"127.0.0.1:{DECRYPT_BASE + i}" for i in up]


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
