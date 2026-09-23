"""
BBC Sounds integration — browse programmes, stream via HLS, download as MP3.

Честность по качеству (21.09.2026): «MP3 320kbps» в этой строке было ложью.
On-demand у BBC Sounds — не больше 102 кбит/с HE-AAC, поэтому цель перекодировки
считается из фактической лесенки потока (``ripster.bbc_quality``), а не пишется
константой. Настоящие 320 — только в live-потоке (``engines/bbc_live.py``).

Stream flow (no yt-dlp needed):
  1. GET bbc.co.uk/programmes/{pid}.json  → versions[0].pid = VPID
  2. GET mediaselector/6/select/...vpid/{vpid}/format/json  → HLS m3u8 URL
"""
from __future__ import annotations

import asyncio
import json
import re
import subprocess
from asyncio.subprocess import PIPE
from datetime import datetime, timezone
from pathlib import Path

import httpx
from ripster import http_client as _HTTP
from ripster.py_runtime import app_python
from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import Response
from pydantic import BaseModel

from ripster.metadata.mixesdb import fetch_mix_detail, search_mixesdb
from ripster.metadata import bbc as _md_bbc
from ripster import bbc_quality as _Q
from ripster.i18n_msg import imsg

router = APIRouter(prefix="/api/bbc")

_config:       dict = {}
_broadcast     = None
_queue_snapshot = None

_RMS      = "https://rms.api.bbc.co.uk/v2"
_PROG_API = "https://www.bbc.co.uk/programmes"
# mediaset "iptv-all" only ever exposes ONE HLS rendition (51 kbps HE-AAC,
# mp4a.40.5) for every show/episode tested (Essential Mix, Radio 1 Dance,
# Pete Tong) — MediaSelector's own "bitrate": 320 field is a nominal quality-
# tier label, not the real encoded rate, so downstream MP3-320K re-encodes
# were just inflating file size around an already-narrow-band ~16.5 kHz-
# lowpassed source (confirmed via ffprobe + spectrogram — this is what a
# tester's spectrum screenshot flagged). mediaset "pc" exposes the SAME
# 51k variant plus a real 102 kbps HE-AAC one in its master playlist
# (verified across 3 different shows) — yt-dlp picks the highest-BANDWIDTH
# HLS variant by default, so this alone doubles real delivered fidelity
# with no other code change. Still well under a genuine 320 kbps source —
# every other mediaset id tried (iptv-uk/iptv-nonuk/apple-ipad-hls/pc-tablet/
# podcast-*/audio-syndication-*) returned "selectionunavailable" from this
# vantage point, so 102 kbps HE-AAC appears to be BBC's real ceiling for
# on-demand Sounds audio reachable without a UK-residential IP.
_MSEL     = "https://open.live.bbc.co.uk/mediaselector/6/select/version/2.0/mediaset/pc/vpid/{vpid}/format/json"
_T        = httpx.Timeout(connect=10, read=20, write=10, pool=5)

BRANDS = [
    {"id": "b006wkfp", "label": "Essential Mix"},
    {"id": "b00f3pc4", "label": "Classic Essential Mix"},
    {"id": "b006ww0v", "label": "Pete Tong"},
    {"id": "m0009y7t", "label": "Radio 1 Dance"},
    {"id": "b01dmw9x", "label": "Dance Anthems"},
    {"id": "m002d2x6", "label": "The 6 Mix"},
    {"id": "m001dkv1", "label": "Rave Forever"},
    {"id": "b01fm4ss", "label": "Gilles Peterson"},
    {"id": "b0072ky7", "label": "Craig Charles Funk & Soul"},
    {"id": "m0021281", "label": "DnB Allstars"},
    {"id": "b006tp52", "label": "Late Junction"},
]


def install(app, ctx) -> None:
    global _config, _broadcast, _queue_snapshot
    _config         = ctx.config
    _broadcast      = ctx.broadcast
    _queue_snapshot = ctx.queue_snapshot
    app.include_router(router)


def _img(template: str, sz: int = 320) -> str:
    """BBC image_url has '{recipe}' placeholder → replace with a LIVE ichef size.

    Размер берётся из лесенки ripster.metadata.bbc: ichef отвечает 403 на
    произвольное число (600×600 и 1000×1000 — из них), поэтому «крупнее» здесь
    означает не 600, а 640, и не 1000, а 1024.
    """
    if not template:
        return ""
    n = _md_bbc.cover_px(sz)
    return re.sub(r'\{recipe\}|\d+x\d+', f"{n}x{n}", template)


def yt_dlp_cmd() -> list[str]:
    # НЕ Scripts\yt-dlp.exe и не shutil.which(): console-script shim под
    # изолированным embeddable-питоном молча выходит с кодом 1
    # (preflight Gate 0.5). Запуск — тем же интерпретатором, `-m yt_dlp`.
    py = app_python()
    probe = subprocess.run([py, "-c", "import yt_dlp"], capture_output=True)
    if probe.returncode != 0:
        raise RuntimeError("yt-dlp не установлен в рабочий питон-окружение Ripster")
    return [py, "-m", "yt_dlp"]


_TC_RE = re.compile(
    r'^\s*(\d{1,2}:\d{2}(?::\d{2})?)\s*[-–—·|•]?\s*(.+)',
    re.MULTILINE,
)


def _parse_timecodes(text: str) -> list[dict]:
    """Parse HH:MM:SS / MM:SS timestamp lines from a YouTube description."""
    result = []
    for m in _TC_RE.finditer(text):
        ts    = m.group(1).strip()
        title = m.group(2).strip().rstrip(" |·-–—")
        if not title or len(title) > 300:
            continue
        parts = [int(x) for x in ts.split(":")]
        seconds = parts[0] * 3600 + parts[1] * 60 + (parts[2] if len(parts) == 3 else 0) if len(parts) == 3 else parts[0] * 60 + parts[1]
        result.append({"time": ts, "seconds": seconds, "title": title})
    return result


def _save_dir() -> Path:
    p = Path(_config.get("save-path", "downloads")) / "BBC"
    p.mkdir(parents=True, exist_ok=True)
    return p


def _parse_dur(d) -> int:
    """BBC duration can be int seconds or {'value': 7200, 'label': '...'}."""
    if isinstance(d, dict):
        return int(d.get("value", 0) or 0)
    return int(d or 0)


def _parse_ep(ep: dict) -> dict:
    titles  = ep.get("titles") or {}
    img_url = ep.get("image_url") or ""
    if not img_url:
        img_pid = (ep.get("image") or {}).get("pid", "")
        img_url = f"https://ichef.bbci.co.uk/images/ic/{{recipe}}/{img_pid}.jpg" if img_pid else ""
    # id = VPID; real episode PID lives in urn:bbc:radio:episode:{pid}
    vpid = ep.get("id", "")
    urn  = ep.get("urn", "")
    pid  = urn.split(":")[-1] if urn else vpid
    # Отложенная запись эфира (ripster/bbc_schedule.py): RMS отдаёт сеть канала
    # и точное время доступности — availability.from для будущей передачи и есть
    # начало эфира, release.date слишком грубое (до дней).
    from ripster import bbc_live_channels as _chl
    channel = (ep.get("network") or {}).get("id") or ""
    return {
        "pid":      pid,
        "vpid":     vpid,
        "title":    titles.get("primary", ep.get("title", "")),
        "subtitle": titles.get("secondary", ""),
        "synopsis": (ep.get("synopses") or {}).get("short", ""),
        "date":     (ep.get("release") or {}).get("date", ""),
        "duration": _parse_dur(ep.get("duration")),
        "image":    _img(img_url),
        "channel":      channel,
        "avail_from":   (ep.get("availability") or {}).get("from") or "",
        "schedulable":  _chl.known(channel),
    }


def _fill_missing_images(items: list[dict], fallback: str) -> list[dict]:
    """Нет своей картинки у выпуска — подставляем КАРТИНКУ БРЕНДА: это тоже
    официальное изображение BBC, той же передачи. Чужие кадры не подставляем:
    когда нет и бренда — пустая строка, фронт честно покажет заглушку.
    Ровно этот запасной путь уже работал в /upcoming (_guide_rows) и в
    preflight (parent.image); в сетке и поиске его не хватало."""
    if fallback:
        for it in items:
            if not it.get("image"):
                it["image"] = fallback
    return items


