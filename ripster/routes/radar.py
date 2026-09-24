"""Radar sources beyond Spotify: BBC shows, SoundCloud channels, Apple artists.

The releases view already merges several services into one feed and already has
per-source toggles — what was missing were the sources themselves. Each endpoint
here returns the SAME item shape the Spotify radar produces, so the frontend can
drop them straight into the same list:

    {id, title, artist, artist_id, type, group, date, year,
     tracks, cover, url, service}

What each source watches, and why that is the right thing to watch:

  bbc         the shows in bbc.BRANDS (Essential Mix, Pete Tong, …). A show is a
              standing thing you follow; a new episode is the release.
  soundcloud  the channels already on the watchlist — following a channel and
              wanting its uploads in the feed are the same intent, so there is no
              second list to maintain.
  apple       the Apple artists on the watchlist — but only their MIXES: live
              sessions and DJ mixes, picked from the release data itself (title
              words, genre, track durations; see _apple_mix_verdict). Ordinary
              albums and label compilations are deliberately kept out: they have
              their own check in the watchlist, this source is for mixes only.
  labels      the LABELS on the watchlist, via watchlist._label_releases. A
              label is not an artist and has no id to resolve, so it needs its
              own source. Off by default (`show-radar-labels`): the radar feed
              is live and must not change until the owner asks for it.

Every source is best-effort and independent: one failing service returns an empty
list with an `error`, and the rest of the feed still renders. Results are cached
briefly because these are third-party APIs and the view refetches on every filter
change; the cache is persisted and served stale-while-revalidate, so a restart
does not make the user wait for a cold refetch.
"""
from __future__ import annotations

import asyncio
import json
import re
import time
from datetime import datetime, timedelta

from fastapi import APIRouter, Query

from ripster import artist_identity as _ident
from ripster import compilations as _comps
from ripster import namesake_audit as _audit
from ripster import owner_anchor as _anchor
from ripster import owner_feedback as _feedback
from ripster.artist_xref import norm as _norm

router = APIRouter()
_s: dict = {}

_CACHE_TTL = 900          # 15 min — a new episode/upload is not a per-second event
_cache: dict = {}         # key -> (ts, payload)
_refreshing: set = set()  # keys with a background refresh already in flight


def install(app, ctx) -> None:
    _s.update({
        "config":    ctx.config,
        "watchlist": ctx.watchlist,
        # Скрытое по идентичности помечается НА подписке — значит вишлист надо
        # уметь сохранять и отсюда, иначе пометка живёт до ближайшего перезапуска.
        "save_watchlist": getattr(ctx, "save_watchlist", None),
        "cache_file": ctx.base_dir / "radar_cache.json",
        "favs_file":  ctx.base_dir / "rel_favorites.json",
        "store_file": ctx.base_dir / "radar_store.json",
        # Отдельный файл: у грядущего релиза другой жизненный цикл —
        # он не «выпал из окна источника», он однажды ВЫХОДИТ.
        "upcoming_file": ctx.base_dir / "upcoming_store.json",
        # Каталог «кто владеет этой работой» — он же резолвер имени для
        # `name_claim_shows`: карточке лейбловой ленты нечем сказать, КТО она,
        # кроме строки имени.
        "base_dir": ctx.base_dir,
    })
    # Якорь владельца судит по его же фонотеке: `ripster_stats.db`,
    # `rel_favorites.json`, `stations.db` лежат рядом со складом радара.
    _anchor.configure(ctx.base_dir)
    # Слово владельца («это не мой артист») и самоаудит склада — свои durable
    # файлы рядом же: `owner_feedback.json`, `namesake_audit.json`.
    _feedback.configure(ctx.base_dir)
    _audit.configure(ctx.base_dir)
    _load_cache()
    app.include_router(router)


# ── Избранные релизы: живут на сервере, а не в браузере ─────────────────────
# Звезда на карточке писалась в localStorage — то есть в конкретный браузерный
# профиль. У Ripster таких профилей минимум два: окно программы (WebView2) и
# запасной ярлык в обычном браузере, и списки у них разные. Плюс любая чистка
# данных сайта стирала избранное молча. Со стороны владельца это выглядело как
# «жму в избранное не первый раз, Ripster не помнит» (01.08.2026).
#
# Храним на сервере: одно избранное на всю программу, переживает перезапуск,
# смену оболочки и переустановку браузера.
_FAV_CAP = 1000


def _favs_load() -> list:
    f = _s.get("favs_file")
    if not f or not f.exists():
        return []
    try:
        d = json.loads(f.read_text(encoding="utf-8"))
        return d if isinstance(d, list) else (d.get("items") or [])
    except Exception as e:
        print(f"[radar] favorites load error: {e}", flush=True)
        return []


def _favs_save(items: list) -> None:
    f = _s.get("favs_file")
    if not f:
        return
    try:
        f.write_text(json.dumps(items[:_FAV_CAP], ensure_ascii=False), encoding="utf-8")
    except Exception as e:
        print(f"[radar] favorites save error: {e}", flush=True)


def _fav_uid(rel: dict) -> str:
    """Тот же ключ, что считает интерфейс, иначе списки разъедутся."""
    return (str(rel.get("service") or "") + "|"
            + str(rel.get("id") or rel.get("url")
                  or f"{rel.get('title') or ''}~{rel.get('artist') or ''}"))


# ── Долговременный склад релизов не-Spotify источников ──────────────────────
# У Spotify есть склад по артистам и снимки уже отданных лент, поэтому находка
# там переживает и перезапуск, и сбой сети. У BBC/SoundCloud/Apple не было
# ничего, кроме 15-минутного кэша: источник отдаёт только своё недавнее окно, и
# всё, что из него выпало, исчезало НАВСЕГДА — вернуть было неоткуда.
#
# Для владельца это выглядело как «карточка была и пропала»: релиз, найденный
# 24 июля, к августу выпадал из окна источника и не возвращался никаким
# обновлением (01.08.2026, разбор пропавшей карточки PROFF).
#
# Склад чинит именно это: всё once-увиденное складывается на диск и подмешивается
# к свежей выдаче. Источник замолчал — записи остаются; вернулся — обновляются.
_STORE_WINDOW_DAYS = 400          # столько храним; дальше запись не нужна никому
_STORE_CAP = 4000                 # на источник — чтобы файл не пух бесконечно


def _rel_uid(r: dict) -> str:
    """Ключ карточки — один на весь радар: склад, лента и отметка «скрыто».

    Формула живёт в `artist_identity.card_key`: аудит и возврат карточки по
    слову владельца обязаны узнавать её по тому же адресу, что и склад.
    """
    return _ident.card_key(r)


def _durable_load() -> dict:
    f = _s.get("store_file")
    if not f or not f.exists():
        return {}
    try:
        d = json.loads(f.read_text(encoding="utf-8"))
        return d if isinstance(d, dict) else {}
    except Exception as e:
        print(f"[radar] store load error: {e}", flush=True)
        return {}


