"""Smart Apple download router.

Apple content needs a different decryption path per requested quality, and each
path depends on a resource that may or may not be available right now:

  - Video (mv)      : gamdl + cookies + gamdl's bundled Widevine CDM. No wrapper.
  - AAC (lossy)     : gamdl + cookies. No wrapper.
  - ALAC/Atmos/AC3  : a *wrapper* is mandatory (lossless/spatial keys). Either
        • AMD     → public wrapper-manager (``amd-instance-url``, e.g. wm.wol.moe);
                    needs NO Docker and NO Apple ID, OR
        • zhaarey → local Docker wrapper (``decrypt-port``, default 10020).

``route_apple()`` probes what is actually reachable (public wrapper HTTP, local
wrapper TCP port, cookies file) and returns the best engine+quality to satisfy
the request — degrading to the best lossy result (AAC via cookies) only when no
wrapper at all is available, so every task yields the maximum possible output.
"""
from __future__ import annotations

import re
import socket
import time
from pathlib import Path

import httpx

# music.apple.com/<storefront>/album/... → the 2-letter region segment.
_RE_STOREFRONT = re.compile(r"music\.apple\.com/([a-z]{2})/", re.I)


def url_storefront(url: str) -> str:
    m = _RE_STOREFRONT.search(url or "")
    return m.group(1).lower() if m else ""


# ── Availability-aware region resolution (pre-release handling) ───────────────
# A release can be live in one storefront before another (e.g. out in /nz/ days
# before our /gb/ account). iTunes flags this per region via `isStreamable`. If
# the link's region can't stream it yet, we find a region that CAN and rewrite
# the storefront — AMD's public wrapper carries multi-region accounts, so it
# pulls the pre-release from there. (Verified earlier: a foreign-region URL
# downloads fine via AMD.)
_REGION_PROBE = ["nz", "au", "us", "ca", "jp", "gb", "de", "fr", "ie", "nl"]
_AVAIL_CACHE: dict = {}        # apple_id -> (ts, url_or_None)
_AVAIL_TTL = 1800.0


def _apple_id(url: str) -> str:
    m = (re.search(r"[?&]i=(\d+)", url or "")
         or re.search(r"/(?:album|song|music-video)/[^/]+/(\d+)", url or "")
         or re.search(r"/(\d+)(?:\?|$)", url or ""))
    return m.group(1) if m else ""


def _rewrite_storefront(url: str, cc: str) -> str:
    return re.sub(r"(music\.apple\.com/)[a-z]{2}(/)", rf"\g<1>{cc}\g<2>", url, count=1, flags=re.I)


def rewrite_storefront_resolved(url: str, cc: str) -> str:
    """Сменить витрину и подставить ЕЁ СОБСТВЕННЫЙ номер альбома.

    🔴 Зачем отдельная функция (08.08.2026). `_rewrite_storefront` меняет только
    код страны, а идентификатор оставляет прежним. Для видео это верно — там ID
    глобальный. Для АЛЬБОМОВ — нет: Apple нумерует один и тот же релиз
    по-разному в разных витринах.

        Apparat — A Hum Of Maybe:  ru/gb/de → 1850997297
                                   ca/us    → 1852529754
                                   jp       → 1878218670

    Из-за этого повтор «своя витрина вместо чужой» был обречён: ссылка честно
    становилась /ca/, но с номером, которого в канадской витрине не существует,
    и каталог отвечал «релиза нет». Дальше задача уходила на публичный wrapper,
    тот лежал, и прогон уходил в пустоту на девятнадцать минут — при живом
    оплаченном аккаунте, у которого этот альбом БЫЛ.

    Если найти местный номер не удалось, возвращаем обычную замену страны: хуже
    прежнего не будет, а сеть могла просто не ответить.
    """
    plain = _rewrite_storefront(url, cc)
    if "/album/" not in url and "/song/" not in url:
        return plain                      # видео и прочее — номер глобальный
    try:
        from ripster.engines.errors import apple_album_by_storefront
        found = apple_album_by_storefront(url)
        local = (found.get((cc or "").lower()) or ("", ""))[0]
        if local:
            return re.sub(r"/(\d+)(\?|$)", rf"/{local}\g<2>", plain, count=1)
    except Exception:
        pass
    return plain