async def _brand_image(brand_id: str) -> str:
    try:
        async with _HTTP.ashared() as c:
            meta = await _brand_meta_of(brand_id, c)
        return meta.get("image") or ""
    except Exception:
        return ""


# ── Brands ────────────────────────────────────────────────────────────────────

@router.get("/brands")
async def get_brands():
    return {"brands": BRANDS}


@router.get("/channels")
async def get_channels():
    """Каталог живых эфиров — то, с чего ставится отложенная запись
    (ripster/bbc_schedule.py пишет эфир именно с live-потока канала).

    Только id: название канала — текст интерфейса, и строит его клиент по
    ключу i18n (скилл ripster-i18n: в ответе API не должно быть ни одного
    слова, которое человек прочтёт как текст).
    """
    from ripster import bbc_live_channels as _chl
    return {"channels": [{"id": c["id"]} for c in _chl.CHANNELS]}


# ── Episodes ──────────────────────────────────────────────────────────────────

@router.get("/episodes")
async def get_episodes(
    brand_id: str = Query("b006wkfp"),
    offset:   int = Query(0, ge=0),
    limit:    int = Query(20, ge=1, le=50),
):
    url = (
        f"{_RMS}/programmes/playable"
        f"?container={brand_id}&sort=sequential&type=episode"
        f"&experience=domestic&offset={offset}&limit={limit}"
    )
    async with _HTTP.ashared() as c:
        r = await c.get(url, headers={"Accept": "application/json"})
    if r.status_code != 200:
        raise HTTPException(502, f"BBC API {r.status_code}")
    data = r.json()
    items = [_parse_ep(ep) for ep in data.get("data", [])]
    _fill_missing_images(items, await _brand_image(brand_id))
    return {
        "total":  data.get("total", 0),
        "offset": offset,
        "items":  items,
    }


# ── Будущие эфиры — вход для планировщика ────────────────────────────────────
#
# Почему этот маршрут вообще нужен: RMS /programmes/playable отдаёт ТОЛЬКО
# вышедшее в эфир. Промер 21.09.2026 (Essential Mix, 3 первых выпуска):
# availability.from = 05.09 / 12.09 / 19.09 — всё в прошлом. Значит
# `_bbcLiveFuture()` в bbc.js не срабатывал ни разу, и кнопка «записать эфир»,
# написанная вместе с планировщиком, не появлялась ни на одной карточке:
# провод был цел, тока по нему не было.
#
# Начало будущего эфира берём из машиныльной разметки (schema.org RadioSeries)
# первой же страницы BBC — /programmes/<brand>/episodes/guide. Это домен bbc.co.uk,
# без авторизации. Ответ кэшируем: страница грузится ~2 с, а расписание меняется
# редко; без кэша каждая перекладка сетки била бы по BBC.
_GUIDE_TTL = 900
_guide_cache: dict[str, tuple[float, list]] = {}
_brand_meta:  dict[str, dict] = {}


def _ldjson_blocks(html: str) -> list:
    out: list = []
    for raw in re.findall(r'<script type="application/ld\+json"[^>]*>(.*?)</script>',
                          html or "", re.S):
        try:
            d = json.loads(raw)
        except Exception:
            continue
        out.extend(d if isinstance(d, list) else [d])
    return out


def _iso(s: str):
    """BBC отдаёт «+00:00», но страница — не контракт: на дату без смещения
    сравнивали бы aware с naive и падали. Нормализуем всё к UTC."""
    try:
        d = datetime.fromisoformat((s or "").replace("Z", "+00:00"))
    except Exception:
        return None
    return d.astimezone(timezone.utc) if d.tzinfo else d.replace(tzinfo=timezone.utc)


async def _brand_meta_of(brand_id: str, c) -> dict:
    """Канал live-потока и картинка бренда — из первой же страницы JSON.
    Картинка бренда — это честный запасной вариант, когда у выпуска своей нет."""
    meta = _brand_meta.get(brand_id)
    if meta is not None:
        return meta
    meta = {"channel": "", "image": ""}
    try:
        r = await c.get(f"{_PROG_API}/{brand_id}.json", headers={"Accept": "application/json"})
        if r.status_code == 200:
            prog = (r.json() or {}).get("programme") or {}
            meta["channel"] = ((prog.get("ownership") or {}).get("service") or {}).get("id") or ""
            meta["image"]   = _md_bbc.ichef((prog.get("image") or {}).get("pid") or "")
    except Exception:
        pass
    _brand_meta[brand_id] = meta
    return meta


def _guide_rows(data: list, brand_id: str, meta: dict, now) -> list[dict]:
    """Радио-эпизоды ld+json → карточки того же вида, что отдаёт /episodes.
    `publication` у повторов — список; берём ближайший будущий показ."""
    from ripster import bbc_live_channels as _chl
    from ripster import bbc_schedule as _bs
    max_dur = _bs.MAX_LIVE_MINUTES * 60
    rows: list[dict] = []
    for block in data:
        if not isinstance(block, dict) or block.get("@type") != "RadioSeries":
            continue
        eps = block.get("episode") or []
        for ep in (eps if isinstance(eps, list) else [eps]):
            if not isinstance(ep, dict):
                continue
            pubs = ep.get("publication") or []
            pubs = pubs if isinstance(pubs, list) else [pubs]
            # Из всех показов ищем ближайший БУДУЩИЙ. Полагаться на «конец ещё
            # не наступил» нельзя: у уже вышедших выпусков publicationEnd — это
            # окно доступности в Sounds (у июльского Essential Mix он сентябрьский,
            # «длительность» 77 суток), а не границы эфира.
            start = end = None
            for p in pubs:
                if not isinstance(p, dict):
                    continue
                s, e = _iso(p.get("startDate") or ""), _iso(p.get("endDate") or "")
                if not s or not e or s <= now or e <= s:
                    continue
                if start is None or s < start:
                    start, end = s, e
            if not start:
                continue
            duration = int((end - start).total_seconds())
            # Планировщик пишет с live-потока и дольше 8 часов не пишет вовсе
            # (bbc_schedule.MAX_LIVE_MINUTES) — карточка с таким эфиром была бы
            # обещанием, которое сервер всегда отклонит.
            if duration <= 0 or duration > max_dur:
                continue
            channel = meta.get("channel") or ""
            pid = ep.get("identifier") or ""
            # У будущих выпусков BBC имя в этой разметке — часто дата эфира
            # («17/10/2026»); тогда людьми читается description.
            name = (ep.get("name") or "").strip()
            desc = (ep.get("description") or "").strip()
            dated = _looks_like_date(name)
            rows.append({
                "pid":      pid,
                "vpid":     "",                      # версии ещё нет — качать нельзя
                "title":    name or pid,
                "subtitle": desc if dated else "",
                "synopsis": desc,
                "date":     start.strftime("%Y-%m-%d"),
                "duration": duration,
                "image":    _md_bbc.resize(ep.get("image") or "", _md_bbc.CARD_PX)
                            or meta.get("image") or "",
                "channel":     channel,
                "avail_from":  _bs.fmt_utc(start),
                "published":   (ep.get("datePublished") or "")[:10],
                # Повтор на сетке виден БЕСПЛАТНО: дата публикации раньше слота
                # значит, что выпуск уже существовал. Промер 21.09.2026 по 35
                # будущим выпускам пяти брендов: у премьер datePublished равна
                # дате эфира (или +1 день из-за летнего времени), у повторов —
                # на 7…53 дня раньше. Порог в 2 дня разделяет все 35 случаев.
                "repeat":      _repeat_from_published(start, ep.get("datePublished")),
                "schedulable": _chl.known(channel),
                "upcoming":    True,
            })
    rows.sort(key=lambda r: r["avail_from"])
    return rows


def _repeat_from_published(slot: datetime, published: str) -> bool:
    """Повтор ли это — по datePublished из сетки (без запроса к BBC).

    Отрицательный ответ здесь не значит «точно премьера»: это быстрый признак
    для сетки, а точный — first_broadcast_date в прогнозе записи.
    """
    p = _iso(published or "")
    return bool(p) and (slot.date() - p.date()).days >= 2



_DATE_RE = re.compile(r'^\d{1,2}[./-]\d{1,2}[./-]\d{2,4}$')


def _looks_like_date(s: str) -> bool:
    return bool(_DATE_RE.match((s or "").strip()))


