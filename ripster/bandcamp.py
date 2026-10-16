"""Bandcamp: чтение публичных страниц релизов и страниц лейблов/артистов.

Зачем (24.09.2026, владелец): андеграундные техно-лейблы не появляются в
прессе и их релизов ещё нет в стриминговых каталогах — предзаказ выкладывается
на Bandcamp за недели до выхода. SEMANTICA 199 (Oscar Mulero, выход
25.09.2026) не был виден НИ В ОДНОМ источнике радара именно поэтому.

Что здесь есть и чего нет:

* ТОЛЬКО чтение публичных страниц. Ни покупок, ни скачивания аудио: файлы на
  Bandcamp выдаются купившим, и мы их не трогаем. Карточка несёт кнопку
  «открыть на Bandcamp».
* Разбор — чистые функции над текстом страницы ([parse_album], [parse_music_grid]),
  сеть отдельно ([fetch_album], [band_upcoming]), чтобы правило проверялось
  тестом на сохранённой странице, а не наблюдением за живым сайтом.
* Вежливость: не чаще запроса в 2 секунды (общий модульный замок) и кэш
  разобранного 6 часов — свой файл, потому что ответ Bandcamp это чужие
  витринные данные, а не наше состояние.

Два контейнера данных на странице, и читаем ОБА:

  `application/ld+json`  Schema.org MusicAlbum: имя, артист, `datePublished`,
                         `publisher.name` (это ЛЕЙБЛ), форматы из `albumRelease`
  `data-tralbum="…"`     внутренний JSON: `is_preorder`, `album_release_date`,
                         `trackinfo[]` с `album_preorder`/`unreleased_track`,
                         `packages[]` с названиями изданий, `current.upc`

ld+json хватает для карточки, но признака предзаказа в нём нет — он только в
data-tralbum. Отсюда и двойной разбор.
"""
from __future__ import annotations

import asyncio
import json
import re
import time
import html as _html
from datetime import datetime, timezone
from pathlib import Path

import httpx

_UA = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"),
    "Accept-Language": "en",
}

#: Пауза между реальными запросами (просьба владельца: ≤1 запроса в 2 с).
MIN_GAP = 2.0
#: Сколько живёт разобранный ответ.
CACHE_TTL = 6 * 3600.0
#: Сколько первых позиций сетки страницы лейбла мы раскрываем за обход. Сетка
#: отсортирована свежими в начало, а дата и признак предзаказа есть ТОЛЬКО на
#: странице релиза — поэтому «взять всё» означало бы сотни запросов на лейбл.
GRID_PROBE = 8

_lock = asyncio.Lock()
_last_hit = 0.0
_base_dir: Path = Path(".")


def configure(base_dir) -> None:
    """Куда класть кэш. Вызывается один раз при старте приложения."""
    global _base_dir
    try:
        _base_dir = Path(base_dir)
    except Exception:
        _base_dir = Path(".")


def _cache_file() -> Path:
    return _base_dir / "bandcamp_cache.json"


_mem: dict = {}          # url -> {"at": ts, "data": ...}
_loaded = False


def _load() -> None:
    global _mem, _loaded
    _loaded = True
    try:
        raw = json.loads(_cache_file().read_text(encoding="utf-8"))
        _mem = raw if isinstance(raw, dict) else {}
    except Exception:
        _mem = {}


def _save() -> None:
    try:
        now = time.time()
        keep = {k: v for k, v in _mem.items()
                if isinstance(v, dict) and now - float(v.get("at") or 0) < CACHE_TTL * 4}
        _cache_file().write_text(json.dumps(keep, ensure_ascii=False), encoding="utf-8")
    except Exception as e:
        print(f"[bandcamp] cache save failed: {e}", flush=True)


def _cached(url: str):
    """Свежее значение кэша или None. Просроченное — как отсутствующее."""
    if not _loaded:
        _load()
    hit = _mem.get(url)
    if not isinstance(hit, dict):
        return None
    if time.time() - float(hit.get("at") or 0) >= CACHE_TTL:
        return None
    return hit.get("data")


