"""Что на самом деле не так с `cookies.txt`: сессия или подписка.

gamdl падает с «No active Apple Music subscription», и по этой строке долго
давался ОДИН совет — «экспортируй cookies.txt заново». Но состояния два, и
лечатся они по-разному:

  * сессия ПРОТУХЛА (401/403) — повторный экспорт из браузера действительно
    чинит;
  * сессия ЖИВА, а подписка на этом аккаунте кончилась (200, `active=false`) —
    повторный экспорт ТЕХ ЖЕ куки не изменит ничего, и человек потратит время
    впустую.

Живой случай 04.09.2026: гостю не отдали `music.apple.com/nz/song/aurora/…`, в
письме стояло «протухли cookies», а на деле сессия отвечала 200 и мертва была
подписка (storefront gb). Владелец пошёл бы экспортировать куки заново и получил
бы ровно то же самое.

Проверка была написана 01.08.2026, но жила ВНУТРИ сторожа здоровья
(`tools/ripster_healthcheck.py`) — то есть знал правду отчёт, а сообщение
движка её не знало. Модуль вынесен, чтобы правда была одна: и сторож, и
`engines/gamdl.py` спрашивают здесь.

Ничего не скачивает и не пишет: только читает `cookies.txt` и делает один
запрос к amp-api.
"""
from __future__ import annotations

import json
import re
import urllib.error
import urllib.request
from pathlib import Path

_CATALOG_PROBE = "https://amp-api.music.apple.com/v1/catalog/us/artists/909253"
_ACCOUNT = "https://amp-api.music.apple.com/v1/me/account?meta=subscription"
_UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}

_dev_token_cache: tuple[str, str] | None = None


# ── media-user-token из cookies.txt ──────────────────────────────────────────

def read_media_user_token(path: str | Path) -> str:
    """Последний `media-user-token` из Netscape-файла куки (их бывает два —
    берём последний, он свежее)."""
    mut = ""
    try:
        for line in Path(path).read_text(encoding="utf-8", errors="ignore").splitlines():
            f = line.split("\t")
            if len(f) >= 7 and f[5].strip() == "media-user-token":
                mut = f[6].strip()
    except Exception:
        return ""
    return mut


# ── developer token ──────────────────────────────────────────────────────────

def _wrapper_dev_token() -> str:
    try:
        with urllib.request.urlopen("http://127.0.0.1:30020", timeout=6) as r:
            d = json.loads(r.read().decode("utf-8", "ignore"))
        return str((d or {}).get("dev_token") or "").strip()
    except Exception:
        return ""


def _dev_token_works(tok: str) -> bool:
    """Единственный честный признак живого dev_token — публичный каталог отвечает."""
    if not tok:
        return False
    try:
        req = urllib.request.Request(
            _CATALOG_PROBE,
            headers={"Authorization": f"Bearer {tok}", "Origin": "https://music.apple.com"})
        with urllib.request.urlopen(req, timeout=12) as r:
            return r.status == 200
    except Exception:
        return False


def _scrape_dev_token() -> str:
    """Web-player dev_token из JS-бандла music.apple.com.

    В бандле несколько JWT, и годится не всякий (у одного iss своя, каталог
    отвечает ему 401) — берём первый, который РЕАЛЬНО ответил."""
    try:
        req = urllib.request.Request("https://music.apple.com/us/browse", headers=_UA)
        with urllib.request.urlopen(req, timeout=20) as r:
            html = r.read().decode("utf-8", "ignore")
        m = re.search(r"/assets/index~[^\"']*\.js", html)
        if not m:
            return ""
        req = urllib.request.Request("https://music.apple.com" + m.group(0), headers=_UA)
        with urllib.request.urlopen(req, timeout=25) as r:
            body = r.read().decode("utf-8", "ignore")
    except Exception:
        return ""
    for tok in sorted(set(re.findall(r"eyJ[\w-]+\.[\w-]+\.[\w-]+", body)), key=len, reverse=True):
        if _dev_token_works(tok):
            return tok
    return ""


