"""
Матрица доступности релиза по сервисам.

«Релиз вышел» — бесполезное утверждение. Мировая дата релиза это анонс, а витрины
наполняются каждая сама, в своей зоне и в своём темпе, да ещё аккаунты владельца
живут в разных странах (Tidal новозеландский, Apple американский, Deezer
европейский). Значение имеет только одно: **в каком сервисе я могу скачать это
прямо сейчас**.

Поэтому храним не «есть/нет», а состояние ПО КАЖДОМУ сервису вместе с причиной —
она различает три совершенно разные ситуации:

  not_in_catalog_yet  витрина ещё не наполнилась     → перепроверить позже
  region_locked       в этом сторефронте не будет    → перепроверять бессмысленно
  no_token            наша недоработка, не витрины   → чинится настройками

Сверяем точными идентификаторами — по названию нельзя: одноимённые синглы,
делюксы и ремастеры дают ложные совпадения. Штрихкод (UPC) для Apple и Deezer,
ISRC — для Qobuz и Tidal, у которых поиска по штрихкоду нет. Один и тот же
релиз при этом живёт в витринах под РАЗНЫМИ штрихкодами (24.09.2026,
Evanescence «Sweet Sacrifice (Remastered 2026)»: Spotify 00888072836037,
Deezer 888072836020) — поэтому промах по одному ключу не вердикт, а ступенька
лестницы: UPC → ISRC → нормализованные варианты штрихкода → и лишь последней
консервативная сверка изданием (артист точный, название точное с учётом шума
переизданий, то же число треков, длительность ±3 с).

Кэш живёт на диске, иначе он не переживает перезапуск и каждое утро всё
опрашивается заново.
"""
from __future__ import annotations

import json
import re
import time
from pathlib import Path
from typing import Optional

# Сервисы, у которых есть точный поиск по идентификатору. Spotify держим
# отдельно: он каталог, а не источник файлов.
SERVICES = ("apple", "deezer", "qobuz", "tidal", "beatport")

REASON_NOT_YET  = "not_in_catalog_yet"
REASON_REGION   = "region_locked"
REASON_NO_TOKEN = "no_token"
# Релиз В КАТАЛОГЕ есть, а скачать эта учётка его не может: прав на поток нет.
# Отдельно от region_locked намеренно — это разные ответы человеку и разные
# действия программы. Регион лечится другой страной аккаунта; отсутствие прав —
# ДРУГОЙ УЧЁТКОЙ ТОГО ЖЕ сервиса (см. фоллбэк по аккаунтам) или покупкой, а
# прокси не добавляет прав вообще. 14.08.2026 Beatport отдавал 403-на-потоке на
# релизах, у которых is_available_for_streaming=True — по витрине не отличить.
REASON_NO_RIGHTS = "no_entitlement"
# Спросить было НЕЧЕМ. У Qobuz и Tidal нет поиска по штрихкоду — им нужен ISRC, и
# без него ответ «ещё не появился» был бы враньём: мы туда вообще не ходили.
# Отдельная причина, потому что лечится она не ожиданием, а добычей ISRC.
REASON_NO_ID    = "no_identifier"

# Найденное больше не перепроверяем: релиз из витрины не исчезает.
_TTL_MISS   = 30 * 60          # ещё не подъехало — заглядываем каждые полчаса
_TTL_REGION = 7 * 24 * 3600    # региональный отказ — раз в неделю, не чаще
_TTL_TOKEN  = 10 * 60          # наша проблема: почини токен — увидим сразу
# Прав нет — но подписку могли продлить, а релиз мог выйти из эксклюзива. Сутки:
# чаще смысла нет (ответ детерминированный), реже — застрянем на «нельзя», когда
# уже можно.
_TTL_RIGHTS = 24 * 3600

_cfg: dict = {}
_base_dir: Path = Path(".")
_cache: dict = {}
_loaded = False


def configure(cfg: dict, base_dir: Path) -> None:
    global _cfg, _base_dir
    _cfg, _base_dir = cfg, Path(base_dir)


def _cache_path() -> Path:
    return _base_dir / "availability_cache.json"


def _load() -> None:
    global _cache, _loaded
    if _loaded:
        return
    try:
        _cache = json.loads(_cache_path().read_text(encoding="utf-8")) or {}
    except Exception:
        _cache = {}
    _loaded = True


def _save() -> None:
    try:
        _cache_path().write_text(json.dumps(_cache, ensure_ascii=False), encoding="utf-8")
    except Exception:
        pass