def _put(url: str, data) -> None:
    if not _loaded:
        _load()
    _mem[url] = {"at": time.time(), "data": data}
    _save()


async def _throttle() -> None:
    global _last_hit
    async with _lock:
        wait = MIN_GAP - (time.time() - _last_hit)
        if wait > 0:
            await asyncio.sleep(wait)
        _last_hit = time.time()


async def _get_text(url: str) -> str:
    await _throttle()
    async with httpx.AsyncClient(timeout=25.0, follow_redirects=True,
                                 headers=_UA) as c:
        r = await c.get(url)
        if r.status_code != 200 or not r.content:
            return ""
        return r.text


# ── разбор ───────────────────────────────────────────────────────────────────

_LD_RE = re.compile(r'<script[^>]*application/ld\+json[^>]*>(.*?)</script>', re.S)
_TRALBUM_RE = re.compile(r'data-tralbum="([^"]*)"')
_MONTHS = {m: i for i, m in enumerate(
    "jan feb mar apr may jun jul aug sep oct nov dec".split(), 1)}


def parse_bc_date(raw: str) -> str:
    """«25 Sep 2026 00:00:00 GMT» → «2026-09-25». Не разобралось — пусто.

    Дату не догадываемся достать: Bandcamp пишет её одним форматом, а голый год
    или «Sept 2026» — это не дата выхода.
    """
    s = str(raw or "").strip()
    m = re.match(r"^(\d{1,2})\s+([A-Za-z]{3})[a-z]*\s+(\d{4})", s)
    if not m:
        m2 = re.match(r"^(\d{4})-(\d{2})-(\d{2})", s)
        return m2.group(0) if m2 else ""
    day, mon, year = int(m.group(1)), _MONTHS.get(m.group(2).lower(), 0), int(m.group(3))
    if not mon:
        return ""
    try:
        return datetime(year, mon, day).strftime("%Y-%m-%d")
    except ValueError:
        return ""


def _ld_blob(page: str) -> dict:
    for m in _LD_RE.finditer(page or ""):
        try:
            d = json.loads(m.group(1).strip())
        except Exception:
            continue
        if isinstance(d, dict) and d.get("@type") in ("MusicAlbum", "MusicRelease"):
            return d
    return {}


def _tralbum(page: str) -> dict:
    m = _TRALBUM_RE.search(page or "")
    if not m:
        return {}
    try:
        d = json.loads(_html.unescape(m.group(1)))
    except Exception:
        return {}
    return d if isinstance(d, dict) else {}


