"""Свёртка дублей анонса и слияние источников.

Два разных контракта, и их важно не путать:

  `identity()` / `merge()` / `put()`  склад: склейка ТОЛЬКО по UPC/ISRC, иначе
      из двух карточек родится релиз, которого нет ни в одном каталоге.

  `soft_key()` / `fold_duplicates()`  лента: тот же анонс на Bandcamp и в
      витрине показывается одной карточкой, но склейка строгая — название И
      артист И дата. Разошлась дата — это два анонса, и пусть их будет два.

Болячка, из-за которой это вообще проверено: 24.09.2026 SEMANTICA 199 (Oscar
Mulero, предзаказ Semantica) не был виден в радаре вообще. Теперь он приходит
с Bandcamp через месяц-другой до того, как встанет в Apple/Deezer, и без
свёртки человек увидел бы один анонс в трёх карточках.
"""
from __future__ import annotations

from ripster import upcoming as up


def _rec(src: str, *, title="Between Two Worlds. SEMANTICA 199",
         artist="Oscar Mulero", date_raw="2026-09-25",
         upc: str = "", url: str = "", label: str = "Semantica Records",
         ident: str = "", cover: str = "", preorder: bool = False,
         **extra) -> dict | None:
    """Дату передаём уже в ISO: и Bandcamp-парсер (`bc.parse_bc_date`), и
    Beatport-ветка нормализуют ДО `make_record`. Разбор строки вида
    «25 Sep 2026 00:00:00 GMT» — отдельный тест в `test_bandcamp_parser.py`,
    и не надо смешивать контракт парсера с контрактом свёртки."""
    ident = ident or f"{src}-{title}"
    url = url or f"https://example.invalid/{src}/{ident}"
    return up.make_record(
        src=src, src_url=url, date_raw=date_raw, ident=ident,
        title=title, artist=artist, url=url, label=label, upc=upc,
        cover=cover, preorder=preorder, **extra)


# ── склад: identity / merge / put ────────────────────────────────────────────

def test_identity_picks_upc_over_src_scoped_key():
    """Релиз с UPC склеивается между источниками. Тот же релиз без UPC
    живёт каждый в своём источнике — склеивать по «артист + название»
    запрещено (урок в доке `identity`)."""
    with_upc = _rec("bandcamp", upc="196589905993")
    without_upc = _rec("bandcamp")
    assert with_upc and without_upc
    assert up.identity(with_upc) == "upc:196589905993"
    assert up.identity(without_upc).startswith("bandcamp:")


def test_merge_across_sources_with_same_upc_confirms():
    bc = _rec("bandcamp", upc="196589905993",
              date_raw="2026-09-25", preorder=True)
    ap = _rec("apple", upc="196589905993", date_raw="2026-09-25",
              ident="123", url="https://music.apple.com/album/123")
    merged = up.merge([bc, ap])
    assert len(merged) == 1, "одинаковый UPC — один релиз в складе"
    assert set(merged[0]["confirmed_by"]) >= {"bandcamp", "apple"}


def test_merge_without_upc_does_not_glue_different_sources():
    """Разные источники без UPC — РАЗНЫЕ записями. Иначе из «Oscar Mulero —
    Between Two Worlds» (Bandcamp) и «Oscar Mulero — Between Two Worlds
    (Extended)» (Apple) вырос бы гибрид, которого ни в одном каталоге нет."""
    bc = _rec("bandcamp")
    ap = _rec("apple", title="Between Two Worlds (Extended)")
    merged = up.merge([bc, ap])
    assert len(merged) == 2


# ── лента: soft_key / fold_duplicates ────────────────────────────────────────

def test_fold_prefers_catalog_and_keeps_bandcamp_in_also_on():
    """Bandcamp предзаказ + витрина Apple с тем же UPC — одна карточка,
    головой которой становится витрина (её ссылку человек и кликнет), а
    Bandcamp уезжает в `also_on`."""
    bc = _rec("bandcamp", upc="196589905993", preorder=True,
              url="https://semanticarecords.bandcamp.com/album/x")
    ap = _rec("apple", upc="196589905993",
              url="https://music.apple.com/us/album/123")
    out = up.fold_duplicates([bc, ap])
    assert len(out) == 1
    head = out[0]
    assert head["src"] == "apple"
    assert [o["src"] for o in head["also_on"]] == ["bandcamp"]
    assert head["also_on"][0]["url"].endswith("/album/x"), \
        "Bandcamp-ссылка обязана остаться видимой"
    assert head["also_on"][0]["date"] == "2026-09-25"


