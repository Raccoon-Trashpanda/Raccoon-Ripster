"""Тексты Musixmatch — пословные (richsync) и построчные, через токен владельца.

Почему с токеном (14.10.2026): анонимный `token.get` у Musixmatch отдаёт
«нулевой» токен (00000…) — проверено с домашнего IP владельца, так было и 12.10.
Конкурент Flowy/LosslessRobot держит токен на своём сервере и раздаёт тексты
через себя (`/api/tiny/musixmatch/lyrics`) — та же схема у нас: токен из
залогиненного Musixmatch кладётся в конфиг (`musixmatch-token`, секрет — гостям
не уходит), сервер ходит с ним. Нет токена — источник молча не участвует.

Ответ — в формате лестницы текстов (как у NetEase/Apple): {synced, plain,
words?, source}; `words` — строки {s, e, words:[{w, t, d, sp}]} в миллисекундах.
"""
from __future__ import annotations

import json
from typing import Any

import httpx

API = "https://apic-desktop.musixmatch.com/ws/1.1/"
APP_ID = "web-desktop-app-v1.0"
_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
       "Musixmatch/0.19.4 Chrome/58.0.3029.110 Electron/1.7.6 Safari/537.36")


def token_from(config: dict | None) -> str:
    t = str((config or {}).get("musixmatch-token") or "").strip()
    return "" if (not t or set(t) == {"0"}) else t


def _ms(sec: Any) -> int:
    try:
        return int(round(float(sec) * 1000))
    except (TypeError, ValueError):
        return 0


def richsync_to_words(body: str | list) -> list[dict]:
    """richsync_body → строки со словами. Формат Musixmatch: [{ts, te, x,
    l:[{c, o}]}], где c — кусок текста (слово или пробел), o — смещение от ts (с)."""
    data = json.loads(body) if isinstance(body, str) else (body or [])
    lines: list[dict] = []
    for ln in data if isinstance(data, list) else []:
        s, e = _ms(ln.get("ts")), _ms(ln.get("te"))
        parts = [p for p in (ln.get("l") or []) if isinstance(p, dict)]
        words: list[dict] = []
        for i, p in enumerate(parts):
            c = str(p.get("c") or "")
            if not c.strip():
                if words:
                    words[-1]["sp"] = True
                continue
            t = s + _ms(p.get("o"))
            nxt = next((s + _ms(q.get("o")) for q in parts[i + 1:] if str(q.get("c") or "").strip()), e)
            words.append({"w": c.strip(), "t": t, "d": max(0, nxt - t), "sp": c != c.rstrip()})
        if words:
            lines.append({"s": s, "e": e or (words[-1]["t"] + words[-1]["d"]), "words": words})
    return lines


def subtitles_to_lrc(body: str | list) -> str:
    """subtitle_body (mxm) → построчный LRC."""
    data = json.loads(body) if isinstance(body, str) else (body or [])
    out = []
    for ln in data if isinstance(data, list) else []:
        tm = (ln.get("time") or {}).get("total")
        if tm is None:
            continue
        ms = _ms(tm)
        out.append(f"[{ms // 60000:02d}:{(ms % 60000) / 1000:05.2f}]{str(ln.get('text') or '').strip()}")
    return "\n".join(out)


def _line_text(ln: dict) -> str:
    return "".join(w["w"] + (" " if w.get("sp") else "") for w in ln["words"]).strip()


def _body(resp: dict, *path) -> Any:
    cur: Any = resp
    for k in path:
        if not isinstance(cur, dict):
            return None
        cur = cur.get(k)
    return cur


async def from_musixmatch(artist: str, track: str, duration: int, config: dict | None,
                          isrc: str = "", timeout: float = 12.0) -> dict:
    tok = token_from(config)
    if not tok or not (artist and track):
        return {}
    base = {"app_id": APP_ID, "usertoken": tok, "format": "json"}
    headers = {"User-Agent": _UA, "Cookie": "x-mxm-token-guid=" + tok}
    async with httpx.AsyncClient(timeout=timeout, headers=headers) as c:
        params = {**base, "namespace": "lyrics_richsynched", "subtitle_format": "mxm",
                  "q_artist": artist, "q_artists": artist, "q_track": track}
        if duration:
            params["q_duration"] = str(int(duration))
            params["f_subtitle_length"] = str(int(duration))
        if isrc:
            params["track_isrc"] = isrc
        r = await c.get(API + "macro.subtitles.get", params=params)
        if r.status_code != 200:
            return {}
        calls = _body(r.json(), "message", "body", "macro_calls") or {}
        trk = _body(calls, "matcher.track.get", "message", "body", "track") or {}
        if not trk:
            return {}
        # Пословный — отдельным вызовом по commontrack_id, если он есть.
        if trk.get("has_richsync") and trk.get("commontrack_id"):
            rr = await c.get(API + "track.richsync.get",
                             params={**base, "commontrack_id": trk["commontrack_id"]})
            rb = _body(rr.json(), "message", "body", "richsync", "richsync_body") if rr.status_code == 200 else None
            lines = richsync_to_words(rb) if rb else []
            if lines:
                synced = "\n".join(
                    f"[{l['s'] // 60000:02d}:{(l['s'] % 60000) / 1000:05.2f}]{_line_text(l)}" for l in lines)
                return {"synced": synced, "plain": "\n".join(_line_text(l) for l in lines),
                        "words": lines, "source": "Musixmatch (word)"}
        subs = _body(calls, "track.subtitles.get", "message", "body", "subtitle_list") or []
        sb = _body(subs[0], "subtitle", "subtitle_body") if subs else None
        synced = subtitles_to_lrc(sb) if sb else ""
        plain = str(_body(calls, "track.lyrics.get", "message", "body", "lyrics", "lyrics_body") or "").strip()
        if synced or plain:
            return {"synced": synced, "plain": plain, "source": "Musixmatch"}
    return {}
