"""Одна папка — один релиз.

Два разных релиза могут иметь одинаковое имя папки: «NORTHERN EXPOSURE REDUX»
Sasha & John Digweed — MIXED-издание (qobuz uvxkonsapjsbb) и UNMIXED (qobuz
e6r7vcsf378jq), один и тот же год/артист/качество. Поток движка (streamrip)
пишет файл открытием 'wb' по имени трека — вторая загрузка молча СТИРАЕТ файлы
первой, а авто-чистка по записи одной задачи сносит ВСЮ папку с чужими файлами
(24.09.2026 так потеряли уже скачанный релиз).

Здесь две стороны одной гарантии:
  * `folder_suffix_for` — движок ДО старта дописывает в имя папки издание
    (version/edition релиза), а если его нет — короткий идент релиза;
  * `separate_collided_files` — страховка ПОСЛЕ загрузки: если чужой релиз всё
    же сидит в той же папке, переносим ТОЛЬКО файлы этой задачи в соседнюю
    папку с суффиксом. Чужие файлы не удаляются и не перезаписываются никогда.
"""
from __future__ import annotations

import os
import re
import time
from pathlib import Path

_AUDIO = {".flac", ".m4a", ".mp3", ".wav", ".ogg", ".opus", ".aac", ".aiff", ".alac"}

# суффикс издания/релиза в конце имени папки: «… [Mixed]», «… [qobuz-uvxkonsa]».
# Качество в скобках («[FLAC] [24B-44.1kHz]») суффиксом НЕ считается.
_RE_SUFFIX = re.compile(r'\s*\[(?!\d+B-|FLAC\b|MP3\b|AAC\b|ALAC\b)[^\[\]]+\]\s*$')

# «Свежая» чужая задача — ещё качает в эту папку; трогать её файлы нельзя.
_FRESH_FOREIGN_SEC = 4 * 3600


def base_of(name: str) -> str:
    """Имя папки без последнего суффикса издания/релиза."""
    m = _RE_SUFFIX.search(name or "")
    return (name[: m.start()] if m else name).strip()


def fmt_suffix(service: str, rid: str, version: str = "") -> str:
    """Как именно различаем издания: сначала человекочитаемое издание, потом идент."""
    v = re.sub(r'[\\/:*?"<>|\[\]]', " ", (version or "")).strip()
    v = re.sub(r'\s+', " ", v)
    if v:
        return f" [{v[:60]}]"
    rid = (rid or "")[:13]
    return f" [{service.lower()[:8]}-{rid}]" if rid else ""


def folder_suffix_for(service: str, url: str, base_name: str,
                      version: str = "", exclude_task_id: str = "") -> str:
    """'' | ' [Mixed]' | ' [qobuz-uvxkonsa]' — хвост, который надо дописать к
    имени папки, чтобы этот релиз не врезался в уже скачанный другой.

    Правило: есть у релиза version/edition — дописываем его ВСЕГДА (тогда
    коллизия не нужна и в будущем); нет — только если папка с таким именем уже
    занята ДРУГИМ релизом (манифест или файлы на диске)."""
    if version:
        return fmt_suffix(service, "", version)
    from ripster import download_manifest as _dm
    rid = _dm.release_id_of(url)
    if not rid:
        return ""
    for _tid, ent in _dm.dir_claims_by_base(base_name, exclude_task_id=exclude_task_id):
        e_id = ent.get("engine_id") or _dm.release_id_of(ent.get("url") or "")
        if e_id and e_id != rid:
            return fmt_suffix(service, rid, "")
    try:
        if base_name:
            base_dir = Path(base_name)
            if base_dir.is_dir() and any(
                    f.suffix.lower() in _AUDIO
                    for f in base_dir.iterdir() if f.is_file()):
                return fmt_suffix(service, rid, "")
    except Exception:
        pass
    return ""


def foreign_claims(d: Path, task_id: str, service: str, rid: str) -> list:
    """Записи манифеста OTHER релизов, сидящих в этой папке (или вложенно)."""
    from ripster import download_manifest as _dm
    out = []
    for tid, ent in _dm.dir_claims(d, exclude_task_id=task_id):
        e_url = ent.get("url") or ""
        e_id = ent.get("engine_id") or _dm.release_id_of(e_url)
        if e_id and rid and e_id == rid:
            continue          # тот же релиз — повтор той жедачи
        if not e_id and e_url and rid and rid in e_url:
            continue
        out.append((tid, ent))
    return out


_SIDECARS = {"cover.jpg", "cover.png", "folder.jpg", "discogs.yaml"}


def separate_collided_files(task: dict, d, service: str, url: str) -> "Path | None":
    """Если папка `d` делит с другим релизом — увести СВОИ файлы в соседнюю
    папку «<имя><суффикс>», вернуть новую папку (или None — трогать нечего).
    Ничего не удаляет. Файлы, которые называет чужая запись манифеста, НЕ
    переносим: список соседа защищает соседа (он сам мог быть замусорен слепым
    glob'ом — поэтому исключение одностороннее), всё остальное считаем своим."""
    d = Path(d)
    try:
        from ripster import download_manifest as _dm
        rid = _dm.release_id_of(url or task.get("url") or "")
        claims = foreign_claims(d, task.get("id") or "", service, rid)
    except Exception:
        return None
    if not claims:
        return None
    now = time.time()
    if any(now - (ent.get("ts") or 0) < _FRESH_FOREIGN_SEC for _t, ent in claims):
        # сосед дописывает папку прямо сейчас — перенос устроит гонку;
        # решение примет его финальный проход (страховка симметрична)
        return None
    suffix = fmt_suffix(service, rid, "")
    if not suffix:
        return None
    owned = {str(fn).lower() for _t, ent in claims for fn in (ent.get("files") or [])}
    dst = d.with_name(d.name + suffix)
    try:
        dst.mkdir(parents=True, exist_ok=True)
    except Exception:
        return None
    n = 0
    try:
        srcs = [p for p in d.iterdir() if p.is_file()]
    except Exception:
        srcs = []
    for src in srcs:
        low = src.name.lower()
        if low in owned:
            continue                                  # чужой по манифесту — не трогаем
        if src.suffix.lower() not in _AUDIO and low not in _SIDECARS:
            continue                                  # служебное (маркеры/кэш) остаётся
        try:
            tgt = dst / src.name
            if tgt.exists():
                continue                              # и в новой папке не перетираем
            os.rename(src, tgt)
            n += 1
        except Exception:
            pass
    if not n:
        try:
            dst.rmdir()
        except Exception:
            pass
        return None
    return dst