@router.get("/upcoming")
async def get_upcoming(brand_id: str = Query("b006wkfp")):
    """Будущие передачи бренда — то, что можно поставить на отложенную запись."""
    now = datetime.now(timezone.utc)
    hit = _guide_cache.get(brand_id)
    if hit and hit[0] > now.timestamp():
        return {"items": hit[1]}
    async with _HTTP.ashared() as c:
        meta = await _brand_meta_of(brand_id, c)
        try:
            r = await c.get(f"{_PROG_API}/{brand_id}/episodes/guide",
                            headers={"Accept": "text/html"})
        except Exception:
            r = None
    items: list[dict] = []
    if r is not None and r.status_code == 200:
        items = _guide_rows(_ldjson_blocks(r.text or ""), brand_id, meta, now)
    else:
        raise HTTPException(502, detail=imsg("err.bbc_upstream", "BBC не отдал расписание",
                                             code=(r.status_code if r is not None else 0)))
    _guide_cache[brand_id] = (now.timestamp() + _GUIDE_TTL, items)
    ladders = await _live_ladders({r["channel"] for r in items if r.get("channel")})
    owned   = await asyncio.to_thread(_own_pids)
    for r in items:
        r["live"]     = ladders.get(r.get("channel") or "", {})
        r["recorded"] = bool(r.get("pid")) and r["pid"] in owned
    return {"items": items}


# ── Прогноз: чем ЭТОТ эфир будет на самом деле ───────────────────────────────
#
# Вопрос, ради которого всё это: прежде чем ставить запись, человек должен
# увидеть не шильдик «320», а ответ про конкретный слот — с какого потока пишем,
# что он отдаёт прямо сейчас, повтор ли это и есть ли у нас уже копия. Всё это
# числа и флаги; текст рисует фронт (скилл ripster-i18n).

_LADDER_TTL = 900
_ladder_cache: dict[str, tuple[float, dict]] = {}


async def _live_ladders(channels) -> dict:
    """Лестница live-потока каждого канала — с тем же кэшем, что и сетка.

    Меряется ОБЫЧНЫЙ HTTP-запрос к мастеру Akamai: «320» в названии движка —
    намерение, а факт — список вариантов, который канал отдаёт сейчас.
    """
    from ripster import bbc_live_channels as _chl
    now = datetime.now(timezone.utc).timestamp()
    out: dict = {}
    todo = []
    for ch in channels:
        hit = _ladder_cache.get(ch)
        if hit and hit[0] > now:
            out[ch] = hit[1]
        elif _chl.known(ch):
            todo.append(ch)
    if not todo:
        return out
    async with _HTTP.ashared() as c:
        for ch in todo:
            res = await _Q.live_ladder(ch, c)
            _ladder_cache[ch] = (now + _LADDER_TTL, res)
            out[ch] = res
    return out


async def _first_broadcast(pid: str, c) -> datetime | None:
    """Точная дата первой публикации выпуска (programmes JSON). None — данных нет."""
    try:
        r = await c.get(f"{_PROG_API}/{pid}.json", headers={"Accept": "application/json"})
        if r.status_code != 200:
            return None
        return _iso(((r.json() or {}).get("programme") or {}).get("first_broadcast_date") or "")
    except Exception:
        return None


def _own_pids() -> set:
    """pid всего, что у нас уже записано или скачано: планы планировщика,
    история загрузок и манифест. Один проход на страницу сетки, а не на карточку.

    Журналы вращаются (history.json — последние 500 задач), поэтому отсутствие
    pid здесь означает «в наших журналах не видно», а не «такой записи нет».
    """
    pids: set = set()
    try:
        from ripster import bbc_schedule as _bs
        for row in _bs.get_store().all():
            if row.get("pid"):
                pids.add(row["pid"])
    except Exception:
        pass
    for path in _own_journals():
        try:
            rows = json.loads(path.read_text(encoding="utf-8")) or []
        except Exception:
            continue
        if isinstance(rows, dict):
            rows = list(rows.values())
        for h in rows:
            if isinstance(h, dict):
                pids.update(_URL_PID.findall(str(h.get("url") or "")))
    pids.update(_cue_pids())
    return pids


_CUE_PID = re.compile(r"^REM BBC_PID (\S+)", re.MULTILINE)
_OWN_TTL = 300
_cue_cache: tuple[float, set] = (0.0, set())


def _cue_pids() -> set:
    """pid из sidecar-CUE в папках BBC: веб-вкладка качает мимо очереди, и в
    журналах её загрузок нет — зато рядом с файлом лежит его паспорт."""
    global _cue_cache
    now = datetime.now(timezone.utc).timestamp()
    if _cue_cache[0] > now:
        return _cue_cache[1]
    found: set = set()
    try:
        for cue in _save_dir().glob("*/*.cue"):
            try:
                found.update(_CUE_PID.findall(cue.read_text(encoding="utf-8",
                                                             errors="replace")))
            except OSError:
                pass
    except Exception:
        pass
    _cue_cache = (now + _OWN_TTL, found)
    return found


# pid встречается в сохранённом адресе задачи в двух формах: страница выпуска
# и псевдо-ссылка live-записи планировщика.
_URL_PID = re.compile(r"/(?:sounds/play|programmes)/([a-z0-9]{6,12})")


def _own_copy(pid: str) -> dict:
    """Есть ли у нас уже запись этого выпуска — по нашим же файлам, без запроса
    к BBC: планы планировщика, история загрузок и манифест.

    Отрицательный ответ честен лишь настолько, насколько живы эти журналы:
    история держит последние 500 задач, поэтому «не нашли» не означает «нет».
    """
    if not pid:
        return {"checked": False}
    try:
        from ripster import bbc_schedule as _bs
        for row in _bs.get_store().all():
            if row.get("pid") == pid:
                v = row.get("verdict") or {}
                return {"checked": True, "have": True, "source": "schedule",
                        "title": row.get("title") or "", "ts": row.get("start_utc") or "",
                        "status": row.get("status") or "",
                        "verdict_state": v.get("state") or "",
                        "kbps": v.get("kbps") or 0, "codec": v.get("codec") or ""}
    except RuntimeError:
        pass
    except Exception:
        pass
    for path in _own_journals():
        try:
            rows = json.loads(path.read_text(encoding="utf-8")) or []
        except Exception:
            continue
        if isinstance(rows, dict):
            rows = list(rows.values())
        for h in rows:
            if not isinstance(h, dict):
                continue
            if pid in str(h.get("url") or "") or pid == str(h.get("pid") or ""):
                return {"checked": True, "have": True,
                        "source": path.name, "title": h.get("title") or "",
                        "ts": str(h.get("ts") or h.get("start_utc") or ""),
                        "quality": h.get("quality") or "",
                        "folder": h.get("_save_dir") or h.get("dir") or ""}
    return {"checked": True, "have": False}


def _own_journals() -> list[Path]:
    """history.json и downloads_manifest.json — оба рядом с bbc_scheduled.json."""
    try:
        from ripster import bbc_schedule as _bs
        base = _bs.get_store().path.parent
    except Exception:
        return []
    return [base / "history.json", base / "downloads_manifest.json"]


async def _forecast(channel: str, start_utc: str, duration: int, pid: str) -> dict:
    """Что даст запись этого эфира — измерено сейчас, а не написано в шильдике."""
    from ripster import bbc_live_channels as _chl
    from ripster import bbc_schedule as _bs
    live = (await _live_ladders([channel])).get(channel) or {}
    repeat: dict = {"state": "unknown"}
    slot = _iso(start_utc)
    if pid:
        async with _HTTP.ashared() as c:
            first = await _first_broadcast(pid, c)
        if first is not None:
            # Промер 21.09.2026 (35 будущих выпусков): премьере BBC ставит
            # first_broadcast_date равной слоту, повтору — его прежнюю дату,
            # 7…53 дня назад. Два дня запаса — на часовые пояса разметки.
            gap = (slot.date() - first.date()).days if slot else None
            repeat = {"state": ("repeat" if (gap or 0) >= 2 else "debut"),
                      "days": gap,
                      "first_broadcast_utc": _bs.fmt_utc(first)}
    return {
        "channel":   channel,
        "stream":    live.get("url") or _chl.stream_url(channel),
        "duration":  int(duration or 0),
        "start_utc": start_utc,
        "pid":       pid,
        "live":      live,
        "ondemand":  {"ceiling_kbps": _Q.ONDEMAND_CEILING},
        "repeat":    repeat,
        "own":       await asyncio.to_thread(_own_copy, pid),
        "promised_kbps": _chl.BITRATE // 1000,
    }


