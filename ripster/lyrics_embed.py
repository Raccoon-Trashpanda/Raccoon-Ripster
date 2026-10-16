"""Тексты в файлах для ВСЕХ сервисов: лучший найденный вариант — в теги и в `.lrc`.

Зачем (12.10.2026, гость бота: «syllable synced lyrics & pronunciation, вшитые в
треки»). Apple-загрузчик (Go) кладёт текст сам. Qobuz, Deezer, Tidal, Spotify и
остальные приходили без него либо с голым текстом. Здесь после загрузки берём
лестницу `lyrics_ladder` (пословный > построчный > голый, любой источник, в том
числе Apple по ISRC и NetEase) и пишем результат в сам файл и рядом.

Что пишем:
  (в теги всегда идёт ПОСТРОЧНЫЙ LRC; пословный «enhanced» — только в `.lrc` рядом)
  .flac/.ogg/.opus — `LYRICS` = LRC построчный, плюс
                     `UNSYNCEDLYRICS` = голый текст;
  .m4a/.mp4        — `©lyr` = LRC (так же делает Apple-загрузчик);
  .mp3             — `USLT` = голый текст и `SYLT` = построчные тайминги;
  рядом            — `<имя>.lrc` (если включено save-lrc-file).

Звук не трогаем: меняются только теги (ripster-audio-integrity). Уже вшитую
синхронизацию не затираем худшей; существующий `.lrc` не перезаписываем.
Исключения наружу не выходят: текст — приятное дополнение, а не повод ронять
задачу.
"""
from __future__ import annotations

import asyncio
import re
from pathlib import Path
from typing import Awaitable, Callable, Optional

from ripster.lyrics_netease import words_to_enhanced_lrc

_RE_STAMP = re.compile(r"^\[(\d+):(\d+(?:\.\d+)?)\]")
_RE_ANY_STAMP = re.compile(r"\[\d+:\d+(?:\.\d+)?\]|<\d+:\d+(?:\.\d+)?>")

# Источники, где искать слова бессмысленно: DJ-миксы и эфиры.
SKIP_SERVICES = {"bbc", "bbc_live", "soundcloud", "mixcloud"}

_AUDIO = {".flac", ".m4a", ".mp4", ".mp3", ".ogg", ".opus"}


def _stamp_sec(line: str) -> Optional[float]:
    m = _RE_STAMP.match(line)
    return int(m.group(1)) * 60 + float(m.group(2)) if m else None


def merge_pronunciation(lrc: str, roma: str, tol: float = 0.35) -> str:
    """Вставляет строку произношения сразу после строки оригинала с тем же временем.

    Так же ведёт себя Apple-загрузчик с `--lyrics-extra=pronunciation`: оригинал
    остаётся, произношение идёт второй строкой. Строки без пары не теряются.
    """
    rom: list[tuple[float, str]] = []
    for ln in (roma or "").splitlines():
        t = _stamp_sec(ln.strip())
        if t is not None and _RE_ANY_STAMP.sub("", ln).strip():
            rom.append((t, ln.strip()))
    if not rom:
        return lrc
    out, used = [], set()
    for ln in (lrc or "").splitlines():
        out.append(ln)
        t = _stamp_sec(ln.strip())
        if t is None:
            continue
        for i, (rt, rl) in enumerate(rom):
            if i not in used and abs(rt - t) <= tol:
                used.add(i)
                out.append(rl)
                break
    return "\n".join(out)


def compose(got: dict, pronunciation: bool = False, words_ok: bool = True) -> dict:
    """Ответ лестницы → {'lrc','plain','synced_lines','kind'}; kind: word|line|plain|''."""
    # Пословная разметка нужна только тем, кто её просил: плеер без поддержки
    # «enhanced LRC» покажет метки <00:12.50> прямо в тексте. Построчная синхра
    # у такого результата всегда есть (`synced` строится из слов).
    words = (got.get("words") or []) if words_ok else []
    synced = (got.get("synced") or "").strip()
    plain = (got.get("plain") or "").strip()
    if words:
        lrc, kind = words_to_enhanced_lrc(words), "word"
    elif synced:
        lrc, kind = synced, "line"
    elif plain:
        return {"lrc": "", "plain": plain, "synced_lines": [], "kind": "plain"}
    else:
        return {"lrc": "", "plain": "", "synced_lines": [], "kind": ""}
    if pronunciation and got.get("roma"):
        lrc = merge_pronunciation(lrc, got["roma"])
    if not plain:
        plain = "\n".join(_RE_ANY_STAMP.sub("", l).strip() for l in lrc.splitlines()).strip()
    # Для тегов — построчный вариант: метки <мм:сс> перед словами плеер без
    # поддержки enhanced LRC покажет мусором. Пословный остаётся только в `lrc`
    # (его кладём рядом файлом).
    lrc_line = "\n".join(re.sub(r"<\d+:\d+(?:\.\d+)?>", "", l) for l in lrc.splitlines()) \
        if kind == "word" else lrc
    lines = []
    for ln in lrc.splitlines():
        t = _stamp_sec(ln.strip())
        txt = _RE_ANY_STAMP.sub("", ln).strip()
        if t is not None and txt:
            lines.append((txt, int(t * 1000)))
    return {"lrc": lrc, "lrc_line": lrc_line, "plain": plain,
            "synced_lines": lines, "kind": kind}


def _tags_of(path: Path) -> dict:
    """artist/title/album/isrc/duration из самого файла."""
    import mutagen
    a = mutagen.File(str(path), easy=True)
    if a is None:
        return {}
    def g(*keys):
        for k in keys:
            v = (a.tags or {}).get(k)
            if v:
                return str(v[0] if isinstance(v, list) else v).strip()
        return ""
    return {"artist": g("artist", "albumartist"), "title": g("title"),
            "album": g("album"), "isrc": g("isrc"),
            "duration": int(getattr(a.info, "length", 0) or 0)}


