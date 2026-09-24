"""Smart Apple download router.

Apple content needs a different decryption path per requested quality, and each
path depends on a resource that may or may not be available right now:

  - Video (mv)      : gamdl + cookies + gamdl's bundled Widevine CDM. No wrapper.
  - AAC (lossy)     : gamdl + cookies. No wrapper.
  - ALAC/Atmos/AC3  : a *wrapper* is mandatory (lossless/spatial keys). Either
        • AMD     → public wrapper-manager (``amd-instance-url``, e.g. wm.wol.moe);
                    needs NO Docker and NO Apple ID, OR
        • zhaarey → local Docker wrapper (``decrypt-port``, default 10020).

``route_apple()`` probes what is actually reachable (local wrapper TCP port,
cookies file) and returns the best engine+quality to satisfy the request.

The public wm.wol.moe wrapper (``amd``) is **manual-only**: it is chosen *only*
when the owner has set ``apple-wrapper = "public"`` in Settings. It is never an
automatic fallback — a foreign-storefront link or a down local wrapper stays on
the local wrapper, and ``runner.py`` rotates through the owner's other Apple
accounts (own storefront → other slots → honest error). This is a hard rule
(03.09.2026): the public pool is treated as gone unless explicitly asked for.
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


def key_request_storefront(url: str, acct_sf: str) -> tuple[str, bool]:
    """Витрина, в которую надо слать ЗАПРОС КЛЮЧА (расшифровки).

    Факт из переписки авторов wrapper'а (и разбор docs/APPLE_STOREFRONT_-
    MISMATCH_2026-09-24.md): DRM-ключ Apple выдаёт по фактической витрине
    АККАУНТА, а не по витрине ссылки. Метаданные могут браться из другой
    витрины — ключ нет. Раньше первый заход локального враппера шёл по витрине
    ССЫЛКИ (ручка `/ru/` при `gb`-аккаунте) и получал заведомый «Invalid store /
    Invalid CKC», который лестница потом исправляла реактивно.

    Возвращает `(cc, changed)`:
      · аккаунт известен и отличается от ссылки → (витрина_аккаунта, True) —
        ключ просим в своей витрине, ССЫЛКУ на ключ не пускаем;
      · витрина аккаунта неизвестна ('' — не спросили/нет сессии) →
        (витрина_ссылки, False): не выдумываем, прежняя лестница разрулит;
      · витрина аккаунта = витрине ссылки → она же, False (менять нечего).

    Чистая функция без сети: переписывание URL на найденный номер альбома —
    отдельный шаг (`rewrite_storefront_resolved`) у вызывающего.
    """
    acct = (acct_sf or "").strip().lower()
    url_sf = url_storefront(url)
    if acct and url_sf and acct != url_sf:
        return acct, True
    return (acct or url_sf), False


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
                                 f"⚠ пре-релиз: в своей витрине не стримится, "
                                 f"подобран регион '{url_storefront(hit[1])}'")
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
                # Кэш по adamId и для публичного враппера: подобранный адрес
                # релиза запоминается под его собственным номером, чтобы
                # повторный заход (пер-таск оверрайд, режим on_region) не
                # начинал поиск витрин заново.
                _AVAIL_CACHE[_apple_id(new)] = (now, new)
                return new, (f"⚠ недоступно в '{url_sf}' — беру регион '{cc}' "
                             f"(пре-релиз; нужна учётка этой страны)")
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


# Ответ `/status` публичного менеджера, разобранный. Пустой словарь = спросить
# не удалось (а не «пул пуст»: это разные вещи и путать их нельзя).
_pool_env: dict = {}
_pool_env_ts: float = 0.0
_POOL_TTL = 60.0


def _pool_envelope(config: dict, timeout: float = 6.0) -> dict:
    """Один дешёвый GET `{scheme}://{host}/status`, разобранный и запомненный.

    Возвращает словарь вида::

        {"status": int|None,   # HTTP-код; None — до сервера не достучались
         "data":   dict,       # что под ключом "data" (или сам ответ-словарь)
         "body":   dict,       # весь разобранный JSON, включая msg/code
         "error":  str}        # текстовая причина транспортного сбоя

    Разбираем ТЕЛО И ПРИ НЕУДАЧНОМ КОДЕ: конверт v2 (`wrapper-lite`) несёт
    причину отказа именно там — на 401 сервер отвечает
    `{"code":-1,"msg":"invalid or missing API key","bot":"wm_auth_bot",...}`,
    и «сервер нас не пускает» это совсем не то же самое, что «сервера нет».

    Кэш на ~60 с: вопрос задаётся и при маршрутизации задачи, и из интерфейса,
    а дергать волонтёрский сервис каждую секунду — значит самим создавать себе
    «слишком много запросов» там, где его нет.
    """
    global _pool_env, _pool_env_ts
    host = (config.get("amd-instance-url") or "").strip()
    if not host:
        _pool_env, _pool_env_ts = {}, 0.0       # без хоста кэш прошлого хоста врёт
        return {"status": None, "data": {}, "body": {}, "error": "no_host"}
    now = time.time()
    cached_host = _pool_env.get("host") if _pool_env else None
    if _pool_env and cached_host == host and now - _pool_env_ts < _POOL_TTL:
        return _pool_env
    scheme = "https" if config.get("amd-instance-secure", True) else "http"
    env = {"status": None, "data": {}, "body": {}, "error": "", "host": host}
    try:
        r = httpx.get(f"{scheme}://{host}/status", timeout=timeout)
        env["status"] = r.status_code
        try:
            body = r.json() or {}
        except Exception:                                      # noqa: BLE001
            body = {}
        if isinstance(body, dict):
            env["body"] = body
            data = body.get("data")
            # Старая (v1) форма отдавала поля прямо в корне — принимаем и так.
            env["data"] = data if isinstance(data, dict) else body
    except Exception as e:                                     # noqa: BLE001
        env["error"] = f"{type(e).__name__}: {e}"[:200]
    _pool_env, _pool_env_ts = env, now
    return env


def public_pool_status(config: dict) -> dict:
    """Что НА САМОМ ДЕЛЕ у публичного пула: готов ли, сколько клиентов, какие регионы.

    Раньше здоровье публичного враппера проверялось запросом в корень с
    условием «код меньше 500». Это доказывает ровно одно: веб-сервер жив. Про
    ПУЛ за ним — который и делает работу — оттуда не узнать ничего, и мы
    годами отправляли задачи в пустой пул, чтобы через ~28 секунд получить
    «no healthy and ready instances available».

    А правда лежит рядом, в одном дешёвом запросе. Замер 06.09.2026::

        GET https://wm.wol.moe/status
        {"code":0,"data":{"clientCount":19,"ready":true,
                          "regions":["cn","in","th","br","sg","jp","nz","tw",
                                     "it","id","kr","tr","my"],"status":true}}

    Здесь важны ОБА поля. `ready` отвечает «пул вообще работает», а `regions` —
    «для каких витрин у него есть живые устройства». Второе для нас решающее:
    витрина у нас `us`, аккаунт канадский, и ни того ни другого в списке нет.
    То есть пул может быть совершенно здоров и всё равно бесполезен именно нам
    — и это надо говорить сразу, а не после проваленной загрузки.

    Пустой словарь означает «спросить не удалось». Не «пул пуст»: незнание
    нельзя записывать как отрицательный ответ.

    Автор сервиса — WorldObservationLog (`wol.moe`), он же автор
    AppleMusicDecrypt, которым работает движок `amd`. Форму эндпоинта видно в
    его же браузерном клиенте amd.wol.moe; кода оттуда не взято ничего, только
    знание, КУДА спрашивать. См. CREDITS.md.
    """
    return _pool_envelope(config)["data"]


# ── ЧЕСТНОЕ СОСТОЯНИЕ ПУБЛИЧНОГО WRAPPER'А ────────────────────────────────────
# Четыре разных положения дела, которые раньше сваливались в одно «здоров»:
#   working        пул ответил и готов работать;
#   refusing     сервер жив, но НАС не обслуживает (401 без API-ключа, пул пуст,
#                формат ответа не понят) — здесь «< 500» врал особенно нагло:
#                21.09.2026 wm.wol.moe отвечает 401 на любой путь, а проверка
#                `status_code < 500` засчитывала это как «враппер работает»;
#   unreachable  до сервера не достучались вообще (DNS, коннект, таймаут);
#   not_configured адрес публичного инстанса не задан.
# Каждое из них означает для владельца СВОЁ действие, поэтому и показывать надо
# каждое отдельно, а не «зелёная точка».
PUBLIC_WRAPPER_STATES = ("working", "refusing", "unreachable", "not_configured")


def public_wrapper_probe(config: dict) -> dict:
    """Состояние публичного враппера для интерфейса и роутера — по одному запросу.

    {"state", "reason", "instance", "ready", "client_count", "regions", "detail"}.
    `reason` — машинный ключ (api_key / pool_empty / no_status / transport /
    http_500 / no_host), `detail` — человекочитаемо (сообщение сервера или текст
    сетевого сбоя). Значений токенов здесь нет и быть не может.
    """
    host = (config.get("amd-instance-url") or "").strip()
    if not host:
        return {"state": "not_configured", "reason": "no_host", "instance": "",
                "ready": False, "client_count": 0, "regions": [], "detail": ""}
    env = _pool_envelope(config)
    code, data, body = env["status"], env["data"], env["body"]
    base = {"instance": host, "regions": [str(x) for x in (data.get("regions") or [])
                                          if isinstance(data.get("regions"), list)]
                            or (data.get("regions") or []),
            "client_count": _as_int(data.get("clientCount") or data.get("client_count"))}
    if code is None:
        return {**base, "state": "unreachable", "reason": "transport", "ready": False,
                "detail": env["error"] or "transport error"}
    msg = str(body.get("msg") or data.get("msg") or "")[:160]
    if code in (401, 403):
        return {**base, "state": "refusing", "reason": "api_key", "ready": False,
                "detail": msg or "invalid or missing API key"}
    if code >= 400:
        return {**base, "state": "refusing", "reason": f"http_{code}", "ready": False,
                "detail": msg or f"HTTP {code}"}
    if "ready" not in data:
        # Живой HTTP-ответ без `ready` — это НЕ «здоров»: это мы перестали
        # понимать протокол (upstream перешёл с gRPC на HTTP, см.
        # docs/APPLE_WRAPPER_UPSTREAM.md). Промолчать тут — значит снова
        # выставить зелёную точку за неизвестность.
        return {**base, "state": "refusing", "reason": "no_status", "ready": False,
                "detail": msg or "unexpected /status response"}
    if not data.get("ready"):
        return {**base, "state": "refusing", "reason": "pool_empty", "ready": False,
                "detail": msg or "no ready instances"}
    return {**base, "state": "working", "reason": "", "ready": True, "detail": msg}


def _as_int(val) -> int:
    try:
        return int(val)
    except (TypeError, ValueError):
        return 0


def public_pool_serves(config: dict, storefront: str = "") -> bool | None:
    """Есть ли в пуле устройства для НАШЕЙ витрины.

    True / False — знаем; None — спросить не удалось, и тогда решение принимать
    не по догадке, а по прежнему поведению.
    """
    st = public_pool_status(config)
    regions = st.get("regions")
    if not isinstance(regions, list) or not regions:
        return None
    sf = (storefront or config.get("apple-country") or config.get("storefront") or "us")
    return str(sf).strip().lower() in {str(x).strip().lower() for x in regions}


def public_pool_pick_region(config: dict, want: str = "") -> str:
    """Какую витрину просить у публичного пула, чтобы он смог отдать ключ.

    Владелец 06.09.2026: «плевать, что он обслуживает — если он работает, он
    нужен в комбайне как вспомогательная опция». Верно: пул отдаёт тринадцать
    витрин, и среди них `nz` — та самая, с которой у нас начинается пятница в
    радаре. То есть его непокрытие нашей витрины это не приговор, а вопрос,
    какую витрину попросить.

    Порядок: сперва та, что просили (вдруг она в пуле и есть), затем
    предпочтения владельца, затем что осталось. Пусто — пул не спросился или
    ничего не обслуживает; тогда вызывающий не выдумывает и оставляет всё как
    было.
    """
    st = public_pool_status(config)
    regions = [str(x).strip().lower() for x in (st.get("regions") or []) if x]
    if not regions:
        return ""
    w = str(want or "").strip().lower()
    if w and w in regions:
        return w
    pref = config.get("amd-region-preference") or ["nz", "jp", "it", "tw", "kr", "sg", "my", "th"]
    for cc in [str(x).strip().lower() for x in pref]:
        if cc in regions:
            return cc
    return regions[0]


def _public_wrapper_ok(config: dict) -> bool:
    """Публичный пул можно ИСПОЛЬЗОВАТЬ — то есть он нам реально отвечает.

    Earlier version ended with `httpx.get(url).status_code < 500`, и это был не
    тест враппера, а тест веб-сервера: 21.09.2026 wm.wol.moe отвечает 401
    «invalid or missing API key» на любой путь, 401 < 500 — и роутер рапортовал
    «здоров» ровно потому, что его вежливо не пускают. Теперь признак один:
    состояние `working` из `public_wrapper_probe` (см. там же про `refusing` /
    `unreachable` / `not_configured`).
    """
    # Health gate first — the server can answer HTTP fine while its instance
    # pool has nobody connected (see public_wrapper_healthy below).
    if not public_wrapper_healthy():
        return False
    if public_pause_remaining():
        # 24-часовая пауза после череды 429: сколько бы /status ни отвечал
        # «готов», мы сами к нему сейчас не ходим.
        return False
    return public_wrapper_probe(config)["state"] == "working"


# ── Local wrapper CKC health gate ─────────────────────────────────────────────
# A TCP-open check is NOT enough: the local docker wrapper's port can be open
# while its saved Apple session can't mint content keys (logs "Invalid CKC
# error", the Go side dies with "decryptFragment: EOF"). When the zhaarey engine
# sees such a decrypt failure it calls ``mark_local_wrapper_unhealthy()`` so the
# router stops sending lossless work to a wrapper that only produces garbage —
# until the session is re-logged in. Куда именно уйти вместо этого, решает
# правило «публичный — только по явному выбору» (см. _decide ниже), а не этот
# флажок.
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


# ── РЕЖИМЫ ПУБЛИЧНОГО WRAPPER'А (выбор владельца, 24.09.2026) ────────────────
# Владелец велел: тумблер публичного враппера на странице настроек Apple несёт
# МНОЖЕСТВО сценариев и по умолчанию ВЫКЛЮЧЕН. Прежний `apple-wrapper: public`
# означал «всё Apple — через wm.wol.moe»; теперь это режим `only` той же
# группы. Правило 03.09 («публичный — только по явному выбору владельца») здесь
# НЕ отменяется: ни один режим не включает публичный путь по коду, только по
# настройке (или по чекбоксу конкретной задачи в диалоге загрузки).
#
#   off            — только свои учётки (поведение до 24.09, значение по умолчанию);
#   on_fail        — свои слоты отказали терминально (лестница runner исчерпана)
#                    → один запасной заход через публичный; следующий релиз — снова свои;
#   on_region      — релиза нет ни в одной витрине своих аккаунтов (пре-релиз,
#                    как nz-случай 24.09) → публичный с регионом, где он есть;
#   on_limit       — свои упёрлись в лимиты/пейсинг (429, device-limit, пауза
#                    входа) → публичный, пока свои не отойдут;
#   only           — всё Apple через публичный (свои учётки мертвы); наследует
#                    старое значение `apple-wrapper: public`;
#   manual_region  — то же, но витрина принудительно берётся из
#                    `amd-region-force` (список регионов из /status).
PUBLIC_MODES = ("off", "on_fail", "on_region", "on_limit", "only", "manual_region")


def public_mode(config: dict) -> str:
    """Режим из настройки `apple-public-mode`; пусто/неизвестно → 'off'.
    Legacy `apple-wrapper: public` читается как `only`: старый явный выбор
    владельца не должен молча исчезнуть с апгрейдом."""
    m = str((config or {}).get("apple-public-mode") or "").strip().lower()
    if m in PUBLIC_MODES:
        return m
    if str((config or {}).get("apple-wrapper") or "").strip().lower() == "public":
        return "only"
    return "off"


# Автоотключение после череда 429: несколько отказов по квоте подряд — режим
# НЕ ОТКЛЮЧАЕТСЯ ЗАПИСЬЮ В КОНФИГ (код не воюет с тумблером владельца), а
# вставляется пауза на 24 часа: маршрутизатор ведёт себя как `off`, карточка
# настроек честно показывает причину и срок, по истечении выбор владельца
# возвращается сам. Метка на диске — перезапуск приложения её не стирает.
PUBLIC_PAUSE_S = 24 * 3600.0
_429_STREAK = 3
_public_429_at: list = []           # отметки отказов 429 за последние 15 минут
_public_paused_until: float = 0.0   # кэш файла, чтобы не читать его на каждой задаче


def _public_pause_path():
    from pathlib import Path as _P
    import os as _os
    base = _P(_os.environ.get("RIPSTER_BASE_DIR") or _P(__file__).resolve().parent.parent)
    return base / "dist" / "public_wrapper_pause.json"


def _pause_on_disk(now: float) -> float:
    try:
        import json as _j
        return float(_j.loads(_public_pause_path().read_text(encoding="utf-8")).get("until") or 0)
    except Exception:
        return 0.0


def mark_public_429(config: dict) -> bool:
    """Отказ публичного API по квоте (HTTP 429). Возвращает True, если достигнута
    черед из `_429_STREAK` и режим автоматически выключен на 24 часа — вызывающий
    обязан сообщить об этом владельцу ОДИН раз, а не при каждом отказе."""
    global _public_429_at, _public_paused_until
    now = time.time()
    _public_429_at = [t for t in _public_429_at + [now] if now - t < 900]
    if len(_public_429_at) < _429_STREAK:
        return False
    _public_429_at = []
    _public_paused_until = now + PUBLIC_PAUSE_S
    try:
        import json as _j
        p = _public_pause_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(".tmp")
        tmp.write_text(_j.dumps({"until": _public_paused_until,
                                 "reason": "quota_429"},
                                ensure_ascii=False), encoding="utf-8")
        tmp.replace(p)
    except Exception as e:                                # noqa: BLE001
        print(f"[apple-router] паузу 429 не записал: {e}", flush=True)
    return True


def public_pause_remaining() -> float:
    global _public_paused_until
    if _public_paused_until <= time.time():
        _public_paused_until = _pause_on_disk(time.time())
    return max(0.0, _public_paused_until - time.time())


def clear_public_pause() -> None:
    """Явное действие владельца (перевыбор режима в Настройках) снимает паузу."""
    global _public_paused_until, _public_429_at
    _public_paused_until = 0.0
    _public_429_at = []
    try:
        _public_pause_path().unlink()
    except Exception:
        pass


# ── Суточный счётчик запросов и кэш повторов (по adamId релиза) ──────────────
# Файл `dist/public_wrapper_usage.json`:
#   {"day": "ГГГГ-ММ-ДД", "releases": {adamId: сколько раз просили сегодня},
#    "last": {adamId: unix-время последней отправки}}
# Одновременно счётчик дневной квоты (`amd-daily-cap`, 0 = не ограничивать) и
# кэш «этот релиз уже уходил в публичный враппер N минут назад» — повторная
# отправка того же adamId волонтёрскому сервису на второй круг бессмысленна.
def _usage_path():
    from pathlib import Path as _P
    import os as _os
    base = _P(_os.environ.get("RIPSTER_BASE_DIR") or _P(__file__).resolve().parent.parent)
    return base / "dist" / "public_wrapper_usage.json"


def _usage_load(roll_day: bool) -> tuple:
    import json as _j
    today = time.strftime("%Y-%m-%d")
    try:
        d = _j.loads(_usage_path().read_text(encoding="utf-8"))
        if not isinstance(d, dict):
            d = {}
    except Exception:
        d = {}
    if d.get("day") != today and roll_day:
        d = {"day": today, "releases": {}, "last": {}}
    d.setdefault("releases", {})
    d.setdefault("last", {})
    return d, today


def public_daily_used(config: dict) -> int:
    """Сколько РАЗНЫХ релизов сегодня ушло в публичный враппер (0 — неспрошено
    или пусто). Для счётчика в карточке настроек."""
    d, _ = _usage_load(roll_day=False)
    return len(d.get("releases") or {})


def public_dispatch_guard(config: dict, url: str) -> dict:
    """Учёт повторных отправок релиза в публичный враппер (кэш по adamId).
    Возвращает {"dup": bool, "blocked": bool, "used_today": int}:
      blocked — суточный кап выбран, нового релиза сегодня просить нельзя;
      dup     — этот релиз уже уходит в враппер прямо сейчас (окно 10 минут):
                отправку не блокируем (владетель жмёт «повтори» осознанно), но
                помечаем в маршруте честно и кап о дубль не расходуем."""
    try:
        cap = int(config.get("amd-daily-cap") or 0)
    except (TypeError, ValueError):
        cap = 0
    d, today = _usage_load(roll_day=True)
    releases, last = d["releases"], d["last"]
    aid = _apple_id(url)
    now = time.time()
    dup = bool(aid) and now - float(last.get(aid) or 0) < 600.0
    # Блокируем ТОЛЬКО новый релиз при исчерпанном капле: сравнение с числом
    # уже учтённых, а не «любой неучтённый» — иначе кап=5 запрещал и первый
    # запрос дня (поймано тестом 24.09.2026).
    if cap and aid and aid not in releases and not dup and len(releases) >= cap:
        return {"dup": False, "blocked": True, "used_today": len(releases)}
    try:
        if aid:
            if not dup:
                releases[aid] = int(releases.get(aid) or 0) + 1
            last[aid] = now
            tmp = _usage_path().with_suffix(".tmp")
            tmp.parent.mkdir(parents=True, exist_ok=True)
            import json as _j
            tmp.write_text(_j.dumps({"day": today, "releases": releases, "last": last},
                                    ensure_ascii=False), encoding="utf-8")
            tmp.replace(_usage_path())
    except Exception as e:                                # noqa: BLE001
        print(f"[apple-router] счётчик публичного враппера не записан: {e}", flush=True)
    return {"dup": dup, "blocked": False, "used_today": len(releases)}


def public_decision(config: dict, url: str = "", task: dict | None = None) -> tuple:
    """Можно ли ПУБЛИЧНЫЙ враппер для этой задачи и какой режим это разрешило.
    (public, режим, причина-блок). Режим on_fail проявляется НЕ здесь, а в
    раннере: он разрешает запасной заход ПОСЛЕ терминального отказа всех своих
    слотов (см. runner.run_task)."""
    mode = public_mode(config)
    if (task or {}).get("public_wrapper"):
        # Пер-таск оверрайд из диалога загрузки — явное действие человека,
        # работает при любом режиме, кроме выключенного паузой 429.
        mode = "only" if mode in ("off", "on_fail", "on_region", "on_limit") else mode
    else:
        if mode == "on_fail":
            return False, mode, ""
        if mode == "off":
            return False, "off", ""
    left = public_pause_remaining()
    if left:
        return False, mode, ("публичный враппер отключён на 24 ч после "
                             f"{_429_STREAK}×429 (осталось {int(left / 3600)} ч)")
    if mode in ("on_region",):
        # Режим решает раннер/роутер ПОСЛЕ проверки витрин: здесь только то,
        # что выбор в принципе сделан; ветка маршрута «свой враппер» остаётся
        # основной, публичный — по факту отсутствия релиза в своих витринах.
        return False, mode, ""
    if mode == "on_limit":
        # Публичный подхватывает отказы пейсинга (`apple_pacing_blocked`) —
        # ниже они не мешают, но и не включают amd заранее.
        return bool(apple_pacing_blocked(config)), mode, ""
    return True, mode, ""


def apple_pacing_blocked(config: dict) -> bool:
    """Наши СОБСТВЕНные пейсинг-лимиты Apple сейчас кусаются (429/403 в
    penalty у ripster.pacing) — для режима `on_limit` это признак «свои на
    лимите, пустить публичный»."""
    try:
        from ripster import pacing
        return pacing.wait_seconds("apple", config=config or {}) > 0
    except Exception:
        return False


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


# ── WRAPPER LITE (второй локальный бэкенд) ─────────────────────────────────
# lite — свой контейнер на петле (127.0.0.1:12340), аудит
# docs/WRAPPER_LITE_AUDIT_2026-09-24.md. Выбран ТОЛЬКО владельцем вручную
# (apple-wrapper = lite); недоступный Lite не уезжает на публичный пул —
# `_decide` его туда и не выпустит — а честно возвращается на локальный
# враппер с пояснением в заметке.
_lite_state: dict = {}
_lite_state_ts: float = 0.0
_LITE_TTL = 60.0


def lite_wrapper_state(config: dict, force: bool = False) -> dict:
    """{reachable, logged_in, regions, temari, error}; кэш 60 с, force — «ещё раз»."""
    global _lite_state, _lite_state_ts
    now = time.time()
    if not force and _lite_state and now - _lite_state_ts < _LITE_TTL:
        return dict(_lite_state)
    from ripster import lite as _lite
    st = {"reachable": False, "logged_in": False, "regions": [],
          "temari": True, "error": ""}
    try:
        import temari  # noqa: F401  — локальная расшифровка обязательна
    except Exception:
        st["temari"] = False
    try:
        data = _lite.get_client(config).status()
        st["reachable"] = True
        st["regions"] = [str(r) for r in (data.get("regions") or [])]
        # без токенов lite отвечает пустыми регионами — это «жив, но не залогинен»
        st["logged_in"] = bool(st["regions"])
    except _lite.LiteError as e:
        st["error"] = str(e)
    _lite_state, _lite_state_ts = st, now
    return dict(st)


def _lite_ready(config: dict) -> tuple:
    """(готов, почему-не-готов) — годный Lite для этой задачи."""
    st = lite_wrapper_state(config)
    if not st["temari"]:
        return False, "нет Temari"
    if not st["reachable"]:
        return False, "сервер недоступен"
    if not st["logged_in"]:
        return False, "учётка не залогинена"
    return True, ""


# ── ЕДИНСТВЕННЫЙ ВЫХОД ИЗ МАРШРУТИЗАТОРА ─────────────────────────────────────
# Правило «не уезжать с локального враппера само по себе» чинится не первый раз,
# и ломается каждый раз одинаково: его переписывают в КАЖДОЙ ветке, а новая
# ветка про него не знает. 03.09.2026 запрет авто-перехода на публичный wrapper
# применили к lossless — и забыли про AAC; 04.09 та же ссылка гостя за минуту
# дала «gamdl aac error» и «zhaarey alac done».
#
# Поэтому решение принимает ветка, а ВЫПУСКАЕТ его только `_decide`. Он знает
# два запрета и умеет их восстановить:
#
#   • `amd` (публичный wm.wol.moe) — только по явному выбору владельца: режим
#     `apple-public-mode` (24.09.2026), legacy `apple-wrapper = public`, или
#     чекбокс конкретной задачи;
#   • `gamdl` (куки) — только для видео, либо когда локальный wrapper реально
#     не отвечает И владелец не прибил движок к локальному.
#
# Нарушение не молчит: печатается «маршрут исправлен», по строке видно, какая
# ветка разъехалась. Тихая коррекция превратила бы сторожа в украшение.
def _decide(engine: str, quality: str, *, pref: str, local_ok: bool,
            is_video: bool, note: str = "", degraded: bool = False,
            public_ok: bool | None = None) -> dict:
    """Проверить решение ветки на два запрета и вернуть маршрут.

    `public_ok` — разрешение публичного враппера от `public_decision` (режимы
    24.09.2026). Переданной None (все старые вызовы и тесты) считается по
    прежнему правилу: `pref == "public"`."""
    if public_ok is None:
        public_ok = (pref == "public")
    fixed = ""
    if engine == "amd" and not public_ok:
        fixed = "публичный wrapper выбирается только владельцем (режим off)"
        engine = "zhaarey"
    elif engine == "gamdl" and not is_video and (local_ok or pref == "local"):
        fixed = ("локальный wrapper доступен" if local_ok
                 else "движок прибит владельцем к локальному врапперу")
        engine = "zhaarey"
    if fixed:
        print(f"[apple-router] маршрут исправлен → zhaarey: {fixed}", flush=True)
        note = (note + " · " if note else "") + "маршрут удержан на локальном wrapper"
    return {"engine": engine, "quality": quality, "degraded": degraded, "note": note}


_MODE_LABELS = {"only": "режим «только публичный»",
                "manual_region": "режим «ручной регион»",
                "on_limit": "режим «резерв при лимите своих»",
                "on_region": "режим «резерв по региону»",
                "on_fail": "режим «резерв при отказе своих»"}


def _public_route(q: str, config: dict, url: str, url_sf: str, acct_sf: str,
                  foreign: bool, cookies: bool, pref: str, pub_mode: str) -> dict:
    """Ветка публичного wrapper'а: честная заметка, учёт повторных отправок
    (кэш по adamId) и суточный кап. Единственный выход — снова `_decide`."""
    if is_apple_music_video(url):
        # Движок AMD audio-only: клипы публичный враппер не умеет ни в каком
        # режиме — остаётся gamdl с cookies.
        note = "" if cookies else "⚠ нет cookies.txt — видео не скачается"
        if pub_mode in _MODE_LABELS:
            note = (note + " · " if note else "") + \
                f"клипы публичный враппер не отдаёт ({_MODE_LABELS[pub_mode]} → gamdl)"
        return _decide("gamdl", "mv", pref=pref, local_ok=_local_wrapper_ok(config),
                       is_video=True, note=note)
    guard = public_dispatch_guard(config, url)
    if guard["blocked"]:
        return _decide("zhaarey", q, pref=pref, local_ok=_local_wrapper_ok(config),
                       is_video=False,
                       note=(f"⚠ суточный кап публичного враппера выбран "
                             f"({config.get('amd-daily-cap')}) — сегодня больше "
                             f"чужих релизов нет; локальный wrapper"))
    label = _MODE_LABELS.get(pub_mode, "выбран вручную")
    note = f"{q.upper()} · публичный wrapper · {label} (выбран вручную)"
    if not _public_wrapper_ok(config):
        note = (f"{q.upper()} · публичный wrapper в очереди · "
                f"{label} (выбран вручную)")
    if guard["dup"]:
        note += " · ⚠ этот релиз уходил в публичный враппер меньше 10 минут назад"
    # ГЛАВНОЕ СКАЗАТЬ СРАЗУ, А НЕ ПОСЛЕ ПРОВАЛЕННОЙ ЗАГРУЗКИ.
    #
    # Пул может быть совершенно здоров и всё равно бесполезен именно нам:
    # устройства в него подключают волонтёры, и витрин там ровно столько,
    # сколько их стран. Замер 06.09.2026 — пул готов, 19 клиентов, регионы
    # cn/in/th/br/sg/jp/nz/tw/it/id/kr/tr/my; нашей витрины `us` и
    # канадского аккаунта в списке нет. Без этой строки человек узнавал бы
    # об этом из «0 треков» через полминуты перебора.
    want_sf = (url_sf or acct_sf or config.get("storefront") or "us")
    forced = str(config.get("amd-region-force") or "").strip().lower()
    if pub_mode == "manual_region" and forced:
        note += f" · витрина принуждена '{forced}'"
    elif public_pool_serves(config, want_sf) is False:
        alt = public_pool_pick_region(config, want_sf)
        if alt and config.get("amd-region-rewrite", True) is not False:
            note += f" · витрины '{want_sf}' в пуле нет → берём '{alt}'"
        elif alt:
            note += (f" · ⚠ витрины '{want_sf}' в пуле нет "
                     f"(есть, например, '{alt}'), смена региона выключена")
    if foreign:
        note += f" · регион {url_sf}"
    return _decide("amd", q, pref=pref, local_ok=_local_wrapper_ok(config),
                   is_video=False, note=note, public_ok=True)


def route_apple(quality: str, config: dict, url: str = "",
                task: dict | None = None) -> dict:
    """Pick the best (engine, quality) for an Apple download of ``quality``.

    Returns ``{engine, quality, degraded, note}``. ``degraded`` is True when the
    requested quality could not be delivered and a lower one was substituted.

    REGION RULE: the cookies-based engine (gamdl) can only reach the catalog of
    the *account's* storefront. A foreign-region link is kept on the local
    wrapper (zhaarey) for lossless; if its account can't mint the key, runner.py
    rotates through the owner's other Apple account slots. The public AMD
    wrapper is reached only by an explicit owner choice: the mode group
    `apple-public-mode` (24.09.2026) or the legacy `apple-wrapper = public`
    (= режим `only`), or the per-task checkbox of the download dialog.
    """
    q = (quality or "").lower().strip()
    # A /music-video/ link is always video, regardless of the requested codec.
    if is_apple_music_video(url):
        q = "mv"
    cookies = _cookies_ok(config)
    # РЕАЛЬНЫЙ регион локального wrapper-аккаунта (спрашиваем Apple, кэш 15 мин),
    # а не `config['storefront']` — тот часто расходится с фактом (в конфиге `us`,
    # а аккаунт враппера `ca`) и тогда роутер отдаёт /us/-ссылку локальному
    # wrapper'у, который на неё получает 401 «Failed to rip song». Проба не
    # удалась → падаем на конфиг.
    acct_sf = (local_wrapper_storefront(config) or config.get("storefront") or "us").lower()
    url_sf  = url_storefront(url)
    foreign = bool(url_sf and url_sf != acct_sf)

    pref = (config.get("apple-wrapper") or "auto").strip().lower()
    # Режимы публичного враппера (выбор владельца 24.09.2026): можно ли этой
    # задаче уходить на `amd`, какой режим это разрешил, и чем заблокирован.
    pub, pub_mode, pub_block = public_decision(config, url, task)
    _pub_all = pub_mode in ("only", "manual_region") and pub

    # Публичный wm.wol.moe (engine "amd") подключается ТОЛЬКО когда владелец сам
    # выбрал его в Настройках — legacy `apple-wrapper = public` или режим
    # `apple-public-mode` (24.09.2026), либо чекбокс конкретной задачи. 03.09.2026
    # владелец потребовал прямо: НИКОГДА не переводить задачу на публичный wrapper
    # автоматически — он ненадёжен, может быть уже мёртв. Поэтому чужой регион
    # больше НЕ уводит задачу на публичный пул: она остаётся на локальном
    # враппере, а несовпадение витрины разруливает перебор Apple-аккаунтов в
    # runner.py (своя витрина → другие свои слоты → честная ошибка).
    # Исключения — режимы, которые владелец ВЫБРАЛ САМ: on_limit подхватывает
    # отказ своих лимитов, on_region — отсутствие релиза в своих витринах
    # (решение принимает runner после лестницы), only/manual_region — весь звук.
    if q in _LOSSLESS and pref == "lite":
        ready, why = _lite_ready(config)
        if ready:
            note = f"{q.upper()} · Wrapper Lite (ключ и расшифровка локально)"
            if foreign:
                regions = ", ".join(lite_wrapper_state(config)["regions"]) or "?"
                note += (f" · ссылка витрины '{url_sf}', витрины Lite: {regions}"
                         " — при отказе ключа сработает перебор учёток")
            return _decide("lite", q, pref=pref, local_ok=_local_wrapper_ok(config),
                           is_video=False, note=note)
        return _decide("zhaarey", q, pref=pref, local_ok=_local_wrapper_ok(config),
                       is_video=False, degraded=True,
                       note=f"{q.upper()} · Wrapper Lite ({why}) → локальный wrapper")

    if q in _LOSSLESS and not pub:
        note = f"{q.upper()} · локальный wrapper (премиум)"
        if pub_block:
            note += f" · {pub_block}"
        if foreign:
            note = (f"{q.upper()} · локальный wrapper · ссылка витрины '{url_sf}', "
                    f"аккаунт '{acct_sf}' — при отказе ключа сработает перебор учёток")
        return _decide("zhaarey", q, pref=pref, local_ok=_local_wrapper_ok(config),
                       is_video=False, note=note)

    # ── Video — gamdl only (cookies + bundled CDM, no wrapper) ───────────────
    if q in _VIDEO:
        note = "" if cookies else "⚠ нет cookies.txt — видео не скачается"
        if foreign:
            note = (note + " · " if note else "") + f"⚠ видео региона '{url_sf}' недоступно — cookies-аккаунт = '{acct_sf}'"
        return _decide("gamdl", "mv", pref=pref, local_ok=_local_wrapper_ok(config),
                       is_video=True, note=note)

    # ── «Только публичный» / «Ручной регион»: ВЕСЬ звук через wm.wol.moe ────
    # Режимы 5-6 (24.09.2026): свои учётки мертвы или владелец принуждает
    # витрину. Видео — исключение: у публичного враппера нет Widevine-пути,
    # клипы по-прежнему только gamdl (движок audio-only).
    if _pub_all and q not in _VIDEO:
        return _public_route(q, config, url, url_sf, acct_sf, foreign,
                             cookies, pref, pub_mode)

    # ── Lossless / spatial — a wrapper is mandatory ──────────────────────────
    # KEEP lossless (never silently fall back to lossy AAC). Сюда доходят только
    # задачи, для которых публичный враппер РАЗРЕШЁН явным выбором владельца
    # (режим only/manual_region, legacy public, чекбокс задачи, on_limit при
    # лимите своих). Никогда автоматически — жёсткое правило 03.09.2026.
    if q in _LOSSLESS:
        return _public_route(q, config, url, url_sf, acct_sf, foreign,
                             cookies, pref, pub_mode)

    # ── AAC / lossy ──────────────────────────────────────────────────────────
    # РАНЬШЕ AAC жёстко уходил в gamdl (cookies) — а куки могут быть от аккаунта
    # БЕЗ подписки: файл на месте, `active=false`, gamdl дохнет «подписка не
    # видна», хотя рядом живой локальный wrapper с подписанным аккаунтом.
    # Локальный wrapper (zhaarey) умеет AAC (aac / aac-lc) и декодит через свой
    # премиум-аккаунт — его и берём первым. Публичный wrapper из AAC-пути убран
    # осознанно: он ненадёжен и в этой сборке владельцем не используется.
    if q in ("aac", "aac-legacy", ""):
        local_ok = _local_wrapper_ok(config)   # pref уже вычислен выше по функции
        # Локальный wrapper — первый выбор: подписка на его аккаунте живая, и он
        # не зависит от cookies.
        #
        # ЧУЖОЙ РЕГИОН БОЛЬШЕ НЕ УВОДИТ ЗАДАЧУ ОТСЮДА (04.09.2026). Правило от
        # 03.09 — «несовпадение витрины разруливает перебор Apple-аккаунтов, а не
        # смена движка» — было применено только к lossless-ветке, а AAC по-старому
        # прыгал на куки. Живой случай: гость дал ссылку /nz/, аккаунт враппера
        # 'ca', и ОДНА И ТА ЖЕ ссылка за минуту дала две записи — AAC ушёл в gamdl
        # и упал «подписка не видна» (куки протухли), а ALAC остался на локальном
        # враппере и скачался. Владелец видел только первую, отсюда и ощущение,
        # что Ripster «постоянно уезжает» с дефолтного движка. Перебор учёток
        # (runner.py) умеет чужую витрину и для AAC — пусть он и работает.
        if local_ok and not pub:
            if pref == "lite":
                ready, why = _lite_ready(config)
                if ready:
                    return _decide("lite", q or "aac", pref=pref, local_ok=local_ok,
                                   is_video=False, note="AAC · Wrapper Lite")
                return _decide("zhaarey", q or "aac", pref=pref, local_ok=local_ok,
                               is_video=False, degraded=True,
                               note=f"AAC · Wrapper Lite ({why}) → локальный wrapper")
            note = "AAC · локальный wrapper"
            if pub_block:
                note += f" · {pub_block}"
            if foreign:
                note += (f" · ссылка витрины '{url_sf}', аккаунт '{acct_sf}' — "
                         f"при отказе ключа сработает перебор учёток")
            return _decide("zhaarey", q or "aac", pref=pref, local_ok=local_ok,
                           is_video=False, note=note)
        # Сюда попадаем, только если локальный wrapper НЕ отвечает (или владелец
        # сам выбрал публичный). Тогда куки — единственный оставшийся путь.
        if cookies and pref != "local":
            return _decide("gamdl", q or "aac", pref=pref, local_ok=local_ok,
                           is_video=False,
                           note=("AAC · gamdl (локальный wrapper не отвечает)"
                                 + (f" · регион {url_sf}" if foreign else "")))
        # Ни враппера, ни куков в своей витрине нет — отдаём в локальный wrapper,
        # пусть очередит и дождётся; публичный НЕ трогаем.
        return _decide("zhaarey", q or "aac", pref=pref, local_ok=local_ok,
                       is_video=False, note="AAC · локальный wrapper (в очереди)")

    # ── Unknown quality id — keep the configured engine, no override ─────────
    return _decide(config.get("engine", "zhaarey"), quality, pref=pref,
                   local_ok=_local_wrapper_ok(config), is_video=False)