async def resolve_available_url(url: str, config: dict):
    """If the Apple link isn't streamable in its own storefront, return a URL
    rewritten to a region that CAN stream it (pre-release case). Returns
    ``(url, note)`` — url unchanged when already streamable or on any error.
    Music videos: gamdl is region-locked to the cookies account, so a foreign
    link (e.g. /nz/music-video/…) 404s. The video ID is GLOBAL, so rewrite the
    link's storefront to the account's region — same video, reachable region."""
    if is_apple_music_video(url):
        acct = (config.get("storefront") or "us").lower()
        sf = url_storefront(url)
        if sf and sf != acct:
            return _rewrite_storefront(url, acct), f"🎬 видео: регион '{sf}'→'{acct}' (cookies-аккаунт)"
        return url, ""
    aid = _apple_id(url)
    if not aid:
        return url, ""
    now = time.time()
    hit = _AVAIL_CACHE.get(aid)
    if hit and now - hit[0] < _AVAIL_TTL:
        return (hit[1] or url), ("" if not hit[1] or hit[1] == url else
                                 f"⚠ пре-релиз: регион '{url_storefront(hit[1])}' (публичный wrapper)")
    url_sf = url_storefront(url) or (config.get("storefront") or "us").lower()

    async def _streamable(client, cc):
        try:
            r = await client.get("https://itunes.apple.com/lookup",
                                  params={"id": aid, "country": cc, "entity": "song"},
                                  timeout=8)
            for x in (r.json().get("results") or []):
                if x.get("kind") == "song" or x.get("wrapperType") == "track":
                    return bool(x.get("isStreamable"))
        except Exception:
            return None
        return None

    try:
        from ripster.http_client import aclient
        c = aclient()
        if await _streamable(c, url_sf):
            _AVAIL_CACHE[aid] = (now, url)
            return url, ""
        for cc in _REGION_PROBE:
            if cc == url_sf:
                continue
            if await _streamable(c, cc):
                new = _rewrite_storefront(url, cc)
                _AVAIL_CACHE[aid] = (now, new)
                return new, f"⚠ недоступно в '{url_sf}' — беру регион '{cc}' (пре-релиз, публичный wrapper)"
    except Exception:
        pass
    _AVAIL_CACHE[aid] = (now, url)
    return url, ""


def is_apple_music_video(url: str) -> bool:
    """True for an Apple Music *music video* link (``/music-video/…``).

    Video can only be handled by gamdl (zhaarey/amd are audio-only) at the ``mv``
    quality — ``route_apple`` forces both when it sees such a URL.
    """
    u = (url or "").lower()
    return "music.apple.com" in u and "/music-video/" in u

# Quality ids that REQUIRE a wrapper (lossless / spatial).
_LOSSLESS = {"alac", "alac-hires", "atmos", "ec3", "ac3", "aac-binaural", "aac-downmix"}
# Quality ids that mean "music video".
_VIDEO = {"mv", "music-video", "video"}

# Probe results are cached briefly so a burst of queue adds doesn't hammer the
# network / re-open sockets on every single call.
_probe_cache: dict[str, tuple[float, bool]] = {}
_TTL = 45.0


def _cached(key: str, fn) -> bool:
    now = time.time()
    hit = _probe_cache.get(key)
    if hit and now - hit[0] < _TTL:
        return hit[1]
    try:
        val = bool(fn())
    except Exception:
        val = False
    _probe_cache[key] = (now, val)
    return val