@router.get("/schedule/forecast")
async def schedule_forecast(
    channel:   str = Query(...),
    start_utc: str = Query(""),
    duration:  int = Query(0, ge=0),
    pid:       str = Query(""),
):
    """Прогноз записи — до того, как её поставили.

    Отвечаем только тем, что измерили прямо сейчас. Если лестницу канала не
    удалось прочитать (сеть, 404 пула) — в ответе будет ok=false с причиной, а не
    молчаливое «320»: человек планирует запись на окно в две ночи, и выдуманное
    обещание стоит ему этой записи.
    """
    from ripster import bbc_live_channels as _chl
    if not _chl.known(channel):
        raise HTTPException(400, detail=imsg("err.bbc_live_unknown_channel",
                                             f"Неизвестный канал эфира: {channel}",
                                             channel=channel))
    return await _forecast(channel, start_utc, duration, pid)



# ── Search ────────────────────────────────────────────────────────────────────

# PID выпуска → его запасная картинка (своя или бренда) из programmes API.
# RMS-поиск отдаёт image_url не у каждого выпуска, а у бренда — есть всегда.
_ep_fallback: dict[str, str] = {}


async def _episode_fallback_cover(pid: str) -> str:
    if not pid:
        return ""
    if pid in _ep_fallback:
        return _ep_fallback[pid]
    img = ""
    try:
        async with _HTTP.ashared() as c:
            r = await c.get(f"{_PROG_API}/{pid}.json",
                            headers={"Accept": "application/json"})
        if r.status_code == 200:
            prog = (r.json() or {}).get("programme") or {}
            par  = ((prog.get("parent") or {}).get("programme") or {})
            img  = _md_bbc.ichef((prog.get("image") or {}).get("pid")
                                 or (par.get("image") or {}).get("pid") or "")
    except Exception:
        img = ""
    _ep_fallback[pid] = img
    return img


@router.get("/search")
async def search_bbc(q: str = Query(..., min_length=1)):
    url = f"{_RMS}/experience/inline/search"
    async with _HTTP.ashared() as c:
        r = await c.get(url, params={"q": q}, headers={"Accept": "application/json"})
    if r.status_code != 200:
        raise HTTPException(502, f"BBC search {r.status_code}")
    data = r.json()
    items = []
    # The response groups hits into blocks — a "Shows" block of container_item
    # (brands/rubrics, e.g. "Radio 1's Essential Mix" the show) and an
    # "Episodes" block of playable_item (actual downloadable episodes). Only
    # the latter has a real vpid/urn:...:episode:... pid that _bbc_preflight
    # can resolve to a stream — a container_item's "pid" is a BRAND id, which
    # has no `versions` and always fails preflight ("нет доступных версий").
    # The "Shows" block sorts first and alone fills the caller's top-N slice,
    # so without this filter real episodes never surface in search results.
    for block in data.get("data", []):
        for ep in block.get("data", []):
            if ep.get("type") != "playable_item":
                continue
            items.append(_parse_ep(ep))
    no_img = [it["pid"] for it in items if not it["image"] and it["pid"]]
    if no_img:
        covers = await asyncio.gather(*(_episode_fallback_cover(p) for p in dict.fromkeys(no_img)))
        by_pid = dict(zip(dict.fromkeys(no_img), covers))
        for it in items:
            if not it["image"]:
                it["image"] = by_pid.get(it["pid"], "")
    return {"items": items}


# ── Отложенная запись эфира ───────────────────────────────────────────────────

class ScheduleReq(BaseModel):
    channel:   str
    start_utc: str          # ISO (с смещением или Z) — храним в UTC
    duration:  int          # секунд
    title:     str = ""
    subtitle:  str = ""
    cover:     str = ""
    pid:       str = ""     # выпуск, чей эфир пишем (для повторов и «есть ли копия»)


@router.get("/schedule")
async def list_scheduled():
    """Все планы (pending + история fired) — фронт подсвечивает карточки
    уже запланированных эфиров."""
    from ripster import bbc_schedule as _bs
    try:
        return {"items": _bs.get_store().all()}
    except RuntimeError:
        return {"items": []}


@router.post("/schedule")
async def schedule_live(req: ScheduleReq, request: Request):
    """Запланировать запись будущего эфира: план в bbc_scheduled.json +
    карточка «scheduled» в очереди (её ведёт ripster/bbc_schedule.py)."""
    if _is_guest(request):
        raise HTTPException(403, "guests cannot schedule recordings")
    from ripster import bbc_schedule as _bs
    from ripster import bbc_live_channels as _chl
    if not _chl.known(req.channel):
        raise HTTPException(400, detail=imsg("err.bbc_live_unknown_channel",
                                             f"Неизвестный канал эфира: {req.channel}",
                                             channel=req.channel))
    try:
        start = _bs.parse_utc(req.start_utc)
        start_utc = _bs.fmt_utc(start)
    except ValueError as e:
        raise HTTPException(400, str(e))
    # Прогноз считаем ЗДЕСЬ и храним свой: план должен показывать то, что мы
    # измерили в момент постановки, а не то, что принёс клиент (и чтобы через
    # неделю в списке было видно, каким обещанием это записывали).
    fc = await _forecast(req.channel, start_utc, req.duration, req.pid)
    try:
        row = _bs.schedule_recording(channel=req.channel, start_utc=start,
                                     duration=req.duration,
                                     title=req.title, subtitle=req.subtitle,
                                     cover=req.cover,
                                     pid=req.pid, forecast=fc)
    except ValueError as e:
        raise HTTPException(400, str(e))
    if _broadcast and _queue_snapshot:
        await _broadcast({"type": "queue_update", "queue": _queue_snapshot()})
    return {"ok": True, "row": row}


@router.delete("/schedule/{sid}")
async def cancel_scheduled(sid: str, request: Request):
    if _is_guest(request):
        raise HTTPException(403, "guests cannot cancel recordings")
    from ripster import bbc_schedule as _bs
    ok = _bs.cancel_recording(sid)
    if not ok:
        raise HTTPException(404, "план не найден")
    if _broadcast and _queue_snapshot:
        await _broadcast({"type": "queue_update", "queue": _queue_snapshot()})
    return {"ok": True}


# ── VPID + stream URL helpers ─────────────────────────────────────────────────

async def _get_vpid(pid: str, client: httpx.AsyncClient) -> str:
    """Fetch VPID (version PID) from the programmes JSON API."""
    r = await client.get(f"{_PROG_API}/{pid}.json")
    if r.status_code != 200:
        raise HTTPException(502, f"BBC programmes API {r.status_code} for {pid}")
    versions = r.json().get("programme", {}).get("versions", [])
    if not versions:
        raise HTTPException(404, f"No versions found for {pid}")
    return versions[0]["pid"]


async def _get_hls_url(vpid: str, client: httpx.AsyncClient) -> str:
    """Query BBC MediaSelector for the best HLS m3u8 URL."""
    url = _MSEL.format(vpid=vpid)
    r   = await client.get(url)
    if r.status_code != 200:
        raise HTTPException(502, f"MediaSelector {r.status_code} for {vpid}")
    # Prefer: https + HLS + cloudfront, fallback to akamai
    best_cf = best_ak = None
    for media in r.json().get("media", []):
        for conn in media.get("connection", []):
            href = conn.get("href", "")
            if conn.get("protocol") != "https" or ".m3u8" not in href:
                continue
            sup = conn.get("supplier", "")
            if "cloudfront" in sup and not best_cf:
                best_cf = href
            elif "akamai" in sup and not best_ak:
                best_ak = href
    chosen = best_cf or best_ak
    if not chosen:
        raise HTTPException(502, f"No HLS stream found for {vpid}")
    return chosen


# ── Stream endpoint ───────────────────────────────────────────────────────────

@router.get("/stream")
async def get_stream(request: Request, pid: str = Query(...), vpid: str = Query(""), name: str = Query("")):
    """Resolve HLS m3u8 URL via BBC MediaSelector. If vpid is known, skips programmes.json lookup."""
    async with _HTTP.ashared() as c:
        if not vpid:
            vpid = await _get_vpid(pid, c)
        url = await _get_hls_url(vpid, c)
    try:
        from ripster import stats_collector as _sc
        from ripster.guest_manager import get_manager as _gm
        sid = _gm().get_session_id_from_request(request) or ""
        ip  = (request.headers.get("x-forwarded-for") or "").split(",")[0].strip() or \
              (request.client.host if request.client else "")
        _sc.record_stream("bbc", name or pid, url, session_id=sid, client_ip=ip)
    except Exception:
        pass
    return {"url": url, "vpid": vpid}