def norm_barcode(upc: str) -> str:
    """Штрихкод к ОДНОЙ форме: 14 знаков с ведущими нулями (GTIN-14).

    Один и тот же релиз приходит с разной разрядностью в зависимости от того,
    кто его назвал: Apple отдаёт `00600574110367`, Deezer — `0600574110367`,
    страница Odesli — `886443927087` вовсе без ведущего нуля. Пока ключ резал
    только нецифры, это были ТРИ РАЗНЫХ КЛЮЧА для одной пластинки: карточка
    спрашивала под одним, рантайм записывал вердикт под другим, и встретиться
    они не могли никогда — тот же силуэт, что у ошибки `name:` против `upc:`.

    UPC-12, EAN-13 и GTIN-14 отличаются только ведущими нулями, поэтому
    приведение к 14 знакам ничего не теряет и делает форму единственной.
    """
    d = "".join(ch for ch in (upc or "") if ch.isdigit()).lstrip("0")
    return d.rjust(14, "0") if d else ""


def _key(upc: str, isrc: str, title: str, artist: str) -> str:
    if upc:
        return "upc:" + norm_barcode(upc)
    if isrc:
        return "isrc:" + isrc.upper()
    return "name:" + (title or "").lower().strip() + "|" + (artist or "").lower().strip()


def _legacy_keys(upc: str) -> list:
    """Как этот же релиз мог лежать в кэше ДО нормализации.

    Выбросить накопленное было бы дороже, чем прочитать его: там лежат вердикты
    настоящих загрузок, которые заново не получить.
    """
    d = "".join(ch for ch in (upc or "") if ch.isdigit())
    if not d:
        return []
    bare = d.lstrip("0")
    return ["upc:" + v for v in {d, bare, bare.rjust(12, "0"), bare.rjust(13, "0")} if v]


def _fresh(rec: dict) -> bool:
    """Нужно ли доверять записи или пора перепроверять."""
    if rec.get("available"):
        return True                     # найденное не пропадает
    age = time.time() - float(rec.get("checked_ts") or 0)
    reason = rec.get("reason") or REASON_NOT_YET
    ttl = {REASON_REGION: _TTL_REGION, REASON_NO_TOKEN: _TTL_TOKEN,
           REASON_NO_RIGHTS: _TTL_RIGHTS}.get(reason, _TTL_MISS)
    return age < ttl


def _has_credentials(service: str) -> bool:
    """Есть ли чем спрашивать этот сервис. Без этого «не найдено» — враньё."""
    c = _cfg or {}
    if service == "apple":
        tok = str(c.get("authorization-token") or "").strip()
        return bool(tok) and tok != "your-authorization-token"
    if service == "deezer":
        return True                     # публичный поиск по штрихкоду, токен не нужен
    if service == "qobuz":
        return bool(str(c.get("qobuz-auth-token") or "").strip())
    if service == "tidal":
        return bool(str(c.get("tidal-token") or "").strip()) or True  # есть путь через OrpheusDL
    if service == "beatport":
        # Каталог Beatport закрыт целиком: без логина не отвечает даже поиск, так
        # что «не нашли» без учётки было бы неправдой.
        return bool(str(c.get("beatport-username") or "").strip()
                    and str(c.get("beatport-password") or "").strip())
    return False


def _apple_storefronts() -> list[str]:
    """Магазины Apple, из которых мы РЕАЛЬНО можем качать, в порядке проверки.

    `storefront` в конфиге — это то, что человек вписал руками; `apple-country`
    приходит от самой Apple при проверке учётки и потому вернее. Разойтись они
    могут запросто, поэтому спрашиваем оба.

    Учётки пула (`wrapper-accounts`) своей страны пока не хранят — как только
    начнут, их коды добавятся сюда же, и проверка станет по-настоящему
    поаккаунтной. Список короткий и без повторов: каждый лишний магазин это
    лишний сетевой запрос на КАЖДУЮ проверку доступности.
    """
    c = _cfg or {}
    out: list[str] = []
    for v in (c.get("apple-country"), c.get("storefront"),
              *(a.get("storefront") or a.get("country")
                for a in (c.get("wrapper-accounts") or []) if isinstance(a, dict))):
        s = str(v or "").strip().lower()
        if s and s not in out:
            out.append(s)
    return out or ["us"]


def _upc_variants(upc: str) -> list[str]:
    """Все честные формы одного штрихкода: как пришёл, без ведущих нулей и в
    разрядностях 12/13/14.

    Витрины заносят один релиз под разной разрядностью: Apple отдаёт
    `00600574110367`, Deezer — `0600574110367`, Odesli — `886443927087`.
    Промах по одной форме — это «не тем числом спросили», а не «релиза нет»,
    поэтому последней точной ступенью перебираем остальные."""
    d = "".join(ch for ch in (upc or "") if ch.isdigit())
    if not d:
        return []
    bare = d.lstrip("0")
    out: list[str] = []
    for v in (d, bare, bare.rjust(12, "0"), bare.rjust(13, "0"), bare.rjust(14, "0")):
        if v and v not in out:
            out.append(v)
    return out


# Шум переизданий: тем, чем лейбл обвешивает ОДНО и то же название, — снимаем
# только скобки с этими словами. «(In the Style of Evanescence)» шумом не
# считается и остаётся частью названия, отчего караоке-подделка под
# «Sweet Sacrifice (Remastered 2026)» сверку не проходит.
_FZ_EDITION = re.compile(
    r"[()\[\]]*\b(?:digitally\s+)?(?:remaster(?:ed)?|deluxe|bonus(?: track)?s?|"
    r"expanded|anniversary|reissue|single\s+version|stereo|mono)\b[^()\[\]]*[()\[\]]*",
    re.I)