def _public_wrapper_ok(config: dict) -> bool:
    # Honour the pool health gate first — the server can answer HTTP fine while
    # its instance pool has nobody connected (see public_wrapper_healthy below).
    if not public_wrapper_healthy():
        return False
    host = (config.get("amd-instance-url") or "").strip()
    if not host:
        return False
    scheme = "https" if config.get("amd-instance-secure", True) else "http"
    url = f"{scheme}://{host}"
    return _cached(f"pub:{url}", lambda: httpx.get(url, timeout=6.0).status_code < 500)


# ── Local wrapper CKC health gate ─────────────────────────────────────────────
# A TCP-open check is NOT enough: the local docker wrapper's port can be open
# while its saved Apple session can't mint content keys (logs "Invalid CKC
# error", the Go side dies with "decryptFragment: EOF"). When the zhaarey engine
# sees such a decrypt failure it calls ``mark_local_wrapper_unhealthy()`` so the
# router stops sending lossless work to a wrapper that only produces garbage —
# it auto-routes to the public wrapper instead until the session is re-logged in.
_local_unhealthy_until: float = 0.0


def local_wrapper_session_alive(config: dict | None = None, timeout: float = 4.0) -> bool:
    """Жива ли Apple-сессия внутри локального враппера ПРЯМО СЕЙЧАС.

    «Invalid CKC» имеет две совершенно разные причины, и лечатся они
    противоположно:
      · сессия протухла — тогда враппер бесполезен для любого контента;
      · у контента нет прав в регионе аккаунта — сессия при этом здорова.

    28.07.2026 второе принимали за первое: альбом, изданный только в tr и ru,
    не дал ключ канадскому аккаунту → враппер помечался нездоровым на 15 минут
    → ВСЯ lossless-загрузка уходила на публичный wrapper, который регулярно
    лежит. Один чужой регион ронял Apple целиком. Причём в те же минуты этот же
    враппер расшифровал соседний альбом 9 треков из 9.

    Аккаунт-API отдаёт media-user-token только при живой сессии — это и есть
    честный признак, в отличие от открытого TCP-порта.
    """
    cfg = config if config is not None else globals().get("config") or {}
    url = str((cfg or {}).get("gamdl-wrapper-account-url") or "http://127.0.0.1:30020").strip()
    if not url:
        return False
    try:
        r = httpx.get(url, timeout=timeout)
        if r.status_code != 200:
            return False
        tok = (r.json() or {}).get("music_token") or ""
        return len(str(tok)) > 50
    except Exception:
        return False


_wrapper_sf_cache: tuple[float, str] = (0.0, "")


def local_wrapper_storefront(config: dict | None = None, timeout: float = 6.0) -> str:
    """Витрина аккаунта ВНУТРИ локального враппера — та, где у него есть права.

    Нужна, потому что «Invalid CKC при живой сессии» почти всегда означает не
    «релиза нет», а «ссылка указывает на ЧУЖУЮ витрину». 01.08.2026: аккаунт
    враппера в `ca`, ссылка была на `gb` — Apple отказал в ключе, хотя тот же
    альбом в канадской витрине есть. Публичный wrapper в этот момент лежал
    («Deadline Exceeded»), и владелец резонно спросил: зачем он вообще, если
    живой аккаунт свой.

    Спрашиваем сам Apple, а не конфиг: аккаунт-API враппера отдаёт и
    developer-token, и media-user-token, а `/v1/me/account?meta=subscription`
    возвращает витрину и заодно доказывает, что подписка активна. Возраст файлов
    и сроки кук этого не показывают (см. [[project_gamdl_cookies_vs_subscription_2026-08-01]]).

    Пустая строка — «не смог узнать»; вызывающий тогда ничего не переписывает.
    """
    global _wrapper_sf_cache
    ts, val = _wrapper_sf_cache
    if val and (time.time() - ts) < 900:
        return val
    cfg = config if config is not None else globals().get("config") or {}
    url = str((cfg or {}).get("gamdl-wrapper-account-url") or "http://127.0.0.1:30020").strip()
    try:
        r = httpx.get(url, timeout=timeout)
        if r.status_code != 200:
            return ""
        d = r.json() or {}
        mut, dev = str(d.get("music_token") or ""), str(d.get("dev_token") or "")
        if len(mut) < 50 or len(dev) < 50:
            return ""
        rr = httpx.get("https://amp-api.music.apple.com/v1/me/account",
                       params={"meta": "subscription"},
                       headers={"Authorization": f"Bearer {dev}",
                                "Media-User-Token": mut,
                                "Origin": "https://music.apple.com"},
                       timeout=timeout)
        if rr.status_code != 200:
            return ""
        sub = ((rr.json() or {}).get("meta") or {}).get("subscription") or {}
        if not sub.get("active"):
            return ""          # подписки нет — переписывать витрину бессмысленно
        sf = str(sub.get("storefront") or "").lower()
        if sf:
            _wrapper_sf_cache = (time.time(), sf)
        return sf
    except Exception:
        return ""