# ── Download (HLS → MP3 по фактической лестнице потока) ──────────────────────

class DownloadReq(BaseModel):
    pid:       str
    vpid:      str = ""   # version PID — skips programmes.json lookup if provided
    title:     str = ""
    artist:    str = "BBC Radio"
    image_url: str = ""
    cover_url: str = ""   # explicit cover override (e.g. chosen from MixesDB)


def _safe(s: str) -> str:
    return re.sub(r'[\\/:*?"<>|]', '_', s).strip(" .")


def _ep_dir(artist: str, title: str, pid: str) -> Path:
    """Returns downloads/BBC/{Artist} - {Title}/ , created."""
    a = _safe(artist) or "BBC Radio"
    t = _safe(title)  or pid
    folder = _save_dir() / f"{a} - {t}"
    folder.mkdir(parents=True, exist_ok=True)
    return folder


@router.post("/download")
async def download_episode(req: DownloadReq, request: Request):
    # BBC downloads run outside the task queue (no task_id to key ownership
    # off), so we stamp the guest session id directly on the progress events
    # instead — "" means owner-triggered, matching the queue's own convention.
    from ripster.guest_manager import get_manager as _gm_fn
    sid = _gm_fn().get_session_id_from_request(request) or ""

    ep_dir = _ep_dir(req.artist, req.title, req.pid)
    title  = _safe(req.title or req.pid)
    out    = str(ep_dir / f"{title}.mp3")

    # Fresh HLS token
    async with _HTTP.ashared() as c:
        vpid = req.vpid or await _get_vpid(req.pid, c)
        hls  = await _get_hls_url(vpid, c)
        # Сколько в потоке на самом деле. Поле «bitrate» у MediaSelector умеет
        # говорить 320 и про 51-килобитный вариант, поэтому смотрим на список
        # вариантов самого плейлиста (ripster/bbc_quality.py).
        ladder = await _Q.probe_url(hls, c)
    target   = _Q.mp3_target_kbps(ladder.get("best_kbps") or 0)

    try:
        ytdlp = yt_dlp_cmd()
    except RuntimeError as e:
        raise HTTPException(status_code=503, detail=str(e))
    cmd = [
        *ytdlp,
        "--quiet",
        "--downloader", "ffmpeg",
        "--hls-use-mpegts",
        "-x", "--audio-format", "mp3", "--audio-quality", f"{target}K",
        "--add-metadata",
        "--ignore-errors",
        "-o", out,
        hls,
    ]
    # store duration for progress calculation
    if req.pid:
        dur_raw = None
        # try to get it via programmes.json (already fetched vpid above)
        try:
            async with _HTTP.ashared() as c2:
                rp = await c2.get(f"{_PROG_API}/{req.pid}.json")
                if rp.status_code == 200:
                    versions = rp.json().get("programme", {}).get("versions", [])
                    dur_raw = (versions[0] if versions else {}).get("duration")
        except Exception:
            pass
        if dur_raw:
            BBC_DURATION_MAP[req.pid] = int(dur_raw)

    asyncio.create_task(_bg_download(cmd, req.pid, req.title, req.artist,
                                     req.image_url, ep_dir, req.cover_url, sid,
                                     out=out, target=target,
                                     source_kbps=int(ladder.get("best_kbps") or 0)))
    return {"status": "started", "pid": req.pid, "dir": str(ep_dir),
            "source_kbps": int(ladder.get("best_kbps") or 0),
            "target_kbps": target,
            "source_codec": ladder.get("best_codec") or ""}


async def _bg_download(cmd: list, pid: str, title: str, artist: str,
                       image_url: str, ep_dir: Path, cover_url: str = "", sid: str = "",
                       out: str = "", target: int = 0, source_kbps: int = 0):
    async def _bcast(msg: dict):
        if _broadcast:
            try:
                await _broadcast({**msg, "session_id": sid})
            except Exception:
                pass

    await _bcast({"type": "bbc_dl_start", "pid": pid, "title": title, "artist": artist})
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        # Stream stderr to parse ffmpeg progress lines
        last_pct = -1
        async for raw in proc.stderr:
            line = raw.decode(errors="replace").rstrip()
            # ffmpeg progress: "size=   1234kB time=00:12:34.56 bitrate= ..."
            m = re.search(r'time=(\d+):(\d+):(\d+)', line)
            if m and BBC_DURATION_MAP.get(pid):
                dur  = BBC_DURATION_MAP[pid]
                secs = int(m.group(1))*3600 + int(m.group(2))*60 + int(m.group(3))
                pct  = min(99, int(secs / dur * 100)) if dur else 0
                if pct != last_pct:
                    last_pct = pct
                    await _bcast({"type": "bbc_dl_progress", "pid": pid,
                                  "title": title, "pct": pct, "elapsed": secs})
        await proc.wait()
    except Exception:
        pass

    fallback_q = f"{artist} {title}".strip() if (artist or title) else ""
    await _save_cover(image_url, ep_dir,
                      fallback_query=fallback_q, cover_override=cover_url)
    # Готовый файл спрашиваем, а не повторяем то, что заказывали: полоса MP3-цели
    # ничего не говорит об источнике, а вот перекодировка вверх — говорит, и
    # человек должен видеть и то, и другое.
    measured = 0
    state = ""
    if out:
        try:
            v = await asyncio.to_thread(_Q.verdict, out, target)
            measured = int(v.get("kbps") or 0)
            state = v.get("state") or ""
        except Exception:
            pass
    await _try_write_cue(pid, title, artist, ep_dir,
                         kbps=measured or target, source_kbps=source_kbps)
    await _bcast({"type": "bbc_dl_done", "pid": pid, "title": title,
                  "dir": str(ep_dir), "source_kbps": source_kbps,
                  "target_kbps": target, "kbps": measured, "state": state})

# pid → episode duration in seconds (stored when download is queued)
BBC_DURATION_MAP: dict[str, int] = {}


# Имя, которое читают все потребители обложки папки: routes/library.py:538 и
# mixcue.py:212 ищут cover.jpg/cover.png/folder.jpg/cover.jpeg. Раньше BBC клал
# `{Title}.jpg` — несовпадающее имя, поэтому обложки скачанного BBC-выпуска не
# видел ни список библиотеки, ни «кодер».
COVER_FILENAME = "cover.jpg"


async def _fetch_image(url: str) -> bytes:
    """Только настоящая картинка: 200 + магические байты. Ошибка сети/403/
    огрылок HTML — пустая строка, чтобы вызывающий честно перешёл к следующему
    источнику, а не записал битый файл."""
    if not url:
        return b""
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(30),
                                     follow_redirects=True) as c:
            r = await c.get(url)
    except Exception:
        return b""
    if r.status_code != 200:
        return b""
    data = r.content or b""
    if data[:3] == b"\xff\xd8\xff" or data[:8] == b"\x89PNG\r\n\x1a\n" \
       or (data[:4] == b"RIFF" and data[8:12] == b"WEBP"):
        return data
    return b""


def _embed_cover(ep_dir: Path, data: bytes) -> int:
    """Обложка в теги каждого аудиофайла папки. Возвращает число успешно
    помеченных файлов — у остальных сервисов она внутри файла, и без этого BBC
    звучало в библиотеке без визуала даже при лежащем рядом cover.jpg."""
    if not data:
        return 0
    from ripster.tagger import embed_cover
    n = 0
    try:
        files = [p for p in ep_dir.iterdir()
                 if p.is_file() and p.suffix.lower() in
                 (".mp3", ".m4a", ".aac", ".flac", ".ogg", ".opus")]
    except OSError:
        return 0
    for p in files:
        try:
            if embed_cover(p, data):
                n += 1
        except Exception as e:
            print(f"[bbc] embed cover {p.name}: {e}", flush=True)
    return n


