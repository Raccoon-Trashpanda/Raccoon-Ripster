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
    GET  /api/pair/credentials  Bearer <token>    → { updated_at, credentials{} }   (stamps synced_at)
    POST /api/pair/mode         Bearer | loopback { mode }   fan-out: mirror|initiator|isolation
    POST /api/pair/share        (localhost only)  { enabled: bool }
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
_state: dict = {}                  # persisted: device_group_id, tokens[], share_credentials, pending, mode
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
    # ARCH says credential sharing must be an explicit opt-in on the PC. Default
    # ON here is a deliberate first-cut tradeoff so pairing is useful out of the
    # box on a single-owner local box; POST /api/pair/share flips it.
    _state.setdefault("share_credentials", True)
    _save_state()


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

    put("soundcloud.oauth", "soundcloud-oauth-token")
    put("deezer.arl", "deezer-arl")
    put("qobuz.app_id", "qobuz-app-id")
    put("qobuz.secret", "qobuz-secrets", "qobuz-secret")
    put("qobuz.email", "qobuz-email")
    put("qobuz.password", "qobuz-password")
    put("qobuz.token", "qobuz-auth-token")
    put("spotify.sp_dc", "spotify-sp-dc")
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
    if not _is_loopback(request):
        return JSONResponse({"error": "forbidden", "detail": "loopback only"}, status_code=403)
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
            "endpoints": _endpoints(),
            "share_credentials": _state["share_credentials"]}


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
        rec.update(token=tok, seen=int(now), ua=ua, name=name or rec.get("name", ""))
    else:
        _state["tokens"].append({
            "token": tok, "mobile_id": mobile_id, "name": name,
            "created": int(now), "seen": int(now), "ua": ua,
        })
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
        "share_credentials": _state["share_credentials"],
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
    if not (_is_loopback(request) or _token_valid(_bearer(request))):
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
    if not _is_loopback(request):
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
    if not _state.get("share_credentials"):
        return JSONResponse({"error": "sharing_disabled",
                             "detail": "enable credential sharing on the PC"}, status_code=403)
    # Устройство реально забрало учётки → помечаем: с этого момента оно НЕ
    # вытесняется молча (см. _evict_tokens).
    rec = _token_rec(tok)
    if rec is not None and not rec.get("synced_at"):
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
    if not _token_valid(_bearer(request)) and not _is_loopback(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    return {"phone_plays": _state.get("phone_plays", [])[:200]}


@router.post("/api/pair/share")
async def pair_share(body: dict, request: Request):
    if not _is_loopback(request):
        return JSONResponse({"error": "forbidden", "detail": "loopback only"}, status_code=403)
    _state["share_credentials"] = bool((body or {}).get("enabled", True))
    _save_state()
    return {"ok": True, "share_credentials": _state["share_credentials"]}


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
    return JSONResponse({"error": "enqueue_failed", "detail": str(res)[:200]}, status_code=502)


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

@router.get("/api/pair/radar")
async def pair_radar(request: Request):
    if not _token_valid(_bearer(request)):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    wl = _s.get("watchlist") or []
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
            "latest_url": e.get("last_release") or "",
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


@router.get("/api/pair/status")
async def pair_status(request: Request):
    if not _is_loopback(request):
        return JSONResponse({"error": "forbidden"}, status_code=403)
    now = int(time.time())
    _prune_pending()
    devices = [
        {
            "name": t.get("name", "") or t.get("ua", "")[:24] or "телефон",
            "mobile_id": t.get("mobile_id", ""),
            "created": t.get("created", 0),
            "seen": t.get("seen", t.get("created", 0)),
            "synced_at": t.get("synced_at", 0),
            "ua": t.get("ua", ""),
            "online": (now - t.get("seen", 0)) < 90,   # был запрос за последние 1.5 мин
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
        "share_credentials": _state["share_credentials"],
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
                  "/api/pair/artist", "/api/pair/label"):
            _auth.add_public_path(p)
            _auth._CSRF_EXEMPT_PATHS.add(p)
    except Exception as e:  # pragma: no cover
        print(f"[pairing] could not register public paths: {e}")

    app.include_router(router)
    eps = ", ".join(e["url"] for e in _endpoints()) or "loopback only"
    print(f"[pairing] ready · pc_id={_pc_id()[:8]}… name={_pc_name()!r} "
          f"mode={_state.get('mode')} devices={len(_state.get('tokens', []))} "
          f"share_credentials={_state['share_credentials']} caps={_capabilities()} · {eps}")
