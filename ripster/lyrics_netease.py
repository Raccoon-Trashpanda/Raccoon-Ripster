"""NetEase Cloud Music: пословный текст (YRC) и романизация, без авторизации.

Зачем (12.10.2026, гость бота: «syllable synced lyrics & pronunciation»).
Apple остаётся самым точным источником пословного текста, но он доступен только
при живой подписке и токенах. NetEase отдаёт ту же пословную разметку анонимно:
`/api/song/lyric?yv=1` кладёт в ответ поле `yrc`, а `romalrc` — построчную
романизацию (произношение) для китайского/японского/корейского. Проверено живьём
12.10.2026 на «Harleys In Hawaii» — Katy Perry: yrc есть, пословные тайминги.

Формат ответа тот же, что у `apple_lyrics.word_lyrics`: `lines=[{s,e,words:
[{t,d,w,sp}]}]` в миллисекундах, чтобы плеер и мобилка не знали, откуда слова.

Исключения наружу не выпускаем: упавший источник обязан пропустить ход лестницы.
"""
from __future__ import annotations

import re
from typing import Any

import httpx
from ripster.lyrics_sources import _dur_ok, _same_track, _ms_to_lrc

_BASE = "https://music.163.com"
_HEAD = {"User-Agent": "Mozilla/5.0", "Referer": _BASE + "/"}


def _client() -> "httpx.AsyncClient":
    """Свой клиент на каждый вызов. Общий процесс-клиент копит куки NetEase, и
    со второго запроса сервис молча отвечает пусто (проверено 12.10.2026: первый
    вызов находит текст, следующие пять — нет). Та же ловушка, что у Deezer."""
    return httpx.AsyncClient(timeout=20, follow_redirects=True, headers=_HEAD)

# Строка YRC: `[начало,длина](t,d,0)слово(t,d,0)слово`. Пробел между словами
# NetEase кладёт в хвост самого слова, поэтому `sp` берём оттуда.
_RE_LINE = re.compile(r"^\[(\d+),(\d+)\](.*)$")
_RE_WORD = re.compile(r"\((\d+),(\d+),-?\d+\)([^(]*)")
# Метаданные в начале YRC приходят JSON-строками: {"t":0,"c":[{"tx":"作词: "}]}.
_RE_META = re.compile(r"^\{.*\}$")
# Первые строки с титрами. Срезаем только пока текст ещё не начался: «Lyrics:»
# посреди песни — уже слова.
_RE_CREDIT = re.compile(
    r"^(作词|作曲|编曲|制作人?|混音|母带|词|曲|lyrics?|words?|music|composed?|"
    r"written|arranged?|produced?|producer|mix(ed)?|master(ed)?)\b[^:：]{0,20}[:：]",
    re.I)


def parse_yrc(yrc: str) -> list[dict]:
    """YRC → [{'s','e','words':[{'t','d','w','sp'}]}] (мс). Пусто — не разобрать."""
    lines: list[dict] = []
    for raw in (yrc or "").splitlines():
        raw = raw.strip()
        if not raw or _RE_META.match(raw):
            continue
        m = _RE_LINE.match(raw)
        if not m:
            continue
        s, d, body = int(m.group(1)), int(m.group(2)), m.group(3)
        words: list[dict] = []
        for wm in _RE_WORD.finditer(body):
            txt = wm.group(3)
            if not txt.strip():
                continue
            words.append({"t": int(wm.group(1)), "d": int(wm.group(2)),
                          "w": txt.strip(), "sp": txt != txt.rstrip()})
        if words:
            ln = {"s": s, "e": s + d, "words": words}
            # Шапка с авторами («作词 : …», «Lyrics by : …») — не текст песни.
            if not lines and _RE_CREDIT.match(_line_text(ln)):
                continue
            lines.append(ln)
    return lines


def _line_text(ln: dict) -> str:
    return "".join(w["w"] + (" " if w.get("sp") else "") for w in ln["words"]).strip()


def words_to_lrc(lines: list[dict]) -> str:
    """Построчный LRC из пословных строк — фолбэк для плееров без word-режима."""
    return "\n".join(_ms_to_lrc(l["s"], _line_text(l)) for l in lines)


def words_to_enhanced_lrc(lines: list[dict]) -> str:
    """«Enhanced LRC»: `[mm:ss.xx]<mm:ss.xx>слово <mm:ss.xx>слово` — пословный
    формат, который умеют читать караоке-плееры, когда он лежит рядом с треком."""
    def stamp(ms: int) -> str:
        s, cs = divmod(int(ms) // 10, 100)
        return f"{s // 60:02d}:{s % 60:02d}.{cs:02d}"
    out = []
    for l in lines:
        parts = [f"<{stamp(w['t'])}>{w['w']}" + (" " if w.get("sp") else "")
                 for w in l["words"]]
        out.append(f"[{stamp(l['s'])}]" + "".join(parts).rstrip())
    return "\n".join(out)


async def _find_song_id(c: Any, artist: str, track: str, duration: int) -> str:
    r = await c.get(f"{_BASE}/api/search/get", headers=_HEAD,
                    params={"s": f"{artist} {track}", "type": 1, "limit": 8})
    if r.status_code != 200:
        return ""
    for it in ((r.json() or {}).get("result") or {}).get("songs") or []:
        a = ", ".join(x.get("name", "") for x in it.get("artists") or [])
        # duration в поиске — миллисекунды.
        dur = round((it.get("duration") or 0) / 1000)
        if _same_track(a, it.get("name") or "", artist, track) and _dur_ok(dur, duration):
            return str(it.get("id") or "")
    return ""


async def from_netease(artist: str, track: str, duration: int = 0,
                       track_id: str = "") -> dict:
    """Текст из NetEase: {'synced','plain','words','roma','source'} либо {}.

    `words` есть только если у трека есть YRC; иначе вернётся обычная построчная
    синхра (`lrc`). Романизация `roma` — строка LRC, пустая для латиницы.
    """
    try:
        async with _client() as c:
            if not track_id:
                track_id = await _find_song_id(c, artist, track, duration)
                if not track_id:
                    return {}
            r = await c.get(f"{_BASE}/api/song/lyric", headers=_HEAD,
                            params={"id": track_id, "lv": 1, "kv": 1, "tv": -1,
                                    "rv": 1, "yv": 1})
            if r.status_code != 200:
                return {}
            j = r.json() or {}
            lines = parse_yrc(((j.get("yrc") or {}).get("lyric")) or "")
            lrc = ((j.get("lrc") or {}).get("lyric")) or ""
            roma = ((j.get("romalrc") or {}).get("lyric")) or ""
            if lines:
                return {"synced": words_to_lrc(lines),
                        "plain": "\n".join(_line_text(l) for l in lines),
                        "words": lines, "roma": roma, "source": "NetEase (word)"}
            plain = re.sub(r"\[[^\]]*\]", "", lrc).strip()
            if not plain:
                return {}
            has_ts = bool(re.search(r"\[\d+:\d+", lrc))
            return {"synced": lrc if has_ts else "", "plain": plain,
                    "roma": roma, "source": "NetEase"}
    except Exception:
        return {}