async def _save_cover(image_url: str, ep_dir: Path,
                     fallback_query: str = "", cover_override: str = "") -> str:
    """Обложка выпуска → cover.jpg в папку + в теги аудио. Возвращает путь к
    cover.jpg, либо "" если достать картинку не удалось (тогда интерфейс
    покажет нейтральную иконку, а не серый квадрат от битого URL)."""
    cover_path = ep_dir / COVER_FILENAME
    data = b""
    src = ""

    # 0) Явный выбор пользователя (например обложка из MixesDB в UI)
    if cover_override:
        data, src = await _fetch_image(cover_override), "override"

    # 1) BBC — размер из живой лесенки ichef (600×600 CDN не отдаёт)
    if not data and image_url:
        data, src = await _fetch_image(_img(image_url, _md_bbc.EMBED_PX)), "bbc"

    # 2) MixesDB
    if not data and fallback_query:
        try:
            results = await search_mixesdb(fallback_query, limit=3)
            for hit in results:
                detail = await fetch_mix_detail(hit["page_title"])
                if detail and detail.get("artworkUrl"):
                    data = await _fetch_image(detail["artworkUrl"])
                    if data:
                        src = f"mixesdb:{hit['page_title']}"
                        break
        except Exception as e:
            print(f"[bbc] mixesdb cover fallback failed: {e}", flush=True)

    if not data:
        return ""
    if _config.get("save-cover-to-folder", True):
        try:
            cover_path.write_bytes(data)
        except OSError as e:
            print(f"[bbc] write cover: {e}", flush=True)
    embedded = _embed_cover(ep_dir, data)
    print(f"[bbc] cover src={src} bytes={len(data)} embedded={embedded}", flush=True)
    return str(cover_path) if cover_path.exists() else ""


async def _cue_tracks(pid: str, title: str, artist: str,
                      net_1001: bool = True) -> list[dict]:
    """Timed rows for a CUE sheet from the best source (BBC → 1001TL → MixesDB),
    in the `offset` shape _build_cue expects. Untimed lists make a CUE of
    00:00 markers — useless, so they yield nothing."""
    art = "" if artist in ("", "BBC Radio") else artist
    ttl = "" if title in ("", "BBC Mix") else title
    best = await best_tracklist(pid, ttl, art, net_1001=net_1001)
    if not (best.get("found") and best.get("timed")):
        return []
    return [{"offset": int(t["seconds"]), "title": t["title"], "artist": t["artist"]}
            for t in best["tracks"] if t.get("seconds") is not None and not t.get("is_with")]


async def _try_write_cue(pid: str, title: str, artist: str, ep_dir: Path,
                        kbps: int = 0, source_kbps: int = 0):
    try:
        tracks = await _cue_tracks(pid, title, artist)
        if not tracks:
            return
        stem = _safe(title or pid)
        cue  = _build_cue(title or pid, artist or "BBC Radio", tracks,
                         pid=pid, kbps=kbps, source_kbps=source_kbps)
        (ep_dir / f"{stem}.cue").write_text(cue, encoding="utf-8")
    except Exception:
        pass


# ── Tracklist ─────────────────────────────────────────────────────────────────

@router.get("/tracklist")
async def tracklist(pid: str = Query(...)):
    return {"tracks": await _fetch_tracklist(pid)}


async def _fetch_tracklist(pid: str) -> list[dict]:
    async with _HTTP.ashared() as c:
        r = await c.get(
            f"{_PROG_API}/{pid}/segments.json",
            headers={"Accept": "application/json"},
        )
    if r.status_code != 200:
        return []
    tracks = []
    for ev in r.json().get("segment_events", []):
        seg    = ev.get("segment", {})
        raw    = ev.get("version_offset", ev.get("offset"))
        offset = raw or 0
        title  = seg.get("title", "")
        artist = (seg.get("primary_contributor") or {}).get("name", "") or seg.get("artist", "")
        if title:
            # `timed` = BBC gave a real position in the episode; old archive
            # episodes list names only (version_offset null) — those still
            # identify the set but can't drive chapters/CUE.
            tracks.append({"offset": int(offset), "title": title, "artist": artist,
                           "timed": raw is not None})
    return tracks


# ── Best tracklist for an episode: BBC → 1001Tracklists → MixesDB ────────────

def _ts_to_sec(ts: str):
    try:
        p = [int(x) for x in str(ts).strip().split(":")]
    except ValueError:
        return None
    if len(p) == 3:
        return p[0] * 3600 + p[1] * 60 + p[2]
    if len(p) == 2:
        return p[0] * 60 + p[1]
    return None


def _norm_rows(rows: list[dict]) -> list[dict]:
    """One row shape for every source: {n, artist, title, seconds, timestamp}."""
    out = []
    for i, t in enumerate(rows, 1):
        sec = t.get("seconds")
        if sec is None and t.get("timestamp"):
            sec = _ts_to_sec(t["timestamp"])
        out.append({"n": str(t.get("n") or i), "artist": t.get("artist", "") or "",
                    "title": t.get("title", "") or "",
                    "seconds": sec, "timestamp": _sec_to_ts(sec) if sec is not None else "",
                    "is_with": bool(t.get("is_with"))})
    return out


def _is_timed(rows: list[dict]) -> bool:
    secs = {t["seconds"] for t in rows if t.get("seconds") is not None}
    return len(secs) >= 3


async def _episode_meta(pid: str) -> dict:
    """Title / DJ / air date / length from the programmes API (short timeout)."""
    try:
        async with httpx.AsyncClient(timeout=6.0, follow_redirects=True) as c:
            r = await c.get(f"{_PROG_API}/{pid}.json")
        if r.status_code != 200:
            return {}
        p = r.json().get("programme") or {}
        brand = ((p.get("parent") or {}).get("programme") or {}).get("title", "")
        vers = p.get("versions") or []
        return {"title": brand or p.get("title", ""), "artist": p.get("title", ""),
                "date": str(p.get("first_broadcast_date") or "")[:10],
                "duration": int((vers[0].get("duration") if vers else 0) or 0)}
    except Exception:
        return {}


_TL_REFETCHED: set[str] = set()


def _tl1001_reject_reason(res: dict, bbc: list[dict], date: str,
                          years: set | None = None) -> str:
    """Hard gates on top of 1001TL's weighted score ('' = accept). For a BBC
    episode we KNOW the broadcast date and often BBC's own track names, so a
    page aired on another day, or whose tracks disagree with BBC's names, is
    another set however well the title matched. ("The Essential Mix 30", 2020,
    got Bontan's 2024-11-30 list: its only identity token "30" sat in the other
    page's date.) Recomputed from the result itself, not its stored checks, so
    a disk-cached entry found by an older/looser rule is re-judged on read."""
    if not res.get("ok") or (res.get("match") or {}).get("tier") == "definitive":
        return ""
    from ripster import tracklist_match as TM
    ov = TM.check_name_overlap({"tracklist": bbc}, {"tracks": res.get("tracks") or []})
    if ov["applicable"]:
        # BBC listed the set: agreement on names is the proof (Classic Essential
        # Mix re-airs carry the ORIGINAL date on the page, so date can't be).
        return "" if ov["score"] >= 0.4 else "names disagree with BBC " + ov["detail"]
    page = TM._dates(str(res.get("url") or ""))
    if years and page and not ({str(d.year) for d in page} & set(years)):
        # "Danny Howells 2002" must not get his 2007 set.
        return f"set year {sorted(years)} vs page {page[0]}"
    air = TM._dates(date)
    if not air:
        return ""
    if not page:
        return "no air date on the page to confirm"
    gap = min(abs((air[0] - d).days) for d in page)
    return "" if gap <= 3 else f"aired {air[0]}, page {page[0]}"


def _is_guest(request: Request | None) -> bool:
    """A guest session (tunnel link) — they may read BBC data, but must not make
    the owner's machine scrape 1001Tracklists (GUEST_BLOCKED_PATHS blocks the
    direct route for the same reason)."""
    if request is None:
        return False
    try:
        from ripster import auth as _auth
        fn = getattr(_auth, "_guest_session_fn", None)
        return bool(fn and fn(request))
    except Exception:
        return False


