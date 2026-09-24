"""
PC↔phone pairing — handshake + service-credential handoff.

Design: ARCH_2026-08-29_pc_phone_pairing.md. This module implements the
*minimum* needed for the mobile app to (a) pair with a running desktop
Ripster and (b) pull the owner's already-configured service tokens so the
phone's native engines can authenticate without re-entering anything.

Not implemented here (later slices): the three fan-out modes
(Mirror/initiator/Isolation) over /ws, and routing a DRM-SoundCloud /
Apple Music download *through* the PC. This is the credential bridge only.

Endpoints
    POST /api/pair/start        (localhost only)  → { code, expires_in, pc_id, pc_name, endpoints[] }
    POST /api/pair/claim        { code, mobile_id?, name? }
                                                  → { pc_id, pc_name, device_group_id, token,
                                                      capabilities, endpoints[], mode }
    GET  /api/pair/ping         (public, no auth) → { ripster, pc_id, pc_name, version }
    GET  /api/pair/credentials  Bearer <token>    → { updated_at, credentials{} }   (stamps synced_at;
                                                                                     only for a device the
                                                                                     owner opted in)
    POST /api/pair/mode         Bearer | loopback { mode }   fan-out: mirror|initiator|isolation
    POST /api/pair/share        (owner only)      { device_id, enabled: bool }  per-device credential opt-in
    POST /api/pair/unpair       Bearer <token>    device unpairs itself
    POST /api/pair/revoke-all   (localhost only)  drop every paired device
    GET  /api/pair/status       (localhost only)  → pc_id, pc_name, endpoints[], mode, devices[]

Identity ("папа-мама"): the PC has a STABLE `ripster-instance-id` in
config.yaml — it outlives `pairing_state.json`. Pairing binds one phone to
that id; the bond is the synced service credentials + the device token, NOT
a live socket, so the autonomous engines (SoundCloud/Deezer/Qobuz/Tidal/…)
keep working on the phone with the PC off or out of range.

Reachability: `endpoints[]` lists only THIS PC's own addresses — every LAN
IP, the `<host>.local` mDNS name, and a remote URL *iff this PC has one*
(`pair-remote-url`, or the serveo tunnel from `tunnel-subdomain`). Nobody
gets someone else's tunnel. The phone tries them in order, caches the
last-good one, and re-finds the PC on the LAN via `/api/pair/ping` when the
IP changes.

Auth model: /start, /share, /revoke-all, /status are loopback-only (the
desktop itself). /claim is authed by the one-shot 8-digit code. /ping is
public and returns no secrets. /credentials, /mode, /unpair and the Apple
proxy are authed by the opaque device token. All are auth-public +
CSRF-exempt: own auth, non-browser client.
"""
from __future__ import annotations

import base64
import ipaddress
import json
import secrets
import socket
import time
import uuid
from datetime import datetime
from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

router = APIRouter()

_s: dict = {}                      # config, save_config, base_dir, broadcast
_state_path: Path | None = None
_state: dict = {}                  # persisted: device_group_id, pending, mode, tokens[]
                                   # (каждый токен несёт свой device_id + share_credentials)
_CODE_TTL = 300                    # seconds a pairing code is valid
_MAX_UNSYNCED = 20                 # cap on device tokens that never pulled creds (test/re-pair churn)
_SYNCED_STALE_DAYS = 180           # a device that HAS synced is dropped only after this long silent
_PROTECT_DAYS = 30                 # ANY token seen within this window is never evicted, period
_claim_fails: list[float] = []     # timestamps of wrong-code attempts (brute-force brake)
_FANOUT_MODES = ("mirror", "initiator", "isolation")
_PORT = 7799                       # Ripster's fixed local port (see CLAUDE.md)


# ── persistence ──────────────────────────────────────────────────────────────

def _load_state() -> None:
    global _state
    try:
        _state = json.loads(_state_path.read_text("utf-8")) if _state_path.exists() else {}
    except Exception:
        _state = {}
    # device_group_id stays for wire back-compat, but it now MIRRORS the stable
    # PC identity from config.yaml — so a lost/rebuilt pairing_state.json keeps
    # the same "папа" id and paired phones re-auth instead of orphaning.
    _state["device_group_id"] = _pc_id()
    _state.setdefault("tokens", [])
    _state.setdefault("pending", {})           # code -> expiry epoch (survives restart)
    _prune_pending()
    _evict_tokens()
    mode = _state.get("mode")
    if mode not in _FANOUT_MODES:
        _state["mode"] = "mirror"
    _migrate_share_flags()
    _save_state()


def _new_device_id() -> str:
    """Непубличный ярлык устройства для UI: по нему владелец щёлкает отдачу
    учёток. Токен показывать в интерфейс нельзя — это и есть секрет доступа."""
    have = {t.get("device_id") for t in _state.get("tokens") or []}
    while True:
        d = uuid.uuid4().hex[:12]
        if d not in have:
            return d


def _migrate_share_flags() -> None:
    """Отдача учёток — ЯВНЫЙ выбор владельца, и на каждом устройстве свой.

    До 24.09.2026 флаг был один на весь ПК и стоял в True: телефон забирал все
    учётки владельца, даже когда он этого не выбирал и тумблера не видел
    (BACKLOG → Security, ARCH_2026-08-29_pc_phone_pairing.md:299). Теперь флаг
    живёт в записи устройства, новое сопряжение рождается выключенным.

    Уже спаренные устройства наследуют то состояние, что действовало раньше, а
    не слетают в False: правка безопасности не должна молча ронять телефон,
    который сегодня работает. Ключа на живой машине просто не было — значит
    действовал старый дефолт True.
    """
    legacy = _state.pop("share_credentials", None)
    inherited = True if legacy is None else bool(legacy)
    inherited_share = []
    for t in _state.get("tokens") or []:
        if "device_id" not in t:
            t["device_id"] = _new_device_id()
        if "share_credentials" not in t:
            t["share_credentials"] = inherited
            inherited_share.append(t.get("name") or t.get("mobile_id") or "устройство")
    if inherited_share:
        n = len(inherited_share)
        names = ", ".join(inherited_share[:5]) + ("…" if n > 5 else "")
        print(f"[pairing] миграция: отдача учёток теперь выбирается по устройству — "
              f"{n} устр. ({names}) унаследовали «{'вкл' if inherited else 'выкл'}», "
              f"новые сопряжения выключены по умолчанию", flush=True)


def _save_state() -> None:
    try:
        tmp = _state_path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(_state, indent=2), "utf-8")
        tmp.replace(_state_path)               # atomic — no half-written state on crash
    except Exception:
        pass


def _prune_pending() -> None:
    now = time.time()
    p = _state.get("pending") or {}
    _state["pending"] = {c: e for c, e in p.items() if isinstance(e, (int, float)) and e > now}


def _evict_tokens() -> None:
    """Никогда не выкидываем молча устройство, которое УЖЕ забирало учётки
    (`synced_at`) — только если оно молчит дольше _SYNCED_STALE_DAYS. Остальные
    (тестовые сопряжения, ре-пейр без синка) держим числом до _MAX_UNSYNCED,
    самые свежие. Раньше жёсткий лимит 3/10 вытеснял реальный телефон — отсюда
    вечное «сопряжение слетело после рестарта»."""
    now = time.time()
    toks = _state.get("tokens") or []
    kept, maybe = [], []
    for t in toks:
        if not t.get("token"):
            continue
        seen = t.get("seen", t.get("created", 0))
        age_days = (now - seen) / 86400.0
        # Любой токен, живой за последние _PROTECT_DAYS — НЕ трогаем никогда.
        if age_days <= _PROTECT_DAYS:
            kept.append(t)
        elif t.get("synced_at") and age_days <= _SYNCED_STALE_DAYS:
            kept.append(t)
        else:
            maybe.append(t)          # старьё — под лимит
    maybe.sort(key=lambda t: t.get("seen", t.get("created", 0)), reverse=True)
    room = max(0, _MAX_UNSYNCED - len(kept))
    _state["tokens"] = kept + maybe[:room]


# ── identity + reachability ─────────────────────────────────────────────────