def dev_token() -> tuple[str, str]:
    """Действующий Apple developer token и откуда он взят.

    02.08.2026: враппер на 30020 генерирует dev_token ОДИН раз при старте, живёт
    тот 300 секунд, а отдаёт враппер его сутками. Через пять минут после запуска
    контейнера любой запрос с ним получает 401 — и проверка куки, опиравшаяся на
    30020, снова печатала «сессия протухла, экспортируй заново». Ровно тот
    ложный совет, который 01.08 уже опровергли живьём.

    Поэтому токен с враппера сперва ПРОВЕРЯЕМ на публичном каталоге и, если он
    протух, берём свежий со страницы (тот живёт ~2 месяца). Пустой ответ значит
    «действующего токена нет» — вызывающий обязан промолчать, а не гадать.
    """
    global _dev_token_cache
    if _dev_token_cache is not None:
        return _dev_token_cache
    tok = _wrapper_dev_token()
    if _dev_token_works(tok):
        _dev_token_cache = (tok, "с враппера 30020")
        return _dev_token_cache
    scraped = _scrape_dev_token()
    _dev_token_cache = (scraped, "со страницы music.apple.com") if scraped else ("", "")
    return _dev_token_cache


# ── media-user-token, вставленный прямо в настройки ──────────────────────────

_ME_STOREFRONT = "https://amp-api.music.apple.com/v1/me/storefront"


def _amp_error_detail(e: urllib.error.HTTPError) -> str:
    """Что сказал amp-api в теле ошибки (`Invalid authentication` и т. п.) —
    это единственная улика, по которой 401 отличают от 403. Тело не содержит
    токена, но мы его всё равно не печатаем целиком."""
    try:
        raw = e.read().decode("utf-8", "ignore")[:2000]
        errs = (json.loads(raw).get("errors") or [{}])[0]
        return str(errs.get("title") or errs.get("code") or "").strip()
    except Exception:                                       # noqa: BLE001
        return ""


def probe_media_user_token(mut: str, timeout: float = 12.0) -> dict:
    """Жив ли этот media-user-token и в какой он витрине.

    Нужен ровно там, где нет `cookies.txt`: человек вставляет токен в поле
    настроек, и ответить надо СРАЗУ и по-разному на три случая
    (23.09.2026, триал ES):

      * `ok`            — 200, витрина получена;
      * `token_rejected`— 403: Apple отвергает САМ токен («Invalid
        authentication»). Ни Origin, ни перелогин здесь ни при чём —
        лечится только свежим токеном с этой же сессии;
      * `bad_request`   — 401: у amp-api претензия к связке заголовков
        (developer token / Origin), а не к токену. То есть сломаны МЫ.

    Различие 401/403 измерено, а не догадано: на заведомо живом токене
    отсутствие `Origin` и подменный dev_token дают 401, а мёртвый токен — 403.
    Смешивать их, как это делает `verdict()` для файла куки (там оба кода
    значат «экспортируй заново»), для поля настроек нельзя: человек получил бы
    совет менять токен там, где надо починить запрос, и наоборот.

    `unknown` — спросить не удалось (нет живого dev_token, сеть). Это ответ, а
    не отказ отвечать: состояние токена при этом НЕ известно, и врать про него
    нельзя.
    """
    tok = (mut or "").strip()
    if not tok:
        return {"state": "empty", "storefront": "", "reason": "токен не вставлен"}
    if len(tok) < 40 or "." not in tok:
        # media-user-token — длинная JWT-подобная строка. Короткий или плоский
        # набор символов — это почти всегда опечатка/обрезок, и спрашивать Apple
        # незачем: ответ 403 ничего не объяснит человеку.
        return {"state": "malformed", "storefront": "",
                "reason": "не похоже на media-user-token (длина или формат)"}
    dev, src = dev_token()
    if not dev:
        return {"state": "unknown", "storefront": "",
                "reason": "нет действующего Apple dev_token — токен не проверен"}
    headers = {"Authorization": f"Bearer {dev}", "Media-User-Token": tok,
               "Origin": "https://music.apple.com", "User-Agent": "Ripster"}
    try:
        req = urllib.request.Request(_ME_STOREFRONT, headers=headers)
        with urllib.request.urlopen(req, timeout=timeout) as r:
            body = json.loads(r.read().decode("utf-8", "ignore"))
    except urllib.error.HTTPError as e:
        detail = _amp_error_detail(e)
        if e.code == 403:
            return {"state": "token_rejected", "storefront": "", "dev_src": src,
                    "http": 403,
                    "reason": f"Apple отвергает этот токен (HTTP 403: "
                              f"{detail or 'без описания'}) — "
                              f"он мёртв или взят не из той сессии; запрос корректный, "
                              f"dev_token проверен ({src})"}
        if e.code == 401:
            return {"state": "bad_request", "storefront": "", "dev_src": src,
                    "http": 401,
                    "reason": f"amp-api не принял связку заголовков (HTTP 401: "
                              f"{detail or 'без описания'}) — "
                              f"это наша сторона, токен при этом мог остаться живым"}
        return {"state": "unknown", "storefront": "", "http": e.code,
                "reason": f"amp-api ответил HTTP {e.code}"}
    except Exception as e:                       # noqa: BLE001
        return {"state": "unknown", "storefront": "",
                "reason": f"запрос не прошёл: {type(e).__name__}"}

    data = body.get("data") or []
    first = data[0] if data and isinstance(data[0], dict) else {}
    attrs = first.get("attributes") or {}
    sid = str(first.get("id") or "").split("-")[0]
    # `/v1/me/storefront` называет витрину ЧИСЛОМ (`143444`) и человекочитаемым
    # именем (`United Kingdom`). Число переводит тот же словарь, что и проба
    # контейнера, — иначе одна и та же витрина в двух местах называлась бы
    # по-разному. Имя в код не переименовываем: «un» из «United Kingdom» был бы
    # страной, которой нет.
    from ripster.apple_accounts import STOREFRONT_IDS
    cc = STOREFRONT_IDS.get(sid, "")
    if not cc and len(sid) == 2 and sid.isalpha():
        cc = sid.lower()
    if not cc:
        return {"state": "ok", "storefront": "", "storefront_name": str(attrs.get("name") or sid),
                "dev_src": src,
                "reason": f"токен жив, но витрину ({sid or attrs.get('name') or '?'}) "
                          f"в код не перевести — впишите страну сами"}
    return {"state": "ok", "storefront": cc, "storefront_name": str(attrs.get("name") or ""),
            "dev_src": src, "reason": f"токен жив, витрина {cc}"}