async def best_tracklist(pid: str, title: str = "", artist: str = "",
                         dur: int = 0, force: bool = False,
                         net_1001: bool = True) -> dict:
    """Timed tracklist for a BBC episode, sources in order of trust:
      1. BBC's own segments (only when they carry positions),
      2. 1001Tracklists — verified with DJ + air date (+ BBC's names when BBC
         listed the set untimed, + the Sounds URL as a backlink),
      3. BBC's own names without times (right set, no chapters),
      4. MixesDB — relevance-scored, DJ-checked, air-date-checked.
    A wrong tracklist is worse than none: every foreign source is gated, and
    any failure (captcha, timeout, cooldown) just falls to the next source.
    Returns {found, source, tracks, timed, url?, match?, tried}."""
    tried: list[str] = []
    try:
        bbc_rows = await _fetch_tracklist(pid)
    except Exception:
        bbc_rows = []
    tried.append("bbc")
    bbc = _norm_rows([{**t, "seconds": t["offset"] if t.get("timed") else None}
                      for t in bbc_rows])
    if len(bbc) >= 3 and _is_timed(bbc):
        return {"found": True, "source": "bbc", "tracks": bbc, "timed": True,
                "url": f"https://www.bbc.co.uk/programmes/{pid}", "tried": tried}

    meta = await _episode_meta(pid)
    title = title or meta.get("title", "")
    artist = artist or meta.get("artist", "")
    dur = int(dur or meta.get("duration") or 0)
    date = meta.get("date", "")
    # Classic Essential Mix = a re-air ("deadmau5 2008" broadcast 2026-09-13):
    # the programmes date is the repeat, the set's own pages carry the original
    # year. A year in the title that isn't the air year → the date proves nothing.
    _yrs = set(re.findall(r"\b((?:19|20)\d{2})\b", f"{title} {artist}"))
    if date and _yrs and date[:4] not in _yrs:
        date = ""

    if title or artist:
        tried.append("1001tracklists")
        from fastapi.concurrency import run_in_threadpool
        from ripster import tl1001
        ref = [{"artist": t["artist"], "title": t["title"]} for t in bbc]

        async def _lookup(frc: bool) -> dict:
            try:
                return await run_in_threadpool(
                    tl1001.tracklist_for, title or artist, artist if title else "", dur,
                    ref, [f"https://www.bbc.co.uk/sounds/play/{pid}"], "bbc_" + pid,
                    frc, date)
            except Exception as e:
                print(f"[bbc] 1001TL lookup failed for {pid}: {e}", flush=True)
                return {}

        if net_1001:
            res = await _lookup(force)
        else:
            # Cache only (guests): an already-verified list is free to show.
            hit = tl1001._disk_get("id:bbc_" + pid) or {}
            res = ({**hit, "cached": True}
                   if hit.get("ok") and tl1001._cached_identity_ok(hit, artist, title) else {})
        why = _tl1001_reject_reason(res, bbc, date, _yrs)
        if why and net_1001 and res.get("cached") and pid not in _TL_REFETCHED:
            # Self-heal: the cached page is someone else's set — look again once
            # per process (not on every play) instead of hiding the right list
            # behind the wrong one for the cache's 10 days.
            _TL_REFETCHED.add(pid)
            print(f"[bbc] 1001TL cache for {pid} rejected ({why}) — re-searching", flush=True)
            res = await _lookup(True)
            why = _tl1001_reject_reason(res, bbc, date, _yrs)
        if why:
            print(f"[bbc] 1001TL {res.get('url')} rejected for {pid}: {why}", flush=True)
            res = {}
        if res.get("ok") and res.get("tracks"):
            rows = _norm_rows(res["tracks"])
            if _is_timed(rows) or not bbc:
                return {"found": True, "source": "1001tracklists", "tracks": rows,
                        "timed": _is_timed(rows), "url": res.get("url"),
                        "match": {k: (res.get("match") or {}).get(k)
                                  for k in ("score", "tier", "reason")},
                        "cached": bool(res.get("cached")), "tried": tried}

    if len(bbc) >= 3:
        return {"found": True, "source": "bbc", "tracks": bbc, "timed": False,
                "url": f"https://www.bbc.co.uk/programmes/{pid}", "tried": tried}

    tried.append("mixesdb")
    # MixesDB search is plain full-text: "Radio 1's Classic … 2008" drowns it,
    # "<DJ> Essential Mix" finds the page. Clean both halves; the year (classic
    # re-airs) or the exact air date then picks the right edition.
    mdb_show = re.sub(r"(?i)\b(bbc|radio\s*1'?s?|classic)\b", " ", title)
    mdb_show = re.sub(r"\s+", " ", mdb_show).strip() or title
    mdb_dj = re.sub(r"\b(?:19|20)\d{2}\b", " ", re.sub(r"(?i)^with\s+", "", artist))
    mdb_dj = re.sub(r"\s+", " ", mdb_dj.split(" @ ")[0]).strip() or artist
    mdb_date = date or (next(iter(_yrs)) if len(_yrs) == 1 else "")
    try:
        m = await mixesdb_match(title=mdb_show, artist=mdb_dj, brand="", date=mdb_date)
    except Exception as e:
        print(f"[bbc] MixesDB lookup failed for {pid}: {e}", flush=True)
        m = {}
    rows = _norm_rows(m.get("tracklist") or []) if m.get("found") else []
    if len(rows) >= 3:
        return {"found": True, "source": "mixesdb", "tracks": rows,
                "timed": _is_timed(rows), "url": m.get("url"),
                "match": {"score": m.get("score"), "tier": "scored"}, "tried": tried}
    return {"found": False, "source": "", "tracks": [], "timed": False, "tried": tried}


@router.get("/tracklist-best")
async def tracklist_best(pid: str = Query(..., pattern=r"^[a-z0-9]{6,12}$"),
                         title: str = Query(""), artist: str = Query(""),
                         dur: int = Query(0, ge=0), *, request: Request):
    return await best_tracklist(pid, title.strip(), artist.strip(), dur,
                                net_1001=not _is_guest(request))


# ── MixesDB search / detail ──────────────────────────────────────────────────

@router.get("/mixesdb/search")
async def mixesdb_search(q: str = Query(..., min_length=1), limit: int = Query(10, ge=1, le=30)):
    results = await search_mixesdb(q, limit=limit)
    return {"results": results}


@router.get("/mixesdb/detail")
async def mixesdb_detail(title: str = Query(...)):
    detail = await fetch_mix_detail(title)
    if not detail:
        raise HTTPException(404, "Mix not found on MixesDB")
    return detail


# Filler words that carry no matching signal for mix/show titles.
_MATCH_STOP = {
    "the", "a", "an", "and", "mix", "set", "live", "at", "with", "feat", "ft",
    "featuring", "presents", "pres", "radio", "show", "episode", "ep", "edition",
    "podcast", "dj", "vol", "volume", "part", "pt", "in", "on", "of", "for",
}


def _match_toks(s: str) -> set[str]:
    return {w for w in re.findall(r"[a-z0-9]+", (s or "").lower())
            if w not in _MATCH_STOP and len(w) > 1}


def _match_nums(s: str) -> set[str]:
    # Episode/edition numbers (1–4 digits). Excludes long runs like years-in-dates only
    # when read from `show`, where dates aren't present.
    return set(re.findall(r"\d{1,4}", s or ""))


def _score_mixesdb_hit(q_title: str, q_artist: str, hit: dict) -> float:
    """Relevance score of a MixesDB hit vs the source mix. Higher = better.

    Token overlap + episode-number gating: if the source title has a number
    (e.g. "Anjunadeep Edition 500") and the candidate show has a DIFFERENT number,
    it's almost certainly the wrong episode → heavy penalty. This is what stops the
    "first hit with artwork" logic from returning a stranger's tracklist."""
    q_tokens = _match_toks(f"{q_artist} {q_title}")
    if not q_tokens:
        return 0.0
    cand = f"{hit.get('artist','')} {hit.get('show','')}"
    c_tokens = _match_toks(cand)
    # Identity veto: the DJ must be in the hit. "Radio 1's Essential Mix" alone
    # matches every episode of the show (HAAi got Hot Since 82's list, 19.09.2026).
    from ripster.tracklist_match import identity_tokens as _ident, _tokens as _tmt
    ident = _ident(q_artist, q_title)
    if ident and not (ident & _tmt(cand)):
        return 0.0
    overlap = len(q_tokens & c_tokens) / len(q_tokens)

    q_nums = _match_nums(q_title)
    c_nums = _match_nums(hit.get("show", ""))   # show only — avoids date digits
    num_adj = 0.0
    if q_nums:
        if q_nums & c_nums:
            num_adj = 0.45            # episode number matches → strong confirm
        elif c_nums:
            num_adj = -0.6            # candidate has a DIFFERENT number → wrong episode
        else:
            num_adj = -0.1            # source numbered, candidate not → mild doubt
    return overlap + num_adj