def parse_album(page: str, page_url: str = "") -> dict | None:
    """Страница релиза → карточка. None — это НЕ страница релиза.

    Возвращает только то, что НАПИСАНО на странице. Пустой `date` означает
    «лейбл дату не назвал», и вызывающий обязан показать это, а не подставить
    сегодня.
    """
    ld = _ld_blob(page)
    tr = _tralbum(page)
    cur = tr.get("current") or {}
    title = str(ld.get("name") or cur.get("title") or "").strip()
    if not title:
        return None
    artist = ""
    by = ld.get("byArtist")
    if isinstance(by, dict):
        artist = str(by.get("name") or "").strip()
    artist = artist or str(cur.get("artist") or tr.get("artist") or "").strip()
    publisher = ld.get("publisher") if isinstance(ld.get("publisher"), dict) else {}
    label = str(publisher.get("name") or "").strip()
    band_url = str(publisher.get("@id") or "").strip()

    date_raw = str(tr.get("album_release_date") or cur.get("release_date")
                   or ld.get("datePublished") or "")
    date = parse_bc_date(date_raw)

    # Предзаказ: собственный признак Bandcamp либо то, что треклист помечен как
    # неизданный. `unreleased_track` без `is_preorder` — страница выставлена
    # заранее, но формально ещё не «pre-order».
    tracks = []
    preorder = bool(tr.get("is_preorder") or tr.get("album_is_preorder"))
    for t in (tr.get("trackinfo") or []):
        if not isinstance(t, dict):
            continue
        if t.get("album_preorder"):
            preorder = True
        ti = str(t.get("title") or "").strip()
        if ti:
            tracks.append({"num": t.get("track_num"), "title": ti,
                           "duration": t.get("duration") or 0,
                           "unreleased": bool(t.get("unreleased_track"))})

    formats: list[str] = []
    for p in (tr.get("packages") or []):
        if isinstance(p, dict):
            nm = str(p.get("type_name") or p.get("title") or "").strip()
            if nm and nm not in formats:
                formats.append(nm)
    for rel in (ld.get("albumRelease") or []):
        if isinstance(rel, dict):
            for pr in (rel.get("additionalProperty") or []):
                if isinstance(pr, dict) and pr.get("name") == "type_name":
                    nm = str(pr.get("value") or "").strip()
                    if nm and nm not in formats:
                        formats.append(nm)
    if not formats and cur.get("id"):
        formats.append("Digital")

    art = ""
    img = ld.get("image")
    if isinstance(img, list) and img:
        art = str(img[0])
    elif isinstance(img, str):
        art = img
    if not art and cur.get("art_id"):
        art = f"https://f4.bcbits.com/img/a{cur['art_id']}_10.jpg"

    upc = str(cur.get("upc") or "").strip()
    ident = str(cur.get("id") or tr.get("id") or "")
    url = str(tr.get("url") or page_url or ld.get("@id") or "").strip()
    if not url and ident:
        url = band_url and f"{band_url}/album/{ident}"
    return {
        "source": "bandcamp",
        "title": title,
        "artist": artist,
        "label": label,
        "band_url": band_url,
        "url": url,
        "cover": art,
        "date": date,
        "date_raw": date_raw.strip(),
        "preorder": bool(preorder),
        "formats": formats,
        "tracklist": tracks,
        "track_count": len(tracks) or int(ld.get("numTracks") or 0),
        "item_id": ident,
        "upc": upc,
        "isrc": "",
        "about": str(cur.get("about") or "")[:600],
    }


_MUSIC_ITEM_RE = re.compile(
    r'<li[^>]*data-item-id="(?P<kind>album|track)-(?P<id>\d+)"[^>]*>\s*'
    r'<a href="(?P<href>/[^"]+)"[^>]*>(?P<body>.*?)</a>', re.S)
_TITLE_RE = re.compile(r'<p class="title">\s*(?P<title>.*?)'
                       r'(?:<br>\s*<span class="artist-override">\s*(?P<artist>.*?)</span>)?'
                       r'\s*</p>', re.S)
_IMG_RE = re.compile(r'<img src="(?P<src>[^"]+)"')


def parse_music_grid(page: str, base_url: str = "") -> list[dict]:
    """Сетка `/music` страницы лейбла/артиста → позиции, свежие в начале.

    Даты и признака предзаказа в сетке НЕТ (замер 24.09.2026 на странице
    Semantica Records: 16 позиций, ни одной даты) — поэтому сетка годится только
    как указатель, какие страницы релизов раскрыть.
    """
    base = (base_url or "").rstrip("/")
    out = []
    for m in _MUSIC_ITEM_RE.finditer(page or ""):
        href = m.group("href")
        body = m.group("body") or ""
        tm = _TITLE_RE.search(body)
        title = artist = ""
        if tm:
            title = re.sub(r"<[^>]+>", " ", tm.group("title") or "")
            title = _html.unescape(re.sub(r"\s{2,}", " ", title)).strip()
            artist = _html.unescape(re.sub(r"<[^>]+>|\s{2,}", " ",
                                           tm.group("artist") or "")).strip()
        im = _IMG_RE.search(body)
        out.append({
            "kind": m.group("kind"),
            "item_id": m.group("id"),
            "url": (base + href) if base else href,
            "title": title,
            "artist": artist,
            "cover": im.group("src") if im else "",
        })
    return out


# ── сеть ─────────────────────────────────────────────────────────────────────

