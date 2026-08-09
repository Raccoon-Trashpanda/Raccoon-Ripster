"""Тексты песен из потоковых сервисов.

Зачем модуль: у каждого сервиса свой способ отдать текст, свои ключи и свои
поводы отказать. Держать это в маршруте `/api/lyrics` значит превратить его в
простыню, где поломка одного сервиса выглядит как поломка лирики вообще.

Что здесь есть и чего нет:

  Apple      — в `routes/discovery.py`, там же где был; трогать не стал.
  Tidal      — `listen.tidal.com/v1/tracks/{id}/lyrics`, есть построчная синхра.
  Spotify    — `spclient.../color-lyrics/v2`, синхра построчная (Musixmatch).
  Deezer     — приватный `gw-light.php`, метод `song.getLyrics`; синхра есть.
  Qobuz      — текстов НЕТ вообще, запрашивать нечего.
  Yandex     — текст есть, но только по подписке и через другой протокол;
               отложено осознанно, а не забыто.

Все функции возвращают ОДИН формат `{"synced": lrc, "plain": text,
"source": "имя"}` либо пустой словарь. Исключения наружу не выпускаем: упавший
источник обязан пропустить ход, а не уронить всю лестницу — на этом уже
обжигались, когда один мёртвый сервис прятал текст, доступный в трёх других.

ID трека в сервисе обычно неизвестен, поэтому у каждого источника две части:
поиск по «артист + название» и получение текста по найденному ID. Поиск может
промахнуться, поэтому длительность, если известна, используется как проверка:
расхождение больше 7 секунд — почти наверняка другая запись (ремикс, лайв,
радио-правка), и такой текст хуже, чем никакого.
"""
from __future__ import annotations

import json
import re
from typing import Any

from ripster import http_client as _HTTP

# Насколько может отличаться длительность найденного трека, чтобы считать его
# тем же самым. 7 секунд — запас на разное округление и тишину в конце; при
# 15+ это уже другая версия записи.
_DUR_TOLERANCE = 7


def _dur_ok(found: Any, want: int) -> bool:
    """Совпадает ли длительность. Без ожидаемой длительности — не возражаем."""
    if not want:
        return True
    try:
        return abs(int(found or 0) - int(want)) <= _DUR_TOLERANCE
    except (TypeError, ValueError):
        return True