@router.get("/mixesdb/match")
async def mixesdb_match(title: str = Query(""), artist: str = Query(""), brand: str = Query(""),
                        date: str = Query("")):
    """Auto-match a BBC episode / SoundCloud mix to a MixesDB entry.

    Scores every search hit for relevance (token overlap + episode-number gating)
    and returns the BEST match above a confidence threshold — never just the first
    hit that happens to have artwork. Returns {found: false} when nothing is a
    confident match (an empty tracklist beats a wrong one)."""
    parts = [p for p in [artist, title, brand] if p]
    q = " ".join(parts).strip()
    if not q:
        return {"found": False}
    print(f"[bbc] mixesdb_match q={q!r}", flush=True)
    try:
        results = await search_mixesdb(q, limit=8)
        scored = sorted(
            ((_score_mixesdb_hit(title or brand, artist, h), h) for h in results),
            key=lambda x: x[0], reverse=True,
        )
        print(f"[bbc] mixesdb scored: {[(round(s,2), h['page_title']) for s,h in scored]}",
              flush=True)
        # Need a confident lead; below threshold we'd rather show nothing.
        _MIN_SCORE = 0.5
        from ripster.tracklist_match import _dates as _tmd
        air = _tmd(str(date or "")[:10])
        year = str(date or "") if re.fullmatch(r"(?:19|20)\d{2}", str(date or "")) else ""
        for score, hit in scored:
            if score < _MIN_SCORE:
                break
            if year and not str(hit.get("date") or "").startswith(year):
                continue
            # Same DJ on the same show years apart (HAAi 2018 vs 2026): when the
            # air date is known, a hit dated more than 3 days away is another set.
            hd = _tmd(str(hit.get("date") or "")[:10])
            if air and hd and abs((air[0] - hd[0]).days) > 3:
                continue
            detail = await fetch_mix_detail(hit["page_title"])
            if detail and (detail.get("tracklist") or detail.get("artworkUrl")):
                return {
                    "found":      True,
                    "score":      round(score, 2),
                    "artworkUrl": detail.get("artworkUrl", ""),
                    "tracklist":  detail.get("tracklist", []),
                    "page_title": hit["page_title"],
                    "url":        hit.get("url", ""),
                    "date":       detail.get("date", ""),
                }
    except Exception as e:
        print(f"[bbc] mixesdb_match failed: {e}", flush=True)
    return {"found": False}


# ── YouTube timecodes ─────────────────────────────────────────────────────────

def _yt_timecodes_from_info(info: dict) -> list[dict]:
    chapters = info.get("chapters") or []
    if chapters:
        return [
            {"time": _sec_to_ts(int(c.get("start_time", 0))),
             "seconds": int(c.get("start_time", 0)),
             "title": c.get("title", "").strip()}
            for c in chapters if c.get("title")
        ]
    return _parse_timecodes(info.get("description") or "")


@router.get("/youtube-timecodes")
async def youtube_timecodes(q: str = Query(..., min_length=1), dur: int = Query(0, ge=0)):
    """Search YouTube via yt-dlp and return timecodes from the BEST-matching video.

    Picks among several candidates by duration closeness (a 90-min mix matches a
    ~90-min upload, not a 3-min clip) + title overlap, and prefers a video that
    actually HAS timecodes — instead of blindly taking the first search result."""
    try:
        cmd = [*yt_dlp_cmd(), "--dump-json", "--no-playlist", "--quiet", f"ytsearch5:{q}"]
        proc = await asyncio.create_subprocess_exec(*cmd, stdout=PIPE, stderr=PIPE)
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=25)
    except asyncio.TimeoutError:
        return {"found": False, "error": "timeout"}
    except Exception as e:
        return {"found": False, "error": str(e)}

    if not stdout:
        return {"found": False}

    candidates = []
    for line in stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            candidates.append(json.loads(line))
        except Exception:
            continue
    if not candidates:
        return {"found": False}

    q_tokens = _match_toks(q)
    q_nums   = _match_nums(q)            # episode/edition numbers, e.g. {"593"}

    def _cand_score(info: dict) -> float:
        score = 0.0
        title = info.get("title", "")
        # Duration closeness (strongest signal for full mixes).
        vdur = info.get("duration") or 0
        if dur and vdur:
            ratio = min(dur, vdur) / max(dur, vdur)
            score += ratio * 2.0            # up to +2.0 for a near-exact length match
            if ratio < 0.7:
                score -= 1.0                # very different length → probably a clip/other set
        # Title overlap.
        if q_tokens:
            t_tokens = _match_toks(title)
            score += len(q_tokens & t_tokens) / len(q_tokens)
        # Episode-number gating — "Edition 593" must not match "Edition 580".
        if q_nums:
            c_nums = _match_nums(title)
            if q_nums & c_nums:
                score += 1.5                # exact episode number → strong confirm
            elif c_nums:
                score -= 1.2                # a DIFFERENT number → wrong episode
        # Prefer videos that actually carry timecodes.
        if _yt_timecodes_from_info(info):
            score += 1.0
        return score

    ranked = sorted(((_cand_score(c), c) for c in candidates), key=lambda x: x[0], reverse=True)
    best_score, best = ranked[0]
    btitle = best.get("title", "")
    print(f"[bbc] yt candidates: {[(round(s,2), c.get('title','')[:48]) for s,c in ranked]}",
          flush=True)

    # Confidence gate — a wrong mix's timecodes are worse than none.
    if q_nums:
        bnums = _match_nums(btitle)
        if bnums and not (q_nums & bnums):
            return {"found": False, "reason": "episode-number mismatch", "title": btitle}
    if dur and best.get("duration"):
        ratio = min(dur, best["duration"]) / max(dur, best["duration"])
        if ratio < 0.6:
            return {"found": False, "reason": "duration mismatch", "title": btitle}
    if best_score < 1.0:
        return {"found": False, "reason": "low confidence", "title": btitle}

    video_id  = best.get("id", "")
    timecodes = _yt_timecodes_from_info(best)
    thumb     = (best.get("thumbnail")
                 or (f"https://i.ytimg.com/vi/{video_id}/maxresdefault.jpg" if video_id else ""))

    if not timecodes:
        return {"found": False, "video_id": video_id, "title": btitle, "thumbnail": thumb}

    return {"found": True, "video_id": video_id, "title": btitle,
            "thumbnail": thumb, "timecodes": timecodes}


def _sec_to_ts(sec: int) -> str:
    h, rem = divmod(sec, 3600)
    m, s   = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


# ── CUE download ──────────────────────────────────────────────────────────────

@router.get("/cue")
async def download_cue(
    pid:    str = Query(...),
    title:  str = Query("BBC Mix"),
    artist: str = Query("BBC Radio"),
    *,
    request: Request,
):
    tracks = await _cue_tracks(pid, title, artist, net_1001=not _is_guest(request))
    if not tracks:
        raise HTTPException(404, "No tracklist found for this episode")
    safe = _safe(title) or "bbc-cue"
    return Response(
        content=_build_cue(title, artist, tracks, pid=pid).encode("utf-8"),
        media_type="text/plain; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{safe}.cue"'},
    )


def _build_cue(title: str, artist: str, tracks: list[dict], *, pid: str = "",
               kbps: int = 0, source_kbps: int = 0) -> str:
    # Имя файла в CUE — то, которое реально легло на диск: заголовок выпуска
    # чистится от запрещённых символов тем же _safe, что и при скачивании,
    # иначе cue-плеер искал бы файл, которого нет.
    #
    # REM-строки — паспорт записи: по ним планировщик и прогноз видят, что этот
    # выпуск уже скачан, и каким битрейтом его действительно кодировали
    # (название папки и расширения об этом молчат).
    lines = [
        f'TITLE "{title}"',
        f'PERFORMER "{artist}"',
    ]
    if pid:
        lines.append(f"REM BBC_PID {pid}")
    if kbps:
        lines.append(f"REM BBC_KBPS {kbps}")
    if source_kbps:
        lines.append(f"REM BBC_SOURCE_KBPS {source_kbps}")
    lines.append(f'FILE "{_safe(title) or title}.mp3" MP3')
    for i, t in enumerate(tracks, 1):
        s  = t.get("offset", 0)
        mm = s // 60
        ss = s % 60
        lines += [
            f"  TRACK {i:02d} AUDIO",
            f'    TITLE "{t.get("title","")}"',
            f'    PERFORMER "{t.get("artist", artist)}"',
            f"    INDEX 01 {mm:02d}:{ss:02d}:00",
        ]
    return "\n".join(lines)