def _pc_id() -> str:
    """Стабильный ID этого Ripster-ПК («папа»). Живёт в config.yaml, а не в
    pairing_state.json — переживает сброс состояния сопряжения. Генерируется
    один раз."""
    cfg = _s.get("config") or {}
    pid = str(cfg.get("ripster-instance-id") or "").strip()
    if not pid:
        pid = str(uuid.uuid4())
        try:
            cfg["ripster-instance-id"] = pid
            if _s.get("save_config"):
                _s["save_config"](cfg)
        except Exception:
            pass
    return pid


def _pc_name() -> str:
    cfg = _s.get("config") or {}
    nm = str(cfg.get("device-name") or "").strip()
    if nm:
        return nm[:60]
    try:
        return (socket.gethostname() or "Ripster PC")[:60]
    except Exception:
        return "Ripster PC"


def _lan_ips() -> list[str]:
    """Приватные IPv4 этого хоста. UDP-connect трюк даёт «тот, что смотрит в
    сеть»; getaddrinfo добирает остальные интерфейсы."""
    ips: list[str] = []
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.connect(("10.255.255.255", 1))
            ips.append(s.getsockname()[0])
        finally:
            s.close()
    except Exception:
        pass
    try:
        for res in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ip = res[4][0]
            if ip not in ips:
                ips.append(ip)
    except Exception:
        pass
    out = []
    for ip in ips:
        try:
            a = ipaddress.ip_address(ip)
            if a.is_private and not a.is_loopback and not a.is_link_local:
                out.append(ip)
        except ValueError:
            continue
    return out


def _remote_url() -> str:
    """Внешний адрес ЭТОГО ПК, если он у него есть. У большинства
    пользователей его нет — тогда телефон работает по LAN + автономно.
    Приоритет: явный `pair-remote-url` → serveo из `tunnel-subdomain`."""
    cfg = _s.get("config") or {}
    explicit = str(cfg.get("pair-remote-url") or "").strip().rstrip("/")
    if explicit:
        if not explicit.startswith(("http://", "https://")):
            explicit = "https://" + explicit
        return explicit
    sub = str(cfg.get("tunnel-subdomain") or "").strip()
    if sub and cfg.get("tunnel-enabled", True):
        return f"https://{sub}.serveousercontent.com"
    return ""


# Кэш проверки достижимости: `_endpoints()` зовут и из /start, и из /status,
# а связку «адрес → слушаем ли» пересчитывать на каждый вызов незачем.
_REACH_CACHE: dict[str, tuple[float, bool]] = {}
_REACH_TTL = 30.0


def _listening_on(ip: str) -> bool:
    """Правда ли, что Ripster слушает на ЭТОМ адресе.

    По умолчанию сервер поднимается на 127.0.0.1 (`RIPSTER_HOST`, см. app.py),
    и тогда все LAN-адреса этой машины существуют, но порт на них закрыт. Раньше
    `_endpoints()` перечислял их безусловно — ПК показывал человеку адрес вида
    http://192.168.1.98:7799, тот вбивал его на телефоне и получал «failed to
    connect» (наступил на это 04.09.2026 при живом сопряжении). Адрес, по
    которому мы заведомо не отвечаем, предлагать нельзя.

    Проверяем не настройкой, а попыткой соединения: так покрываются и запуск с
    `--host`, и firewall, и случай, когда порт занял другой процесс. Таймаут
    короткий — это соединение к самому себе, оно либо мгновенное, либо не нужно.
    """
    now = time.time()
    hit = _REACH_CACHE.get(ip)
    if hit and now - hit[0] < _REACH_TTL:
        return hit[1]
    ok = False
    try:
        with socket.create_connection((ip, _PORT), timeout=0.35):
            ok = True
    except OSError:
        ok = False
    _REACH_CACHE[ip] = (now, ok)
    return ok


def _endpoints() -> list[dict]:
    """Адреса ТОЛЬКО этого ПК, в порядке предпочтения телефона: LAN (быстро,
    приватно) → mDNS-имя → внешний (если есть). Ни один пользователь не
    получает чужой адрес — список строит тот ПК, с которым идёт сопряжение.

    Локальные адреса попадают сюда, только если по ним реально отвечает порт
    (см. [_listening_on]); иначе остаётся внешний, и он же становится первым —
    когда сервер слушает только петлю, туннель является единственным рабочим
    путём, а не запасным."""
    eps: list[dict] = []
    lan_ok = False
    for ip in _lan_ips():
        if _listening_on(ip):
            eps.append({"url": f"http://{ip}:{_PORT}", "kind": "lan"})
            lan_ok = True
    if lan_ok:
        # mDNS-имя ведёт на те же интерфейсы: предлагаем, только если они живы.
        try:
            host = socket.gethostname()
            if host and "." not in host:
                eps.append({"url": f"http://{host}.local:{_PORT}", "kind": "mdns"})
        except Exception:
            pass
    rurl = _remote_url()
    if rurl:
        eps.append({"url": rurl, "kind": "remote"})
    return eps


# ── helpers ──────────────────────────────────────────────────────────────────

def _is_loopback(request: Request) -> bool:
    host = (request.client.host if request.client else "") or ""
    return host in ("127.0.0.1", "::1", "localhost")


def _owner_ok(request: Request) -> bool:
    """Owner-authenticated for OWNER-ONLY pairing actions (start/share/status/
    revoke-all/mode). `_is_loopback` ALONE is NOT owner-proof: with
    `remote-enabled` on, the serveo/cloudflared tunnel forwards to 127.0.0.1 and
    uvicorn runs WITHOUT proxy_headers, so EVERY request off the public tunnel also
    satisfies `_is_loopback`. That let anyone with the (guest-shared) tunnel URL
    mint a pairing code → claim a device token → pull the owner's streaming
    credentials, with no cookie at all — the `/api/pair/` subtree is a public
    prefix, so the global owner-auth gate never runs (SECURITY FIX 18.09.2026,
    same trap auth.py documents for the login limiter / open-url).

    So: trust the forge-proof owner SESSION COOKIE (a tunnel attacker cannot mint
    it without `session-secret`); fall back to raw loopback ONLY when no tunnel is
    exposing us (`remote-enabled` off) — that keeps a purely-local, no-password box
    working. When remote is on and a password is set, the local UI carries the
    cookie, so the real pairing flow is unaffected."""
    try:
        from ripster import auth as _auth
        if _auth.verify_session_cookie(request.cookies.get("ripster-session", "")):
            return True
    except Exception:
        pass
    remote = bool((_s.get("config") or {}).get("remote-enabled", False))
    return _is_loopback(request) and not remote


def _bearer(request: Request) -> str:
    h = request.headers.get("authorization", "")
    return h[7:].strip() if h.lower().startswith("bearer ") else ""


_last_touch_save: float = 0.0


def _token_rec(tok: str) -> dict | None:
    if not tok:
        return None
    for t in _state.get("tokens", []):
        if t.get("token") == tok:
            return t
    return None


def _token_valid(tok: str) -> bool:
    """Проверить токен И заодно отметить устройство «живым» — ПК так понимает,
    что мобильный Ripster сейчас на связи. Запись на диск троттлится (раз в 30с),
    чтобы каждый poll телефона не бил по файлу."""
    hit = _token_rec(tok)
    if hit is None:
        return False
    now = time.time()
    hit["seen"] = int(now)
    global _last_touch_save
    if now - _last_touch_save > 30:
        _last_touch_save = now
        _save_state()
    return True


def _capabilities() -> list[str]:
    """What this desktop can do that the phone can't do alone."""
    caps: list[str] = []
    cfg = _s["config"]
    if cfg.get("wrapper-mode"):
        caps.append("apple_music")
    base = _s["base_dir"]
    wvd_here = any((base / p).exists() for p in
                   ("tools/widevine/device.wvd", "AppleMusicDecrypt/assets/device.wvd"))
    if wvd_here and cfg.get("soundcloud-oauth-token"):
        caps.append("sc_drm")
    return caps


def _apple_storefront() -> str:
    """Витрина Apple, в которой РЕАЛЬНО качает движок ПК. `apple-country`
    (страна подписки) главнее `storefront`: ссылка из чужой витрины даёт
    «0 треков». Телефон строит iTunes-запросы и trackViewUrl в этой витрине."""
    cfg = _s["config"]
    sf = (cfg.get("apple-country") or cfg.get("storefront") or "us")
    return str(sf).strip().lower() or "us"


