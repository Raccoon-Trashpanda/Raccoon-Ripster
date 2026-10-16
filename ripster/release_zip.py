"""Упаковка скачанного релиза в zip: «Артист - Название [качество].zip».

Включается ключом `zip-releases` или флагом `zip` в задаче (задача главнее).
Архив кладётся РЯДОМ с папкой релиза, сама папка остаётся: боту и плееру нужны
распакованные файлы. ZIP_STORED без сжатия — аудио уже сжато, DEFLATE только
жжёт процессор. Имя архива и качество берутся из самих файлов, а не из
запрошенного пресета (заказали FLAC, а Deezer отдал MP3 — на архиве должно стоять MP3).
"""
from __future__ import annotations

import re
import zipfile
from pathlib import Path
from typing import Iterable, Optional

_UNSAFE = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_SIDE_EXT = {".lrc", ".jpg", ".jpeg", ".png", ".cue", ".txt", ".log"}


def _clean(s: str) -> str:
    out = _UNSAFE.sub("_", s or "").strip(". ")
    return re.sub(r"\s+", " ", out)


def file_quality(path: Path) -> str:
    """«FLAC 24-96», «FLAC 16-44.1», «MP3 320», «ALAC 24-48», «AAC 256» по самому файлу."""
    try:
        import mutagen
        a = mutagen.File(str(path))
        if a is None:
            return ""
        info = a.info
        suf = path.suffix.lower()
        khz = (getattr(info, "sample_rate", 0) or 0) / 1000
        khz_s = f"{khz:g}" if khz else ""
        if suf == ".flac":
            bits = getattr(info, "bits_per_sample", 0) or 16
            return f"FLAC {bits}-{khz_s}".rstrip("-")
        if suf == ".mp3":
            kbps = round((getattr(info, "bitrate", 0) or 0) / 1000)
            for std in (320, 256, 192, 160, 128):    # VBR/неточные → ближайшая ступень
                if abs(kbps - std) <= 8:
                    kbps = std
                    break
            return f"MP3 {kbps}" if kbps else "MP3"
        if suf in (".m4a", ".mp4"):
            codec = str(getattr(info, "codec", "") or "").lower()
            if "alac" in codec:
                bits = getattr(info, "bits_per_sample", 0) or 16
                return f"ALAC {bits}-{khz_s}".rstrip("-")
            kbps = round((getattr(info, "bitrate", 0) or 0) / 1000)
            return f"AAC {kbps}" if kbps else "AAC"
        if suf in (".ogg", ".opus"):
            kbps = round((getattr(info, "bitrate", 0) or 0) / 1000)
            return f"{suf[1:].upper()} {kbps}" if kbps else suf[1:].upper()
    except Exception:  # noqa: BLE001
        pass
    return ""


def release_quality(audio: Iterable[Path]) -> str:
    """Одно качество на релиз; если треки разные — самое низкое из встретившихся честно перечислено."""
    seen: list[str] = []
    for f in audio:
        q = file_quality(Path(f))
        if q and q not in seen:
            seen.append(q)
    if not seen:
        return ""
    return seen[0] if len(seen) == 1 else " + ".join(seen[:3])


def _tags(path: Path) -> tuple[str, str]:
    """(артист альбома, название альбома) из первого файла."""
    try:
        import mutagen
        a = mutagen.File(str(path), easy=True)
        t = (a.tags or {}) if a is not None else {}

        def g(*keys):
            for k in keys:
                v = t.get(k)
                if v:
                    return str(v[0] if isinstance(v, list) else v).strip()
            return ""
        return g("albumartist", "artist"), g("album", "title")
    except Exception:  # noqa: BLE001
        return "", ""


def archive_name(audio: list[Path], meta: Optional[dict] = None, fallback: str = "release") -> str:
    """«Артист - Название [качество].zip»."""
    meta = meta or {}
    artist, title = _tags(Path(audio[0])) if audio else ("", "")
    artist = artist or str(meta.get("artist") or "")
    title = title or str(meta.get("title") or meta.get("album") or "")
    base = " - ".join(p for p in (_clean(artist), _clean(title)) if p) or _clean(fallback) or "release"
    q = _clean(release_quality(audio))
    name = f"{base} [{q}]" if q else base
    return name[:150].rstrip(". ") + ".zip"


def pack_release(audio: list[Path], base_dir: Path, meta: Optional[dict] = None,
                 fallback: str = "release", out_dir: Optional[Path] = None) -> Optional[Path]:
    """Собирает архив рядом с `base_dir`. Возвращает путь или None, если паковать нечего.

    В архив идут аудио задачи и «боковые» файлы из той же папки (обложка, .lrc,
    .cue, лог). Структура папок (CD 1/…) сохраняется. `out_dir` — класть архив туда (бот: временная папка, после отправки удаляется).
    Существующий архив с тем же
    именем не затирается: добавляется « (2)».
    """
    audio = [Path(f) for f in audio if Path(f).is_file()]
    if not audio:
        return None
    base_dir = Path(base_dir)
    members: list[Path] = list(audio)
    stems = {f.with_suffix("").name.lower() for f in audio}
    roots = {f.parent for f in audio}
    for r in roots:
        for s in r.iterdir():
            if not s.is_file() or s in members or s.suffix.lower() not in _SIDE_EXT:
                continue
            # обложка/лог относятся к релизу целиком, .lrc — только к своему треку
            if s.suffix.lower() == ".lrc" and s.with_suffix("").name.lower() not in stems:
                continue
            members.append(s)
    if out_dir is not None:
        target_dir = Path(out_dir)
        target_dir.mkdir(parents=True, exist_ok=True)
    else:
        target_dir = base_dir.parent if base_dir.parent.is_dir() else base_dir
    name = archive_name(audio, meta, fallback or base_dir.name)
    target = target_dir / name
    n = 2
    while target.exists():
        target = target_dir / f"{name[:-4]} ({n}).zip"
        n += 1
    tmp = target.with_suffix(".zip.part")
    try:
        with zipfile.ZipFile(tmp, "w", zipfile.ZIP_STORED) as zf:
            seen: set[str] = set()
            for f in members:
                try:
                    arc = str(f.relative_to(base_dir)).replace("\\", "/")
                except ValueError:
                    arc = f.name
                if arc in seen:
                    continue
                seen.add(arc)
                zf.write(str(f), arc)
        tmp.replace(target)
    finally:
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                pass
    return target
