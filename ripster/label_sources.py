"""Сверка релизов лейбла НЕОДНИМ источником.

Проблема, из-за которой написан этот модуль (замер 24.09.2026): подтверждение
лейбла висело на одном месте — Spotify `/v1/albums`, — и этот эндпоинт нашему
client-credentials токену отказывает (пакетный `?ids=` — 403, одиночный — 429
с баном на сутки). Отказ выглядит как «лейбл не подтверждён ни у одного
релиза», лента лейбла пустеет, а два лейбла вотчлиста («Semantica Records»,
«Night Time Stories (NTS)») висели с `last_check = null` ВООБЩЕ.

Здесь — цепочка запасных источников. Каждый из них обмерян живым запросом
24.09.2026, и то, что он умеет, записано рядом с ним:

  deezer        поиск `label:"X"` отдаёт СИИ, но БЕЗ поля label и даты;
                `GET /album/{id}` отдаёт `label`, `release_date`, `upc`.
                Публично, без ключа. Строгая сверка возможна — и она здесь.
  bandcamp      страница лейбла = его собственные релизы, включая те, которых
                ещё НЕТ ни в одном каталоге (предзаказ). Издатель в JSON-LD
                (`publisher.name`) — это и есть лейбл. Разбор в ripster.bandcamp.
  musicbrainz   browse `release?label=<mbid>` — каталог релизов лейбла, дата из
                release-events. Метаданные: подтверждает ФАКТ, но это не витрина
                стриминга, и дата там указана по стране издания.
  discogs       `labels/<id>/releases` — год и номер по каталогу, без точной
                даты. То же назначение: метаданные.
  beatport      релизы лейбла с `publish_date` (будущая дата = предзаказ) и
                `label.name`. Требует учётку Beatport; без неё — честно «нет».
  tidal         то же, что Beatport: нужен вход в аккаунт.
  apple/qobuz   СПРАШИВАЮЩИЙ, а не перечислитель: у них нет фильтра по лейблу,
                зато поле label/copyright есть у конкретного релиза. Ими МЕРЯЮТ
                кандидата, найденного другим источником.

Правило честности: источник, который НЕ смог ответить, не имеет права выглядеть
как «лейбл пустой». Поэтому каждая попытка оставляет строку в `info["sources"]`
со статусом ok / empty / refused / no_creds / error, а `info["health"]` — то,
что показывать человеку, когда подтверждённых релизов нет вовсе.
"""
from __future__ import annotations

import asyncio
import json
import re
import time
from datetime import datetime

import httpx

from ripster import bandcamp as _bc