# ── вердикт ──────────────────────────────────────────────────────────────────

def verdict(cookies_path: str | Path) -> dict:
    """Что не так с куками. Ключ `state`:

      * `ok`              — сессия жива, подписка активна;
      * `no_subscription` — сессия жива, подписки нет (экспорт НЕ поможет);
      * `expired`         — сессия отвергнута (экспорт поможет);
      * `no_token`        — в файле нет media-user-token;
      * `no_file`         — файла нет;
      * `unknown`         — спросить не удалось (нет живого dev_token или сеть).

    `unknown` — полноценный ответ, а не заглушка: без ЗАВЕДОМО живого dev_token
    ответ 401 неотличим от «протухли куки», и назвать причину значило бы
    угадать. Молчание честнее ложного совета.
    """
    p = Path(cookies_path)
    if not p.exists():
        return {"state": "no_file", "storefront": "", "reason": f"нет файла {p.name}"}
    mut = read_media_user_token(p)
    if not mut:
        return {"state": "no_token", "storefront": "",
                "reason": f"в {p.name} нет media-user-token"}
    dev, src = dev_token()
    if not dev:
        return {"state": "unknown", "storefront": "",
                "reason": "нет действующего Apple dev_token — состояние куки не проверено"}
    try:
        req = urllib.request.Request(_ACCOUNT, headers={
            "Authorization": f"Bearer {dev}", "Media-User-Token": mut,
            "Origin": "https://music.apple.com", "User-Agent": "Ripster"})
        with urllib.request.urlopen(req, timeout=12) as r:
            body = json.loads(r.read().decode("utf-8", "ignore"))
    except urllib.error.HTTPError as e:
        if e.code in (401, 403):
            return {"state": "expired", "storefront": "", "dev_src": src,
                    "reason": f"сессия отвергнута ({e.code}); dev_token проверен и жив — {src}"}
        return {"state": "unknown", "storefront": "",
                "reason": f"amp-api ответил HTTP {e.code}"}
    except Exception as e:                       # noqa: BLE001
        return {"state": "unknown", "storefront": "",
                "reason": f"запрос не прошёл: {type(e).__name__}"}

    sub = (body.get("meta") or {}).get("subscription") or {}
    sf = str(sub.get("storefront") or "?")
    if sub.get("active"):
        return {"state": "ok", "storefront": sf, "dev_src": src,
                "reason": f"подписка активна (storefront {sf})"}
    return {"state": "no_subscription", "storefront": sf, "dev_src": src,
            "reason": f"сессия жива, но подписки на этом аккаунте нет (storefront {sf})"}