def _ms_to_lrc(ms: int, text: str) -> str:
    s, cs = divmod(int(ms) // 10, 100)
    return f"[{s // 60:02d}:{s % 60:02d}.{cs:02d}]{text}"


def _norm(s: str) -> str:
    """Огрубление названия для сравнения: регистр, скобки, знаки."""
    s = re.sub(r"\([^)]*\)|\[[^]]*]", " ", (s or "").lower())
    return re.sub(r"[^a-z0-9а-яё]+", " ", s).strip()


def _same_track(a_found: str, t_found: str, a_want: str, t_want: str) -> bool:
    """Грубая проверка, что нашли то же самое.

    Без неё поиск по «артист + название» уверенно возвращает кавер или чужой
    трек с похожим именем, и текст уезжает не тот — заметить это по одному
    только тексту почти невозможно.
    """
    tf, tw = _norm(t_found), _norm(t_want)
    if not tf or not tw or (tf not in tw and tw not in tf):
        return False
    af, aw = _norm(a_found), _norm(a_want)
    return not (af and aw) or af in aw or aw in af


# ─────────────────────────── Tidal ────────────────────────────────────────

async def from_tidal(artist: str, track: str, duration: int, config: dict,
                     track_id: str = "") -> dict:
    """Текст из Tidal. Токен берём у движка — второй авторизации не заводим."""
    try:
        from ripster.engines.tidal import _tidal_token_country
        token, country = await _tidal_token_country(config)
        if not token:
            return {}
        head = {"Authorization": f"Bearer {token}"}
        # Именно api.tidal.com. Внешне похожий listen.tidal.com на тот же
        # запрос с нашим OAuth-токеном отвечает 401 «Missing auth parameter»:
        # он ждёт токен приложения, а не пользовательский. Проверено 08.08.2026.
        api = "https://api.tidal.com/v1"
        async with _HTTP.ashared() as c:
            if not track_id:
                r = await c.get(f"{api}/search/tracks", headers=head,
                                params={"query": f"{artist} {track}", "limit": 5,
                                        "countryCode": country})
                if r.status_code != 200:
                    return {}
                items = (r.json() or {}).get("items") or []
                for it in items:
                    a = ((it.get("artist") or {}).get("name")
                         or ", ".join(x.get("name", "") for x in it.get("artists") or []))
                    if _same_track(a, it.get("title") or "", artist, track) \
                       and _dur_ok(it.get("duration"), duration):
                        track_id = str(it.get("id") or "")
                        break
                if not track_id:
                    return {}

            r = await c.get(f"{api}/tracks/{track_id}/lyrics", headers=head,
                            params={"countryCode": country, "locale": "en_US",
                                    "deviceType": "BROWSER"})
            if r.status_code != 200:
                return {}
            j = r.json() or {}
            synced, plain = j.get("subtitles") or "", j.get("lyrics") or ""
            if not (synced or plain):
                return {}
            return {"synced": synced, "plain": plain, "source": "tidal"}
    except Exception:
        return {}


# ─────────────────────────── Spotify ──────────────────────────────────────

async def from_spotify(artist: str, track: str, duration: int,
                       track_id: str = "") -> dict:
    """Текст из Spotify (подложка Musixmatch).

    Нужен веб-токен из куки `sp_dc` — обычный токен приложения сюда не пускают,
    отвечают 401. Синхра построчная: пословной этот ответ не содержит.
    """
    try:
        from ripster.routes.spotify import _sp_dc_get_token
        token = await _sp_dc_get_token()
        if not token:
            return {}
        head = {
            "Authorization": f"Bearer {token}",
            "app-platform": "WebPlayer",
            "User-Agent": "Mozilla/5.0",
        }
        async with _HTTP.ashared() as c:
            if not track_id:
                r = await c.get("https://api.spotify.com/v1/search", headers=head,
                                params={"q": f"{artist} {track}", "type": "track", "limit": 5})
                if r.status_code != 200:
                    return {}
                for it in ((r.json() or {}).get("tracks") or {}).get("items") or []:
                    a = ", ".join(x.get("name", "") for x in it.get("artists") or [])
                    if _same_track(a, it.get("name") or "", artist, track) \
                       and _dur_ok(round((it.get("duration_ms") or 0) / 1000), duration):
                        track_id = it.get("id") or ""
                        break
                if not track_id:
                    return {}

            r = await c.get(
                f"https://spclient.wg.spotify.com/color-lyrics/v2/track/{track_id}",
                headers=head, params={"format": "json", "market": "from_token"})
            if r.status_code != 200:
                return {}
            lines = ((r.json() or {}).get("lyrics") or {}).get("lines") or []
            plain, lrc = [], []
            for ln in lines:
                words = (ln.get("words") or "").strip()
                if not words or words == "♪":
                    continue
                plain.append(words)
                st = ln.get("startTimeMs")
                if st not in (None, "", "0"):
                    lrc.append(_ms_to_lrc(int(st), words))
            if not plain:
                return {}
            return {"synced": "\n".join(lrc), "plain": "\n".join(plain),
                    "source": "spotify"}
    except Exception:
        return {}


# ─────────────────────────── Deezer ───────────────────────────────────────

async def from_deezer(artist: str, track: str, duration: int, config: dict,
                      track_id: str = "") -> dict:
    """Текст из Deezer.

    Открытый `api.deezer.com` текстов не отдаёт вовсе — только приватный
    `gw-light.php`, и тот требует сессию, которую поднимаем по ARL. Поиск при
    этом делаем через открытый API: он не требует авторизации и не тратит
    сессию.
    """
    arl = (config.get("deezer-arl") or "").strip()
    if not arl:
        try:
            from ripster import deezer_pool
            accs = deezer_pool._configured_accounts(config) or []
            arl = (accs[0].get("arl") or "").strip() if accs else ""
        except Exception:
            arl = ""
    if not arl:
        return {}

    try:
        async with _HTTP.ashared() as c:
            if not track_id:
                r = await c.get("https://api.deezer.com/search",
                                params={"q": f'artist:"{artist}" track:"{track}"', "limit": 5})
                if r.status_code != 200:
                    return {}
                for it in (r.json() or {}).get("data") or []:
                    if _same_track((it.get("artist") or {}).get("name") or "",
                                   it.get("title") or "", artist, track) \
                       and _dur_ok(it.get("duration"), duration):
                        track_id = str(it.get("id") or "")
                        break
                if not track_id:
                    return {}

            gw = "https://www.deezer.com/ajax/gw-light.php"
            base = {"api_version": "1.0", "input": "3", "api_token": ""}
            cookies = {"arl": arl}
            # Первый вызов без токена нужен только чтобы получить сам токен.
            r = await c.post(gw, params={**base, "method": "deezer.getUserData"},
                             cookies=cookies, json={})
            if r.status_code != 200:
                return {}
            tok = ((r.json() or {}).get("results") or {}).get("checkForm") or ""
            if not tok:
                return {}
            r = await c.post(gw, params={**base, "api_token": tok, "method": "song.getLyrics"},
                             cookies=cookies, json={"sng_id": track_id})
            if r.status_code != 200:
                return {}
            res = (r.json() or {}).get("results") or {}
            plain = (res.get("LYRICS_TEXT") or "").strip()
            lrc = []
            for ln in res.get("LYRICS_SYNC_JSON") or []:
                w = (ln.get("line") or "").strip()
                ts = ln.get("lrc_timestamp") or ""
                if w and ts:
                    lrc.append(f"{ts}{w}")
            if not (plain or lrc):
                return {}
            return {"synced": "\n".join(lrc), "plain": plain, "source": "deezer"}
    except Exception:
        return {}