def _jwt_claim(token: str, key: str) -> str:
    """Best-effort read of a claim from a JWT without verifying it."""
    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        return json.loads(base64.urlsafe_b64decode(payload)).get(key, "") or ""
    except Exception:
        return ""


def _best_slots(cfg: dict) -> dict:
    """Лучшая учётка каждого сервиса — по уже измеренному здоровью.

    Сопряжение годами отдавало телефону ОСНОВНОЕ поле конфига. Для Deezer это
    оказался бесплатный аккаунт (BG) при двух Family с lossless в пуле: телефон
    качал 128 kbps, и по списку полей это было незаметно. Для Qobuz — токен со
    сроком 28.09 при соседнем до 14.10.

    Берём слот с наименьшим рангом (`health_rank`: 0 — лучший), при равенстве —
    тот, что раньше в конфиге. В сеть не ходим: ранг читается из кэша сторожа.
    Ничего не измерено или что-то пошло не так — возвращаем пусто, и вызывающий
    оставляет прежнее поведение.
    """
    out: dict[str, str] = {}

    def pick(mod_name: str, secret_of, dst: str) -> None:
        try:
            import importlib
            pool = importlib.import_module(mod_name)
            accounts = pool._configured_accounts(cfg)
            if len(accounts) < 2:
                return                      # выбирать не из чего
            ranked = sorted(
                ((pool.health_rank(secret_of(a)), i, secret_of(a))
                 for i, a in enumerate(accounts)),
                key=lambda t: (t[0], t[1]),
            )
            rank, _idx, secret = ranked[0]
            # Подменяем ТОЛЬКО на измеренном (ранг 0/1). Ранг 2 значит «не
            # спрашивали»: выбирать по незнанию — это гадание, а прежнее
            # поведение (основная учётка) как минимум предсказуемо.
            if rank < 2 and secret:
                out[dst] = secret
        except Exception as e:              # noqa: BLE001
            print(f"[pair] лучший слот {dst} не выбран: {e!r}", flush=True)

    pick("ripster.deezer_pool", lambda a: a["arl"], "deezer.arl")
    pick("ripster.soundcloud_pool", lambda a: a["token"], "soundcloud.oauth")
    for dst, secret in _best_qobuz(cfg).items():
        out[dst] = secret
    return out


def _best_qobuz(cfg: dict) -> dict:
    """Какую учётку Qobuz отдать телефону — и почему именно её.

    Правило владельца 06.09.2026, дословно: «токены и секреты должны вшиваться
    в телефон, но именно валидные, самые лучшие, желательно Зеландия».

    Прошлая версия брала слот с наименьшим рангом и молча падала обратно на
    основное поле конфига, если ничего не измерено. Из-за этого телефон
    получал `qobuz-auth-token` первого слота вне зависимости от того, что
    сторож про него знает, и заменить это на телефоне было нечем — синк
    перекрывал ручной ввод при каждом запуске.

    Теперь порядок явный и в одном месте:

      1. МЁРТВОЕ НЕ ОТДАЁМ ВОВСЕ. Ранг 3 (401, снятая, погасшая подписка) —
         не «хуже», а непригодно: отдать такое значит сломать телефон молча.
      2. Ранг: 0 (lossless/hi-res) лучше 1 (обычное качество) лучше 2 (не
         спрашивали). Неизмеренное берём, только если измеренного нет вообще,
         и говорим об этом в журнале.
      3. При равном ранге — предпочитаемая страна (`pair-qobuz-country`,
         по умолчанию `qobuz-country`, иначе NZ).
      4. Дальше — у кого дольше действует подписка. Неизвестный срок — не
         «бесконечный» и не «нулевой»: нейтральные 30 дней, чтобы известный
         длинный срок выигрывал, а известный короткий — проигрывал.
      5. При полном равенстве — порядок в конфиге.

    Отдаём КОМПЛЕКТ: токен + app_id + secret. Токен от одной учётки с ключами
    от другой — это ровно те 400/401, за которыми не видно настоящей причины.
    """
    out: dict[str, str] = {}
    try:
        from ripster import qobuz_pool as _qp, qobuz_accounts as _qa
    except Exception as e:                  # noqa: BLE001
        print(f"[pair] qobuz: пул недоступен ({e!r}) — отдаю поля конфига", flush=True)
        return out

    want_cc = str(cfg.get("pair-qobuz-country") or cfg.get("qobuz-country") or "NZ").strip().upper()

    def days_left(info: dict) -> int:
        end = str((info or {}).get("expires") or "")[:10]
        if not end:
            return 30                       # не знаем — нейтрально, не «навсегда»
        try:
            from datetime import date as _date
            y, m, d = (int(x) for x in end.split("-"))
            return (_date(y, m, d) - _date.today()).days
        except Exception:                   # noqa: BLE001
            return 30

    try:
        accounts = _qp._configured_accounts(cfg)
    except Exception as e:                  # noqa: BLE001
        print(f"[pair] qobuz: список учёток не прочитан: {e!r}", flush=True)
        return out

    cands = []
    for i, a in enumerate(accounts):
        tok = (a.get("qobuz-auth-token") or "").strip()
        if not tok:
            continue                        # телефон умеет только токен-режим
        rank = _qp.health_rank(a)
        if rank >= 3:
            continue                        # непригодное не отдаём вообще
        info = _qa.known(_qa.account_secret(a)) or {}
        cc = str(info.get("country") or "").upper()
        cands.append((rank, 0 if cc == want_cc else 1, -days_left(info), i, tok, cc, info))

    if not cands:
        print("[pair] qobuz: пригодных учёток нет — токен НЕ отдаю "
              "(лучше без токена, чем с мёртвым)", flush=True)
        return out

    cands.sort()
    measured = [c for c in cands if c[0] < 2]
    if measured:
        cands = measured
    else:
        print("[pair] qobuz: ни одна учётка не измерена — отдаю первую по конфигу", flush=True)

    rank, _cc_pen, negdays, idx, tok, cc, _info = cands[0]
    out["qobuz.token"] = tok
    print(f"[pair] qobuz: слот {idx} ({cc or '??'}), ранг {rank}, "
          f"осталось {-negdays} дн., токен {tok[:4]}…", flush=True)

    # Ключи идут ВМЕСТЕ с токеном. Они у Qobuz общие на приложение, а не на
    # учётку, но отдать токен без них — оставить телефон с «Invalid or missing
    # app_id», где виноватым выглядит токен.
    app_id = str(cfg.get("qobuz-app-id") or "").strip()
    secret = str(cfg.get("qobuz-secrets") or cfg.get("qobuz-secret") or "").strip()
    if app_id:
        out["qobuz.app_id"] = app_id
    if secret:
        out["qobuz.secret"] = secret
    if not (app_id and secret):
        print("[pair] qobuz: ВНИМАНИЕ — отдаю токен без полной пары app_id/secret "
              f"(app_id={'есть' if app_id else 'нет'}, secret={'есть' if secret else 'нет'})",
              flush=True)
    return out