def mark_local_wrapper_unhealthy(ttl: float = 900.0) -> None:
    """Flag the local docker wrapper as unable to decrypt (bad/expired Apple
    session) for ``ttl`` seconds — the router will skip it meanwhile."""
    global _local_unhealthy_until
    _local_unhealthy_until = time.time() + ttl


def local_wrapper_healthy() -> bool:
    """False while the local wrapper is in its post-CKC-failure cooldown."""
    return time.time() >= _local_unhealthy_until


# ── Public wrapper-manager pool health gate ───────────────────────────────────
# The plain HTTP reachability check (``_public_wrapper_ok``) only proves the
# wm.wol.moe server itself answers — it says nothing about whether the POOL
# behind it has any actual wrapper instance online. Confirmed 2026-07-22: the
# gRPC channel opens fine, but every real request fails with
# "WrapperManagerException: no healthy and ready instances available" — a
# volunteer-hosted pool with zero connected instances at that moment. The AMD
# engine calls ``mark_public_wrapper_unhealthy()`` on that exact error so the
# router stops sending traffic into a ~28s guaranteed-fail retry loop until
# the cooldown expires and it's worth probing again.
_public_unhealthy_until: float = 0.0


_public_down: bool = False        # выключен до подтверждения работоспособности
_public_next_probe: float = 0.0   # раньше этого времени не пробуем даже разово
_public_fail_streak: int = 0

# Пауза перед разведкой растёт: пул волонтёрский и лежит по часу и дольше,
# долбиться в него каждые пять минут бессмысленно.
_PUBLIC_BACKOFF = (300.0, 900.0, 1800.0, 3600.0)


def mark_public_wrapper_unhealthy(ttl: float = 0.0) -> None:
    """Выключить публичный wrapper-manager — до подтверждения, что он ожил.

    Раньше это был просто таймер на 300 с: истёк — и трафик снова шёл в мёртвый
    пул, снова упирался, снова ждал. Владелец сформулировал правило иначе, и
    оно правильнее: <b>определили как нерабочий — не использовать, пока не
    определится как рабочий</b>. Поэтому здесь защёлка, а не срок.

    `ttl` оставлен ради обратной совместимости вызовов и игнорируется: паузу
    выбирает сама функция по длине череды отказов.
    """
    global _public_down, _public_next_probe, _public_fail_streak
    _public_down = True
    _public_fail_streak = min(_public_fail_streak + 1, len(_PUBLIC_BACKOFF) - 1)
    _public_next_probe = time.time() + _PUBLIC_BACKOFF[_public_fail_streak]


def mark_public_wrapper_healthy() -> bool:
    """Публичный пул только что реально отработал — снять защёлку.

    Возвращает True, если состояние поменялось (был выключен), чтобы вызывающий
    мог сообщить об этом один раз, а не при каждом успешном треке.
    """
    global _public_down, _public_fail_streak
    changed = _public_down
    _public_down = False
    _public_fail_streak = 0
    return changed