# Караоке/трибьюн-маркеры: релиз с таким словом — чужая запись, каким бы ни
# было остальное название.
_FZ_BAD = re.compile(
    r"in the style of|karaoke|tribute to|made famous by|originally performed|"
    r"originally by|cover (?:of|version)|sing[- ]?along", re.I)

# Допуск длительности между витринами, секунды: сервисы округляют длины
# треков по-разному; 3 с покрывают наблюдаемый разброс и не пускают чужую запись.
_FZ_TOL_S = 3


def _fz_norm(s: str) -> str:
    s = _FZ_EDITION.sub(" ", s or "")
    s = re.sub(r"[^a-z0-9\u0430-\u044f\u0451]+", " ", s.lower())
    return " ".join(s.split())


def _fuzzy_ok(cand: dict, title: str, artist: str, expect: dict) -> bool:
    """Пройдёт ли кандидат консервативную сверку.

    Требует ТОЧНОГО совпадения артиста после нормализации и названия — с
    точностью до шума переизданий. Что известно с обеих сторон (число треков,
    длительность ±3 с) — обязано совпасть; чего не знают — не проверяется,
    но и само совпадением не становится."""
    if not title or not artist:
        return False
    if _FZ_BAD.search(str(cand.get("title", ""))) or _FZ_BAD.search(str(cand.get("artist", ""))):
        return False
    if _fz_norm(cand.get("artist", "")) != _fz_norm(artist):
        return False
    if _fz_norm(cand.get("title", "")) != _fz_norm(title):
        return False
    try:
        want_n, have_n = int(expect.get("tracks") or 0), int(cand.get("tracks") or 0)
    except (TypeError, ValueError):
        want_n = have_n = 0
    if want_n and have_n and want_n != have_n:
        return False
    try:
        want_d = int(expect.get("duration_s") or 0)
        have_d = int(cand.get("duration") or cand.get("duration_s") or 0)
    except (TypeError, ValueError):
        want_d = have_d = 0
    if want_d and have_d and abs(want_d - have_d) > _FZ_TOL_S:
        return False
    return True


async def _fuzzy_find(service: str, title: str, artist: str, expect: dict) -> Optional[dict]:
    """Последняя ступень: поиск по изданию с проверкой консервативной сверкой."""
    if not title or not artist:
        return None
    from ripster.routes import discovery as _disc
    term = f"{artist} {title}".strip()
    try:
        if service == "apple":
            r = await _disc._search_apple(term, "album", 5, "")
        elif service == "deezer":
            r = await _disc._search_deezer(term, "album", 5)
        elif service == "qobuz":
            r = await _disc._search_qobuz(term, "album", 5)
        elif service == "tidal":
            r = await _disc._search_tidal(term, "album", 5)
        elif service == "beatport":
            r = await _disc._search_beatport(term, "album", 5)
        else:
            return None
    except Exception:
        return None
    for cand in (r.get("results") or []):
        if _fuzzy_ok(cand, title, artist, expect or {}):
            return {"id": str(cand.get("id") or ""), "title": cand.get("title", ""),
                    "artist": cand.get("artist", ""), "url": cand.get("url", ""),
                    "cover": cand.get("cover", ""), "service": service,
                    "type": "album", "matched_by": "fuzzy"}
    return None


async def _qobuz_by_barcode(upcs: list[str]) -> Optional[dict]:
    """Qobuz по штрихкоду: его `/album/search` принимает UPC текстовым
    запросом (тот же приём у движка, engines/qobuz.py
    `_resolve_upc_to_album_id`), но верим мы только кандидату, у которого
    поле `upc` СОВПАЛО с нашей цифрой — поисковая строка сама по себе ничего
    не гарантирует."""
    if not upcs:
        return None
    from ripster import http_client as _HTTP
    c = _cfg or {}
    app_id = str(c.get("qobuz-app-id") or "").strip() or "312369995"
    token = str(c.get("qobuz-auth-token") or "").strip()
    want = {u.lstrip("0") for u in upcs if u}
    try:
        async with _HTTP.ashared() as cl:
            for code in upcs:
                r = await cl.get("https://www.qobuz.com/api.json/0.2/album/search",
                                 params={"query": code, "limit": 5, "app_id": app_id},
                                 headers={"X-User-Auth-Token": token} if token else {})
                if r.status_code != 200:
                    continue
                for it in ((r.json().get("albums") or {}).get("items") or []):
                    got = str(it.get("upc") or it.get("ean") or "").strip().lstrip("0")
                    if not got or got not in want:
                        continue
                    alb_id = str(it.get("id") or "")
                    img = it.get("image") if isinstance(it.get("image"), dict) else {}
                    return {"id": alb_id, "title": it.get("title", ""),
                            "artist": (it.get("artist") or {}).get("name", ""),
                            "url": it.get("url") or f"https://www.qobuz.com/album/{alb_id}",
                            "cover": img.get("large") or img.get("small") or "",
                            "service": "qobuz", "type": "album", "matched_by": "upc"}
    except Exception:
        return None
    return None