async def _credentials_payload() -> dict:
    """Only the fields the mobile CredentialStore knows how to store. Empty
    values are omitted so a missing service on the PC doesn't wipe a value the
    user typed on the phone."""
    cfg = _s["config"]
    out: dict[str, str] = {}

    def put(dst: str, *src_keys: str) -> None:
        for k in src_keys:
            v = cfg.get(k)
            if isinstance(v, str) and v.strip():
                out[dst] = v.strip()
                return

    # Телефон получает ЛУЧШУЮ учётку сервиса, а не первое поле конфига.
    #
    # 04.09.2026: основной `deezer-arl` — бесплатный аккаунт (BG), а две Deezer
    # Family с lossless лежат в пуле. Телефон качал Deezer в 128 kbps при двух
    # доступных lossless-учётках, и по списку полей этого было не видно. У Qobuz
    # то же по срокам: основной токен NZ действует до 28.09, а рядом NZ до 14.10
    # — после первой даты телефон молча остался бы без Qobuz.
    #
    # Здоровье слотов уже измерено сторожем (`*_accounts.known`), сети тут нет.
    # Ничего не измерено — отдаём основную, как раньше.
    put("soundcloud.oauth", "soundcloud-oauth-token")
    put("deezer.arl", "deezer-arl")
    put("qobuz.app_id", "qobuz-app-id")
    put("qobuz.secret", "qobuz-secrets", "qobuz-secret")
    put("qobuz.email", "qobuz-email")
    put("qobuz.password", "qobuz-password")
    # qobuz.token НЕ берётся из поля конфига напрямую: выбор учётки — целиком
    # за _best_qobuz(), и он же обязан не отдать мёртвую. Прямой put() здесь
    # означал «слот 0 в любом случае», из-за чего телефон получал основной
    # токен даже когда рядом лежал лучший (06.09.2026).
    for dst, secret in _best_slots(cfg).items():
        out[dst] = secret
    put("spotify.sp_dc", "spotify-sp-dc")
    # Яндекс: тоже ЛУЧШИЙ токен, а не первый. Без Plus сервис отдаёт только
    # lossy, и телефон, получив такую учётку при живой Plus-учётке рядом,
    # молча качал бы хуже, чем может ПК.
    _yx_done = False
    try:
        from ripster import yandex_accounts as _ya

        for _e in _ya.configured_tokens(cfg):
            _info = _ya.known(_e["token"]) or {}
            if _info.get("alive") and _info.get("plus"):
                out["yandex.oauth"] = _e["token"]
                _yx_done = True
                break
    except Exception:
        pass
    if not _yx_done:
        put("yandex.oauth", "yandex-token")
    put("beatport.username", "beatport-username")
    put("beatport.password", "beatport-password")

    # Tidal: refresh-токен ПК привязан к ДРУГОМУ client_id, чем публичный
    # zU4XHVVkc2tDPo4t мобильного клиента — обновить он им не сможет. Поэтому
    # отдаём ЖИВОЙ access-токен ПК; телефон использует его напрямую, а по
    # истечении — просто ре-синк с ПК.
    #
    # ИСТОЧНИК токена: сперва живая OrpheusDL-сессия (`loginstorage.bin`) — её
    # access_token минтуется из refresh раз в ~4ч и всегда принадлежит АКТУАЛЬНОЙ
    # учётке. `config.yaml` `tidal-token` — запасной: он пастился руками, живёт
    # ~16ч и мог протухнуть, а его uid расходился с реальной сессией
    # (209070577 в сессии против 208582242 в конфиге — из-за этого мобильный
    # Tidal и падал «не удалось получить поток»).
    access = ""
    refresh = ""
    cc = ""
    # ЛУЧШАЯ учётка Tidal, а не первая попавшаяся сессия.
    #
    # 12.09.2026: сессия OrpheusDL принадлежит ОСНОВНОЙ учётке, а основная на
    # этой машине оказалась INTRO (GB) со сроком, истёкшим 20.05 — lossless она
    # не отдаёт вовсе. Телефон получал именно её, хотя рядом лежали две
    # PREMIUM с HI_RES. Ровно тот же дефект, что закрыли у Deezer и Qobuz:
    # «первое поле конфига» вместо «лучший слот».
    #
    # Порядок слотов даёт пул: сперва пригодность (измеренная подписка), затем
    # страна — новозеландская витрина получает пятничные релизы первой.
    try:
        from ripster import account_fallback as _afb_p
        from ripster import tidal_pool as _tp

        _accts = _tp.configured_accounts(cfg)
        for _i in _afb_p.order_indices(_accts):
            _a = _accts[_i]
            _rt = (_a.get("tidal-refresh") or "").strip()
            if not _rt or _tp.health_rank(_a) >= 3:
                continue
            import httpx as _httpx

            async with _httpx.AsyncClient(timeout=25) as _c:
                _r = await _c.post(
                    "https://auth.tidal.com/v1/oauth2/token",
                    data={"refresh_token": _rt,
                          "client_id": "cgiF7TQuB97BUIu3",
                          "client_secret": "1nqpgx8uvBdZigrx4hUPDV2hOwgYAAAG5DYXOr6uNf8=",
                          "grant_type": "refresh_token"},
                )
            if _r.status_code != 200:
                continue
            _j = _r.json()
            access = _j.get("access_token") or ""
            refresh = _j.get("refresh_token") or _rt
            cc = ((_j.get("user") or {}).get("countryCode")
                  or _a.get("tidal-country") or "").upper()
            break
    except Exception:
        pass
    if not access:
        try:
            from ripster.engines.tidal import _orpheus_access_token, _read_tv_session
            access, cc = await _orpheus_access_token()
            tv = _read_tv_session() or {}
            refresh = (tv.get("refresh_token") or "").strip()
            cc = (cc or (tv.get("country_code") or "")).upper()
        except Exception:
            pass
    if not access:
        access = (cfg.get("tidal-token") or "").strip()
        refresh = (cfg.get("tidal-refresh") or "").strip()
        cc = cc or cfg.get("tidal-country") or _jwt_claim(access, "cc") or "US"
    if access:
        out["tidal.oauth"] = json.dumps({
            "accessToken": access,
            "refreshToken": refresh,
            "countryCode": cc or "US",
        })

    return out


def _config_mtime_ms() -> int:
    try:
        return int((_s["base_dir"] / "config.yaml").stat().st_mtime * 1000)
    except Exception:
        return int(time.time() * 1000)


# ── endpoints ────────────────────────────────────────────────────────────────

@router.post("/api/pair/start")
async def pair_start(request: Request):
    if not _owner_ok(request):
        return JSONResponse({"error": "forbidden", "detail": "owner only"}, status_code=403)
    now = time.time()
    _prune_pending()
    # 8 цифр (≈100 млн вариантов) + тормоз перебора ниже — код в открытом
    # виде живёт 5 минут, и подобрать его за это время нереально. Держим на
    # диске — переживает рестарт ПК между «Показать код» и вводом на телефоне
    # (это и была главная причина «сопряжение с кодом упало»).
    _state["pending"] = {}            # один активный код за раз
    code = f"{secrets.randbelow(100_000_000):08d}"
    _state["pending"][code] = now + _CODE_TTL
    _save_state()
    return {"code": code, "expires_in": _CODE_TTL,
            "pc_id": _pc_id(), "pc_name": _pc_name(),
            "device_group_id": _state["device_group_id"],
            "endpoints": _endpoints()}


@router.post("/api/pair/claim")
async def pair_claim(body: dict, request: Request):
    now = time.time()
    # brute-force brake: 10 wrong codes / 10 min → cool-off
    global _claim_fails
    _claim_fails = [t for t in _claim_fails if now - t < 600]
    if len(_claim_fails) >= 10:
        return JSONResponse({"error": "rate_limited", "detail": "too many attempts"},
                            status_code=429, headers={"Retry-After": "600"})

    _prune_pending()
    code = str((body or {}).get("code", "")).strip()
    exp = (_state.get("pending") or {}).get(code)
    if not exp or exp < now:
        _claim_fails.append(now)
        _state.get("pending", {}).pop(code, None)
        _save_state()
        return JSONResponse({"error": "bad_code", "detail": "invalid or expired code"},
                            status_code=401)
    _state["pending"].pop(code, None)          # one-shot
    _claim_fails = []                          # good code clears the brake

    mobile_id = str((body or {}).get("mobile_id", "")).strip()[:64]
    name = str((body or {}).get("name", "")).strip()[:60]
    ua = request.headers.get("user-agent", "")[:80]

    # Ре-пейр ТОГО ЖЕ телефона (совпал mobile_id) — обновляем его запись, не
    # плодим второй токен: связка «папа-мама» одна, просто перевыпуск ключа.
    tok = secrets.token_urlsafe(32)
    rec = None
    if mobile_id:
        for t in _state["tokens"]:
            if t.get("mobile_id") == mobile_id:
                rec = t
                break
    if rec is not None:
        # Выбор владельца по ЭТОМУ устройству переживает перевыпуск ключа:
        # слетевшее после переустановки приложения «вкл» означало бы молча
        # вставший телефон. Новое устройство проходит мимо этой ветки — ему
        # учётки не полагаются, пока владелец не включит тумблер сам.
        rec.update(token=tok, seen=int(now), ua=ua, name=name or rec.get("name", ""))
    else:
        # Новый телефон: учётки НЕ отдаём, пока владелец не включит явно по
        # этому устройству (настройка → Сопряжение).
        rec = {"mobile_id": mobile_id, "name": name,
               "created": int(now), "seen": int(now), "ua": ua,
               "device_id": _new_device_id(), "share_credentials": False,
               "token": tok}
        _state["tokens"].append(rec)
    _evict_tokens()
    _save_state()

    return {
        "pc_id": _pc_id(), "pc_name": _pc_name(),
        "device_group_id": _state["device_group_id"],
        "token": tok,
        "capabilities": _capabilities(),
        "apple_storefront": _apple_storefront(),
        "endpoints": _endpoints(),
        "mode": _state.get("mode", "mirror"),
        "share_credentials": bool(rec.get("share_credentials")),
    }