def _durable_save(store: dict) -> None:
    f = _s.get("store_file")
    if not f:
        return
    try:
        f.write_text(json.dumps(store, ensure_ascii=False), encoding="utf-8")
    except Exception as e:
        print(f"[radar] store save error: {e}", flush=True)


async def _durable_merge(source: str, fresh: list, days: int) -> list:
    """Слить свежую выдачу источника со складом и отдать окно в `days`.

    Свежие записи ОБНОВЛЯЮТ складские (у релиза могла уточниться дата или
    обложка), но никогда их не удаляют: отсутствие в текущем ответе означает
    лишь «источник больше не показывает», а не «релиза не было».

    До слияния — шаг доказательств (`_anchor_evidence`): уточнённые карточки
    уходят на склад уже с лейблом/жанром, и следующее чтение не переспрашивает.
    """
    await _anchor_evidence(fresh or [])
    store = _durable_load()
    bucket = store.get(source) or {}
    for r in (fresh or []):
        if r.get("date"):
            bucket[_rel_uid(r)] = r

    keep = _cutoff(_STORE_WINDOW_DAYS)
    bucket = {k: v for k, v in bucket.items() if (v.get("date") or "") >= keep}
    if len(bucket) > _STORE_CAP:
        newest = sorted(bucket.items(), key=lambda kv: kv[1].get("date", ""), reverse=True)
        bucket = dict(newest[:_STORE_CAP])

    store[source] = bucket
    _durable_save(store)

    cut = _cutoff(days)
    out = [r for r in bucket.values() if (r.get("date") or "") >= cut]
    out.sort(key=lambda x: x.get("date", ""), reverse=True)
    out = _identity_filter(source, out)
    restored = len(out) - sum(1 for r in (fresh or []) if (r.get("date") or "") >= cut)
    if restored > 0:
        print(f"[radar] {source}: source gave {len(fresh or [])}, added from store "
              f"{restored} (window {days}d)", flush=True)
    return out


def _identity_filter(source: str, rels: list) -> list:
    """Самоизлечение при ЧТЕНИИ: склад не трогается, а в ленту не выходит то,
    чью принадлежность нельзя подтвердить.

    Ловит три беды прежних версий:
      • карточки name-сшивки (`via_xref`) — их id никогда не подтверждался
        общими работами, это и были чужие однофамильцы в ленте;
      • карточки со склеенной страницы подписки, где релиз принадлежит другому
        человеку с тем же именем;
      • карточки лейбловой ленты (`via_label`), у которых на подписку указывает
        ТОЛЬКО строка имени — их гейт по `via_xref` молча пропускал, и так в
        подписке на рэпера «BOP» жил dnb-релиз Hospital Records (21.09.2026).

    Ничего не удаляется: запись остаётся в складе (иначе мы теряем находку —
    см. ripster-radar-persistence) и становится видна владельцу в /api/identity
    как скрытая с причиной.

    С 23.09.2026 правило живёт в `artist_identity.feed_filter`: та же дверь
    обязана стоять и на кросс-сервисных лентах Deezer/Qobuz/Tidal, а не только
    на радарном складе — иначе жалоба «не тот Соломон» переживает любую правку
    здесь. `source` остался в сигнатуре для вызывающего кода складов.
    """
    return _ident.feed_filter(rels, _s.get("watchlist") or [],
                              _s.get("base_dir"), _s.get("save_watchlist"))


async def _anchor_evidence(rels: list) -> None:
    """Добать лейбл/жанр карточкам, о которых витрина в списке молчит.

    Правило якоря судит по доказательствам: без этого шага ленты, где у
    карточки нет ни лейбла, ни жанра (Deezer отдаёт список релизов вообще без
    них), вечно проходили бы с вердиктом «нечем судить». Один публичный запрос
    на карточку, ответ — на диск: во второй раз лента читается сразу.
    """
    try:
        await _anchor.evidence(rels)
    except Exception as e:                                     # noqa: BLE001
        print(f"[radar] обогащение карточек: {e}", flush=True)


@router.get("/api/identity")
async def identity_report():
    """Кого радар считает кем и где он НЕ разобрался — отчёт для владельца.

    «Не показал» — это решение, и оно должно быть объяснимо: у каждой подписки
    лежат вердикты по витринам с числами (сколько общих работ, штрихкодов,
    лейблов) и список скрытых релизов с причиной.
    """
    entries = _s.get("watchlist") or []
    rep = _ident.summary(entries)
    rep["ok"] = True
    # Счётчики исходов правила — против того, чтобы «скрыто 12» выглядело
    # победой: сколько карточек правило НЕ тронуло, потому что судить было нечем.
    rep["counters"] = _anchor.counters()
    rep["dropped"] = _ident.dropped_cards(30)
    # Слово владельца и последний самоаудит: «однофамильцы: скрыто N,
    # возвращено M, утечек L» — то, по чему проверяют, что лечили алгоритм.
    rep["feedback"] = _feedback.counts()
    rep["feedback_by_artist"] = {
        a: _feedback.describe(a) for a in
        sorted({str(e.get("name") or "") for e in entries if e.get("name")})}
    rep["audit"] = _audit.report()
    rep["hidden"] = _ident.hidden_cards(entries)
    return rep


@router.post("/api/identity/choice")
async def identity_choice(body: dict):
    """Кого из склеенных однофамильцев считать «своим» — решает владелец.

    Данные показывают на id двух человек, но не показывают, какого из них он
    хотел, когда подписывался. Скрываем ровно названные им лейбловые группы;
    карточки не удаляются, а помечаются, и выбор переживает перезагрузку.

    `show_titles` — обратный ход: вернуть карточку, спрятанную автоправилом
    якоря. Решение человека стоит выше правила, поэтому возврат не перекрывается
    ни следующим сканом, ни новым вердиктом витрины.
    """
    entries = _s.get("watchlist") or []
    want = str(body.get("name") or "").strip()
    aid = str(body.get("artist_id") or "").strip()
    hit = [e for e in entries
           if (aid and str(e.get("artist_id") or "") == aid)
           or (want and str(e.get("name") or "").strip().lower() == want.lower())]
    if not hit:
        return {"ok": False, "error": "watchlist entry not found"}
    for e in hit:
        _ident.set_choice(e, hide=body.get("hide"),
                          hide_titles=body.get("hide_titles"),
                          show_titles=body.get("show_titles"))
    if _s.get("save_watchlist"):
        _s["save_watchlist"](entries)
    return {"ok": True, "updated": len(hit),
            "profile": _ident.identity_of(hit[0]).get("profile") or {}}


# ── Слово владельца: «это не мой артист» / «Это мой» ─────────────────────────
#
# Якорь v5 выводит чужого из косвенных дел (что качал, что звучало). Одно
# нажатие — единственное ПРЯМОЕ доказательство, и оно хранится своим файлом
# (`owner_feedback.json`), переживает перепривязку подписки и перезапуск и
# уезжает в резервную копию настроек. Нажатие лечит НЕ одну карточку: запомнив,
# чем карточка выдала себя (id артиста в чужой витрине, лейбл, функциональный
# узор заголовка), правило прячет всё похожее без новых жалоб.