async def _tidal_by_barcode(upcs: list[str]) -> Optional[dict]:
    """Tidal по штрихкоду: текстовый поиск `/search/albums` принимает UPC, а
    объект альбома несёт поле `upc` — принимаем только точное совпадение."""
    if not upcs:
        return None
    from ripster import http_client as _HTTP
    tok, cc = "", ""
    try:
        from ripster.engines import tidal as _tid
        tok, cc = await _tid._orpheus_access_token()
    except Exception:
        tok = ""
    if not tok:
        tok = str((_cfg or {}).get("tidal-token") or "").strip()
        cc = str((_cfg or {}).get("tidal-country") or "US").strip().upper()
    if not tok:
        return None
    want = {u.lstrip("0") for u in upcs if u}
    try:
        async with _HTTP.ashared() as cl:
            for code in upcs:
                r = await cl.get("https://api.tidal.com/v1/search/albums",
                                 params={"query": code, "limit": 5,
                                         "countryCode": cc or "US"},
                                 headers={"Authorization": f"Bearer {tok}"})
                if r.status_code != 200:
                    continue
                for it in (r.json().get("items") or []):
                    got = str(it.get("upc") or it.get("ean") or "").strip().lstrip("0")
                    if not got or got not in want:
                        continue
                    alb_id = str(it.get("id") or "")
                    cuuid = str(it.get("cover") or "").replace("-", "/")
                    return {"id": alb_id, "title": it.get("title", ""),
                            "artist": (it.get("artist") or {}).get("name", ""),
                            "url": f"https://listen.tidal.com/album/{alb_id}",
                            "cover": (f"https://resources.tidal.com/images/{cuuid}/320x320.jpg"
                                      if cuuid else ""),
                            "service": "tidal", "type": "album", "matched_by": "upc"}
    except Exception:
        return None
    return None