def _existing_text(path: Path) -> str:
    """Что уже вшито в файл (любой формат), пусто — ничего.

    Ключи пробуем по одному и каждый в своём try: у Vorbis-тегов ключ `©lyr`
    недопустим и `get` бросает исключение — один общий try прятал бы тогда и
    нормальный `LYRICS`, и уже вшитый текст не находился.
    """
    try:
        import mutagen
        a = mutagen.File(str(path), easy=False)
        if a is None or not a.tags:
            return ""
        tg = a.tags
    except Exception:
        return ""
    for k in ("©lyr", "LYRICS", "lyrics", "UNSYNCEDLYRICS", "unsyncedlyrics"):
        try:
            v = tg.get(k)
        except Exception:
            continue
        if v:
            return str(v[0] if isinstance(v, list) else v)
    try:
        for k in list(tg.keys()):
            if str(k).startswith(("USLT", "SYLT")):
                fr = tg[k]
                return str(getattr(fr, "text", "") or "[sylt]")
    except Exception:
        pass
    return ""


def embed_into_file(path: Path, comp: dict) -> bool:
    """Пишет текст в теги. True — записали. Звуковые данные не затрагиваются."""
    import mutagen
    suf = path.suffix.lower()
    lrc, plain = comp.get("lrc_line", comp["lrc"]), comp["plain"]
    if suf in (".flac", ".ogg", ".opus"):
        a = mutagen.File(str(path))
        if a is None:
            return False
        a["LYRICS"] = [lrc or plain]
        if plain:
            a["UNSYNCEDLYRICS"] = [plain]
        a.save()
        return True
    if suf in (".m4a", ".mp4"):
        from mutagen.mp4 import MP4
        a = MP4(str(path))
        a["\xa9lyr"] = [lrc or plain]
        a.save()
        return True
    if suf == ".mp3":
        from mutagen.id3 import ID3, ID3NoHeaderError, USLT, SYLT, Encoding
        try:
            t = ID3(str(path))
        except ID3NoHeaderError:
            t = ID3()
        t.delall("USLT")
        t.add(USLT(encoding=Encoding.UTF8, lang="eng", desc="", text=plain or lrc))
        if comp["synced_lines"]:
            t.delall("SYLT")
            t.add(SYLT(encoding=Encoding.UTF8, lang="eng", format=2, type=1,
                       desc="", text=comp["synced_lines"]))
        t.save(str(path))
        return True
    return False


async def embed_for_files(files: list, ladder: Callable[..., Awaitable[dict]], *,
                          want_embed: bool = True, want_file: bool = True,
                          pronunciation: bool = False, words_ok: bool = True,
                          concurrency: int = 3,
                          per_track_timeout: float = 40.0,
                          total_budget: float = 240.0,
                          log: Callable[[str], None] = print) -> dict:
    """Находит и вшивает текст. Возвращает счётчики для честного отчёта."""
    stats = {"total": 0, "embedded": 0, "kept": 0, "none": 0, "failed": 0,
             "skipped_budget": 0, "word": 0, "line": 0, "plain": 0}
    sem = asyncio.Semaphore(max(1, concurrency))
    loop = asyncio.get_running_loop()
    deadline = loop.time() + total_budget
    audio = [Path(f) for f in files if Path(f).suffix.lower() in _AUDIO]
    stats["total"] = len(audio)

    async def one(p: Path) -> None:
        async with sem:
            if loop.time() > deadline:
                stats["skipped_budget"] += 1
                return
            try:
                sidecar = p.with_suffix(".lrc")
                have = await asyncio.to_thread(_existing_text, p)
                # Уже есть синхронизация (Apple-загрузчик, прошлый прогон) — не лезем.
                if _RE_ANY_STAMP.search(have) or have == "[sylt]" or \
                        (sidecar.is_file() and sidecar.stat().st_size > 0):
                    stats["kept"] += 1
                    return
                tg = await asyncio.to_thread(_tags_of, p)
                if not tg.get("title"):
                    stats["none"] += 1
                    return
                got = await asyncio.wait_for(
                    ladder(artist=tg["artist"], track=tg["title"], album=tg["album"],
                           duration=tg["duration"], isrc=tg["isrc"]),
                    timeout=per_track_timeout)
                comp = compose(got or {}, pronunciation, words_ok)
                if not comp["kind"]:
                    stats["none"] += 1
                    return
                # Голый текст не заменяет уже вшитый голый — только дополняет пустое.
                if comp["kind"] == "plain" and have:
                    stats["kept"] += 1
                    return
                # Пословный текст живёт ТОЛЬКО рядом (`.lrc`): в теги идёт построчный.
                # Поэтому для него файл пишем, даже если save-lrc-file выключен.
                side = comp["lrc"] and (want_file or (want_embed and comp["kind"] == "word"))
                if side:
                    await asyncio.to_thread(sidecar.write_text, comp["lrc"], "utf-8")
                ok = False
                if want_embed:
                    ok = await asyncio.to_thread(embed_into_file, p, comp)
                if ok or side:
                    stats["embedded"] += 1
                    stats[comp["kind"]] += 1
                else:
                    stats["none"] += 1
            except Exception as e:  # noqa: BLE001
                stats["failed"] += 1
                log(f"[lyrics-embed] {p.name}: {type(e).__name__}: {e}")

    await asyncio.gather(*(one(p) for p in audio))
    return stats