def _store_rows() -> list:
    """Все карточки долгосрочного склада — одним списком."""
    out = []
    for bucket in (_durable_load() or {}).values():
        if isinstance(bucket, dict):
            out.extend(v for v in bucket.values() if isinstance(v, dict))
    return out


def _entries_named(name: str) -> list:
    """Подписки этого ИМЕНИ. Имени, а не id витрины: отзыв принадлежит человеку,
    которого владелец имел в виду, и обязан действовать и на перепривязанную
    подписку, и на карточку без id."""
    nm = _norm(name)
    return [e for e in (_s.get("watchlist") or [])
            if e.get("kind") != "label"
            and _norm(str(e.get("name") or "")) == nm]


@router.post("/api/identity/feedback")
async def identity_feedback(body: dict):
    """HTTP-дверь слова владельца (браузер). Логика — в `submit_feedback`."""
    return await submit_feedback(body)


async def submit_feedback(body: dict) -> dict:
    """«Это не мой артист» (или обратный ход «это мой») по карточке радара.

    Звено общее для веба (`/api/identity/feedback`) и телефона
    (`/api/pair/feedback`): слово владельца — одни и те же данные, и две
    версии ответа разошлись бы обязательно в пользу одной из витрин.

      • слово ложится в durable-реестр и обобщается на тот же id/лейбл/узор;
      • «не мой» по карточке, которую ПРАВИЛО пропустило, засчитывается как
        утечка: метрика, по которой видно, что алгоритм лечат, а не замазывают;
      • «это мой» дополнительно снимает отметку «скрыто» с подписки и возвращает
        релиз через `choice.show_titles` — слово человека выше автоматики в обе
        стороны;
      • в ответе — сколько ЕЩЁ карточек на складе затронуло это нажатие, чтобы
        владелец видел силу жеста, а не молчаливую правку одной строки.
    """
    item = dict(body.get("item") or {})
    for k in ("artist", "title", "service", "label", "url", "id", "artist_id",
              "genres", "genre", "date", "type"):
        if body.get(k) is not None:
            item.setdefault(k, body.get(k))
    verdict = str(body.get("verdict") or "").strip().lower()
    if verdict not in ("not_mine", "mine"):
        return {"ok": False, "error": "verdict must be 'not_mine' or 'mine'"}
    artist = str(item.get("artist") or "").strip()
    title = str(item.get("title") or item.get("name") or "").strip()
    if not artist or not title:
        return {"ok": False, "error": "нужны артист и название релиза"}
    if not item.get("genres") and not item.get("genre"):
        item["genres"] = []

    entries = _s.get("watchlist") or []
    base_dir = _s.get("base_dir")
    # Вердикт ПРАВИЛА до того, как записано слово владельца: «показал, и его
    # отвергли» — вот и вся метрика утечки.
    shown_by_rule = bool(_ident.feed_filter([dict(item)], entries, base_dir, None))
    _feedback.record(artist, item, verdict)
    if verdict == "not_mine" and shown_by_rule:
        _feedback.mark_leak(artist, item)
    why = _feedback.gate(item, artist)[1] or f"хозяин: {title}"

    touched = 0
    for e in _entries_named(artist):
        if verdict == "mine":
            _ident.set_choice(e, show_titles=[title])
            _ident.hidden_clear(e, item)
        else:
            _ident.hidden_add(e, item, f"хозяин: {why}")
        touched += 1
    if touched and _s.get("save_watchlist"):
        try:
            _s["save_watchlist"](entries)
        except Exception as e:                                 # noqa: BLE001
            print(f"[identity] вишлист не сохранён: {e}", flush=True)

    # Сколько ЕЩЁ карточек склада это нажатие спрячало (или вернуло): сила
    # жеста обязана быть видна сразу, иначе «вылечено навсегда» — просто слова.
    key = _ident.card_key(item)
    same = [r for r in _store_rows()
            if _norm(str(r.get("artist") or "")) == _norm(artist)
            and _ident.card_key(r) != key]
    kept = _ident.feed_filter([dict(r) for r in same], entries, base_dir, None)
    kept_keys = {_ident.card_key(r) for r in kept}
    _anchor.invalidate()
    return {"ok": True, "verdict": verdict, "artist": artist, "title": title,
            "reason": why, "subscriptions": touched,
            "generalised": len([r for r in same
                                if _ident.card_key(r) not in kept_keys]),
            "affected": [str(r.get("title") or "") for r in same
                         if _ident.card_key(r) not in kept_keys][:20],
            "feedback": _feedback.counts(),
            "what_we_learned": _feedback.describe(artist)}


@router.get("/api/identity/hidden")
async def identity_hidden():
    """HTTP-дверь экрана «Скрытые» (браузер). Логика — в `hidden_view`."""
    return await hidden_view()


async def hidden_view() -> dict:
    """Экран «Скрытые»: всё, что дверь не выпустила в ленту, — с причиной.

    Скрытое без экрана — это молчаливая цензура: находка остаётся на складе,
    но владелец о ней не знает и не может сказать «это мой». Здесь видно и кто
    решил — автомат («авто: …») или он сам («хозяин: …»).
    """
    entries = _s.get("watchlist") or []
    rows = _ident.hidden_cards(entries)
    rep = _ident.summary(entries)
    return {"ok": True, "items": rows, "count": len(rows),
            "feedback": _feedback.counts(),
            "auto_hidden": rep.get("auto_hidden") or [],
            "audit": _audit.report()}


@router.post("/api/identity/audit")
async def identity_audit(body: dict | None = None):
    """Пересобрать вердикты по всему складу сейчас (кнопка «проверить заново»).

    Ночной прогон делает то же по расписанию; ручной нужен владельцу сразу
    после правки правила — иначе «вылечено» выглядит как «не проверено».
    """
    rep = _audit.sweep(_s.get("watchlist") or [],
                       save=_s.get("save_watchlist"), reason="вручную")
    return {"ok": True, **(rep or {})}


@router.get("/api/rel-favs")
async def rel_favs_get():
    return {"ok": True, "items": _favs_load()}


@router.post("/api/rel-favs")
async def rel_favs_post(body: dict):
    """Добавить/убрать релиз, либо влить список целиком.

    `merge` нужен ровно один раз на человека: старое избранное лежит в
    localStorage, и при первом запуске новой версии интерфейс переливает его
    сюда. Слияние идёт по ключу и ничего не затирает.
    """
    items = _favs_load()
    have = {_fav_uid(r) for r in items}

    if body.get("merge"):
        added = 0
        for rel in (body.get("items") or []):
            u = _fav_uid(rel)
            if u not in have:
                have.add(u)
                items.append(rel)
                added += 1
        _favs_save(items)
        return {"ok": True, "items": items, "added": added}

    rel = body.get("item") or {}
    uid = body.get("uid") or _fav_uid(rel)
    if body.get("remove"):
        items = [r for r in items if _fav_uid(r) != uid]
    elif uid not in have and rel:
        items.insert(0, rel)
    _favs_save(items)
    return {"ok": True, "items": items}


