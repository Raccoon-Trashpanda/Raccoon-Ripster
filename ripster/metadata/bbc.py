"""BBC Sounds metadata — populates the queue/history card (title, artist,
cover) at enqueue time, before the download even starts. Same programmes.json
endpoint the download preflight (ripster.runner._bbc_preflight) re-queries
right before the actual fetch — cheap, public, no auth.

Заодно — единственный в проекте источник знания о том, какие размеры отдаёт
BBC-картинковый CDN (см. COVER_LADDER). Пуляем в `relCover` на фронте той же
лесенкой, чтобы адрес обложки рос/усыхал по одному правилу.
"""
from __future__ import annotations

import re

import httpx

# Тот же провод, что в runner._RE_BBC_PID: принимается и /sounds/play/<pid>, и
# /programmes/<pid> (одна передача, один pid). Иначе карта очереди для ссылок из
# радара остаётся пустой (fetch_meta_bbc вернёт None), хотя качается потом успешно.
_RE_PID = re.compile(r'/(?:sounds/play|programmes|iplayer)/([a-zA-Z0-9]+)')

# ichef.bbci.co.uk принимает НЕ произвольный размер, а дискретную лесенку.
# Промер 21.09.2026 на живом объекте (p0m0slhx.jpg, 42 значения): отдают 200
# 32/64/96/112/128/160/192/224/240/256/288/304/320/352/384/400/416/448/480/512/
# 576/624/640/672/704/736/768/800/…/992/1024/1104/1200/1280/1440/1600/1920;
# отвечают 403 — 120, 540, 600, 900, 1000. Ровно `600x600` мы и строили в
# artworkUrl, поэтому у BBC в очереди и истории обложки не было никогда: URL
# оказывался заведомо мёртвым, а не «почти живым».
# В лесенку ниже вошли только проверенные значения, снятые ступеньками ~×2.
# Потолок поднят до 1280 по слову владельца (21.09.2026): «обложки идут от
# 320 и кратно, например 1280×1280 будет норм нам». Проверено на живом объекте
# — 1280 отдаёт ровно 1280×1280 и весит ~311 КБ. Более крупные (1440/1600/1920)
# тоже живые, но в теги файла их класть незачем.
COVER_LADDER = (64, 128, 240, 320, 480, 640, 768, 1024, 1280)

CARD_PX     = 320    # сетки и карточка очереди: правило дома — грузим мелкое
EMBED_PX    = 1280   # обложка в теги файла
LIGHTBOX_PX = 1000   # зум; 1000×1000 у BBC нет, ближайший живой — 1024 (см. 403 выше)


def cover_px(px: int) -> int:
    """Ближайший живой размер >= запрошенного (или потолок лесенки)."""
    for n in COVER_LADDER:
        if n >= int(px or 0):
            return n
    return COVER_LADDER[-1]


def ichef(img_pid: str, px: int = CARD_PX) -> str:
    """Адрес обложки BBC по pid картинки. Пустой pid — пустая строка, а не битый
    URL: интерфейс честно покажет нейтральную иконку вместо серого квадрата."""
    if not img_pid:
        return ""
    n = cover_px(px)
    return f"https://ichef.bbci.co.uk/images/ic/{n}x{n}/{img_pid}.jpg"


def _pid_from_url(url: str) -> str:
    """pid картинки из готового адреса ichef (нужен, чтобы пересобрать другой
    размер). Сегмент размера — не только «600x600»: BBC раздаёт и «480xn».
    Без этого resize() молча возвращал чужой размер и обложка не росла."""
    m = re.search(r'/images/ic/[^/]+/([A-Za-z0-9]+)\.(?:jpg|png|webp)', url or "")
    return m.group(1) if m else ""


def resize(url: str, px: int) -> str:
    """Переставить размер в уже готовом ichef-адресе по лесенке. Чужой адрес —
    как есть (фронт сам решает, что с ним делать)."""
    pid = _pid_from_url(url)
    return ichef(pid, px) if pid else (url or "")


_JPEG_MAGIC = b"\xff\xd8\xff"
_PNG_MAGIC  = b"\x89PNG"


def attach_artwork(folder, image_url: str) -> int:
    """Обложка выпуска — в папку (cover.jpg) и в теги каждого аудиофайла.

    Движок BBC (yt-dlp + ffmpeg) сам картинки не пишет, а общая пост-обработка
    очереди (`runner._apply_cover_to_folder`) достаёт обложку ИЗ тегов — то есть
    для BBC ей брать нечего. Возвращает число файлов, куда обложка легла.
    Молча 0 при любой неудаче: отсутствие обложки не имеет права валить загрузку.
    """
    from pathlib import Path

    url = resize(image_url, EMBED_PX)
    if not url:
        return 0
    try:
        folder = Path(folder)
        r = httpx.get(url, timeout=20, follow_redirects=True)
        data = r.content if r.status_code == 200 else b""
        if not data or not (data.startswith(_JPEG_MAGIC) or data.startswith(_PNG_MAGIC)):
            return 0
        try:
            (folder / "cover.jpg").write_bytes(data)
        except Exception:
            pass                       # sidecar — косметика, не условие встраивания
        mime = "image/png" if data.startswith(_PNG_MAGIC) else "image/jpeg"
        from ripster.tagger import embed_cover
        n = 0
        for p in sorted(folder.iterdir()):
            if p.suffix.lower() in (".mp3", ".m4a", ".aac", ".flac", ".ogg", ".opus"):
                try:
                    if embed_cover(p, data, mime):
                        n += 1
                except Exception:
                    pass
        return n
    except Exception:
        return 0



async def fetch_meta_bbc(url: str) -> dict | None:
    m = _RE_PID.search(url or "")
    if not m:
        return None
    pid = m.group(1)
    try:
        async with httpx.AsyncClient(timeout=10) as c:
            r = await c.get(f"https://www.bbc.co.uk/programmes/{pid}.json")
            if r.status_code != 200:
                return None
            prog = (r.json() or {}).get("programme") or {}
    except Exception:
        return None

    title  = prog.get("title") or (prog.get("display_title") or {}).get("title") or pid
    parent = (prog.get("parent") or {}).get("programme") or {}
    artist = (parent.get("title")
              or ((prog.get("ownership") or {}).get("service") or {}).get("title")
              or "BBC Radio")
    # У выпуска своей картинки не бывает (редкие страницы) — берём картинку
    # передачи-брэнда: обложка шоу честнее серого квадрата и точно не чужая.
    img_pid = ((prog.get("image") or {}).get("pid")
               or (parent.get("image") or {}).get("pid") or "")
    cover   = ichef(img_pid, CARD_PX)
    versions = prog.get("versions") or []
    duration = int((versions[0].get("duration") if versions else 0) or 0)
    date = (prog.get("first_broadcast_date") or "")[:10]

    return {
        "id":          pid,
        "type":        "episode",
        "title":       title,
        "artist":      artist,
        "album":       artist,
        "year":        date[:4],
        "date":        date,
        "artworkUrl":  cover,
        "duration":    duration * 1000,
        "trackCount":  1,
        "totalTracks": 1,
        "service":     "bbc",
    }