def test_fold_keeps_two_cards_when_dates_disagree():
    """Дата разошлась — это два разных анонса, и честно показать оба.
    Склеить по «артист+название» — значит выбрать одну из двух дат наугад."""
    bc = _rec("bandcamp", date_raw="25 Sep 2026 00:00:00 GMT")
    lb = _rec("label", date_raw="2026-10-02")
    out = up.fold_duplicates([bc, lb])
    assert len(out) == 2
    assert all(not o.get("also_on") for o in out)


def test_fold_refuses_to_merge_without_date():
    """Ни даты, ни UPC — свёртки нет. Две карточки с одним названием, но
    без даты, это разные вещи: релиз перенесли, или это две разные публикации."""
    bc = _rec("bandcamp", date_raw="")
    ap = _rec("apple", date_raw="", ident="42")
    # make_record обязана пропустить карточку без даты? — нет: она обязана
    # пропустить запись без src_url/ident/title. Пустая дата допустима.
    assert bc and ap
    out = up.fold_duplicates([bc, ap])
    assert len(out) == 2, "soft_key без даты и UPC пуст — карточки не сворачиваем"


def test_fold_inherits_score_and_dedups_why():
    """Свёртка не имеет права унести карточку вниз только потому, что более
    надёжный источник её не оценил. Причина «лейбл в наблюдении» обязана
    пережить склейку — иначе человек видит витрину без объяснения, почему она
    вообще в ленте."""
    bc = _rec("bandcamp", upc="196589905993")
    bc["score"] = 130
    bc["why"] = [{"key": "up.why.watched_label", "args": {"label": "Semantica Records"}}]
    ap = _rec("apple", upc="196589905993", date_raw="2026-09-25")
    ap["score"] = 0
    ap["why"] = [{"key": "up.why.watched_label", "args": {"label": "Semantica Records"}}]
    out = up.fold_duplicates([bc, ap])
    assert len(out) == 1
    head = out[0]
    assert head["src"] == "apple"
    assert head["score"] == 130, "интерес Bandcamp-карточки обязан переехать в голову"
    assert len(head["why"]) == 1, "одинаковая `why` не удваивается"


def test_fold_carries_confirmed_by_from_both_sides():
    bc = _rec("bandcamp", upc="196589905993")
    lb = _rec("label", upc="196589905993", date_raw="2026-09-25")
    lb["confirmed_by"] = ["label", "deezer"]
    out = up.fold_duplicates([bc, lb])
    assert len(out) == 1
    assert "bandcamp" in out[0]["confirmed_by"]
    assert "deezer" in out[0]["confirmed_by"], \
        "потеря подтверждений — это потеря доверия к релизу"


def test_fold_leaves_records_without_key_untouched():
    """Запись без артиста или без названия — не «похожа ни на что»: её надо
    показать как есть, не выбросить и не склеить с соседом."""
    orphan = {"src": "bandcamp", "ident": "x", "title": "", "artist": ""}
    out = up.fold_duplicates([orphan])
    assert out == [orphan]


# ── сквозная регрессия ───────────────────────────────────────────────────────

def test_semantica_199_bandcamp_preorder_survives_the_pipeline():
    """Карточка, которую радар не показал вовсе. Здесь — весь путь: разбор
    `make_record` → `identity` → склад `put` → отбор по горизонту → свёртка.
    На каждом шаге SEMANTICA 199 обязана остаться и остаться предзаказом."""
    bc = _rec("bandcamp",
              ident="3334282113",
              url="https://semanticarecords.bandcamp.com/album/"
                  "between-two-worlds-semantica-199",
              preorder=True, cover="https://f4.bcbits.com/img/x.jpg")
    assert bc, "make_record отбраковала предзаказ — контракт не выполнен"
    assert bc["src_tier"] == 2, "Bandcamp — витрина, не каталог"
    assert bc["date"] == "2026-09-25"
    store: dict = {}
    added = up.put(store, [bc])
    assert added == 1 and up.identity(bc) in store
    # Тот же анонс приходит с лейбловой ветки, у которой UPC не спросили:
    # different `ident`, same soft_key — и склад остаётся честным (2 записи,
    # UPC/ISRC нет), а ЛЕНТА сворачивает их в одну карточку.
    lb = _rec("label", date_raw="2026-09-25", ident="spotify:album:999",
              url="https://open.spotify.com/album/999")
    merged = up.merge([lb])
    up.put(store, merged)
    assert len(store) == 2, "склад не имеет права склеивать без UPC"
    out = up.fold_duplicates(list(store.values()))
    assert len(out) == 1, "лента же обязана показать ОДИН анонс"
    head = out[0]
    assert head["src"] == "label", "ярус 1 (лейбл/витрина каталога) — голова"
    assert head["also_on"][0]["url"].endswith("/album/"
                                              "between-two-worlds-semantica-199"), \
        "Bandcamp-ссылка должна остаться кликабельной"
