"""Обложка релиза в максимальном разрешении, которое отдаёт сам сервис.

Движки кладут в папку релиза обложку того размера, что задан в конфиге
(Apple — `cover-size: 1000x1000`), и её же вшивают в теги: огромная картинка в
каждом треке — лишние мегабайты. Оригинал отдельным файлом нужен «перфекционистам»
(14.10.2026: гость спросил, прилетят ли обложки Apple в максимальном разрешении,
а прилетали 1000×1000).

Адреса проверены живьём 14.10.2026:
  Apple  …/{w}x{h}bb.jpg → …/9999x9999-100.jpg  — исходник целиком (не растягивает
         больше оригинала: обычно 3000×3000), JPEG качества 100;
  Qobuz  …_600.jpg       → …_org.jpg            — оригинал (часто 4000×4000);
  Tidal  …/1280x1280.jpg → …/origin.jpg         — оригинал (часто 4000×4000);
  Deezer …/500x500-000000-80-0-0.jpg → 1900x1900-000000-100-0-0.jpg — потолок CDN,
         3000 отвечает 403.
Огромные размеры у Apple (100000x100000) теперь 400 — не использовать.
"""
from __future__ import annotations

import re
import tempfile
from pathlib import Path

_APPLE = re.compile(r"(mzstatic\.com/image/thumb/.+?)/(?:\{w\}x\{h\}|\d+x\d+)[a-z]{0,3}(?:-\d+)?\.(?:jpg|jpeg|png|webp)$", re.I)
_QOBUZ = re.compile(r"(static\.qobuz\.com/images/covers/.+?)_(?:\d+|max|small|large|org)\.jpg$", re.I)
_DEEZER = re.compile(r"(dzcdn\.net/images/cover/[0-9a-f]+)/\d+x\d+-[^/]+\.jpg$", re.I)
_TIDAL = re.compile(r"(resources\.tidal\.com/images/.+?)/(?:\d+x\d+|origin)\.jpg$", re.I)


def candidates(url: str) -> list[str]:
    """Адреса от самого большого к запасным. Пусто — сервис не знаем, трогать нечего."""
    u = (url or "").strip()
    if m := _APPLE.search(u):
        b = u[:m.start(1)] + m.group(1)
        return [f"{b}/9999x9999-100.jpg", f"{b}/3000x3000bb.jpg"]
    if m := _QOBUZ.search(u):
        b = u[:m.start(1)] + m.group(1)
        return [f"{b}_org.jpg", f"{b}_max.jpg"]
    if m := _TIDAL.search(u):
        b = u[:m.start(1)] + m.group(1)
        return [f"{b}/origin.jpg", f"{b}/1280x1280.jpg"]
    if m := _DEEZER.search(u):
        b = u[:m.start(1)] + m.group(1)
        return [f"{b}/1900x1900-000000-100-0-0.jpg", f"{b}/1000x1000-000000-80-0-0.jpg"]
    return []


def image_size(path: Path) -> tuple[int, int]:
    try:
        from PIL import Image
        with Image.open(path) as im:
            return im.size
    except Exception:  # noqa: BLE001
        return 0, 0


async def fetch_max(url: str, timeout: float = 30.0) -> "tuple[Path, int, int] | None":
    """Скачивает первый рабочий кандидат во временный файл → (путь, ширина, высота).
    Файл удаляет вызывающий. None — ни один адрес не отдал картинку."""
    import httpx
    for cand in candidates(url):
        try:
            async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as cl:
                r = await cl.get(cand)
            if r.status_code != 200 or not r.headers.get("content-type", "").startswith("image/"):
                continue
            ext = ".png" if "png" in r.headers.get("content-type", "") else ".jpg"
            fd, name = tempfile.mkstemp(prefix="ripster_cover_", suffix=ext)
            p = Path(name)
            with open(fd, "wb") as f:
                f.write(r.content)
            w, h = image_size(p)
            if w and h:
                return p, w, h
            p.unlink(missing_ok=True)
        except Exception:  # noqa: BLE001
            continue
    return None