def public_wrapper_healthy() -> bool:
    """Можно ли сейчас направлять задачи в публичный пул.

    Пока защёлка стоит — нельзя. Исключение одно: после паузы пропускаем ОДНУ
    разведочную попытку, иначе выключенный однажды пул никогда не вернётся сам.
    Успех такой попытки снимает защёлку через `mark_public_wrapper_healthy()`,
    неуспех — заводит её снова, уже с большей паузой.
    """
    if not _public_down:
        return True
    return time.time() >= _public_next_probe


def public_wrapper_state() -> dict:
    """Состояние для интерфейса и отчётов: выключен ли и когда следующая проба."""
    return {"down": _public_down,
            "next_probe_in": max(0, int(_public_next_probe - time.time())) if _public_down else 0,
            "fail_streak": _public_fail_streak}


def _local_wrapper_ok(config: dict) -> bool:
    # Honour the CKC health gate first — a wrapper that just failed to decrypt
    # is treated as down even though its socket is still listening.
    if not local_wrapper_healthy():
        return False
    raw = str(config.get("decrypt-port") or "127.0.0.1:10020")
    host, _, port_s = raw.rpartition(":")
    host = host or "127.0.0.1"
    try:
        port = int(port_s)
    except ValueError:
        return False

    def _probe() -> bool:
        s = socket.socket()
        s.settimeout(1.0)
        try:
            s.connect((host, port))
            return True
        finally:
            s.close()

    return _cached(f"loc:{host}:{port}", _probe)


def _cookies_ok(config: dict) -> bool:
    p = (config.get("gamdl-cookies-path") or "").strip() or "cookies.txt"
    try:
        return Path(p).is_file() and Path(p).stat().st_size > 0
    except OSError:
        return False