@router.get("/api/pair/ping")
async def pair_ping(request: Request):
    """Публичный, без секретов. Телефон бьёт по нему при обходе LAN-подсети,
    чтобы заново найти СВОЙ ПК после смены IP (сверяет `pc_id`)."""
    return {"ripster": True, "pc_id": _pc_id(), "pc_name": _pc_name(),
            "version": str((_s.get("config") or {}).get("app-version") or "")}


@router.post("/api/pair/mode")
async def pair_mode(body: dict, request: Request):
    """Режим fan-out по /ws на всю пару: mirror | initiator | isolation.
    Меняется с любой стороны (bearer телефона ИЛИ loopback ПК), применяется к
    обоим — рассылаем `pair_mode`."""
    if not (_owner_ok(request) or _token_valid(_bearer(request))):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    mode = str((body or {}).get("mode", "")).strip().lower()
    if mode not in _FANOUT_MODES:
        return JSONResponse({"error": "bad_mode", "detail": f"one of {_FANOUT_MODES}"},
                            status_code=400)
    _state["mode"] = mode
    _save_state()
    try:
        bc = _s.get("broadcast")
        if bc:
            import asyncio
            asyncio.create_task(bc({"type": "pair_mode", "mode": mode}))
    except Exception:
        pass
    return {"ok": True, "mode": mode}


@router.post("/api/pair/unpair")
async def pair_unpair(request: Request):
    """Устройство отвязывает СЕБЯ (по своему bearer). Мобильный клиент зовёт
    это из «Отвязать», прежде чем стереть токен у себя — расцепление обоюдное."""
    tok = _bearer(request)
    before = len(_state.get("tokens", []))
    _state["tokens"] = [t for t in _state.get("tokens", []) if t.get("token") != tok]
    _save_state()
    return {"ok": True, "removed": before - len(_state["tokens"])}


@router.post("/api/pair/revoke-all")
async def pair_revoke_all(request: Request):
    """Владелец с самого ПК (loopback) сбрасывает ВСЕ спаренные устройства —
    после этого ни один старый токен телефона не действует."""
    if not _owner_ok(request):
        return JSONResponse({"error": "forbidden"}, status_code=403)
    n = len(_state.get("tokens", []))
    _state["tokens"] = []
    _state["pending"] = {}
    _save_state()
    return {"ok": True, "revoked": n}


@router.get("/api/pair/credentials")
async def pair_credentials(request: Request):
    tok = _bearer(request)
    if not _token_valid(tok):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    # Флаг — на УСТРОЙСТВЕ: один телефон может получать учётки, другой — нет.
    # Записи без флага (чужой/ручной pairing_state.json) — отказ, а не раздача.
    rec = _token_rec(tok)
    if rec is None or not rec.get("share_credentials"):
        return JSONResponse({"error": "sharing_disabled",
                             "detail": "the owner has not enabled credential sharing "
                                       "for this device (Settings → Pairing)"},
                            status_code=403)
    # Устройство реально забрало учётки → помечаем: с этого момента оно НЕ
    # вытесняется молча (см. _evict_tokens).
    if not rec.get("synced_at"):
        rec["synced_at"] = int(time.time())
        _save_state()
    return {"updated_at": _config_mtime_ms(), "credentials": await _credentials_payload()}


@router.post("/api/pair/activity")
async def pair_activity(request: Request):
    """Спаренный телефон шлёт СЮДА, что он скачал и что слушал — чтобы история
    ПК-версии видела активность телефона. Скачивания подмешиваются в общий
    `download_history` (с меткой источника), прослушивания копятся отдельным
    списком в состоянии пары."""
    tok = _bearer(request)
    rec = _token_rec(tok)
    if not _token_valid(tok) or rec is None:
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    try:
        body = await request.json()
    except Exception:
        body = {}
    dev = rec.get("name") or "телефон"
    now_iso = datetime.now().isoformat(timespec="seconds")

    plays = body.get("plays") or []
    downloads = body.get("downloads") or []
    added_d = added_p = 0

    hist = _s.get("download_history")
    if isinstance(hist, list):
        have = {(x.get("title"), x.get("artist"), x.get("ts")) for x in hist}
        for d in downloads[:500]:
            key = (d.get("title"), d.get("artist"), d.get("at"))
            if not d.get("title") or key in have:
                continue
            hist.insert(0, {
                "id": f"ph-{abs(hash(key)) & 0xffffffff:x}",
                "title": d.get("title", ""), "artist": d.get("artist", ""),
                "album": d.get("album", ""), "service": d.get("service", ""),
                "format": d.get("format", ""),
                "ts": d.get("at") or now_iso,
                "source": "phone", "device": dev,
                "ok": bool(d.get("ok", True)),
            })
            have.add(key)
            added_d += 1
        if added_d and callable(_s.get("save_history")):
            del hist[2000:]
            _s["save_history"](hist)

    pp = _state.setdefault("phone_plays", [])
    have_p = {(x.get("title"), x.get("artist"), x.get("at")) for x in pp}
    for p in plays[:500]:
        key = (p.get("title"), p.get("artist"), p.get("at"))
        if not p.get("title") or key in have_p:
            continue
        pp.insert(0, {
            "title": p.get("title", ""), "artist": p.get("artist", ""),
            "album": p.get("album", ""), "service": p.get("service", ""),
            "genre": p.get("genre", ""),
            "at": p.get("at") or now_iso, "device": dev,
        })
        have_p.add(key)
        added_p += 1
    if added_p:
        del pp[300:]
        _save_state()

    return {"ok": True, "plays_added": added_p, "downloads_added": added_d}


@router.get("/api/pair/artist")
async def pair_artist(request: Request, service: str = "", id: str = "",
                      types: str = "album,single,ep,compilation,live"):
    """Дискография артиста для мобильного «перехода на артиста» — проксируем
    зрелый движковый `get_artist` ПК (тот же, что даёт `/api/artist/...`).
    Нужен `artist_id`; без него телефон использует свой поиск."""
    if not _token_valid(_bearer(request)):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    svc = (service or "").lower().strip()
    aid = (id or "").strip()
    if not aid:
        return {"error": "no artist id", "releases": []}
    try:
        from ripster.routes.discovery import _ENGINE_SERVICES
        from ripster.engines import get_engine
        eng_name = _ENGINE_SERVICES.get(svc)
        if not eng_name:
            return {"error": f"unsupported service: {svc}", "releases": []}
        return await get_engine(eng_name).get_artist(aid, types, _s["config"])
    except Exception as e:  # pragma: no cover
        print(f"[pairing] artist proxy failed: {e}", flush=True)
        return {"error": str(e), "releases": []}


@router.get("/api/pair/lyrics-word")
async def pair_lyrics_word(request: Request, title: str = "", artist: str = "", isrc: str = ""):
    """Пословная (караоке) лирика из Apple по ИСКОМОМУ треку — независимо от того,
    из какого сервиса/скачанного он играет (владелец 14.09.2026). Матч по ISRC/
    названию → Apple syllable-lyrics → word-timed JSON. Нет токена/совпадения →
    `{lines: []}` (телефон честно падает на построчный LRCLIB)."""
    if not _token_valid(_bearer(request)):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    t = (title or "").strip()
    if not t:
        return {"src": "", "lines": []}
    try:
        from ripster import apple_lyrics
        res = await apple_lyrics.word_lyrics(t, (artist or "").strip(), (isrc or "").strip())
        return res or {"src": "", "lines": []}
    except Exception as e:  # pragma: no cover
        print(f"[pairing] word-lyrics failed: {e}", flush=True)
        return {"src": "", "lines": [], "error": str(e)}


