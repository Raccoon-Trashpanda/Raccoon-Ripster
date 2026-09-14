"""Apple Music пословная лирика (караоке) — syllable-lyrics TTML → word-timed JSON.

Зачем (14.09.2026, владелец: «караоке по словам»). LRCLIB даёт только построчную
синхру; Apple Music отдаёт ПОСЛОВНУЮ (`syllable-lyrics`, TTML со `<span begin end>`
на каждое слово) — по нашей подписке. Musixmatch richsync блокирует анонимов
капчей, NetEase по западным трекам пусто → Apple единственный надёжный источник.

Ключевая ловушка: `syllable-lyrics` доступна ТОЛЬКО в storefront аккаунта. Наш
media-user-token — Canada (`ca`); запрос в `us` даёт 404 «No related resources».
Поэтому storefront берём из `/v1/me/storefront`, а не из конфига.

Токены: `authorization-token` (dev-bearer, его же держит свежим _apple_bearer_keeper)
+ `media-user-token` (долгоживущий, подписка). Оба уже есть в config.yaml.
"""
from __future__ import annotations

import re
import time
from typing import Optional

import httpx

_AMP = "https://amp-api.music.apple.com"

# storefront аккаунта меняется редко — кэшируем, чтобы не дёргать /me на каждый трек.
_sf_cache: dict = {"sf": "", "ts": 0.0}
_SF_TTL = 6 * 3600.0


def _tokens() -> tuple[str, str]:
    """(bearer, media-user-token) из конфига. Пусто — Apple не настроен."""
    try:
        from ripster import config_service
        cfg = config_service.load_config()
    except Exception:  # noqa: BLE001
        import yaml
        from pathlib import Path
        cfg = yaml.safe_load(Path("config.yaml").read_text(encoding="utf-8")) or {}
    return (str(cfg.get("authorization-token") or "").strip(),
            str(cfg.get("media-user-token") or "").strip())


def _headers(bearer: str, mut: str) -> dict:
    return {
        "Authorization": f"Bearer {bearer}",
        "Music-User-Token": mut,
        "Origin": "https://music.apple.com",
    }


def _ttml_ms(s: str) -> int:
    """TTML-время → мс. Форматы: '12.340', '1:02.340', '1:02:03.5'."""
    s = (s or "").strip()
    if not s:
        return 0
    try:
        if ":" in s:
            parts = s.split(":")
            sec = float(parts[-1])
            if len(parts) >= 2:
                sec += int(parts[-2]) * 60
            if len(parts) >= 3:
                sec += int(parts[-3]) * 3600
            return int(sec * 1000)
        return int(float(s) * 1000)
    except Exception:  # noqa: BLE001
        return 0


def parse_syllable_ttml(ttml: str) -> list[dict]:
    """TTML syllable → [{'s','e','words':[{'t','d','w'}]}] (мс). Пусто — не разобрать."""
    lines: list[dict] = []
    for pm in re.finditer(r"<p\b([^>]*)>(.*?)</p>", ttml or "", re.S):
        pat, inner = pm.group(1), pm.group(2)
        pb = re.search(r'begin="([^"]+)"', pat)
        pe = re.search(r'end="([^"]+)"', pat)
        words: list[dict] = []
        spans = list(re.finditer(r"<span\b([^>]*)>(.*?)</span>", inner, re.S))
        for idx, sm in enumerate(spans):
            sat = sm.group(1)
            txt = re.sub(r"<[^>]+>", "", sm.group(2))
            b = re.search(r'begin="([^"]+)"', sat)
            e = re.search(r'end="([^"]+)"', sat)
            if b and e and txt.strip():
                t = _ttml_ms(b.group(1))
                # Apple режет на СЛОГИ: между слогами одного слова (with|drawals)
                # пробела в TTML нет, между словами — есть. Смотрим текст ПОСЛЕ
                # этого <span> до начала следующего: пробел ⇒ конец слова.
                nxt = spans[idx + 1].start() if idx + 1 < len(spans) else len(inner)
                gap = re.sub(r"<[^>]+>", "", inner[sm.end():nxt])
                words.append({"t": t, "d": max(0, _ttml_ms(e.group(1)) - t),
                              "w": txt, "sp": bool(gap.strip() == "" and gap != "")})
        if words:
            lines.append({
                "s": _ttml_ms(pb.group(1)) if pb else words[0]["t"],
                "e": _ttml_ms(pe.group(1)) if pe else words[-1]["t"] + words[-1]["d"],
                "words": words,
            })
        else:
            # строка без пословных спанов — построчный фолбэк, чтобы не терять текст
            txt = re.sub(r"<[^>]+>", "", inner).strip()
            if txt:
                s0 = _ttml_ms(pb.group(1)) if pb else 0
                lines.append({"s": s0, "e": _ttml_ms(pe.group(1)) if pe else s0,
                              "words": [{"t": s0, "d": 0, "w": txt}]})
    return lines


async def _storefront(c: httpx.AsyncClient, h: dict) -> str:
    now = time.time()
    if _sf_cache["sf"] and now - _sf_cache["ts"] < _SF_TTL:
        return _sf_cache["sf"]
    try:
        r = await c.get(f"{_AMP}/v1/me/storefront", headers=h)
        if r.status_code == 200:
            sf = (r.json().get("data") or [{}])[0].get("id") or "us"
            _sf_cache.update(sf=sf, ts=now)
            return sf
    except Exception:  # noqa: BLE001
        pass
    return _sf_cache["sf"] or "us"


async def _find_song_id(c: httpx.AsyncClient, h: dict, sf: str,
                        title: str, artist: str, isrc: str) -> Optional[str]:
    # ISRC — точнее всего.
    if isrc:
        try:
            r = await c.get(f"{_AMP}/v1/catalog/{sf}/songs",
                            headers=h, params={"filter[isrc]": isrc, "limit": 1})
            if r.status_code == 200:
                d = r.json().get("data") or []
                if d:
                    return d[0]["id"]
        except Exception:  # noqa: BLE001
            pass
    term = f"{artist} {title}".strip() or title
    try:
        r = await c.get(f"{_AMP}/v1/catalog/{sf}/search",
                        headers=h, params={"term": term, "types": "songs", "limit": 3})
        if r.status_code == 200:
            songs = (r.json().get("results", {}).get("songs", {}) or {}).get("data") or []
            tl = title.lower()
            for s in songs:
                if tl and tl in (s.get("attributes", {}).get("name", "").lower()):
                    return s["id"]
            if songs:
                return songs[0]["id"]
    except Exception:  # noqa: BLE001
        pass
    return None


async def word_lyrics(title: str, artist: str = "", isrc: str = "") -> Optional[dict]:
    """Пословная лирика Apple → {'src':'apple','lines':[...]} или None (нет токена/
    совпадения/подписки/лирики). Никаких выдумок — не нашли, честно None."""
    bearer, mut = _tokens()
    if not bearer or not mut:
        return None
    h = _headers(bearer, mut)
    async with httpx.AsyncClient(timeout=20) as c:
        sf = await _storefront(c, h)
        sid = await _find_song_id(c, h, sf, title, artist, isrc)
        if not sid:
            return None
        r = await c.get(f"{_AMP}/v1/catalog/{sf}/songs/{sid}/syllable-lyrics", headers=h)
        if r.status_code != 200:
            return None
        data = r.json().get("data") or []
        if not data:
            return None
        ttml = data[0].get("attributes", {}).get("ttml", "")
        lines = parse_syllable_ttml(ttml)
        return {"src": "apple", "lines": lines} if lines else None