# ── Cache: survives a restart, and never makes the user wait ─────────────────
# A cold source costs ~2.5s (BBC polls 11 shows, Apple two lookups per artist).
# In-memory only, that price was paid again after every restart, and a stale
# entry made the user wait for a refetch before seeing anything. So: persist it,
# and serve what we have immediately while refreshing behind the request.

def _load_cache() -> None:
    f = _s.get("cache_file")
    try:
        if f and f.exists():
            raw = json.loads(f.read_text(encoding="utf-8")) or {}
            for k, v in raw.items():
                if isinstance(v, list) and len(v) == 2:
                    _cache[k] = (float(v[0]), v[1])
            print(f"[radar] cache loaded: {len(_cache)} sources", flush=True)
    except Exception as e:
        print(f"[radar] cache load failed: {e}", flush=True)


def _save_cache() -> None:
    f = _s.get("cache_file")
    if not f:
        return
    try:
        f.write_text(json.dumps({k: [ts, p] for k, (ts, p) in _cache.items()},
                                ensure_ascii=False), encoding="utf-8")
    except Exception as e:
        print(f"[radar] cache save failed: {e}", flush=True)


def _cutoff(days: int) -> str:
    return (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")


def _serve_or_refresh(key: str, builder):
    """Fresh → return it. Stale → return the stale copy NOW and refresh behind
    the request. Nothing cached → the caller has to wait; there is nothing to
    show yet. Returns the payload, or None when there is no cached copy."""
    hit = _cache.get(key)
    if not hit:
        return None
    ts, payload = hit
    if (time.time() - ts) < _CACHE_TTL:
        return payload
    if key not in _refreshing:
        _refreshing.add(key)

        async def _bg():
            try:
                await builder()
            except Exception as e:
                print(f"[radar] background refresh {key}: {e}", flush=True)
            finally:
                _refreshing.discard(key)

        asyncio.create_task(_bg())
    return {**payload, "stale": True}


def _store(key: str, payload: dict) -> dict:
    _cache[key] = (time.time(), payload)
    _save_cache()
    return payload


def _watch_entries(service: str) -> list:
    return [e for e in (_s.get("watchlist") or [])
            if (e.get("service") or "apple") == service]


# ── BBC ───────────────────────────────────────────────────────────────────────

@router.get("/api/releases/bbc")
async def releases_bbc(days: int = Query(90, ge=1, le=365),
                       force: int = Query(0)):
    """New episodes of the BBC shows we know about."""
    key = f"bbc|{days}"
    if not force:
        hit = _serve_or_refresh(key, lambda: releases_bbc(days=days, force=1))
        if hit is not None:
            return hit

    from ripster.routes.bbc import BRANDS, get_episodes

    cutoff = _cutoff(days)
    sem = asyncio.Semaphore(4)          # be polite to the BBC API

    async def _one(brand: dict) -> list:
        async with sem:
            try:
                data = await get_episodes(brand_id=brand["id"], offset=0, limit=12)
            except Exception as e:
                print(f"[radar] bbc {brand['label']}: {e}", flush=True)
                return []
        out = []
        for ep in data.get("items", []):
            date = (ep.get("date") or "")[:10]
            if not date or date < cutoff:
                continue
            out.append({
                "id":        ep.get("pid", ""),
                "title":     ep.get("subtitle") or ep.get("title", ""),
                "artist":    brand["label"],
                "artist_id": brand["id"],
                "type":      "mix",
                "group":     "mix",
                "date":      date,
                "year":      date[:4],
                "tracks":    None,
                "cover":     ep.get("image", ""),
                "url":       f"https://www.bbc.co.uk/programmes/{ep.get('pid','')}",
                "service":   "bbc",
                "duration":  ep.get("duration") or 0,
                # Отложенная запись будущего эфира (ripster/bbc_schedule.py):
                # карточке нужно знать канал и точное начало, а не «дату».
                "channel":     ep.get("channel") or "",
                "avail_from":  ep.get("avail_from") or "",
                "schedulable": bool(ep.get("schedulable")),
            })
        return out

    results = await asyncio.gather(*(_one(b) for b in BRANDS),
                                   return_exceptions=True)
    releases = [r for res in results if isinstance(res, list) for r in res]
    # Склад: то, что источник уже не показывает, всё равно остаётся.
    releases = await _durable_merge("bbc", releases, days)
    return _store(key, {"ok": True, "releases": releases,
                        "sources": len(BRANDS)})


# ── Грядущие релизы ───────────────────────────────────────────────────────────
# Радар показывает ВЫШЕДШЕЕ и отсекает будущее строкой `date > today` (см.
# releases_apple). При этом предзаказы в каталоге УЖЕ ЕСТЬ — Apple отдаёт их
# вместе с остальным, и Ripster их выбрасывал. Здесь тот же самый обход, только
# берётся ровно то, что там отбрасывалось.
#
# Ни одного нового парсера: источники — те, чей разбор в проекте уже работает на
# живых данных. Контракт записи и запрет изданиям заводить записи — в
# ripster/upcoming.py.

@router.get("/api/releases/upcoming")
async def releases_upcoming(days: int = Query(120, ge=1, le=400),
                            force: int = Query(0)):
    """Что ещё НЕ вышло — по артистам и лейблам вотчлиста.

    `days` здесь смотрит ВПЕРЁД, в отличие от остальных ручек радара, где он
    смотрит назад. Имя то же намеренно: для человека это «окно», и разворот
    смысла у одного и того же параметра запутал бы сильнее, чем помог.
    """
    from ripster import upcoming as _up

    cfg = _s.get("config") or {}
    if cfg.get("show-upcoming") is not True:
        return {"ok": True, "releases": [], "sources": 0, "hint": "upcoming_off",
                "registry": _up.source_report()}

    key = f"upcoming|{days}"
    if not force:
        hit = _serve_or_refresh(key, lambda: releases_upcoming(days=days, force=1))
        if hit is not None:
            return hit

    import httpx
    from ripster.routes.watchlist import _apple_artist_collections, _label_releases

    today = datetime.now().strftime("%Y-%m-%d")
    horizon = (datetime.now() + timedelta(days=days)).strftime("%Y-%m-%d")
    fresh: list = []
    used: set = set()

    # ── Apple: предзаказы по артистам вотчлиста ───────────────────────────────
    entries = [e for e in _watch_entries("apple") if e.get("artist_id")]
    storefront = cfg.get("storefront", "us") or "us"
    with_comps = cfg.get("watchlist-compilations", True) is not False
    if entries:
        sem = asyncio.Semaphore(4)

        async def _one_apple(client, entry: dict) -> list:
            async with sem:
                try:
                    rels = await _apple_artist_collections(
                        client, entry["artist_id"], storefront, with_comps)
                except Exception as e:
                    print(f"[upcoming] apple {entry.get('name')}: {e}", flush=True)
                    return []
            out = []
            for x in rels:
                raw = str(x.get("date") or "")
                url = x.get("url", "")
                if not url:
                    continue          # без ссылки запись не предъявить
                r = _up.make_record(
                    src="apple", src_url=url, date_raw=raw,
                    ident=url, title=x.get("name", ""),
                    artist=x.get("artist") or entry.get("name", ""),
                    artist_id=entry.get("artist_id", ""),
                    cover=x.get("cover", ""), url=url, service="apple",
                    type="compilation" if x.get("compilation") else "album")
                if r and today < (r.get("date") or "") <= horizon:
                    out.append(r)
            return out

        async with httpx.AsyncClient(timeout=20) as client:
            got = await asyncio.gather(*(_one_apple(client, e) for e in entries))
        for lst in got:
            fresh.extend(lst)
        used.add("apple")

    # ── Лейблы: тот же источник, что и у блока лейблов радара ────────────────
    labels = [e for e in (_s.get("watchlist") or [])
              if e.get("kind") == "label" and str(e.get("name") or "").strip()]
    for entry in labels[:20]:
        name = str(entry["name"]).strip()
        try:
            rels = await _label_releases(name, 40)
        except Exception as e:
            print(f"[upcoming] label {name}: {e}", flush=True)
            continue
        for x in rels:
            raw = str(x.get("date") or x.get("year") or "")
            url = x.get("url", "")
            sid = str(x.get("id") or "")
            if not (url or sid):
                continue
            r = _up.make_record(
                src="label", src_url=url or f"spotify:album:{sid}",
                date_raw=raw, ident=sid or url,
                title=x.get("title", ""), artist=x.get("artist", ""),
                cover=x.get("cover", ""), url=url, label=name,
                service=x.get("service") or "spotify", via_label=True)
            if r and today < (r.get("date") or "") <= horizon:
                fresh.append(r)
        used.add("label")

    # ── Слияние, склад, «наступило» ──────────────────────────────────────────
    merged = _up.merge(fresh)
    path = _s.get("upcoming_file")
    store = _up.load(path)
    added = _up.put(store, merged)
    released = _up.promote_released(store, today)
    _up.save(path, store)

    out = [r for r in store.values()
           if not r.get("released") and today < (r.get("date") or "") <= horizon]
    out.sort(key=lambda r: r.get("date", ""))
    # Та же дверь, что и у остальных источников: предзаказ чужого однофамильца
    # владельцу не нужен, а «скрытый» кластер из грядущего возвращался бы каждый
    # раз, когда витрина показывает его заранее.
    await _anchor_evidence(out)
    out = _identity_filter("upcoming", out)

    if added or released:
        print(f"[upcoming] источников {len(used)}, новых {added}, "
              f"наступило {len(released)}, в окне {len(out)}", flush=True)

    return _store(key, {
        "ok": True, "releases": out, "sources": len(used),
        # Числа, а не «готово»: обход без счётчика неотличим от обхода вхолостую.
        "added": added, "released_today": released,
        "registry": _up.source_report(),
    })


@router.get("/api/releases/upcoming/suggest")
async def releases_upcoming_suggest(days: int = Query(120, ge=1, le=400)):
    """Кого стоило бы добавить в вотчлист: грядущее у НЕотслеживаемых артистов,
    чей жанр совпал с профилем вкуса.

    Отдаёт предложения, а не подписывает. Автоподписка по совпадению жанра —
    действие по догадке: разметка жанров в каталогах грубая, а цена ошибки
    несимметрична (лишний лейбл — это десятки автозагрузок в месяц). У каждого
    предложения написано, ПОЧЕМУ оно предложено.
    """
    from ripster import upcoming as _up
    base = await releases_upcoming(days=days)
    if not base.get("ok") or base.get("hint"):
        return {"ok": True, "suggest": [], "hint": base.get("hint")}
    wl = _s.get("watchlist") or []
    watched_ids = {str(e.get("artist_id")) for e in wl if e.get("artist_id")}
    watched_names = {_up._norm(e.get("name")) for e in wl if e.get("name")}
    genres: list = []
    try:
        # Именно build_profile и именно await: функция асинхронная, и мой первый
        # заход звал несуществующую `profile()` — вернулось бы пусто, а выглядело
        # бы как «у человека нет вкусового профиля». Проверено по коду digs.py.
        from ripster import digs as _digs
        prof = await _digs.build_profile(limit=40, with_genres=True)
        genres = [g.get("genre") for g in (prof.get("genres") or []) if g.get("genre")]
    except Exception as e:
        print(f"[upcoming] профиль вкуса недоступен: {e}", flush=True)
    if not genres:
        # Честный отказ вместо предложений наугад: без профиля «совпало по
        # жанру» означало бы «совпало ни с чем».
        return {"ok": True, "suggest": [], "hint": "no_profile"}
    sug = _up.suggest(base.get("releases") or [], watched_ids, watched_names, genres)
    return {"ok": True, "suggest": sug, "genres": genres}


# ── SoundCloud ────────────────────────────────────────────────────────────────

@router.get("/api/releases/soundcloud")
async def releases_soundcloud(days: int = Query(90, ge=1, le=365),
                              force: int = Query(0)):
    """Recent uploads from the SoundCloud channels on the watchlist."""
    key = f"sc|{days}"
    if not force:
        hit = _serve_or_refresh(key, lambda: releases_soundcloud(days=days, force=1))
        if hit is not None:
            return hit

    from ripster.routes.soundcloud import sc_user_tracks
    from ripster.routes.watchlist import _sc_permalink

    entries = _watch_entries("soundcloud")
    if not entries:
        # Not an error: nothing followed yet. The UI shows a hint rather than
        # an empty feed with no explanation.
        return _store(key, {"ok": True, "releases": [], "sources": 0,
                            "hint": "no_channels"})

    cutoff = _cutoff(days)
    sem = asyncio.Semaphore(3)

    async def _one(entry: dict) -> list:
        permalink = _sc_permalink(entry)
        if not permalink:
            return []
        async with sem:
            try:
                r = await sc_user_tracks(permalink=permalink, limit=15)
            except Exception as e:
                print(f"[radar] sc {permalink}: {e}", flush=True)
                return []
        if not r.get("ok"):
            return []
        out = []
        for tr in r.get("results", []):
            date = (tr.get("date") or "")[:10]
            if not date or date < cutoff:
                continue
            out.append({
                "id":        str(tr.get("id", "")),
                "title":     tr.get("title", ""),
                "artist":    tr.get("artist") or entry.get("name", permalink),
                "artist_id": permalink,
                "type":      "mix",
                "group":     "mix",
                "date":      date,
                "year":      date[:4],
                "tracks":    None,
                "cover":     tr.get("artwork_sm") or tr.get("artwork", ""),
                "url":       tr.get("url", ""),
                "service":   "soundcloud",
                "duration":  tr.get("duration") or 0,
            })
        return out

    results = await asyncio.gather(*(_one(e) for e in entries),
                                   return_exceptions=True)
    releases = [r for res in results if isinstance(res, list) for r in res]
    # Склад: то, что источник уже не показывает, всё равно остаётся.
    releases = await _durable_merge("soundcloud", releases, days)
    return _store(key, {"ok": True, "releases": releases,
                        "sources": len(entries)})


# ── Apple: только миксы (live-сессии и DJ-миксы) ─────────────────────────────
# Спека радара: этот источник приносит МИКСТРЕКУЩИХСЯ артистов — живые сессии
# и DJ-миксы. Обычные альбомы и лейбловые сборники здесь не нужны: за ними и
# так следит сам вишлист, а радар дублировал их отдельным источником.
#
# Отбор идет по ДАННЫМ РЕЛИЗА, а не по списку «правильных» артистов: микс
# узнаётся по прямому называнию (DJ Mix, Mixed by, Continuous Mix), по жанру
# «DJ-Mixes» и по длительности треков (часовой трек-однушка — это сет, а не
# песня). Слова «Live», «Sessions», «Mixtape» сами по себе ничего не значат
# (концертник «Live at Wembley» — не микс), поэтому принимаются только вместе
# с жанром или хронометражом.

# Прямое называние сета: этого признака хватает самого по себе.
_MIX_TITLE_RE = re.compile(
    r"\bdj[\s&/-]*mix(?:es|set)?\b|\bmixed\s+by\b|\bcontinuous\s+mix\b",
    re.IGNORECASE)
# Слабые слова: у концертника «Live at Wembley» и у акустических «Sessions»
# они тоже есть, поэтому в одиночку не принимаются — нужен жанр или хронометраж.
_MIX_WEAK_RE = re.compile(r"\blive\b|\bsessions?\b|\bmixed\b|\bmixtape\b",
                          re.IGNORECASE)
_MIX_GENRE_RE = re.compile(r"dj[\s-]*mix", re.IGNORECASE)
_MIX_CONT_MS = 40 * 60_000      # одна дорожка ≥ 40 мин — непрерывный сет
_MIX_AVG_MS = 12 * 60_000       # средняя длина треков ≥ 12 мин — сессионный лонг-форм
_MIX_MIN_TOTAL_MS = 40 * 60_000 # и общий хронометраж ≥ 40 мин, а не три баллады


def _apple_mix_verdict(rel: dict) -> tuple[bool, str]:
    """Микс или обычный релиз — по данным релиза. (is_mix, reason).

    rel: name, genres (list[str]), track_times (list[int] ms; [] = не знаем).
    Причина возвращается не для красоты: проверка источника показывает
    ОТКЛОНЁННЫЕ релизы вместе с тем, почему они отклонены.
    """
    name = str(rel.get("name") or "")
    m = _MIX_TITLE_RE.search(name)
    if m:
        return True, f"title:«{m.group(0).lower()}»"
    if any(_MIX_GENRE_RE.search(str(g) or "") for g in (rel.get("genres") or [])):
        return True, "genre:dj-mixes"
    weak = _MIX_WEAK_RE.search(name)
    times = [t for t in (rel.get("track_times") or []) if t]
    if times:
        total = sum(times)
        if len(times) == 1 and total >= _MIX_CONT_MS:
            return True, f"continuous:{total // 60000} min set"
        if total >= _MIX_MIN_TOTAL_MS and total / len(times) >= _MIX_AVG_MS:
            return True, f"long-form:avg {total // len(times) // 60000} min"
        w = f" (weak title «{weak.group(0).lower()}» unconfirmed)" if weak else ""
        return False, (f"regular:{len(times)} tracks, "
                       f"avg {total // len(times) // 60000} min{w}")
    if weak:
        # Длительностей ещё нет — сборщик добьёт их лукупом и спросит снова.
        return False, f"weak title «{weak.group(0).lower()}», no proof yet"
    if rel.get("is_compilation"):
        return False, "compilation"
    return False, "regular album"


async def _apple_itunes(client, params: dict) -> list:
    """Публичный iTunes lookup — без токена. Пустой список при любом отказе."""
    try:
        r = await client.get("https://itunes.apple.com/lookup", params=params)
        if r.status_code != 200:
            return []
        return (r.json() or {}).get("results") or []
    except Exception as e:
        print(f"[radar] apple lookup ({params.get('entity')}): {e}", flush=True)
        return []


def _apple_cover(row: dict) -> str:
    return (row.get("artworkUrl100", "") or "").replace("100x100", "400x400")


async def _apple_artist_albums(client, artist_id: str, storefront: str,
                               with_comps: bool) -> list:
    """Все релизы артиста из публичного iTunes: свои альбомы плюс родительские
    коллекции его треков (так видны миксы, где артист — один из дорожек,
    «Boiler Room x …»-образные)."""
    albums: dict[str, dict] = {}
    for x in await _apple_itunes(client, {"id": artist_id, "entity": "album",
                                          "limit": 100, "sort": "recent",
                                          "country": storefront}):
        if x.get("wrapperType") != "collection" or not x.get("collectionId"):
            continue
        albums[str(x["collectionId"])] = {
            "id": str(x["collectionId"]),
            "name": x.get("collectionName", ""),
            "artist": x.get("artistName", ""),
            "date": (x.get("releaseDate", "") or "")[:10],
            "cover": _apple_cover(x),
            "url": x.get("collectionViewUrl", ""),
            "genres": [x.get("primaryGenreName", "")] if x.get("primaryGenreName") else [],
            "track_count": x.get("trackCount"),
            "is_compilation": _comps.is_compilation(
                album_artist=x.get("artistName", ""),
                title=x.get("collectionName", "")),
            "track_times": [],
        }
    if not with_comps:
        return list(albums.values())
    # Родительские коллекции их треков — так видны миксы, где артист не значится
    # альбом-артистом («Boiler Room x …», лейбловые сессии). Отдельных лукупов
    # не просим: у трек-строки уже есть collectionName, жанр и длительность —
    # ровно те признаки, по которым вердикт и выносится.
    parents: dict[str, dict] = {}
    for x in await _apple_itunes(client, {"id": artist_id, "entity": "song",
                                          "limit": 200, "sort": "recent",
                                          "country": storefront}):
        cid = str(x.get("collectionId") or "")
        if x.get("wrapperType") != "track" or not cid or cid in albums:
            continue
        p = parents.setdefault(cid, {
            "id": cid,
            "name": x.get("collectionName", ""),
            "artist": x.get("collectionArtistName") or x.get("artistName", ""),
            "date": (x.get("releaseDate", "") or "")[:10],
            "cover": _apple_cover(x),
            "url": x.get("collectionViewUrl", ""),
            "genres": [x.get("primaryGenreName", "")] if x.get("primaryGenreName") else [],
            "track_count": 0,
            "is_compilation": _comps.is_compilation(
                album_artist=x.get("collectionArtistName") or x.get("artistName", ""),
                title=x.get("collectionName", "")),
            "track_times": [],
        })
        p["track_count"] += 1
        if x.get("trackTimeMillis"):
            p["track_times"].append(x["trackTimeMillis"])
    albums.update({k: v for k, v in parents.items() if k not in albums})
    return list(albums.values())


async def _apple_catalog_boost(client, artist_id: str, storefront: str,
                               bearer: str, albums: dict) -> None:
    """Каталог с bearer из конфига: точные genreNames, честный isCompilation и
    recordLabel. Публичный lookup называет жанром только один тег, а каталог —
    весь список, и именно там миксы лежат под «DJ-Mixes»; `recordLabel` там же —
    публичный lookup лейбла не отдаёт вовсе. Молча проходим мимо, если токена нет
    или каталог его не принимает."""
    if not bearer:
        return
    hdr = {"Authorization": f"Bearer {bearer}", "Origin": "https://music.apple.com",
           "Accept": "application/json"}
    params = {"limit": "25",
              "fields[albums]": "name,artistName,artworkUrl100,releaseDate,"
                                "trackCount,isCompilation,isSingle,genreNames,"
                                "recordLabel,url"}
    off = 0
    for _ in range(4):                            # ≤100 релизов на артиста
        try:
            r = await client.get(
                f"https://amp-api.music.apple.com/v1/catalog/{storefront}/artists/{artist_id}/albums",
                headers=hdr, params={**params, "offset": str(off)})
            if r.status_code != 200:
                return
            data = (r.json() or {}).get("data") or []
        except Exception as e:
            print(f"[radar] apple catalog {artist_id}: {e}", flush=True)
            return
        for x in data:
            a = x.get("attributes") or {}
            cid = str(x.get("id") or "")
            if not cid:
                continue
            g = albums.get(cid)
            if g is None:
                albums[cid] = g = {"id": cid, "name": a.get("name", ""),
                                   "artist": a.get("artistName", ""),
                                   "date": (a.get("releaseDate", "") or "")[:10],
                                   "cover": _apple_cover(a), "url": a.get("url", ""),
                                   "genres": [], "label": "",
                                   "track_count": a.get("trackCount"),
                                   "is_compilation": bool(a.get("isCompilation")),
                                   "track_times": []}
            g["genres"] = list(a.get("genreNames") or []) or g["genres"]
            # Лейбл — доказательство для правила якоря (23.09): у Aruna жанры
            # подписки склеены в кашу, а «Enhanced Recordings» на карточке
            # отличает её транс от телугу-певца с тем же именем.
            g["label"] = str(a.get("recordLabel") or "") or g.get("label") or ""
            g["is_compilation"] = bool(a.get("isCompilation"))
            g["track_count"] = a.get("trackCount") or g["track_count"]
            g["cover"] = g["cover"] or _apple_cover(a)
            g["url"] = g["url"] or a.get("url", "")
        if len(data) < 25:
            return
        off += 25


async def _apple_album_times(client, album_id: str, storefront: str) -> list:
    rows = await _apple_itunes(client, {"id": album_id, "entity": "song",
                                        "limit": 200, "country": storefront})
    return [x["trackTimeMillis"] for x in rows
            if x.get("wrapperType") == "track" and x.get("trackTimeMillis")]


async def collect_apple_mixes(client, entries: list, storefront: str = "us",
                              bearer: str = "", days: int = 90,
                              with_comps: bool = True) -> dict:
    """Миксы артистов вишлиста: {"mixes": [radar-предмет…], "rejected": [доказательство]}.

    rejected — обычные релизы из того же окна с причиной отклонения: по ним
    видно, что фильтр не просто «ничего не нашёл», а именно отсеивает альбомы.
    """
    cutoff = _cutoff(days)
    today = datetime.now().strftime("%Y-%m-%d")
    sem = asyncio.Semaphore(4)
    mixes, rejected = [], []

    async def _one(entry: dict):
        aid = str(entry.get("artist_id") or "")
        name = entry.get("name", "")
        async with sem:
            try:
                albums = await _apple_artist_albums(client, aid, storefront, with_comps)
                byid = {a["id"]: a for a in albums}
                await _apple_catalog_boost(client, aid, storefront, bearer, byid)
                todo = []
                for a in byid.values():
                    if not a["date"] or a["date"] < cutoff or a["date"] > today:
                        continue
                    ok, why = _apple_mix_verdict(a)
                    if ok:
                        if _home_ok(entry, a):
                            mixes.append(_apple_mix_item(a, entry, why))
                    elif not a["track_times"]:
                        todo.append(a)   # вердикт вслепую — добьем длительностями
                for a in todo[:8]:              # ≤8 лукупов на артиста — окно и так узкое
                    if a.get("track_times"):
                        continue
                    a["track_times"] = await _apple_album_times(client, a["id"], storefront)
                    ok, why = _apple_mix_verdict(a)
                    if ok and not _home_ok(entry, a):
                        continue
                    (mixes if ok else rejected).append(
                        _apple_mix_item(a, entry, why) if ok
                        else {"artist": name, "title": a["name"], "date": a["date"],
                              "reason": why})
            except Exception as e:
                print(f"[radar] apple {name}: {e}", flush=True)

    await asyncio.gather(*(_one(e) for e in entries))
    seen, uniq = set(), []
    for r in mixes:
        if r["id"] in seen:
            continue
        seen.add(r["id"])
        uniq.append(r)
    uniq.sort(key=lambda x: x["date"], reverse=True)
    return {"mixes": uniq, "rejected": rejected[:200]}


def _home_ok(entry: dict, rel: dict) -> bool:
    """Пустить ли релиз со страницы подписки; скрытое — пометить, не стереть."""
    ok, why = _ident.home_show(entry, rel)
    if ok:
        return True
    _ident.hidden_add(entry, rel, why)
    if _s.get("save_watchlist"):
        _s["save_watchlist"](_s.get("watchlist") or [])
    print(f"[radar] {entry.get('name')}: скрыт «{rel.get('name') or rel.get('title')}»"
          f" — {why}", flush=True)
    return False


def _apple_mix_item(a: dict, entry: dict, why: str) -> dict:
    # id — идентификатор коллекции, а НЕ url: у релиза, найденного через трек
    # артиста, url несёт «?i=<трек>» и у каждого артиста свой, из-за чего один
    # и тот же микс приезжал в ленту по разу на каждого участника.
    url = (a.get("url") or "").split("?i=")[0]
    return {
        "id":        a.get("id") or url or a.get("name", ""),
        "title":     a.get("name", ""),
        "artist":    entry.get("name", ""),
        "artist_id": entry.get("artist_id", ""),
        "alb_artist": a.get("artist", ""),
        "type":      "mix",
        "group":     "mix",
        "mix_signal": why,
        "date":      a.get("date", ""),
        "year":      (a.get("date") or "")[:4],
        "tracks":    a.get("track_count"),
        "cover":     a.get("cover", ""),
        "url":       url,
        "service":   "apple",
        # Доказательства для правила якоря (`artist_identity.anchor_show`):
        # без них карточка нечем не отвечает на вопрос «чьё это» и проходит
        # молча — а должна быть видна в отчёте как «нечем судить».
        "label":     str(a.get("label") or ""),
        "genres":    list(a.get("genres") or []),
    }


@router.get("/api/releases/apple")
async def releases_apple(days: int = Query(90, ge=1, le=365),
                         force: int = Query(0)):
    """Apple-миксы артистов вишлиста — живые сессии и DJ-миксы, и НИЧЕГО
    сверх них. Обычные альбомы и сборники отсевает _apple_mix_verdict.

    Ключи кэша и склада — apple_mixes, а не apple: старая выдача была собранной
    лентой альбомов, подмешать её к миксам означало бы кормить владельца ими
    ещё и со склада."""
    key = f"apple_mixes|{days}"
    if not force:
        hit = _serve_or_refresh(key, lambda: releases_apple(days=days, force=1))
        if hit is not None:
            return hit

    import httpx

    entries = [e for e in _watch_entries("apple") if e.get("artist_id")]
    if not entries:
        return _store(key, {"ok": True, "releases": [], "sources": 0,
                            "hint": "no_artists"})

    cfg        = _s.get("config") or {}
    storefront = cfg.get("storefront", "us") or "us"
    with_comps = cfg.get("watchlist-compilations", True) is not False
    bearer     = str(cfg.get("authorization-token") or "").strip()

    async with httpx.AsyncClient(timeout=20) as client:
        got = await collect_apple_mixes(client, entries, storefront, bearer,
                                        days, with_comps)
    uniq = await _durable_merge("apple_mixes", got["mixes"], days)
    return _store(key, {"ok": True, "releases": uniq, "sources": len(entries),
                        "rejected": len(got["rejected"])})


# ── Лейблы ────────────────────────────────────────────────────────────────────
# Лейбл — не артист: у него нет id, xref его не резолвит, и именно поэтому
# радар отбрасывал записи `kind == "label"` (releases.py:_watchlist_artists).
# Отдельный источник — единственный честный способ их показать: спрашиваем не
# «что выпустил артист», а «что выпустил лейбл», через тот же `_label_releases`,
# которым живёт подписка на лейбл в вишлисте.
#
# Источник ВЫКЛЮЧЕН по умолчанию и включается ключом `show-radar-labels`.
# Пока владелец его не включил, эндпоинт не ходит ни в один каталог и отдаёт
# пустоту: живая лента радара не должна меняться молча.

def _label_entries() -> list:
    return [e for e in (_s.get("watchlist") or [])
            if e.get("kind") == "label" and str(e.get("name") or "").strip()]


def _label_date(rel: dict) -> str:
    """Дата релиза в виде YYYY-MM-DD.

    Каталоги отдают лейбловые релизы то полной датой, то одним годом. Год без
    месяца нельзя сравнивать со срезом окна как строку («2024» < «2026-01-01»
    случайно верно, а «2026» < «2026-01-01» — уже нет), поэтому нормализуем.
    """
    d = str(rel.get("date") or "").strip()
    if len(d) >= 10:
        return d[:10]
    if len(d) == 7:
        return d + "-01"
    if len(d) == 4:
        return d + "-01-01"
    y = str(rel.get("year") or "").strip()
    return (y + "-01-01") if len(y) == 4 else ""


@router.get("/api/releases/labels")
async def releases_labels(days: int = Query(90, ge=1, le=9999),
                          force: int = Query(0)):
    """Новые релизы лейблов из вишлиста — если источник включён владельцем."""
    cfg = _s.get("config") or {}
    if cfg.get("show-radar-labels") is not True:
        # Выключено: ни запросов, ни записей в кэш, ни строчки в ленте.
        return {"ok": True, "releases": [], "sources": 0, "hint": "labels_off"}

    # labels2, а не labels: с 16.08 у источника изменилась верхняя граница дат
    # (см. ниже про +2 дня). Старые записи кэша собраны по прежнему правилу и
    # НЕ содержат завтрашних релизов — оставить прежний ключ значило бы кормить
    # владельца прежним ответом ещё сутки и выглядеть как «правка не помогла».
    key = f"labels2|{days}"
    if not force:
        hit = _serve_or_refresh(key, lambda: releases_labels(days=days, force=1))
        if hit is not None:
            return hit

    from ripster.routes.watchlist import _label_releases

    entries = _label_entries()
    if not entries:
        return _store(key, {"ok": True, "releases": [], "sources": 0,
                            "hint": "no_labels"})

    cutoff = _cutoff(days)
    # Верхняя граница НЕ «сегодня», а сегодня + 2 дня. Отсечку `date > today`
    # источник лейблов унаследовал от Apple, где рядом написано «pre-orders carry
    # a future date — they are not out yet». Для предзаказов на месяцы вперёд это
    # верно, а для лейблов ломает главное: релиз, датированный завтра по местному
    # календарю, в Новой Зеландии уже вышел и уже качается. Ровно это опережение
    # на полсуток и описано в докстринге releases.py как смысл кросс-сервисного
    # радара — а здесь оно молча выбрасывалось. +2 дня ловят и часовые пояса, и
    # завтрашний релиз, но оставляют настоящие предзаказы за бортом.
    today  = (datetime.now() + timedelta(days=2)).strftime("%Y-%m-%d")
    sem    = asyncio.Semaphore(3)

    async def _one(entry: dict) -> list:
        name = str(entry.get("name") or "").strip()
        async with sem:
            try:
                rels = await _label_releases(name, 40)
            except Exception as e:
                print(f"[radar] label {name}: {e}", flush=True)
                return []
        out = []
        for r in rels:
            date = _label_date(r)
            # Предзаказ (дата в будущем) ещё не вышел — как и у Apple-источника.
            if not date or date < cutoff or date > today:
                continue
            out.append({
                "id":        str(r.get("id") or r.get("url") or ""),
                "title":     r.get("title", ""),
                "artist":    r.get("artist", ""),
                "artist_id": "",              # у лейблового сида id артиста нет
                "label":     name,            # чем помечен: именем лейбла…
                "via_label": True,            # …и признаком «пришёл от лейбла»
                "type":      (r.get("type") or "album"),
                "group":     "album",
                "date":      date,
                "year":      date[:4],
                "tracks":    r.get("tracks"),
                "cover":     r.get("cover", ""),
                "url":       r.get("url", ""),
                "service":   r.get("service") or "spotify",
            })
        return out

    results = await asyncio.gather(*(_one(e) for e in entries),
                                   return_exceptions=True)
    releases = [r for res in results if isinstance(res, list) for r in res]
    # Один и тот же релиз приходит и из Spotify, и из Deezer, и через два лейбла.
    seen, uniq = set(), []
    for r in releases:
        uid = _rel_uid(r)
        if uid in seen:
            continue
        seen.add(uid)
        uniq.append(r)
    uniq.sort(key=lambda x: x["date"], reverse=True)
    # Склад: то, что источник уже не показывает, всё равно остаётся.
    uniq = await _durable_merge("labels", uniq, days)
    return _store(key, {"ok": True, "releases": uniq, "sources": len(entries)})