async def search_band(name: str) -> dict:
    """Найти страницу Bandcamp по имени лейбла/артиста.

    Публичный автодополнитель bandcamp.com отдаёт `type: "b"` — страницы бэндов
    (лейбл и артист для Bandcamp одно и то же). Сверка точная по нормализованному
    имени: подстрочный поиск приводил на чужие страницы (тот же урок, что и в
    `genre_sources.bandcamp_tags`).
    """
    name = str(name or "").strip()
    if not name:
        return {}
    key = f"band:{name.lower()}"
    hit = _cached(key)
    if hit is not None:
        return hit
    want = re.sub(r"[^a-z0-9]+", "", name.lower())
    res: dict = {}
    try:
        await _throttle()
        async with httpx.AsyncClient(timeout=25.0, follow_redirects=True,
                                     headers=_UA) as c:
            r = await c.post(
                "https://bandcamp.com/api/bcsearch_public_api/1/autocomplete_elastic",
                json={"search_text": name, "search_filter": "b", "full_page": False})
            if r.status_code == 200:
                for x in ((r.json().get("auto") or {}).get("results")) or []:
                    got = re.sub(r"[^a-z0-9]+", "", str(x.get("name") or "").lower())
                    url = str(x.get("item_url_root") or "").strip()
                    if not got or not url:
                        continue
                    if got == want:
                        res = {"url": url, "name": x.get("name") or name,
                               "tags": x.get("tag_names") or []}
                        break
                    if not res and (got in want or want in got):
                        res = {"url": url, "name": x.get("name") or name,
                               "tags": x.get("tag_names") or [], "fuzzy": True}
    except Exception as e:
        print(f"[bandcamp] band search «{name}»: {e}", flush=True)
        return {}
    # Пустой ответ НЕ кэшируем: «не нашлись с одного захода» — не факт надолго.
    if res:
        _put(key, res)
    return res


async def fetch_album(url: str) -> dict:
    """Страница релиза → карточка, {} если прочесть не удалось.

    Пустой ответ кэшируется на тот же срок: без этого обход снова и снова бился
    бы в страницу, которая уже показала, что релизом не является.
    """
    if not url:
        return {}
    hit = _cached(url)
    if hit is not None:
        return hit or {}
    page = await _get_text(url)
    data = parse_album(page, url) if page else None
    data = data or {}
    _put(url, data)
    return data


async def fetch_music_page(band_url: str) -> list[dict]:
    """Сетка релизов страницы лейбла/артиста. Кэш 6 ч."""
    url = band_url.rstrip("/") + "/music"
    hit = _cached(url)
    if hit is not None:
        return hit or []
    page = await _get_text(url)
    items = parse_music_grid(page, band_url) if page else []
    _put(url, items)
    return items


async def band_releases(name: str = "", band_url: str = "",
                        probe: int = GRID_PROBE) -> dict:
    """Релизы страницы Bandcamp с датами и предзаказами.

    {"band": url, "band_name": …, "releases": [карточка…], "error": …}

    Сначала сетка (1 запрос), затем раскрываются `probe` первых позиций.
    Уже известные кэшу страницы повторных запросов не стоят, поэтому регулярный
    обход дорожает только на новые релизы.
    """
    if not band_url:
        found = await search_band(name)
        band_url = str(found.get("url") or "")
    if not band_url:
        return {"band": "", "releases": [], "error": "no_band_page"}
    grid = await fetch_music_page(band_url)
    band_name = ""
    releases = []
    for it in [g for g in grid if g.get("kind") == "album"][:max(0, int(probe))]:
        card = await fetch_album(it["url"])
        if not card:
            continue
        band_name = band_name or card.get("label") or ""
        card.setdefault("grid_artist", it.get("artist") or "")
        if not card.get("artist") and it.get("artist"):
            card["artist"] = it["artist"]
        if not card.get("cover") and it.get("cover"):
            card["cover"] = it["cover"]
        card["title"] = card.get("title") or it.get("title") or ""
        releases.append(card)
    return {"band": band_url, "band_name": band_name, "releases": releases}


def is_future(rec: dict, today: str = "") -> bool:
    """Дата в будущем — предзаказ. Даты нет — не считаем будущим."""
    d = str((rec or {}).get("date") or "")
    if len(d) != 10:
        return False
    return d > (today or datetime.now(timezone.utc).strftime("%Y-%m-%d"))
