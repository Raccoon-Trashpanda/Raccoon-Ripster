"""Предзаказы watched-лейблов и watched-артистов — то, чего в каталогах ещё нет.

Два источника, и оба — витрины, а не пресса (24.09.2026, владелец):

  Bandcamp  андеграундный лейбл выкладывает предзаказ сюда за недели до выхода,
            когда ни в Spotify, ни в Apple, ни в Deezer релиза ещё нет.
            SEMANTICA 199 (Oscar Mulero, выход 25.09.2026) не попал в радар
            именно потому, что этого источника не было.
  Beatport  у лейбровой и артистской выдачи `publish_date` бывает БУДУЩЕЙ датой
            и стоит флаг `is_pre_order` — переиспользуем уже живущее в проекте
            соединение с Beatport (учётка в настройках), парсер не заводим новый.

Записи уходят в ТОТ ЖЕ склад, что и остальное грядущее (`ripster.upcoming`), и в
ТУ ЖЕ ленту — отдельного списка не появляется.

Магазиночные ленты («coming soon» Juno / deejay.de / decks) сознательно здесь
нет: почему — в `SHOP_FEEDS` ниже, честно, а не «когда-нибудь».
"""
from __future__ import annotations

from datetime import datetime

from ripster import bandcamp as _bc
from ripster import label_sources as _ls
from ripster import upcoming as _up

#: Позиций сетки, раскрываемых на одну страницу Bandcamp за проход. Дата выхода
#: и признак предзаказа есть только на странице релиза, а страница лейбла их не
#: отдаёт — поэтому «взять всё» = сотни запросов.
BC_PROBE = 8

#: Витрины-магазины, которые мы НЕ парсим, и почему. Владелец просил: «только
#: если разбор надёжный, иначе — задокументировать».
SHOP_FEEDS = {
    "juno":       "страницы лейбла отдаются переполненной JS-разметкой без "
                  "устойчивого контракта; JSON-эндпоинт требует партнёрского ключа",
    "decks":      "агрегирует релиз-даты, но отдаёт 403 краулеру без браузерных "
                  "заголовков; публичного API нет",
    "deejayde":   "закрыт Cloudflare для запросов без cookies",
    "traxsource": "сетка /genre/<id>/<name>/upcoming отдаётся без дат: дата "
                  "предзаказа видна только на странице релиза",
    "boomkat":    "публичной лейбловой ленты «coming soon» нет",
    "bleep":      "listing с датой, но без идентификатора релиза: запись без "
                  "`ident` не имеет права склеиваться с каталогом "
                  "(контракт ripster/upcoming.py)",
}


def _watch_pairs(watchlist: list) -> tuple[list, list]:
    """[(имя, bandcamp_url)] для лейблов и артистов вотчлиста."""
    labels, artists = [], []
    for e in watchlist or []:
        name = str(e.get("name") or "").strip()
        if not name:
            continue
        bc_url = str(e.get("bandcamp_url") or "").strip()
        (labels if e.get("kind") == "label" else artists).append((name, bc_url))
    return labels, artists


def _record(card: dict, *, src: str, label_hint: str = "") -> dict | None:
    """Карточка Bandcamp/Beatport → запись склада грядущего.

    Дата берётся дословно у источника; без даты записи не бывает — иначе
    «предзаказ» превратился бы в «когда-нибудь».
    """
    raw = str(card.get("date") or card.get("date_raw") or "")
    ident = str(card.get("item_id") or card.get("id") or card.get("url") or "")
    return _up.make_record(
        src=src, src_url=card.get("url") or "", date_raw=raw, ident=ident,
        title=card.get("title") or "", artist=card.get("artist") or "",
        cover=card.get("cover") or "", url=card.get("url") or "",
        label=card.get("label") or label_hint or "",
        upc=card.get("upc") or "", type="album",
        preorder=bool(card.get("preorder")),
        formats=card.get("formats") or [],
        tracklist=[t.get("title") for t in (card.get("tracklist") or [])][:40],
        tracks=int(card.get("track_count") or card.get("tracks") or 0))


async def bandcamp_preorders(watchlist: list, probe: int = BC_PROBE,
                             only: list | None = None) -> tuple[list, dict]:
    """Грядущие предзаказы со страниц Bandcamp watched-лейблов и артистов.

    `only` — ограничить обход несколькими именами (первый проход, фоновый цикл
    и тесты не должны платить за весь вотчлист).
    Возврат: `(записи, журнал)`; в журнале у каждого имени — статус и причина
    молчания, потому что «лейбл не нашёл свою страницу на Bandcamp» и «мы не
    заглядывали» для человека выглядят одинаково.
    """
    labels, artists = _watch_pairs(watchlist or [])
    todo = (labels + artists) if only is None else [
        p for p in (labels + artists) if p[0] in set(only)]
    out, log = [], {}
    for name, manual_url in todo:
        try:
            got = await _bc.band_releases(name=name, band_url=manual_url, probe=probe)
        except Exception as e:
            log[name] = {"status": "error", "error": f"{type(e).__name__}: {e}"}
            continue
        rels = got.get("releases") or []
        page = got.get("band") or ""
        made = 0
        for card in rels:
            if not _bc.is_future(card):
                continue                      # вышедшее — не грядущее
            rec = _record(card, src="bandcamp",
                          label_hint=(got.get("band_name") or name))
            if rec:
                rec["via_label"] = bool(manual_url or got.get("band_name"))
                out.append(rec)
                made += 1
        log[name] = {"status": "ok" if page else "no_band_page",
                     "band": page, "seen": len(rels), "preorders": made}
    return out, log


async def beatport_preorders(watchlist: list, per_label: int = 12,
                             only: list | None = None) -> tuple[list, dict]:
    """Грядущие релизы watched-лейблов с Beatport (`is_pre_order`, будущая дата).

    Переиспользует `_ls._beatport_label_releases`: там уже разбор `label.name` и
    `publish_date`. Без учётки Beatport источник честно молчит статусом
    `no_creds` — он не «не нашёл», он «не спросил».
    """
    labels, _artists = _watch_pairs(watchlist or [])
    out, log = [], {}
    for name, _u in labels:
        if only is not None and name not in set(only):
            continue
        try:
            got, st = await _ls._beatport_label_releases(name, per_label)
        except Exception as e:
            log[name] = {"status": "error", "error": f"{type(e).__name__}: {e}"}
            continue
        made = 0
        for s in got:
            if not _up.in_horizon({"date": s.get("date") or ""}):
                continue
            rec = _record(s, src="beatport", label_hint=name)
            if rec:
                rec["preorder"] = bool(s.get("preorder")) or (rec.get("date", "") >
                                                              datetime.now().strftime("%Y-%m-%d"))
                out.append(rec)
                made += 1
        log[name] = {"status": st.get("status"), "seen": st.get("candidates"),
                     "preorders": made}
        if st.get("error"):
            log[name]["error"] = st["error"]
    return out, log


async def collect(watchlist: list, only: list | None = None,
                  with_beatport: bool = True) -> tuple[list, dict]:
    """Собрать предзаказы по обоим источникам. Сеть, вежливо.

    Bandcamp сам по себе медленный (не чаще запроса в 2 с), поэтому в фоновом
    цикле его обходят по чуть-чуть, а не всем списком за раз: `only` режет
    вотчлист на порции.
    """
    bc, bc_log = await bandcamp_preorders(watchlist, only=only)
    bp, bp_log = (await beatport_preorders(watchlist, only=only)
                  if with_beatport else ([], {}))
    return _up.merge(bc + bp), {"bandcamp": bc_log, "beatport": bp_log}