_MB_UA = "Ripster/1.0 (https://github.com/Raccoon-Trashpanda/Raccoon-Ripster)"
_DZ_UA = {"User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                         "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")}

#: Сколько страниц релиза раскрыть за один вызов. Deezer берёт label только из
#: отдельного альбома, Bandcamp — только со страницы релиза; без потолка один
#: обход 22 лейблов превращается в сотни запросов (урок 23.08 со Spotify-баном).
_DETAIL_CAP = 12

#: Кэш «какой источник отвечает на этот лейбл» — чтобы следующий обход не
#: начинался с заведомо отказавшего. Живёт сутки: страницы лейблов меняются
#: реже, чем мы ходим по радару.
_ROUTE_TTL = 24 * 3600.0
_route_cache: dict = {}
_route_file = None


def configure(path=None) -> None:
    """Файл памяти об источниках. Путом = выключить запись на диск."""
    global _route_file
    _route_file = str(path) if path else None
    if _route_file:
        try:
            with open(_route_file, encoding="utf-8") as f:
                d = json.load(f)
            if isinstance(d, dict):
                _route_cache.update(d)
        except Exception:
            pass


def _route_get(label: str) -> str:
    hit = _route_cache.get(label.lower())
    if isinstance(hit, dict) and time.time() - float(hit.get("at") or 0) < _ROUTE_TTL:
        return str(hit.get("src") or "")
    return ""


def _route_put(label: str, src: str) -> None:
    _route_cache[label.lower()] = {"src": src, "at": time.time()}
    if not _route_file:
        return
    try:
        with open(_route_file, "w", encoding="utf-8") as f:
            json.dump(_route_cache, f, ensure_ascii=False)
    except Exception:
        pass


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", str(s or "").lower())


def _seed(*, src: str, ident: str, title: str, artist: str, url: str,
          label: str, date: str = "", year: str = "", cover: str = "",
          tracks: int = 0, upc: str = "") -> dict | None:
    """Запись-кандидат в том же формате, что отдаёт `discovery._label_seeds`.

    Без названия запись не имеет смысла: сверка и радар работают по паре
    «артист + название».
    """
    title = str(title or "").strip()
    if not title:
        return None
    date = str(date or "").strip()
    return {
        "id": str(ident or ""),
        "title": title,
        "artist": str(artist or "").strip(),
        "date": date[:10],
        "year": (date[:4] or str(year or "")),
        "url": str(url or ""),
        "cover": str(cover or ""),
        "tracks": int(tracks or 0),
        "label": str(label or "").strip(),
        "service": src,
        "upc": str(upc or ""),
        "confirmed_label": True,
    }


# ── Deezer: перечислитель со СТРОГОЙ сверкой ---------------------------------

async def _deezer_label_releases(label: str, limit: int) -> tuple[list, dict]:
    """Релизы лейбла по Deezer со сверкой по его же полю `label`.

    Поиск `label:"X"` у Deezer — не фильтр, а подсказка ранжирования (на
    «Balance Music» он отдаёт артиста «Balance Autonomic Nerves with Music
    Therapy»), а в самих результатах поиска поля label НЕТ. Поэтому дважды:
    ищем, затем спрашиваем каждый альбом отдельно и оставляем только то, у
    чего лейбл сошёлся. Это и есть то, чего не хватало, чтобы назвать Deezer
    вторым каталогом наравне со Spotify.
    """
    from ripster.routes import discovery as _disc
    out, st = [], {"status": "empty", "candidates": 0, "confirmed": 0}
    try:
        async with httpx.AsyncClient(timeout=20.0, headers=_DZ_UA) as c:
            r = await c.get("https://api.deezer.com/search/album",
                            params={"q": f'label:"{label}"', "limit": max(limit, 10)})
            if r.status_code != 200:
                st["status"] = "refused"
                st["error"] = f"HTTP {r.status_code}"
                return out, st
            items = (r.json() or {}).get("data") or []
            st["candidates"] = len(items)
            for it in items[:_DETAIL_CAP]:
                aid = str(it.get("id") or "")
                if not aid:
                    continue
                try:
                    ra = await c.get(f"https://api.deezer.com/album/{aid}")
                    if ra.status_code != 200:
                        st["status"] = "refused"
                        continue
                    alb = ra.json() or {}
                except Exception:
                    continue
                got = str(alb.get("label") or "")
                if not got or not _disc._label_matches(got, label):
                    continue          # чужой релиз из «мягкого» поиска
                art = str(((alb.get("artist") or {}).get("name"))
                          or it.get("artist") or "")
                s = _seed(src="deezer", ident=aid, title=alb.get("title") or it.get("title") or "",
                          artist=art, url=alb.get("link") or it.get("link") or "",
                          label=got, date=alb.get("release_date") or "",
                          cover=alb.get("cover_xl") or alb.get("cover_big") or "",
                          tracks=alb.get("nb_tracks") or 0, upc=str(alb.get("upc") or ""))
                if s:
                    out.append(s)
                    st["confirmed"] += 1
    except Exception as e:
        st["status"] = "error"
        st["error"] = f"{type(e).__name__}: {e}"
    if out:
        st["status"] = "ok"
    return out, st


# ── Bandcamp: то, чего ещё нет в каталогах ----------------------------------

async def _bandcamp_label_releases(label: str, limit: int,
                                   band_url: str = "") -> tuple[list, dict]:
    """Релизы со страницы лейбла на Bandcamp.

    Для андеграунда это ПЕРВЫЙ источник, а не запасной: листинг предзаказа
    появляется там за недели до того, как релиз встанет в Spotify/Apple/Deezer
    (SEMANTICA 199 — ровно этот случай). Страница принадлежит лейблу, поэтому
    её издатель (`publisher.name`) и есть подтверждение лейбла.
    """
    out, st = [], {"status": "empty", "candidates": 0, "confirmed": 0}
    try:
        # 6 страниц на лейбл: при паузе Bandcamp в 2 с это ~12 с на первого
        # захода, и столько же КЭШ на следующие 6 часов — то есть ноль.
        got = await _bc.band_releases(name=label, band_url=band_url,
                                      probe=min(max(limit, 4), 6))
        rels = got.get("releases") or []
        st["candidates"] = len(rels)
        if not got.get("band"):
            st["status"] = "empty"
            st["error"] = "страница лейбла на Bandcamp не найдена"
            return out, st
        for rec in rels:
            got_label = str(rec.get("label") or "")
            from ripster.routes import discovery as _disc
            if got_label and not _disc._label_matches(got_label, label):
                continue
            s = _seed(src="bandcamp", ident=str(rec.get("item_id") or rec.get("url") or ""),
                      title=rec.get("title"), artist=rec.get("artist"),
                      url=rec.get("url"), label=got_label or label,
                      date=rec.get("date") or "", cover=rec.get("cover"),
                      tracks=rec.get("track_count"), upc=rec.get("upc"),
                      )
            if s:
                s["preorder"] = bool(rec.get("preorder"))
                s["formats"] = rec.get("formats") or []
                out.append(s)
                st["confirmed"] += 1
    except Exception as e:
        st["status"] = "error"
        st["error"] = f"{type(e).__name__}: {e}"
    if out:
        st["status"] = "ok"
    return out, st


# ── MusicBrainz / Discogs: метаданные ---------------------------------------

async def _mb_get(c, path: str, params: dict) -> dict:
    try:
        r = await c.get(f"https://musicbrainz.org/ws/2/{path}",
                        params={**params, "fmt": "json"},
                        headers={"User-Agent": _MB_UA}, timeout=20.0)
        return r.json() if r.status_code == 200 else {}
    except Exception:
        return {}


async def _musicbrainz_label_releases(label: str, limit: int) -> tuple[list, dict]:
    """Каталог лейбла по MusicBrainz. МЕТАДАННЫЕ, не витрина.

    Дату берём из release-events; там страна и месяц, поэтому для «когда
    выйдет» эта ветка не годится и служит только для подтверждения того, что
    лейбл существет и что он выпускал.
    """
    out, st = [], {"status": "empty", "candidates": 0, "confirmed": 0}
    try:
        async with httpx.AsyncClient(timeout=25.0, follow_redirects=True) as c:
            d = await _mb_get(c, "label", {"query": f'labelname:"{label}" OR "{label}"',
                                           "limit": 3})
            labs = d.get("labels") or []
            mbid = ""
            want = _norm(label)
            for l in labs:
                names = [l.get("name") or ""] + [a.get("name") or ""
                                                 for a in (l.get("aliases") or [])]
                if any(_norm(n) == want for n in names):
                    mbid = str(l.get("id") or "")
                    break
            if not mbid and labs:
                mbid = str(labs[0].get("id") or "")
            if not mbid:
                st["error"] = "лейбл не найден в MusicBrainz"
                return out, st
            d = await _mb_get(c, "release", {"label": mbid, "limit": max(limit, 5),
                                             "inc": "artist-credits"})
            rels = d.get("releases") or []
            st["candidates"] = len(rels)
            for rel in rels[:limit]:
                ev = (rel.get("release-events") or [{}])[0]
                date = str(ev.get("date") or "")
                if len(date) == 7:
                    date += "-01"
                arts = [a.get("name") or "" for a in (rel.get("artist-credit") or [])
                        if not a.get("joinphrase")]
                s = _seed(src="musicbrainz", ident=str(rel.get("id") or ""),
                          title=rel.get("title"), artist=", ".join(arts)[:120],
                          url=f"https://musicbrainz.org/release/{rel.get('id')}",
                          label=label, date=date[:10], year=date[:4])
                if s:
                    s["metadata_only"] = True
                    out.append(s)
                    st["confirmed"] += 1
    except Exception as e:
        st["status"] = "error"
        st["error"] = f"{type(e).__name__}: {e}"
    if out:
        st["status"] = "ok"
    return out, st


async def _discogs_label_releases(label: str, limit: int) -> tuple[list, dict]:
    """Каталог лейбла по Discogs (годовалые даты). МЕТАДАННЫЕ, не витрина."""
    out, st = [], {"status": "empty", "candidates": 0, "confirmed": 0}
    try:
        hdr = {**_DZ_UA, "Accept": "application/json"}
        async with httpx.AsyncClient(timeout=25.0, follow_redirects=True,
                                     headers=hdr) as c:
            r = await c.get("https://api.discogs.com/database/search",
                            params={"q": label, "type": "label", "per_page": 5})
            if r.status_code != 200:
                st["status"] = "refused"
                st["error"] = f"HTTP {r.status_code}"
                return out, st
            want = _norm(label)
            lab = None
            for x in (r.json().get("results") or []):
                if _norm(x.get("title") or "") == want:
                    lab = x
                    break
            lab = lab or next(iter(r.json().get("results") or []), None)
            if not lab:
                st["error"] = "лейбл не найден в Discogs"
                return out, st
            lid = str(lab.get("id") or "")
            if not lid:
                st["error"] = "лейбл не найден в Discogs"
                return out, st
            rr = await c.get(f"https://api.discogs.com/labels/{lid}")
            if rr.status_code != 200:
                st["status"] = "refused"
                return out, st
            pages = int(((rr.json().get("pagination") or {}).get("pages")) or 1)
            rl = await c.get(f"https://api.discogs.com/labels/{lid}/releases",
                             params={"page": max(1, pages), "per_page": max(limit, 10)})
            if rl.status_code != 200:
                st["status"] = "refused"
                st["error"] = f"HTTP {rl.status_code}"
                return out, st
            items = rl.json().get("releases") or []
            st["candidates"] = len(items)
            for x in items[:limit]:
                s = _seed(src="discogs", ident=str(x.get("id") or ""),
                          title=x.get("title"), artist=x.get("artist"),
                          url=f"https://www.discogs.com/release/{x.get('id')}",
                          label=label, year=str(x.get("year") or ""),
                          cover=x.get("thumb") or "")
                if s:
                    s["metadata_only"] = True
                    out.append(s)
                    st["confirmed"] += 1
    except Exception as e:
        st["status"] = "error"
        st["error"] = f"{type(e).__name__}: {e}"
    if out:
        st["status"] = "ok"
    return out, st


# ── Beatport / Tidal: витрины с будущими датами ------------------------------

async def _beatport_label_releases(label: str, limit: int) -> tuple[list, dict]:
    """Релизы лейбла с Beatport: `publish_date` там бывает БУДУЩЕЙ датой."""
    out, st = [], {"status": "empty", "candidates": 0, "confirmed": 0}
    try:
        from ripster.routes import beatport as _bp
        token = await _bp._get_token()
        if not token:
            st["status"] = "no_creds"
            st["error"] = "учётка Beatport не настроена"
            return out, st
        async with httpx.AsyncClient(timeout=25.0, follow_redirects=True) as c:
            r = await c.get(f"{_bp._BASE}/catalog/search/",
                            params={"q": label, "type": "labels", "per_page": 5},
                            headers=_bp._auth_headers(token))
            if r.status_code != 200:
                st["status"] = "refused"
                st["error"] = f"HTTP {r.status_code}"
                return out, st
            labs = ((r.json() or {}).get("labels") or {}).get("data") or []
            want = _norm(label)
            lab = next((l for l in labs if _norm(l.get("name") or "") == want), None) \
                or (labs[0] if labs else None)
            if not lab:
                st["error"] = "лейбл не найден на Beatport"
                return out, st
            rr = await c.get(f"{_bp._BASE}/catalog/labels/{lab['id']}/releases/",
                             params={"per_page": max(min(limit, 50), 10),
                                     "order_by": "-publish_date"},
                             headers=_bp._auth_headers(token))
            if rr.status_code != 200:
                st["status"] = "refused"
                st["error"] = f"HTTP {rr.status_code}"
                return out, st
            items = rr.json().get("data") or []
            st["candidates"] = len(items)
            for x in items:
                got = str(((x.get("label") or {}).get("name")) or "")
                arts = ", ".join(a.get("name", "") for a in (x.get("artists") or []))
                s = _seed(src="beatport", ident=str(x.get("id") or ""),
                          title=x.get("name"), artist=arts,
                          url=f"https://www.beatport.com/release/{x.get('slug','')}/{x.get('id','')}",
                          label=got or label,
                          date=str(x.get("publish_date") or ""),
                          cover=str(((x.get("image") or {}).get("uri") or "")
                                    ).replace("{w}", "400").replace("{h}", "400"))
                if s:
                    s["preorder"] = bool(x.get("is_pre_order"))
                    out.append(s)
                    st["confirmed"] += 1
    except Exception as e:
        st["status"] = "error"
        st["error"] = f"{type(e).__name__}: {e}"
    if out:
        st["status"] = "ok"
    return out, st


async def _tidal_label_releases(label: str, limit: int) -> tuple[list, dict]:
    """Tidal релизов по лейблу НЕ перечисляет: фильтра по лейблу у его поиска
    нет. Ветка оставлена спрашивающей — её зовут на КАНДИДАТЕ, чтобы подтвердить
    поле `label` конкретного альбома (см. `confirm_release_label`)."""
    return [], {"status": "unsupported", "error":
                "Tidal не умеет перечислять лейбл — только подтверждать релиз"}


# ── Спрашивающие: подтвердить лейбл конкретного релиза -----------------------

async def confirm_release_label(service: str, *, album_id: str = "",
                                upc: str = "", title: str = "",
                                artist: str = "") -> str:
    """Настоящий label релиза в этом сервисе, либо пусто («не удалось»).

    Ничего не додумываем: пусто означает «этот источник промолчал», и вызывающий
    обязан отнести это к отказу проверки, а не к вердикту «лейбл не тот».
    """
    svc = (service or "").lower()
    try:
        async with httpx.AsyncClient(timeout=20.0, follow_redirects=True,
                                     headers=_DZ_UA) as c:
            if svc == "deezer" and album_id:
                r = await c.get(f"https://api.deezer.com/album/{album_id}")
                return str((r.json() or {}).get("label") or "") if r.status_code == 200 else ""
            if svc == "apple" and upc:
                r = await c.get("https://itunes.apple.com/lookup",
                                params={"upc": upc, "entity": "album", "country": "us"})
                res = ((r.json() or {}).get("results") or [{}])[0] if r.status_code == 200 else {}
                from ripster.routes import discovery as _disc
                return _disc._clean_label(str(res.get("copyright") or
                                               res.get("recordLabel") or ""))
            if svc == "qobuz" and album_id:
                app_id = _qobuz_app_id()
                r = await c.get("https://www.qobuz.com/api.json/0.2/album/get",
                                params={"album_id": album_id, "app_id": app_id})
                alb = ((r.json() or {}).get("album")) or {}
                return str(((alb.get("label") or {}).get("name")) or "")
            if svc == "bandcamp" and album_id.startswith("http"):
                rec = await _bc.fetch_album(album_id)
                return str((rec or {}).get("label") or "")
    except Exception as e:
        print(f"[label] confirm via {service} failed: {e}", flush=True)
    return ""


def _qobuz_app_id() -> str:
    try:
        from ripster.routes import discovery as _disc
        return (str((_disc._config.get("qobuz-app-id") or "").strip())
                or _disc._QOBUZ_DEFAULT_APP_ID)
    except Exception:
        return "312369995"


# ── цепочка ──────────────────────────────────────────────────────────────────

#: Порядок обхода. Первым — то, что реально отвечает по андеграундным лейблам:
#: Bandcamp (предзаказы) и Deezer (строгая сверка), затем витрины с учётками,
#: затем метаданные.
ORDER = ("bandcamp", "deezer", "beatport", "musicbrainz", "discogs")

#: Источник имеет право называть дату выхода. Метаданные (MusicBrainz, Discogs)
#: — нет: там датаCountry издания, и по ней радар будет врать про «когда».
DATED_SOURCES = ("bandcamp", "deezer", "beatport", "spotify")

_FUNCS = {
    "bandcamp": _bandcamp_label_releases,
    "deezer": _deezer_label_releases,
    "beatport": _beatport_label_releases,
    "musicbrainz": _musicbrainz_label_releases,
    "discogs": _discogs_label_releases,
    "tidal": _tidal_label_releases,
}


async def label_releases_any(label: str, limit: int = 20,
                             sources: tuple = ORDER,
                             band_url: str = "",
                             stop_after_first: bool = True) -> tuple[list, dict]:
    """Релизы лейбла первым ответившим источником + журнал попыток.

    Возвращает `(seeds, info)`:
      info["sources"]  {имя: {status, candidates, confirmed, error}}
      info["via"]      список источников, ДАВШИХ релизы с датой
      info["metadata"] список источников, подтвердивших релиз, НО не дату
                       (MusicBrainz/Discogs: там дата страны издания)
      info["health"]   {(key, args)} — что сказать человеку, если пусто; ключ i18n,
                       строка не зашивается в код, потому что интерфейс двуязычный.

    `stop_after_first=False` — обойти ВСЕ источники: так зовёт фоновый обход
    предзаказов, которому мало «лейбл существует», ему нужны будущие даты, а их
    у ответивших каталогов может и не быть (SEMANTICA 199 есть на Bandcamp, в
    Deezer — его предыдущий релиз).
    """
    label = str(label or "").strip()
    info: dict = {"sources": {}, "via": [], "metadata": [], "health": ("", {})}
    if not label:
        return [], info
    seeds: list = []
    seen: set = set()

    # Помним, что отвечало в прошлый раз, и идём в ПЕРВУЮ очередь.
    remembered = _route_get(label)
    order = list(sources)
    if remembered in order:
        order.remove(remembered)
        order.insert(0, remembered)

    for name in order:
        fn = _FUNCS.get(name)
        if fn is None:
            continue
        if info["via"] and (not stop_after_first) and name == remembered:
            continue          # remembered уже отработал первым — не платим дважды
        try:
            if name == "bandcamp":
                got, st = await fn(label, limit, band_url=band_url)
            else:
                got, st = await fn(label, limit)
        except Exception as e:                       # цепочка не падает целиком
            got, st = [], {"status": "error", "candidates": 0, "confirmed": 0,
                           "error": f"{type(e).__name__}: {e}"}
        info["sources"][name] = st
        for s in got:
            k = (_norm(s.get("artist")), _norm(s.get("title")))
            if k in seen:
                continue
            seen.add(k)
            seeds.append(s)
        dated = int(st.get("confirmed") or 0) - sum(1 for s in got if s.get("metadata_only"))
        if dated > 0:
            info["via"].append(name)
            _route_put(label, name)
        elif st.get("confirmed"):
            # подтверждён факт релиза, даты нет — не имеет права остановить
            # цепочку и не имеет права запоминаться как «ответивший источник»
            info["metadata"].append(name)
        if stop_after_first and (info["via"] or len(seeds) >= limit):
            break

    if not seeds:
        info["health"] = _health_line(info)
    return seeds, info


def _health_line(info: dict) -> tuple[str, dict]:
    """Честная строка: лейбл не подтверждён НИ ОДНИМ источником.

    «Проверь написание» здесь было бы враньем: чаще всего отказывают сами
    источники (нет учётки, лимит, страницу не нашли), и человек обязан видеть
    какой именно отказ.
    """
    src = info.get("sources") or {}
    refused = [k for k, v in src.items() if v.get("status") in ("refused", "error")]
    nocreds = [k for k, v in src.items() if v.get("status") == "no_creds"]
    if refused and not any(v.get("candidates") for v in src.values()):
        return "lbl.health_sources_down", {"sources": ", ".join(sorted(refused))}
    if nocreds and len(nocreds) == len([v for v in src.values()
                                        if v.get("status") != "unsupported"]):
        return "lbl.health_no_creds", {"sources": ", ".join(sorted(nocreds))}
    return "lbl.health_unverified", {}


def stamp_label_checks(items: list, label: str, info: dict) -> bool:
    """Отметить `last_check` у подписок на этот лейбл, ХОТЯ БЫ ОДИН источник
    подтвердил релизы.

    Почему именно здесь: `last_check` писал только обход вишлиста, а радар и
    страница лейбла звали ту же сверку, не отмечая её. Два лейбла
    («Semantica Records», «Night Time Stories (NTS)») висели с `last_check =
    null` именно так: их проверял радар, отметку ставил вишлист, а вишлист к тем
    заходам не доходил. Отметка ставится по ЛЮБОМУ успешному пути — иначе
    «не проверен» неотличим от «проверен и пусто».
    """
    if not (info or {}).get("via"):
        return False
    want = _norm(label)
    now = datetime.now().isoformat(timespec="seconds")
    hit = False
    for e in items or []:
        if e.get("kind") == "label" and _norm(e.get("name") or "") == want:
            e["last_check"] = now
            if not e.get("last_release_date"):
                e["label_verified_via"] = ",".join(info["via"])
            hit = True
    return hit