async def _probe_one(service: str, upc: str, isrc: str,
                     title: str = "", artist: str = "",
                     expect: Optional[dict] = None) -> dict:
    """Спросить один сервис — всей лестницей, от точного ключа к осторожному:

      1. точный штрихкод                     matched_by: "upc"
         (Apple/Deezer/Beatport — прямые endpoint'ы; Qobuz/Tidal — поиск
          текстом с проверкой поля `upc` у кандидата);
      2. ISRC — трек и альбом, в котором он  matched_by: "isrc";
      3. нормализованные варианты штрихкода  matched_by: "upc_variant"
         (12↔13↔14, ведущие нули);
      4. консервативная нечёткая сверка      matched_by: "fuzzy".

    `no_identifier` — только когда идентификатора НЕ БЫЛО ВОВСЕ: с штрихкодом
    или ISRC мы спрашиваем всем, что витрина принимает, и пустой ответ есть
    «ещё не появился». Раньше Qobuz/Tidal без ISRC отписывались «нечем
    спросить», даже когда UPC в руках был, — и Spotify-релиз, чей ISRC никто
    не добыл, оказывался «нигде не доступен» при живом Deezer (24.09.2026,
    «Sweet Sacrifice (Remastered 2026)»)."""
    now = time.time()
    if not _has_credentials(service):
        return {"available": False, "reason": REASON_NO_TOKEN, "checked_ts": now}
    try:
        from ripster.routes import discovery as _disc
        expect = expect or {}
        hit = None
        tried = False                        # ходили ли к витрине с чем-нибудь
        upcs = _upc_variants(upc)
        isrcs = [x.strip().upper() for x in re.split(r"[,\s]+", (isrc or "").strip())
                 if x.strip()]

        # ── 1. точный штрихкод ───────────────────────────────────────────────
        if upcs:
            if service == "apple":
                # Права выдаются на УЧЁТКУ, а не на сервис: релиза может не быть в
                # магазине из конфига и быть в магазине второй учётки. Раньше
                # спрашивали ровно один storefront — и «нет в Apple» означало на
                # деле «нет в том магазине, который вписан руками» (08.08.2026:
                # в конфиге стояло US, а учётка оказалась CA).
                tried = True
                for sf in _apple_storefronts():
                    hit = await _disc._find_by_upc(upcs[0], service, sf)
                    if hit:
                        hit = dict(hit, storefront=sf)
                        break
            elif service == "deezer":
                tried = True
                hit = await _disc._find_by_upc(
                    upcs[0], service, str((_cfg or {}).get("storefront", "us")))
            elif service == "beatport":
                # Beatport умеет ОБА точных ключа (проверено 14.08.2026 на живом
                # API: /catalog/releases/?upc= и /catalog/tracks/?isrc= оба отдают
                # count=1), поэтому штрихкод первым, ISRC — запасной ход ниже.
                tried = True
                hit = await _disc._find_by_upc(upcs[0], service)
            elif service == "qobuz":
                tried = True
                hit = await _qobuz_by_barcode(upcs)
            elif service == "tidal":
                tried = True
                hit = await _tidal_by_barcode(upcs)
        # ── 2. ISRC: трек и альбом, в котором он лежит ───────────────────────
        # ISRC как ЗАПАСНОЙ ключ — для всех, кто его умеет, а не только для тех,
        # у кого нет штрихкода. Издание Deezer или Apple может нести ДРУГОЙ
        # штрихкод, и тогда поиск по UPC промахивается при живом релизе.
        # Список (несколько первых треков) — одного мало: его ISRC может не
        # быть в каталоге при том, что релиз там есть.
        if hit is None and isrcs and service in ("qobuz", "tidal", "beatport", "deezer", "apple"):
            tried = True
            for one in isrcs:
                hit = await _disc._find_by_isrc(one, service)
                if hit:
                    break
        # ── 3. тот же штрихкод в другой разрядности ──────────────────────────
        if hit is None and service in ("apple", "deezer", "beatport") and len(upcs) > 1:
            for code in upcs[1:]:
                tried = True
                if service == "apple":
                    for sf in _apple_storefronts():
                        h = await _disc._find_by_upc(code, service, sf)
                        if h:
                            hit = dict(h, storefront=sf, matched_by="upc_variant")
                            break
                else:
                    h = await _disc._find_by_upc(code, service)
                    if h:
                        hit = dict(h, matched_by="upc_variant")
                if hit:
                    break
        # ── 4. консервативная нечёткая сверка изданием ────────────────────────
        if hit is None and title and artist:
            tried = True
            hit = await _fuzzy_find(service, title, artist, expect)
        if hit:
            return {"available": True, "url": hit.get("url", ""),
                    "title": hit.get("title", ""), "artist": hit.get("artist", ""),
                    "cover": hit.get("cover", ""), "matched_by": hit.get("matched_by", ""),
                    # В КАКОМ магазине нашлось. Когда учёток несколько, «доступно
                    # в Apple» без этого не отвечает на главный вопрос — какой
                    # именно учёткой качать.
                    "storefront": hit.get("storefront", ""),
                    "checked_ts": now}
        # «Ещё не появился» — это ВЫВОД ИЗ ПРОВЕРКИ, а не значение по
        # умолчанию: если к витрине не ходили НИ С ЧЕМ (ни штрихкода, ни ISRC,
        # ни пары имя/артист), говорим правду — спросить было нечем.
        if tried:
            return {"available": False, "reason": REASON_NOT_YET, "checked_ts": now}
        return {"available": False, "reason": REASON_NO_ID, "checked_ts": now}
    except Exception as e:                                     # noqa: BLE001
        return {"available": False, "reason": REASON_NOT_YET,
                "error": str(e)[:120], "checked_ts": now}


async def _derive_isrc(service: str, hit: dict) -> str:
    """Достать ISRC первого трека из найденного релиза.

    Он и есть ключ к Qobuz и Tidal: штрихкода у них нет, а ISRC опознаёт запись
    однозначно — без него пришлось бы сверять по названию, а это ложные
    совпадения на делюксах, ремастерах и одноимённых синглах.
    """
    try:
        from ripster.routes import discovery as _disc
        url = str(hit.get("url") or "")
        aid = ""
        if service == "deezer":
            import re
            m = re.search(r"/album/(\d+)", url)
            aid = m.group(1) if m else ""
        if service == "beatport":
            # Beatport отдаёт ISRC прямо в треках релиза — это САМЫЙ дешёвый
            # источник идентификатора для Qobuz и Tidal, у которых своего ключа
            # нет. Без него релиз, найденный только в Beatport, оставлял их в
            # состоянии «не спрашивал, нет ISRC» навсегда.
            import re
            m = re.search(r"/release/[^/]*/(\d+)", url)
            rid = m.group(1) if m else str(hit.get("id") or "")
            if not rid:
                return ""
            from ripster.routes import beatport as _bp
            from ripster import http_client as _HTTP
            tok = await _bp._get_token()
            if not tok:
                return ""
            async with _HTTP.ashared() as c:
                r = await c.get(f"{_bp._BASE}/catalog/releases/{rid}/tracks/",
                                headers=_bp._auth_headers(tok))
            if r.status_code != 200:
                return ""
            vals = [str(t.get("isrc") or "").strip()
                    for t in ((r.json() or {}).get("results") or [])[:4]]
            return ",".join(v for v in vals if v)
        if service == "apple":
            # ISRC ИЗ APPLE. Раньше этой ветки не было, и когда Apple
            # оказывался ЕДИНСТВЕННЫМ сеятелем, нашедшим релиз, добывать ISRC
            # было не из чего: Qobuz и Tidal навсегда оставались в состоянии
            # «не спрашивал, нет идентификатора». Замер 06.09.2026, Daft Punk
            # «Random Access Memories» по ссылке Qobuz: Apple нашёл по
            # штрихбарку в магазине `ca`, а два лучших по качеству сервиса
            # так и не были опрошены.
            #
            # `_seed_isrcs` сюда не годится: он умеет только Spotify и Deezer.
            # У Apple ISRC лежит прямо в атрибутах трека альбома, и магазин
            # брать надо ТОТ, в котором релиз реально нашёлся (его кладёт
            # `_probe_one`), а не из конфига — права у учёток разные, и в
            # чужом магазине альбома может не быть вовсе.
            bearer = str((_cfg or {}).get("authorization-token") or "").strip()
            if not bearer or bearer == "your-authorization-token":
                return ""
            alb = str(hit.get("id") or "")
            if not alb:
                import re
                m = re.search(r"/album/[^/]*/(\d+)", str(hit.get("url") or ""))
                alb = m.group(1) if m else ""
            if not alb:
                return ""
            sf = str(hit.get("storefront") or (_cfg or {}).get("storefront") or "us").lower()
            from ripster import http_client as _HTTP
            async with _HTTP.ashared() as c:
                r = await c.get(
                    f"https://api.music.apple.com/v1/catalog/{sf}/albums/{alb}/tracks",
                    params={"limit": 4},
                    headers={"Authorization": f"Bearer {bearer}",
                             "Origin": "https://music.apple.com"})
            if r.status_code != 200:
                return ""
            vals = [str(((t.get("attributes") or {}).get("isrc") or "")).strip()
                    for t in ((r.json() or {}).get("data") or [])[:4]]
            return ",".join(v for v in vals if v)
        if not aid:
            return ""
        vals = await _disc._seed_isrcs({"id": aid, "service": service}) or []
        return ",".join(vals)
    except Exception:
        return ""