@router.get("/api/pair/lyrics")
async def pair_lyrics(request: Request, artist: str = "", track: str = "",
                      album: str = "", duration: int = 0, isrc: str = ""):
    """Полная ПК-лестница текстов по запросу телефона.

    Телефон видит LRCLIB — общественную базу, где нового и
    нишевого трека часто просто нет. На ПК той же самый трек
    ищется ещё и в подписках владельца (Tidal, Spotify, Deezer, Apple).
    Отвечаем ТОЛЬКО текстом и именем источника: ключи, токены и
    прочие подробности учёток за пределы ответа не уходят —
    телефон получает результат поиска, а не доступ к учёткам.

    Пословную (караоке) лирику здесь не просим: она у телефона
    есть свой `/api/pair/lyrics-word`, и тянуть её повторно значит
    удваивать задержку панели.
    """
    if not _token_valid(_bearer(request)) and not _owner_ok(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    a = (artist or "").strip()
    t = (track or "").strip()
    if not a or not t:
        return {"synced": "", "plain": "", "source": ""}
    try:
        from ripster.routes.discovery import lyrics_ladder
        got = await lyrics_ladder(artist=a, track=t, album=(album or "").strip(),
                                  duration=max(0, int(duration or 0)),
                                  isrc=(isrc or "").strip(), words=False)
    except Exception as e:  # pragma: no cover
        # Подробности — только в лог ПК. В ответе им не место: сообщение
        # упавшего источника часто содержит URL учётки или токен запроса, а
        # телефон — чужое устройство.
        print(f"[pairing] lyrics ladder failed: {e}", flush=True)
        return {"synced": "", "plain": "", "source": ""}
    return {"synced": str(got.get("synced") or ""),
            "plain": str(got.get("plain") or ""),
            "source": str(got.get("source") or "")}


@router.get("/api/pair/label")
async def pair_label(request: Request, name: str = "", limit: int = 60):
    """Релизы лейбла для мобильного «перехода на лейбл» — та же форма ответа,
    что `/api/pair/artist` (`{artist:{name}, releases:[…]}`)."""
    if not _token_valid(_bearer(request)):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    nm = (name or "").strip()
    if not nm:
        return {"error": "no label", "releases": []}
    try:
        from ripster.routes.watchlist import _label_releases_ex
        rels, info = await _label_releases_ex(nm, max(1, min(int(limit), 120)))
        return {"artist": {"name": nm}, "releases": rels or [],
                "error": (info or {}).get("error") if isinstance(info, dict) and not rels else None}
    except Exception as e:  # pragma: no cover
        print(f"[pairing] label proxy failed: {e}", flush=True)
        return {"error": str(e), "releases": []}


@router.get("/api/pair/activity")
async def pair_activity_view(request: Request):
    """Что натворил телефон — для истории/аналитики ПК-версии."""
    if not _token_valid(_bearer(request)) and not _owner_ok(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    return {"phone_plays": _state.get("phone_plays", [])[:200]}


@router.post("/api/pair/share")
async def pair_share(body: dict, request: Request):
    """Владелец на ПК включает отдачу учёток КОНКРЕТНОМУ устройству.

    Глобального «включить всем» нет намеренно: право на чужие учётки
    разбирается по одному телефону, а не разом со всеми спаренными.
    Токен устройства в интерфейс не отдаём — знание его = доступ к учёткам,
    поэтому UI работает по непубличному `device_id`."""
    if not _owner_ok(request):
        return JSONResponse({"error": "forbidden", "detail": "owner only"}, status_code=403)
    b = body or {}
    dev = str(b.get("device_id") or "").strip()
    enabled = b.get("enabled")
    if not dev or not isinstance(enabled, bool):
        return JSONResponse({"error": "bad_request",
                             "detail": "device_id and boolean enabled required"},
                            status_code=400)
    rec = next((t for t in _state.get("tokens", []) if t.get("device_id") == dev), None)
    if rec is None:
        return JSONResponse({"error": "no_device", "detail": "unknown device_id"},
                            status_code=404)
    rec["share_credentials"] = enabled
    _save_state()
    return {"ok": True, "device_id": dev, "share_credentials": enabled}


# ── Apple Music (и другой «только-ПК» контент) через сопряжение ──────────────
# Телефон нативно тянет всё, КРОМЕ Apple Music (Docker-враппер живёт на ПК).
# Здесь он отдаёт ссылку ПК, тот качает своим движком, телефон забирает файл и
# дальше тегирует/кладёт в библиотеку сам.

_APPLE_HOSTS = ("music.apple.com", "geo.music.apple.com", "itunes.apple.com")


@router.post("/api/pair/fetch")
async def pair_fetch(body: dict, request: Request):
    if not _token_valid(_bearer(request)):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    url = str((body or {}).get("url", "")).strip()
    if not url:
        return JSONResponse({"error": "no_url"}, status_code=400)
    if not any(h in url.lower() for h in _APPLE_HOSTS):
        # Остальное телефон умеет сам — не проксируем, чтобы не плодить пути.
        return JSONResponse({"error": "not_pc_only",
                             "detail": "only Apple Music is fetched via the PC"}, status_code=400)

    quality = str((body or {}).get("quality", "")).strip() or None
    try:
        from ripster.routes import queue as _queue_mod
        payload = {"url": url, "source": "pair"}
        if quality:
            payload["quality"] = quality
        res = await _queue_mod.add_to_queue(payload, request)
    except Exception as e:
        return JSONResponse({"error": "enqueue_failed", "detail": str(e)[:200]}, status_code=502)

    if isinstance(res, dict) and res.get("id"):
        return {"task_id": res["id"], "duplicate": bool(res.get("duplicate"))}

    # «Уже в очереди» — НЕ отказ. Просили скачать — оно качается, просто заявку
    # подали раньше. Раньше этот ответ падал в общий 502 `enqueue_failed`, и
    # телефон честно пересказывал его как ошибку: 05.09.2026 «Скачать альбом»
    # на 19 треках Massive Attack дало 19 красных строк вида
    #   __e.pc_rejected__ HTTP 502: {'ok': False, 'msg': 'Already in queue'…}
    # при том, что ПК в это самое время спокойно скачал весь альбом в ALAC.
    # Отдаём id той задачи, что уже идёт, — телефону есть за чем следить.
    if isinstance(res, dict) and res.get("duplicate"):
        ids = res.get("ids") or []
        return {"task_id": ids[0] if ids else "", "duplicate": True}

    return JSONResponse({"error": "enqueue_failed", "detail": str(res)[:200]}, status_code=502)


@router.get("/api/pair/station")
async def pair_station(request: Request, genre: str = "", limit: int = 40):
    """Редакторская подборка Apple по жанру — для станций на телефоне.

    Телефон сам Apple не стримит и dev-токена не имеет, зато умеет разрешить
    вещь в играбельную копию у своего сервиса по ISRC. Поэтому ПК отдаёт
    только МЕТАДАННЫЕ подборки, без ссылок на поток.

    Отдаём и то, ЧЬЯ это подборка: редакция Apple или сторонний составитель.
    Разница по качеству между ними велика, и прятать её от клиента нельзя —
    пусть решает, ставить такой список во главу станции или на добор.
    """
    if not _token_valid(_bearer(request)):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    from ripster import apple_stations as _as
    res = await _as.station(genre, _s.get("config") or {}, limit=max(1, min(100, limit)))
    return res


@router.get("/api/pair/taste")
async def pair_taste(request: Request, limit: int = 40):
    """Вкус владельца с ПК: Раскопки + подписки Spotify.

    Телефон строит станции по своей истории прослушиваний, а это капля рядом с
    фонотекой и пятью с лишним тысячами подписок на компьютере. Отдаём то, что
    ПК уже знает, вместе с ИСТОЧНИКОМ каждой цифры: измеренный вес Раскопок и
    двоичная подписка — разные вещи, и решать по ним надо по-разному.
    """
    if not _token_valid(_bearer(request)):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    from ripster import taste as _taste
    return await _taste.build(Path(_s.get("base_dir") or "."), limit=max(1, min(200, limit)))


@router.post("/api/pair/genre")
async def pair_genre(body: dict, request: Request):
    """Жанры пачки треков по справочнику Beatport.

    Зачем отдельный маршрут, а не поле в другом ответе: у Apple, Deezer и
    Яндекса поджанров НЕТ вовсе — замер 05.09.2026 даёт «Dance, Music», где
    «Music» это корневая категория каталога. Точный ярлык есть только у
    Beatport, и он висит на релизе, поэтому спрашивать надо про конкретный
    трек. Телефон своего Beatport-токена не имеет — спрашивает ПК.

    Вход: `{"tracks": [{"artist": "...", "title": "..."}, ...]}`.
    Выход: `{"genres": {"<artist>|<title>": "<жанр>"}}` — ТОЛЬКО то, что
    удалось узнать. Отсутствие ключа означает «не знаю», и подставлять туда
    пустую строку нельзя: обучаемый модуль на той стороне отличает одно от
    другого.
    """
    if not _token_valid(_bearer(request)):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    items = (body or {}).get("tracks") or []
    if not isinstance(items, list) or not items:
        return {"genres": {}, "reason": "no_tracks"}

    from ripster.engines import orpheus_beatport as _bp
    from ripster import genre_oracle as _go
    # Токена Beatport может не быть — это не повод молчать: Discogs работает
    # без ключа и покрывает шире, включая всё неэлектронное, где Beatport
    # прямо ошибается.
    token = ""
    try:
        token = await _bp._beatport_access_token()
    except Exception:
        token = ""

    out: dict = {}
    sources: dict = {}
    # Потолок на пачку: справочник ходит в сеть на каждый трек, и пускать сюда
    # список любой длины значит подвесить и ПК, и телефон.
    for it in items[:40]:
        artist = str((it or {}).get("artist") or "").strip()
        title = str((it or {}).get("title") or "").strip()
        if not artist or not title:
            continue
        g, src = await _go.best_genre(artist, title, token)
        if g:
            key = f"{artist}|{title}"
            out[key] = g
            sources[key] = src
    return {"genres": out, "sources": sources, "asked": min(len(items), 40),
            "beatport": bool(token)}


def _find_pair_task(task_id: str) -> dict | None:
    for t in _s.get("queue") or []:
        if t.get("id") == task_id:
            return t
    try:
        from ripster.routes.download import _find_task_or_history
        return _find_task_or_history(task_id)
    except Exception:
        return None


@router.get("/api/pair/fetch/{task_id}")
async def pair_fetch_status(task_id: str, request: Request):
    if not _token_valid(_bearer(request)):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    t = _find_pair_task(task_id)
    if not t:
        return JSONResponse({"error": "not_found"}, status_code=404)
    meta = t.get("meta") or {}
    return {
        "task_id": task_id,
        "status": t.get("status", "queued"),           # queued | running | done | error
        "progress": t.get("progress", 0),
        "title": meta.get("title", ""),
        "artist": meta.get("artist", ""),
        "error": t.get("error") or meta.get("error") or "",
        # Короткий локализуемый вердикт для телефона: длинная аббревиатура
        # своих витрин — для ПК-истории, экрану нужна одна строка.
        # `available_on` дописывает запасной путь, пока задача ещё в статусе.
        "error_key": meta.get("error_key") or "",
        "available_on": meta.get("available_on") or [],
        "note": meta.get("route_note", ""),
    }


@router.get("/api/pair/file/{task_id}")
async def pair_fetch_file(task_id: str, request: Request):
    if not _token_valid(_bearer(request)):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    t = _find_pair_task(task_id)
    if not t:
        return JSONResponse({"error": "not_found"}, status_code=404)
    if t.get("status") != "done":
        return JSONResponse({"error": "not_ready", "status": t.get("status")}, status_code=409)
    try:
        from ripster.routes.download import _get_task_dir, _find_audio_files, _AUDIO_MEDIA
        from fastapi.responses import FileResponse
        d = _get_task_dir(t)
        files = _find_audio_files(d) if d else []
        if not files:
            return JSONResponse({"error": "no_files"}, status_code=404)
        # Apple-задача из сопряжения — это один трек/альбом; отдаём первый файл.
        # (Альбомы телефон ставит в очередь по одному URL трека, так что тут один.)
        f = files[0]
        media = _AUDIO_MEDIA.get(f.suffix.lower(), "application/octet-stream")
        return FileResponse(str(f), media_type=media, filename=f.name)
    except Exception as e:
        return JSONResponse({"error": "serve_failed", "detail": str(e)[:200]}, status_code=502)


# ── Радар: список отслеживаемых артистов + их последние релизы ──────────────
# ПК ведёт вотчлист и фоном проверяет новые релизы. Телефон показывает его
# «как есть» — понимание того, ЧТО радар отслеживает, важнее, чем красивая
# витрина. Тап по «последнему релизу» ставит его в очередь телефона.

@router.get("/api/pair/upcoming")
async def pair_upcoming(request: Request):
    """Радар ГРЯДУЩЕГО для телефона.

    Собирает и подтверждает анонсы ПК: там уже есть и обход лент, и сверка с
    MusicBrainz, и накопленный список. Гонять то же самое с телефона значило бы
    держать второй разбор новостей и второй лимит запросов к MusicBrainz — при
    том, что телефон и так ходит на ПК за обычным радаром.
    """
    if not _token_valid(_bearer(request)):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    try:
        from pathlib import Path as _P
        from ripster import upcoming_radar as _ur
        items = _ur.drop_released(_ur.load(_P(_s.get("base_dir") or ".")))
    except Exception as e:
        print(f"[pair] upcoming failed: {e}", flush=True)
        items = []

    def _order(u):
        return (0, u.release_date) if u.release_date else (1, "")

    items.sort(key=_order)
    return {"items": [{
        "artist":      u.artist,
        "title":       u.title,
        "date":        u.release_date,
        "label":       u.label,
        "track_count": u.track_count,
        "credits":     u.credits,
        "genres":      u.genres,
        "artwork":     u.artwork,
        "sources":     u.sources,
        "kind":        u.kind,
    } for u in items[:200]]}


@router.post("/api/pair/upcoming/wait")
async def pair_upcoming_wait(request: Request):
    """«Жду» с телефона: артист уходит в вишлист ПК.

    Вишлист один на оба устройства — иначе телефон копил бы своё ожидание,
    а ловил релиз всё равно ПК, и они бы расходились.
    """
    if not _token_valid(_bearer(request)):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    try:
        body = await request.json()
    except Exception:
        body = {}
    from ripster.routes import upcoming as _up
    return await _up.api_upcoming_wait(body if isinstance(body, dict) else {})


@router.get("/api/pair/radar")
async def pair_radar(request: Request):
    if not _token_valid(_bearer(request)):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    wl = _s.get("watchlist") or []
    def _is_url(v: str) -> bool:
        return v.startswith("http://") or v.startswith("https://")

    def _last_release_url(entry: dict) -> str:
        v = str(entry.get("last_release") or "").strip()
        return v if _is_url(v) else ""

    def _last_release_title(entry: dict) -> str:
        # 1) Явное поле, если вотчлист успел его записать.
        t = str(entry.get("last_release_title") or "").strip()
        if t:
            return t
        v = str(entry.get("last_release") or "").strip()
        # 2) Само поле — уже название (ветки лейблов).
        if v and not _is_url(v) and not v.isdigit():
            return v
        # 3) Добор для СТАРЫХ записей: явного поля у них нет и не появится до
        #    следующей находки. Берём название ИЗ САМОЙ ССЫЛКИ — у Apple, Deezer,
        #    Qobuz и Tidal слаг альбома лежит прямо в пути
        #    (…/album/remember-your-life/12345). Это единственный источник,
        #    который гарантированно относится к ТОМУ ЖЕ релизу.
        #
        #    Список `seen` для этого НЕ годится, хотя и выглядит подходящим: он
        #    хранит нормализованные имена всего замеченного, и последний элемент
        #    сплошь и рядом — другой релиз (у William Orbit ссылка на «Remember
        #    Your Life», а в seen последним лежит чужой DJ-микс). Подставить его
        #    значило бы совершить ровно ту ошибку, которую мы чиним.
        if _is_url(v):
            slug = v.split("?")[0].rstrip("/").split("/")
            for part in reversed(slug):
                if part and not part.isdigit() and "." not in part and part not in ("album", "track", "playlist"):
                    return part.replace("-", " ").strip().title()
        return ""

    items = []
    for e in wl:
        name = (e.get("name") or "").strip()
        if not name:
            continue
        items.append({
            "name": name,
            "service": e.get("service", ""),
            "artist_id": e.get("artist_id", ""),
            "kind": str(e.get("kind") or "artist").lower(),
            "last_check": e.get("last_check"),
            "date": str(e.get("last_release_date") or ""),
            # `last_release` в вотчлисте перегружено: разные пути пишут туда то
            # НАЗВАНИЕ релиза (watchlist.py 222/983/991), то id трека (898), то
            # настоящую ссылку (1397). Раньше всё это уезжало на телефон под
            # именем `latest_url` — и телефон пытался открыть название как ссылку:
            # резолв не удавался, играло «что нашлось» (жалоба 04.09.2026 —
            # карточка Etherwood включала чужой релиз). Отдаём по факту.
            "latest_url": _last_release_url(e),
            "latest_title": _last_release_title(e),
            "cover_url": e.get("last_release_cover") or e.get("cover") or "",
            "auto": bool(e.get("auto_download")),
            "seen_count": len(e.get("seen") or []),
        })

    # Spotify — «первичка» релиз-радара у владельца, но живёт НЕ в вотчлисте, а в
    # своём per-artist складе (spotify.py `_sp_artist_state`). Подмешиваем его
    # сюда, схлопывая до одной записи на артиста (самый свежий релиз).
    try:
        from ripster.routes import spotify as _sp
        # 1) отфильтрованная лента (по followed-списку). 2) если followed-кэш
        # пуст — весь per-artist склад целиком, чтобы Spotify не пропал.
        sp_releases = list(_sp._build_feed(3650, "album,single,compilation").get("releases", []))
        if not sp_releases:
            for st in (_sp._sp_artist_state or {}).values():
                for r in (st.get("releases") or []):
                    sp_releases.append({**r, "artist": r.get("artist") or st.get("name", "")})
        by_artist: dict = {}
        for rel in sp_releases:
            art = (rel.get("artist") or "").strip()
            url = rel.get("url") or ""
            if not art or not url:
                continue
            date = str(rel.get("date") or "")
            cur = by_artist.get(art)
            if cur is None or date > cur["_d"]:
                by_artist[art] = {
                    "name": art, "service": "spotify",
                    "artist_id": str(rel.get("artist_id") or ""),
                    "kind": "artist",
                    "last_check": date, "date": date, "latest_url": url,
                    "latest_title": str(rel.get("title") or ""),
                    "cover_url": rel.get("cover") or "",
                    "auto": False, "seen_count": 0, "_d": date,
                }
        have = {(i["name"], i["service"]) for i in items}
        for v in by_artist.values():
            v.pop("_d", None)
            if (v["name"], "spotify") not in have:
                items.append(v)
    except Exception as e:
        print(f"[pairing] spotify radar merge skipped: {e}", flush=True)

    # свежепроверенные — вперёд
    items.sort(key=lambda x: (x["last_check"] or ""), reverse=True)
    return {"count": len(items), "items": items}


@router.post("/api/pair/feedback")
async def pair_feedback(request: Request):
    """Слово владельца с телефона: «это не мой артист» / «это мой».

    Тонкая дверь поверх вебской (`routes.radar.submit_feedback`): данные и
    вердикт одни и те же, и вторая реализация неизбежно начала бы расходиться
    с первой — ровно тот класс ошибок, из-за которого однофамильцев «лечили»
    шесть раз и каждый раз только в одной витрине.
    """
    if not _token_valid(_bearer(request)):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    try:
        body = await request.json()
    except Exception:                                          # noqa: BLE001
        body = {}
    from ripster.routes import radar as _radar
    return await _radar.submit_feedback(body if isinstance(body, dict) else {})


@router.get("/api/pair/hidden")
async def pair_hidden(request: Request):
    """«Скрытые» на телефоне: то, что дверь не выпустила в ленту, — с причиной
    и возможностью сказать «это мой» (см. `/api/identity/hidden`)."""
    if not _token_valid(_bearer(request)):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    from ripster.routes import radar as _radar
    return await _radar.hidden_view()


@router.get("/api/pair/status")
async def pair_status(request: Request):
    if not _owner_ok(request):
        return JSONResponse({"error": "forbidden"}, status_code=403)
    now = int(time.time())
    _prune_pending()
    # Флаг отдачи — НА УСТРОЙСТВЕ: UI рисует по тумблеру на каждый телефон и
    # щёлкает им по `device_id`. Токена в ответе нет — знание его = доступ.
    devices = [
        {
            "device_id": t.get("device_id", ""),
            "name": t.get("name", "") or t.get("ua", "")[:24] or "телефон",
            "mobile_id": t.get("mobile_id", ""),
            "created": t.get("created", 0),
            "seen": t.get("seen", t.get("created", 0)),
            "synced_at": t.get("synced_at", 0),
            "ua": t.get("ua", ""),
            "online": (now - t.get("seen", 0)) < 90,   # был запрос за последние 1.5 мин
            "share_credentials": bool(t.get("share_credentials")),
        }
        for t in _state.get("tokens", [])
    ]
    return {
        "pc_id": _pc_id(),
        "pc_name": _pc_name(),
        "device_group_id": _state["device_group_id"],
        "endpoints": _endpoints(),
        "mode": _state.get("mode", "mirror"),
        "paired_devices": len(devices),
        "devices": devices,
        "online_devices": sum(1 for d in devices if d["online"]),
        "sharing_devices": sum(1 for d in devices if d["share_credentials"]),
        "capabilities": _capabilities(),
        "apple_storefront": _apple_storefront(),
        "pending_code": bool(_state.get("pending")),
    }


def install(app, ctx) -> None:
    global _state_path
    _s["config"] = ctx.config
    _s["save_config"] = ctx.save_config
    _s["base_dir"] = Path(ctx.base_dir)
    _s["broadcast"] = ctx.broadcast
    _s["queue"] = ctx.queue
    _s["watchlist"] = getattr(ctx, "watchlist", None)
    _s["download_history"] = getattr(ctx, "download_history", None)
    _s["save_history"] = getattr(ctx, "save_history", None)
    _state_path = _s["base_dir"] / "pairing_state.json"
    _load_state()

    # These carry their own auth (loopback / one-shot code / bearer token) and
    # are hit by a non-browser client, so exempt them from owner-cookie auth
    # and the Origin-based CSRF guard — same rationale as /api/telemetry/ingest.
    try:
        from ripster import auth as _auth
        # /api/pair/* all carry their own auth (loopback / one-shot code /
        # bearer device token) — exempt the whole subtree from owner-cookie
        # auth and the Origin CSRF guard (variable {task_id} segments need the
        # prefix form; exact paths still added for the CSRF set).
        _auth.add_public_prefix("/api/pair/")
        for p in ("/api/pair/start", "/api/pair/claim", "/api/pair/credentials",
                  "/api/pair/share", "/api/pair/status", "/api/pair/fetch",
                  "/api/pair/unpair", "/api/pair/revoke-all",
                  "/api/pair/ping", "/api/pair/mode", "/api/pair/activity",
                  "/api/pair/artist", "/api/pair/label",
                  "/api/pair/station", "/api/pair/taste",
                  "/api/pair/feedback", "/api/pair/hidden",
                  "/api/pair/genre"):
            _auth.add_public_path(p)
            _auth._CSRF_EXEMPT_PATHS.add(p)
    except Exception as e:  # pragma: no cover
        print(f"[pairing] could not register public paths: {e}")

    app.include_router(router)
    eps = ", ".join(e["url"] for e in _endpoints()) or "loopback only"
    devs = _state.get("tokens", [])
    print(f"[pairing] ready · pc_id={_pc_id()[:8]}… name={_pc_name()!r} "
          f"mode={_state.get('mode')} devices={len(devs)} "
          f"с учётками={sum(1 for t in devs if t.get('share_credentials'))} "
          f"caps={_capabilities()} · {eps}")