def route_apple(quality: str, config: dict, url: str = "") -> dict:
    """Pick the best (engine, quality) for an Apple download of ``quality``.

    Returns ``{engine, quality, degraded, note}``. ``degraded`` is True when the
    requested quality could not be delivered and a lower one was substituted.

    REGION RULE: the cookies-based engine (gamdl) can only reach the catalog of
    the *account's* storefront. When the link points at a DIFFERENT region (e.g.
    a release that's already out in /nz/ but not yet in our /gb/ account), gamdl
    would 404 — so we steer such audio to AMD, whose public wrapper-manager
    carries many regional accounts. (Video stays gamdl-only and can't cross
    regions; ALAC/Atmos already go to AMD.)
    """
    q = (quality or "").lower().strip()
    # A /music-video/ link is always video, regardless of the requested codec.
    if is_apple_music_video(url):
        q = "mv"
    cookies = _cookies_ok(config)
    acct_sf = (config.get("storefront") or "us").lower()
    url_sf  = url_storefront(url)
    foreign = bool(url_sf and url_sf != acct_sf)

    # When the owner forces the local wrapper, return it immediately and NEVER
    # probe the public wm.wol.moe (keeps it out of the logs and the path entirely).
    if q in _LOSSLESS and (config.get("apple-wrapper") or "auto").strip().lower() == "local":
        return {"engine": "zhaarey", "quality": q, "degraded": False,
                "note": f"{q.upper()} · локальный wrapper (премиум)"}

    pub_ok  = _public_wrapper_ok(config)

    # ── Video — gamdl only (cookies + bundled CDM, no wrapper) ───────────────
    if q in _VIDEO:
        note = "" if cookies else "⚠ нет cookies.txt — видео не скачается"
        if foreign:
            note = (note + " · " if note else "") + f"⚠ видео региона '{url_sf}' недоступно — cookies-аккаунт = '{acct_sf}'"
        return {"engine": "gamdl", "quality": "mv", "degraded": False, "note": note}

    # ── Lossless / spatial — a wrapper is mandatory ──────────────────────────
    # Policy: KEEP lossless (never silently fall back to lossy AAC). Prefer the
    # public wrapper (multi-region, reliably subscribed); use the local docker
    # wrapper only when it's same-region AND CKC-healthy (see the health gate).
    # If no wrapper looks ready we still hand it to AMD's public wrapper and let
    # it queue — quality over speed, by the user's choice.
    if q in _LOSSLESS:
        # Which wrapper to use is the OWNER's choice (Settings → Apple → Wrapper):
        #   "local"  → always the local docker wrapper (owner's premium account);
        #   "public" → always the public wm.wol.moe wrapper-manager;
        #   "auto"   → local for same-region (reliable premium), public otherwise.
        # Nothing is hard-wired: each mode just works when selected. Foreign-region
        # links can only be served by the multi-region public wrapper, so a "local"
        # choice still uses public for those (the local account can't see them).
        pref = (config.get("apple-wrapper") or "auto").strip().lower()
        local_ok = _local_wrapper_ok(config)

        if pref == "public":
            if pub_ok:
                return {"engine": "amd", "quality": q, "degraded": False,
                        "note": f"{q.upper()} · публичный wrapper" + (f" · регион {url_sf}" if foreign else "")}
            return {"engine": "amd", "quality": q, "degraded": False,
                    "note": f"{q.upper()} · публичный wrapper в очереди"}

        if pref == "local":
            # Explicit user choice: ALWAYS the local wrapper, NEVER public — even
            # if the local wrapper looks down (it'll queue/retry on its own). The
            # owner does not want the public pool used under any circumstances.
            return {"engine": "zhaarey", "quality": q, "degraded": False,
                    "note": f"{q.upper()} · локальный wrapper (премиум)"}

        # auto: prefer the local premium wrapper whenever it's up (reliable),
        # public pool as fallback.
        if local_ok:
            return {"engine": "zhaarey", "quality": q, "degraded": False,
                    "note": f"{q.upper()} · локальный wrapper (премиум)"}
        if pub_ok:
            return {"engine": "amd", "quality": q, "degraded": False,
                    "note": f"{q.upper()} · публичный wrapper" + (f" · регион {url_sf}" if foreign else "")}
        return {"engine": "amd", "quality": q, "degraded": False,
                "note": f"{q.upper()} · wrapper в очереди — ждём lossless"}

    # ── AAC / lossy ──────────────────────────────────────────────────────────
    # РАНЬШЕ AAC жёстко уходил в gamdl (cookies) — а куки могут быть от аккаунта
    # БЕЗ подписки: файл на месте, `active=false`, gamdl дохнет «подписка не
    # видна», хотя рядом живой локальный wrapper с подписанным аккаунтом.
    # Локальный wrapper (zhaarey) умеет AAC (aac / aac-lc) и декодит через свой
    # премиум-аккаунт — его и берём первым. Публичный wrapper из AAC-пути убран
    # осознанно: он ненадёжен и в этой сборке владельцем не используется.
    if q in ("aac", "aac-legacy", ""):
        pref = (config.get("apple-wrapper") or "auto").strip().lower()
        local_ok = _local_wrapper_ok(config)
        # Локальный wrapper — первый выбор: подписка на его аккаунте живая, и он
        # не зависит от cookies. Чужой регион он не видит — там нужен gamdl/куки.
        if local_ok and not foreign and pref != "public":
            return {"engine": "zhaarey", "quality": q or "aac", "degraded": False,
                    "note": "AAC · локальный wrapper"}
        # Враппер лёг или ссылка чужого региона — пробуем gamdl (его витрина = регион
        # cookies-аккаунта). Это фолбэк, не основной путь.
        if cookies:
            return {"engine": "gamdl", "quality": q or "aac", "degraded": False,
                    "note": (f"AAC · gamdl · регион {url_sf}" if foreign else "")}
        # Ни враппера, ни куков в своей витрине нет — отдаём в локальный wrapper,
        # пусть очередит и дождётся; публичный НЕ трогаем.
        return {"engine": "zhaarey", "quality": q or "aac", "degraded": False,
                "note": "AAC · локальный wrapper (в очереди)"}

    # ── Unknown quality id — keep the configured engine, no override ─────────
    return {"engine": config.get("engine", "zhaarey"), "quality": quality,
            "degraded": False, "note": ""}