async def matrix(upc: str = "", isrc: str = "", title: str = "", artist: str = "",
                 services: Optional[tuple] = None, force: bool = False,
                 seed: Optional[dict] = None) -> dict:
    """Где этот релиз можно взять прямо сейчас.

    `seed` — источник, из которого релиз пришёл ({"id": ..., "service":
    "spotify"|"deezer"}). По нему ДО опроса витрин добываются ISRC треков и
    параметры издания (число треков, полная длительность): одно и то же
    издание живёт в витринах под РАЗНЫМИ штрихкодами — промах по UPC это не
    «релиза нет», а «не тем ключом спросили» (24.09.2026, Evanescence
    «Sweet Sacrifice (Remastered 2026)»: Spotify 00888072836037, Deezer
    888072836020, и радар из-за этого врал «нет ни на одном сервисе» при
    живом Deezer).

    Кэш: найденное не перепроверяется никогда, ненайденное — по своему TTL,
    региональный отказ — раз в неделю (в этом сторефронте оно не появится).
    Записи, рождённые БЕЗ ISRC («ещё не появился»/«нечем спросить» при
    пустом isrc), переспрашиваются при следующем чтении, не дожидаясь TTL:
    под этими вердиктами и прячется ошибка другого штрихкода. Один раз —
    если и с новым ключом ничего не добыли, дальше обычный график.
    """
    _load()
    svcs = tuple(services or SERVICES)
    k = _key(upc, isrc, title, artist)
    rec = _cache.get(k)
    if rec is None and upc:
        # Запись могла быть создана до нормализации штрихкода — переносим её на
        # новый ключ, а не начинаем с нуля.
        for lk in _legacy_keys(upc):
            if lk != k and lk in _cache:
                rec = _cache.pop(lk)
                print(f"[availability] запись перенесена {lk} → {k}", flush=True)
                break
    rec = rec or {"services": {}}
    out = dict(rec.get("services") or {})

    # По штрихкоду умеют Apple, Deezer и Beatport, поэтому идём ими первыми: если
    # релиз там нашёлся, из него добываем ISRC первых треков — и тогда Qobuz с
    # Tidal можно спросить по-настоящему, а не отписаться «нечем спросить».
    # Beatport добавлен в эту голову (а не в хвост) именно ради ISRC: у клубного
    # релиза он часто ЕДИНСТВЕННЫЙ, кто уже знает про пластинку.
    _SEEDERS = ("deezer", "apple", "beatport")
    ordered = [s for s in _SEEDERS if s in svcs] + \
              [s for s in svcs if s not in _SEEDERS]

    # ISRC, добытый КОГДА-ТО, годится всегда: он свойство записи, а не сервиса.
    # Раньше он поднимался из кэша только при живом «доступен» у сервиса-донора —
    # и стоило донору стать недоступным (403 по правам, ушёл из витрины), как
    # Qobuz с Tidal снова получали «не спрашивал, нет ISRC», хотя нужный ISRC
    # лежал в этой же записи. Программа знала и выбрасывала.
    cached_isrc = str(rec.get("isrc") or "")
    isrc = isrc or cached_isrc
    name = title or str(rec.get("title") or "")
    by_artist = artist or str(rec.get("artist") or "")

    # ── самолечение уже испорченного ─────────────────────────────────────────
    # Вердикты, поставленные БЕЗ ISRC, не заслуживают доверия: ровно так
    # выглядела запись, где релиз есть, а штрихкод у витрины другой. Их
    # переспрашиваем при следующем чтении, не дожидаясь TTL; повторный обход
    # без результата не повторяем в рамках TTL, иначе молотим витрину даром.
    def _unhealed(v: dict) -> bool:
        return (not v.get("available")
                and v.get("reason") in (REASON_NOT_YET, REASON_NO_ID)
                and v.get("verified_by") != "download")

    heal = (not cached_isrc) and not force and any(
        _unhealed(v or {}) for v in out.values())
    if heal and time.time() - float(rec.get("healed_ts") or 0) < _TTL_MISS:
        heal = False

    # ── ISRC ДО ВСЕХ ОПРОСОВ ─────────────────────────────────────────────────
    expect: dict = {}
    seed_id = str((seed or {}).get("id") or "").strip()
    # Ид — токен витрины, а не ссылка: карточки кладут в id URL про запас.
    # С таким в /v1/albums/<...> не ходят.
    if seed_id and not re.fullmatch(r"[A-Za-z0-9]{6,}", seed_id):
        seed_id = ""
    seed_svc = str((seed or {}).get("service") or "").lower()
    if seed_id and seed_svc in ("spotify", "deezer"):
        try:
            from ripster.routes import discovery as _disc
            known = [x.strip().upper() for x in (isrc or "").split(",") if x.strip()]
            prof = await _disc._seed_profile(
                {"id": seed_id, "service": seed_svc},
                isrcs=known or None) or {}
            new_isrcs = [str(v).strip().upper() for v in (prof.get("isrcs") or [])
                         if str(v).strip()]
            merged = [x for chunk in (isrc, ",".join(new_isrcs))
                      for x in chunk.split(",") if x.strip()]
            isrc = ",".join(dict.fromkeys(x.strip().upper() for x in merged))
            upc = upc or str(prof.get("upc") or "")
            expect = {"tracks": int(prof.get("tracks") or 0),
                      "duration_s": int(prof.get("duration_s") or 0)}
        except Exception:
            pass

    probed_with_isrc: set = set()
    for svc in ordered:
        cur = out.get(svc)
        if cur and not force and _fresh(cur) and not (heal and _unhealed(cur)):
            continue
        out[svc] = await _probe_one(svc, upc, isrc, name, by_artist, expect)
        if isrc:
            probed_with_isrc.add(svc)
        if (svc in _SEEDERS and out[svc].get("available") and not isrc):
            isrc = await _derive_isrc(svc, out[svc])

    # ВТОРОЙ ПРОХОД. Сеятели ходят первыми и на своём ходу ISRC ещё не знают —
    # значит те из них, кто промахнулся по штрихкоду, спрашивались НЕПОЛНО.
    # У Deezer и Apple есть точный поиск по ISRC, и издание с другим штрихкодом
    # ловится именно им: замер 06.09.2026 по «Random Access Memories» — Deezer
    # по UPC не нашёл, хотя релиз у него есть.
    if isrc:
        for svc in ordered:
            if svc in probed_with_isrc:
                continue            # его уже спрашивали с ISRC на руках
            v = out.get(svc) or {}
            if v.get("available"):
                continue
            if v.get("reason") not in (REASON_NOT_YET, REASON_NO_ID):
                continue                      # регион, права, токен — ISRC не лечит
            if v.get("verified_by") == "download":
                continue                      # вердикт загрузки важнее опроса
            again = await _probe_one(svc, upc, isrc, name, by_artist, expect)
            if again.get("available"):
                out[svc] = again

    # Запись ДОПОЛНЯЕТСЯ, а не переписывается. Один и тот же релиз спрашивают из
    # разных мест с разной полнотой данных: карточка — со штрихкодом и названием,
    # рантайм после загрузки — только со штрихкодом. Пока здесь стояла замена,
    # бедный вызов затирал ISRC, добытый богатым, и следующая проверка снова
    # отвечала «не спрашивал, нет ISRC» — про то, что уже знала.
    new_rec = {**rec, "services": out,
               "upc": upc or rec.get("upc", ""),
               "isrc": isrc or cached_isrc,
               "title": title or rec.get("title", ""),
               "artist": artist or rec.get("artist", ""),
               "ts": time.time()}
    if heal:
        new_rec["healed_ts"] = time.time()
    _cache[k] = new_rec
    _save()
    return {"key": k, "upc": upc, "isrc": isrc, "services": out,
            "available_in": [s for s, v in out.items() if v.get("available")]}


# Токены причин из `runner._classify_partial_reason` → состояния матрицы. Здесь
# только те, что ГОВОРЯТ О ДОСТУПНОСТИ. Сетевые и врапперные отказы
# (_RE_WRAPPER_DEAD, _RE_PATIENT, _RE_DECRYPT_DOWN, «postprocess») сюда не
# попадают намеренно: авария нашей стороны — не факт о витрине, и записать её как
# «нельзя скачать» значит на сутки соврать самим себе.
_OUTCOME_TO_REASON = {
    "entitlement": REASON_NO_RIGHTS,
    "region":      REASON_REGION,
}


def record_outcome(service: str, outcome: str, *, upc: str = "", isrc: str = "",
                   title: str = "", artist: str = "", account: str = "") -> bool:
    """Записать в матрицу РЕАЛЬНЫЙ итог загрузки, а не итог опроса витрины.

    Опрос отвечает «есть ли в каталоге», и для Beatport этого мало: релиз с
    is_available_for_streaming=True всё равно отдаёт 403 на потоке, если у учётки
    нет прав. Единственный источник правды здесь — попытка скачать, поэтому
    рантайм после каждой отдаёт вердикт сюда.

    `outcome` — токен из `runner._classify_partial_reason` либо "ok".
    Возвращает True, если запись изменилась (то есть вердикт был про доступность).
    """
    if not service:
        return False
    _load()
    k = _key(upc, isrc, title, artist)
    rec = _cache.get(k) or {"services": {}}
    svcs = dict(rec.get("services") or {})
    now = time.time()
    if outcome == "ok":
        cur = dict(svcs.get(service) or {})
        cur.update({"available": True, "checked_ts": now, "verified_by": "download"})
        if account:
            cur["account"] = account
        svcs[service] = cur
    else:
        reason = _OUTCOME_TO_REASON.get(outcome)
        if not reason:
            return False
        svcs[service] = {"available": False, "reason": reason, "checked_ts": now,
                         "verified_by": "download",
                         **({"account": account} if account else {})}
    _cache[k] = {**rec, "services": svcs, "upc": upc or rec.get("upc", ""),
                 "isrc": isrc or rec.get("isrc", ""),
                 "title": title or rec.get("title", ""),
                 "artist": artist or rec.get("artist", ""), "ts": now}
    _save()
    return True


def pick_source(matrix_services: dict, preference: Optional[list] = None) -> str:
    """Откуда качать: пересечение предпочтений владельца с тем, что реально есть.

    Порядок по умолчанию — от лучшего качества к худшему. Ждать «свой» сервис,
    когда релиз уже доступен в другом, значит потерять сутки.
    """
    # Beatport последним: он покупочный магазин, и именно у него чаще всего
    # каталог есть, а прав на скачивание нет — брать его раньше значит менять
    # рабочий источник на тот, что вероятнее упрётся в 403.
    pref = preference or list((_cfg or {}).get("availability-preference")
                              or ["apple", "qobuz", "deezer", "tidal", "beatport"])
    for svc in pref:
        if (matrix_services.get(svc) or {}).get("available"):
            return svc
    # Предпочтения — это ПОРЯДОК, а не белый список. В config.yaml лежит
    # `availability-preference: [tidal, qobuz, apple]` — трёх сервисов из пяти там
    # нет вовсе, и релиз, доступный ТОЛЬКО в deezer (или теперь в beatport),
    # получал пустой ответ: «доступно, но качать неоткуда». Ровно та жалоба, из-за
    # которой затевался фоллбэк. Дальше идём по всем оставшимся доступным в
    # порядке SERVICES — молча и без ошибки.
    for svc in SERVICES:
        if svc not in pref and (matrix_services.get(svc) or {}).get("available"):
            return svc
    return ""


def summary_ru(matrix_services: dict) -> str:
    """Строка для владельца: не «релиз доступен», а где именно."""
    ready = [s for s, v in matrix_services.items() if v.get("available")]
    waiting = [s for s, v in matrix_services.items()
               if not v.get("available") and v.get("reason") == REASON_NOT_YET]
    blocked = [s for s, v in matrix_services.items()
               if v.get("reason") == REASON_REGION]
    no_rights = [s for s, v in matrix_services.items()
                 if v.get("reason") == REASON_NO_RIGHTS]
    no_tok = [s for s, v in matrix_services.items()
              if v.get("reason") == REASON_NO_TOKEN]
    no_id = [s for s, v in matrix_services.items()
             if v.get("reason") == REASON_NO_ID]
    parts = []
    if ready:
        parts.append("✅ " + ", ".join(ready) + " — можно скачать")
    if waiting:
        parts.append("⏳ " + ", ".join(waiting) + " — ещё не появился")
    if blocked:
        parts.append("🚫 " + ", ".join(blocked) + " — нет в регионе аккаунта")
    if no_rights:
        parts.append("🔒 " + ", ".join(no_rights) + " — есть в каталоге, но у аккаунта нет прав")
    if no_tok:
        parts.append("🔑 " + ", ".join(no_tok) + " — нет токена")
    if no_id:
        parts.append("❔ " + ", ".join(no_id) + " — нечем спросить: нет ни штрихкода, ни ISRC")
    return " · ".join(parts) or "нигде не найден"
