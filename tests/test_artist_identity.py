"""Однофамильцы в радаре: тождество решают работы, а не имя.

Жалоба владельца живёт с 03.09.2026: в «Релизах» показывало чужих артистов —
«BOP» (Apple 719709163, drum&bass) получал каретки одноимённого рэпера, а
«Solomon Grey» (668535482) — испанские госпел-синглы другого Solomon Grey.
Виновник — сверка ПО ИМЕНИ: прежний резолвер «имя → id» (`artist_xref.resolve_many`,
удалён) и `musicbrainz.search_artist` искали кандидатов по строке `name`, а при
неоднозначности брал самого популярного. Имя у этих людей одинаковое, личность разная.

Тесты держат ровно те ловушки, на которых прежняя схема падала, и границу, за
которую новая не имеет права заходить: «не знаю» — это не «чужой».
"""
import json

import pytest

from ripster import artist_identity as ai


# ── classify: чистое решение «тот / не тот / не знаю» ───────────────────────

def test_same_person_confirmed_by_shared_works():
    anchor = [{"title": "Human Music", "label": "Mercury Classics"},
              {"title": "Goliaths", "label": "Mercury Classics"},
              {"title": "3D1T", "label": "Decca"}]
    cand = [{"title": "Human Music"}, {"title": "Goliaths"},
            {"title": "Selected Works"}]
    v = ai.classify(anchor, cand)
    assert v["verdict"] == ai.CONFIRMED
    assert v["evidence"]["titles_n"] == 2


def test_isrc_wins_over_thin_discography():
    # Узкий артист: дома один релиз, снаружи — тоже сходится по ISRC.
    v = ai.classify([{"title": "Nightingale", "isrc": "GBUM72001234"}],
                    [{"title": "Nightingale"}, {"title": "Other"}])
    assert v["verdict"] == ai.CONFIRMED


def test_homonyms_with_disjoint_catalogs_are_a_conflict():
    # Ровно случай BOP: два длинных каталога, ни одной общей работы.
    anchor = [{"title": "Human Music", "label": "Mercury Classics"},
              {"title": "Goliaths", "label": "Decca"},
              {"title": "3D1T", "label": "Decca"}]
    cand = [{"title": "Battlefield 3 OST", "label": "Monstercat"},
            {"title": "Rapture", "label": "Spinnin"},
            {"title": "Neon", "label": "OWSLA"}]
    v = ai.classify(anchor, cand)
    assert v["verdict"] == ai.CONFLICT


def test_one_shared_title_between_two_long_catalogs_is_not_proof():
    # Фита/кавер с совпадающим названием не делает людей одним человеком.
    a = [{"title": "Neon"}, {"title": "A"}, {"title": "B"}, {"title": "C"}]
    b = [{"title": "Neon"}, {"title": "D"}, {"title": "E"}, {"title": "F"}]
    assert ai.classify(a, b)["verdict"] == ai.PENDING


def test_artist_without_common_releases_is_pending_not_foreign():
    # «Не знаю» обязано оставаться «не знаю»: подписку нельзя помечать чужой.
    a = [{"title": "Only One"}]
    b = [{"title": "Something Else"}, {"title": "Another"}]
    assert ai.classify(a, b)["verdict"] == ai.PENDING


def test_tkey_absorbs_store_variants_of_one_work():
    for x, y in [("Dawn - Single", "Dawn (Single)"), ("Dawn - EP", "Dawn"),
                 ("Dawn", "DAWN")]:
        assert ai.tkey(x) == ai.tkey(y)


# ──Confirmed map: карточка показывается только по подтверждённому id ──────────

def _entry(name, aid, services):
    # `rule_version` здесь — не украшение: подпись «собрано нынешними
    # правилами» это то, по чему `services_bound` разрешает НЕ перепроверять.
    # Без неё фикстура означала бы «привязано версией из доисторического
    # склада», и тест на TTL проверял бы не TTL.
    return {"name": name, "artist_id": aid,
            "identity": {"services": services, "ts": 0,
                         "rule_version": ai.IDENTITY_RULE_VERSION}}


def test_stitch_card_needs_a_confirmed_id():
    entries = [_entry("BOP", "719709163",
                      {"spotify": {"id": "1", "status": ai.CONFIRMED}})]
    ok = {"via_xref": True, "service": "spotify", "artist_id": "1",
          "title": "Neon"}
    bad = {"via_xref": True, "service": "spotify", "artist_id": "999",
           "title": "Battlefield 3 OST"}
    assert ai.stitch_shows(ok, entries)
    assert not ai.stitch_shows(bad, entries)


def test_unbound_homonym_contributes_no_cards():
    entries = [_entry("BOP", "719709163", {"spotify": {"status": ai.PENDING}})]
    assert ai.confirmed_map(entries, "spotify") == {}
    card = {"via_xref": True, "service": "spotify", "artist_id": "1"}
    assert not ai.stitch_shows(card, entries)


def test_native_subscription_cards_are_not_gated():
    # Гейт ровно на name-сшивку: собственные подписки сервиса идут по своим id.
    assert ai.stitch_shows({"service": "spotify", "artist_id": "zzz",
                            "title": "x"}, [])


def test_pending_binds_expire_and_are_retried():
    # Протухший промах перепроверяется, свежий — не долбивит сеть каждый скан.
    now = __import__("time").time()
    e = _entry("BOP", "1", {"deezer": {"status": ai.PENDING}})
    e["identity"]["ts"] = now
    assert ai.services_bound(e) is True
    e["identity"]["ts"] = 0
    assert ai.services_bound(e) is False
    confirmed = _entry("BOP", "1", {"deezer": {"id": "9", "status": ai.CONFIRMED}})
    assert ai.services_bound(confirmed) is True


# ── home_show: склеенная Apple-страница ──────────────────────────────────────

def _merged_entry(choice=None, hidden=()):
    """Страница Solomon Grey: свой кластер Mercury/Decca + чужой Spaceman."""
    own = ["Human Music", "Goliaths", "3D1T", "Selected Works"]
    foreign = ["Descanso en los Brazos del Senor", "Gracia en la Oscuridad",
               "Luz que Guia mis Pasos"]
    return _entry("Solomon Grey", "668535482", {}), {
        "identity": {"services": {}, "profile": {
            "titles": [ai.tkey(t) for t in own],
            "hidden_titles": [ai.tkey(t) for t in hidden],
            "labels": ["mercury classics", "decca"],
            "genres": ["classical", "soundtrack"],
            "merged": bool(hidden),
            "two_people": True,
            "group_titles": {"mercury classics": [ai.tkey(t) for t in own],
                             "spaceman recordings": [ai.tkey(t) for t in foreign]},
        }, "choice": choice or {}}}


def test_clean_page_censors_nothing():
    entry = _entry("Normal", "1", {})
    assert ai.home_show(entry, {"title": "Whatever"})[0] is True


def test_unattested_release_on_merged_page_survives_without_owner_choice():
    # Без решения владельца скрывать нечего: иначе терялись бы законные релизы.
    entry, prof = _merged_entry()
    entry["identity"] = prof["identity"]
    ok, _ = ai.home_show(entry, {"title": "Brand New Album", "label": "Ninja Tune"})
    assert ok is True


def test_owner_choice_hides_the_foreign_cluster():
    entry, prof = _merged_entry(choice={"hide": ["Spaceman Recordings"]})
    entry["identity"] = prof["identity"]
    ident = ai.set_choice(entry, hide=["Spaceman Recordings"])
    assert ident["merged"] is True
    hidden = {"title": "Descanso en los Brazos del Senor",
              "label": "Spaceman Recordings"}
    own = {"title": "Human Music", "label": "Mercury Classics"}
    assert ai.home_show(entry, hidden)[0] is False
    assert ai.home_show(entry, own)[0] is True


def test_choice_recomputes_hidden_titles_from_groups():
    entry, prof = _merged_entry()
    entry["identity"] = prof["identity"]
    before = set(ai.identity_of(entry)["profile"]["titles"])
    ai.set_choice(entry, hide=["spaceman recordings"])
    prof2 = ai.identity_of(entry)["profile"]
    assert ai.tkey("Descanso en los Brazos del Senor") in prof2["hidden_titles"]
    assert set(prof2["titles"]) <= before            # своё не исчезло
    assert prof2["two_people"] is True


def test_clearing_choice_restores_everything():
    entry, prof = _merged_entry(choice={"hide": ["Spaceman Recordings"]})
    entry["identity"] = prof["identity"]
    ai.set_choice(entry, hide=["Spaceman Recordings"])
    assert ai.identity_of(entry)["profile"]["hidden_titles"]
    ai.set_choice(entry, hide=[])
    assert ai.identity_of(entry)["profile"]["hidden_titles"] == []
    assert ai.home_show(entry, {"title": "Descanso en los Brazos del Senor"})[0]


def test_second_round_confirmations_keep_choice():
    # Профиль пересобирается из якоря на каждом бинде — выбор владельца обязан
    # пережить это, иначе скрытый чужак вернётся в ленту сам.
    own = [("Human Music", "Mercury Classics", "Classical Crossover"),
           ("Goliaths", "Mercury Classics", "Classical Crossover"),
           ("Human Remix", "Mercury Classics", "Classical Crossover")]
    foreign = [("Descanso en los Brazos del Senor", "Spaceman Recordings",
                "Christian & Gospel"),
               ("Gracia en la Oscuridad", "Spaceman Recordings", "Christian & Gospel"),
               ("Luz que Guia mis Pasos", "Spaceman Recordings", "Christian & Gospel")]
    anchor = [{"title": t, "label": lbl, "genre": g} for t, lbl, g in own + foreign]
    # Два чистых свидетеля, каждый несёт ровно один кластер → взаимное
    # опровержение: на id лежат два человека.
    witnesses = {"qobuz": [{"title": t, "genre": g} for t, _, g in own],
                 "deezer": [{"title": t, "genre": g} for t, _, g in foreign]}
    p1 = ai._profile_of(anchor, witnesses)
    assert p1["two_people"] is True and p1["merged"] is False
    p2 = ai._profile_of(anchor, witnesses, {"hide": ["Spaceman Recordings"]})
    assert p2["merged"] is True
    assert ai.tkey("Gracia en la Oscuridad") in p2["hidden_titles"]
    assert ai.tkey("Human Music") in p2["titles"]


def test_one_career_across_many_labels_is_not_two_people():
    """Лейбл меняется — человек остаётся. Жанр у обеих групп общий.

    Живой случай 21.09.2026: на «deadmau5» группы mau5trap, mau5trap LLC и
    Oth Records опровергают друг друга свидетелями (на витрине каждого — свой кусок
    дискографии), и без жанрового условия бейдж «склеена» висел бы на каждом
    артисте с длинной историей.
    """
    early = [("Jimaugh", "mau5trap"), ("Bring the Night", "mau5trap"),
             ("I Remember", "mau5trap")]
    late = [("Snowhase", "mau5trap LLC"), ("Let Go", "mau5trap LLC"),
            ("Post Toxic", "mau5trap LLC")]
    anchor = [{"title": t, "label": lbl, "genre": "Dance & Electronic"}
              for t, lbl in early + late]
    witnesses = {"spotify": [{"title": t, "genre": "Dance & Electronic"} for t, _ in early],
                 "tidal": [{"title": t, "genre": "Dance & Electronic"} for t, _ in late]}
    p = ai._profile_of(anchor, witnesses)
    assert p["groups"] and p["two_people"] is False


# ── Жанровые СЕМЬИ: словар витрин не должен плодить «второго человека» ───────

def _two_clusters(own, foreign, g_own, g_foreign, lbl_own, lbl_foreign):
    """Якорь-страница с двумя лейбловыми кластерами и два чистых свидетеля,
    каждый несёт ровно свой — взаимное опровержение налицо, спорить может
    только жанровое условие."""
    rows_own = [(t, lbl_own, g_own) for t in own]
    rows_foreign = [(t, lbl_foreign, g_foreign) for t in foreign]
    anchor = [{"title": t, "label": lbl, "genre": g}
              for t, lbl, g in rows_own + rows_foreign]
    witnesses = {lbl_own: [{"title": t, "genre": g} for t, _, g in rows_own],
                 lbl_foreign: [{"title": t, "genre": g} for t, _, g in rows_foreign]}
    return anchor, witnesses


def test_splitted_store_vocabulary_within_dance_family_is_not_two_people():
    """Живые ложные срабoтки 21.09.2026: Ronski Speed / Rinzen / Jiminy Hop /
    Michael Cassette. Те же самые релизы один и тот же магазин помечает то
    «Trance», то «Dance», то «Electronic»/«House» — это словарь витрины, а не
    два человека. Свидетели кластеры взаимно опровергают, поэтому спасает
    только сравнение жанровых семей, а не строк."""
    cases = [
        # Ronski Speed: maracaido (trance/techno) × group therapy (dance)
        (["Arise", "Corellia", "Evolve", "Rapture"],
         ["Group Therapy 686 DJ Mix", "Group Therapy 687 DJ Mix",
          "Group Therapy 688 DJ Mix"],
         "Trance", "Dance"),
        # Jiminy Hop: majestic (house/techno) × shambhala (dance)
        (["Nomad", "Sahara", "Dubai"], ["Twelve Senses", "Asteroid",
                                        "Between Dimensions"],
         "Techno", "Dance"),
        # Michael Cassette: digitally remastered (electronic/house) × GT (dance)
        (["MOTUS", "Astra", "Reverie"],
         ["Group Therapy 699 DJ Mix", "Group Therapy 700 DJ Mix",
          "Group Therapy 701 DJ Mix"],
         "Electronic", "Dance"),
    ]
    for own, foreign, g_own, g_foreign in cases:
        anchor, wit = _two_clusters(own, foreign, g_own, g_foreign,
                                    "own-label", "foreign-label")
        assert ai._profile_of(anchor, wit)["two_people"] is False


def test_hip_hop_vs_drum_and_bass_homonym_still_splits():
    """Настоящий BOP: рэпер и dnb-артист — РАЗНЫЕ семьи, и «drum & bass»
    сводится семьёй именно в танцевальную корзину, а «Hip-Hop/Rap» — нет.
    Ужесточение семантической проверки не имеет права это заглушить."""
    anchor, wit = _two_clusters(
        ["Neon", "Battlefield", "Highlander"],
        ["Turn Me Up", "No Regrets", "Talk My Talk"],
        "Drum & Bass", "Hip-Hop/Rap", "hospitality", "hot-rail")
    p = ai._profile_of(anchor, wit)
    assert p["two_people"] is True


def test_store_tags_of_solomon_grey_real_shapes_still_split():
    """Снимок витрин 21.09.2026: свой кластер Mercury Classics помечен там
    «Electronic», испанский госпел-однофамилец на Spaceman Recordings —
    «Ambient»/«Jazz». Семьи разные → склейка обязана остаться видимой."""
    anchor, wit = _two_clusters(
        ["Human Music", "Goliaths", "3D1T"],
        ["Descanso en los Brazos del Senor", "Gracia en la Oscuridad",
         "Luz que Guia mis Pasos"],
        "Electronic", "Jazz", "mercury classics", "spaceman recordings")
    assert ai._profile_of(anchor, wit)["two_people"] is True


# ── Сетевой провал — не «чужой» ───────────────────────────────────────────────

def test_network_failure_yields_retryable_pending_never_foreign(monkeypatch):
    """Каталоги лежат: bind обязан выдать pending (перепроверится по TTL),
    НЕ conflict/«чужой» — иначе отсутствие сети осело бы в данных как
    приговор и увело карточки в карантин."""
    import asyncio as _a
    import time as _t

    class _Dead:
        async def get(self, *a, **k):
            raise OSError("network is down")

    e = _entry("BOP", "719709163", {})
    monkeypatch.setattr(ai, "_mb_ids", lambda *a, **k: [])
    idn = _a.run(ai.bind(e, _Dead(), {}, services=("deezer", "tidal", "qobuz")))
    st = {s: (r or {}).get("status") for s, r in idn["services"].items()}
    assert set(st.values()) == {ai.PENDING}          # ни conflict, ни confirmed
    assert "conflict" not in idn
    idn["ts"] = _t.time()
    assert ai.services_bound(e) is True               # свежий — сеть не долбим
    idn["ts"] = 0
    assert ai.services_bound(e) is False              # протух — перепроверим


# ── summary: всё, что радар спрятал, видно владельцу ─────────────────────────

def test_summary_reports_what_it_hid_and_what_it_did_not_resolve():
    hidden = _entry("BOP", "1", {"spotify": {"status": ai.PENDING}})
    hidden["identity"]["profile"] = {"merged": True, "two_people": True}
    hidden["identity"]["needs_owner"] = True
    ok = _entry("Solomon Grey", "2", {"qobuz": {"id": "3", "status": ai.CONFIRMED}})
    rep = ai.summary([hidden, ok])
    assert rep["pending"] == 1 and rep["confirmed"] == 1
    assert rep["needs_owner"] == 1
    names = {i["name"] for i in rep["items"]}
    assert names == {"BOP"}


# ── Карточка, у которой на подписку указывает ТОЛЬКО имя (via_label) ─────────
# Живой случай жалобы: «BOP» pending на четырёх витринах, а его карточка
# {"artist": "BOP", "artist_id": "", "label": "hospital records",
#  "via_label": true} жила в подписке на рэпера, потому что прежний гейт смотрел
# ровно на `via_xref`.

_BOP_LABEL_CARD = {"artist": "BOP", "artist_id": "", "label": "hospital records",
                   "via_label": True, "service": "spotify", "title": "Rumination",
                   "id": "3HPZHv2gZFbDDV3qwIs37q"}


def _spotify_catalog(owners: dict, names: dict) -> dict:
    return {"service": "spotify", "owners": owners, "names": names,
            "state": {"artists": {}, "album_meta": {}}}


def test_name_only_card_of_unconfirmed_subscription_does_not_show():
    # Разбор БЫЛ, личности нет — держать такую карточку в ленте значит оставить
    # ровно ту дыру, из-за которой живёт жалоба.
    e = _entry("BOP", "719709163", {"spotify": {"status": ai.PENDING},
                                    "deezer": {"status": ai.PENDING}})
    cat = _spotify_catalog({"3HPZHv2gZFbDDV3qwIs37q": {"02ZCVD3nqfqNId8lvpvCBb"}},
                           {"02ZCVD3nqfqNId8lvpvCBb": "BOP"})
    ok, why = ai.name_claim_shows(_BOP_LABEL_CARD, [e], cat)
    assert ok is False and "pending" in why


def test_name_only_card_survives_while_subscription_unexamined():
    # «Не проверяли» ≠ «чужой»: до первого разбора гейт молчит (иначе 299
    # карточек настоящих подписок ушли бы в никуда).
    e = _entry("BOP", "719709163", {})
    ok, _ = ai.name_claim_shows(_BOP_LABEL_CARD, [e], _spotify_catalog({}, {}))
    assert ok is True


def test_name_only_card_owned_by_confirmed_id_shows():
    e = _entry("BOP", "719709163",
               {"spotify": {"id": "02ZCVD3nqfqNId8lvpvCBb", "status": ai.CONFIRMED}})
    cat = _spotify_catalog({"3HPZHv2gZFbDDV3qwIs37q": {"02ZCVD3nqfqNId8lvpvCBb"}},
                           {"02ZCVD3nqfqNId8lvpvCBb": "BOP"})
    ok, why = ai.name_claim_shows(_BOP_LABEL_CARD, [e], cat)
    assert ok is True and "confirmed id" in why


def test_name_only_card_of_refuted_same_name_owner_is_quarantined():
    own = [{"title": t} for t in ("Turf Stories", "Sac It Up", "Perseverance",
                                 "Audio Visual", "Bop S Music")]
    e = _entry("BOP", "719709163",
               {"spotify": {"id": "1", "status": ai.CONFIRMED},
                "deezer": {"id": "9", "status": ai.CONFIRMED}})
    e["identity"]["profile"] = {"titles": [ai.tkey(r["title"]) for r in own]}
    # Каталог знает настоящего владельца работы — однофамильца с ДРУГИМ id,
    # и его дискография с профилем подписки не пересекается вовсе.
    cat = {"service": "spotify",
           "owners": {"3HPZHv2gZFbDDV3qwIs37q": {"2"}},
           "names": {"1": "BOP", "2": "BOP"},
           "state": {"artists": {"2": {"name": "BOP", "releases": [
               {"title": "Rumination"}, {"title": "Elastic Dreams"},
               {"title": "Drift"}, {"title": "Blips 3000"}]}},
               "album_meta": {}}}
    ok, why = ai.name_claim_shows(_BOP_LABEL_CARD, [e], cat)
    assert ok is False and "не пересекаются" in why


def test_owner_clusters_invent_no_foreign_work_without_owner_choice():
    """До решения владельца чужих работ нет: иначе под цензуру попали бы
    законные appear-on (мик, саундтрек, фит) — их кластерная разметка не
    покрывает, и «не попало в группу» не значит «чужой»."""
    e = _entry("Solomon Grey", "668535482", {})
    e["identity"]["profile"] = {
        "titles": [ai.tkey(t) for t in ("Human Music", "Goliaths", "3D1T",
                                        "Group Therapy 686 DJ Mix")],
        "group_titles": {"mercury classics": [ai.tkey("Human Music"),
                                              ai.tkey("Goliaths"),
                                              ai.tkey("3D1T")]},
        "group_detail": {"mercury classics": {
            "titles": [ai.tkey(t) for t in ("Human Music", "Goliaths", "3D1T")],
            "genres": ["electronic"], "carriers": ["tidal"], "absent": ["deezer"]}},
        "two_people": True}
    own, foreign = ai._owner_clusters(e)
    assert foreign == [] and len(own) == 4
    ai.set_choice(e, hide=["mercury classics"])       # владелец назвал своим госпел
    own2, foreign2 = ai._owner_clusters(e)
    assert ai.tkey("Human Music") in foreign2 and ai.tkey("Human Music") not in own2


# ── Склад как источник id: молчаливая пустота хуже незнания ──────────────────

def test_load_spotify_state_accepts_str_and_path_alike(tmp_path):
    """`base_dir / "…"` на СТРОКЕ бросает TypeError, он глушится — и склад
    отвечал `{}`, то есть «кандидатов в Spotify нет». Самоизлечение BOP и
    Solomon Grey спотыкалось ровно об это (21.09.2026)."""
    from pathlib import Path

    (tmp_path / "spotify_artist_state.json").write_text(
        json.dumps({"artists": {}, "followed": {"artists": []}}), encoding="utf-8")
    assert ai.load_spotify_state(str(tmp_path)) != {}
    assert ai.load_spotify_state(Path(tmp_path)) != {}
    assert ai.load_spotify_state(tmp_path / "нет-такой-папки") == {}


# ── Компилляции не доказывают личность (причина A) ───────────────────────────

def test_dj_mix_of_the_rival_cluster_is_not_identity_evidence():
    """«Group Therapy Anjuna25 Special with Above & Beyond (DJ Mix)» — та самая
    строка, которой испанский госпел-однофамилец привязался к Solomon Grey.
    Склад знает её настоящего владельца (`album_meta.artist`), и он — не
    Solomon Grey."""
    st = {"artists": {"7pCfN": {"name": "Solomon Grey", "releases": [
              {"id": "13bHD", "title": "Group Therapy Anjuna25 Special with "
                                       "Above & Beyond (DJ Mix)"},
              {"id": "4TtW", "title": "Gracia en la Oscuridad"}]}},
          "album_meta": {"13bHD": {"artist": "Above & Beyond Group Therapy",
                                   "label": "Group Therapy", "type": "album"},
                         "4TtW": {"artist": "Solomon Grey",
                                  "label": "Spaceman Recordings", "type": "single"}}}
    rows = ai._spotify_rows(st, "7pCfN")
    assert rows[0]["album_artist"] == "Above & Beyond Group Therapy"
    ok, neutral = ai._evidence_split(rows)
    assert ai.tkey("Gracia en la Oscuridad") in ok
    assert not any("group therapy" in t for t in ok)


def test_shared_title_with_anchor_release_id_is_proof_not_guesswork():
    """Одно общее название — догадка; название, которое якорь знает под своим
    номером выпуска, — конкретная работа (тот же смысл, что у ISRC)."""
    anchor = [{"title": "Nightingale", "collection": "6773045988"},
              {"title": "One"}, {"title": "Two"}, {"title": "Three"}]
    cand = [{"title": "Nightingale"}, {"title": "Four"}, {"title": "Five"},
            {"title": "Six"}]
    assert ai.classify(anchor, cand)["verdict"] == ai.CONFIRMED
    anchor[0].pop("collection")
    assert ai.classify(anchor, cand)["verdict"] == ai.PENDING


# ── Провод, а не модуль: гейт обязан стоять НА ЛЕНТЕ ─────────────────────────

def test_feed_filter_drops_the_name_only_card(tmp_path):
    """`name_claim_shows` написан и покрыт тестом — и НИЦЕМ не вызывался,
    ровно как `session_feedback()` в станциях. Проверяем не предикат, а
    место в ленте: карточка лейбловой ленты должна исчезать до выдачи."""
    from ripster.routes import radar as radar_route

    e = _entry("BOP", "719709163", {"spotify": {"status": ai.PENDING}})
    (tmp_path / "spotify_artist_state.json").write_text(json.dumps(
        {"artists": {"02ZC": {"name": "BOP", "releases": [
            {"id": "3HPZHv2gZFbDDV3qwIs37q", "title": "Rumination"}]}},
         "followed": {"artists": []},
         "album_meta": {"3HPZHv2gZFbDDV3qwIs37q":
                        {"artist": "BOP", "label": "Hospital Records"}}}),
        encoding="utf-8")
    ai._catalog_cache.clear()
    saved = dict(radar_route._s)
    try:
        radar_route._s.update({"watchlist": [e], "base_dir": tmp_path,
                               "save_watchlist": None})
        assert radar_route._identity_filter("labels", [_BOP_LABEL_CARD]) == []
        clean = dict(_BOP_LABEL_CARD, via_label=False)
        assert radar_route._identity_filter("labels", [clean]) == [clean]
    finally:
        radar_route._s.clear()
        radar_route._s.update(saved)
        ai._catalog_cache.clear()


@pytest.mark.parametrize("name", ["BOP", "X & Y", "B.O.P."])
def test_names_are_only_a_lookup_key_never_a_verdict(name):
    # Имя участвует лишь в том, чтобы НАЙТИ кандидата; вердикт — за работами.
    entries = [_entry(name, "1", {"deezer": {"id": "7", "status": ai.CONFIRMED}})]
    assert ai.confirmed_map(entries, "deezer")[name]["id"] == "7"
    assert ai.confirmed_map(entries, "tidal") == {}


# ── Склеенная витрина не свидетель (Solomon Grey, 22.09.2026) ─────────────────

def _sg_page():
    """Снимок живых витрин: tidal знает только австралийца, spotify — только
    испанского госпел-однофамильца, а deezer и qobuz несут ОБОИХ. Последние
    двое и есть склейка: на их «общих заголовках» висело всё ложное
    подтверждение, и пускай они свидетельствуют — взаимного опровержения не
    видно никогда."""
    own = ["Human Music", "Goliaths", "3D1T", "Selected Works"]
    foreign = ["Descanso en los Brazos del Senor", "Gracia en la Oscuridad",
               "Luz que Guia mis Pasos", "Paz que Sobrepasa Todo"]
    anchor = ([{"title": t, "label": "Mercury Classics", "genre": "Electronic"}
               for t in own]
              + [{"title": t, "label": "Spaceman Recordings", "genre": "Jazz"}
                 for t in foreign])
    wit = {
        "tidal": [{"title": t, "genre": "Electronic"} for t in own],
        "spotify": [{"title": t, "genre": "Jazz"} for t in foreign],
        "deezer": [{"title": t, "genre": "Electronic"} for t in own]
                  + [{"title": t, "genre": "Jazz"} for t in foreign],
        "qobuz": [{"title": t, "genre": "Electronic"} for t in own]
                 + [{"title": t, "genre": "Jazz"} for t in foreign],
    }
    return anchor, wit


def test_merged_store_witnesses_nobody_but_does_not_hide_the_split():
    anchor, wit = _sg_page()
    p = ai._profile_of(anchor, wit)
    assert p["two_people"] is True
    assert p["merged_witnesses"] == ["deezer", "qobuz"]
    # Личности остаются две, и у каждой — только ЧИСТЫЕ носители: именно этот
    # список видит владелец в /api/identity, когда выбирает своего.
    assert len(p["persons"]) == 2
    assert {tuple(x["carriers"]) for x in p["persons"]} == {("tidal",), ("spotify",)}


def test_identity_built_by_older_rules_is_reverified():
    """Чужую склейку, записанную старой версией правил, нельзя лечить молча:
    пока вердикт считается «привязанным», ни один проход его не пересмотрит, и
    22.09.2026 весь склад (354 подписки из 358) так и прожил бы с тем, что
    насчитал прежний алгоритм."""
    now = __import__("time").time()
    e = _entry("Solomon Grey", "668535482",
               {"deezer": {"id": "4949652", "status": ai.CONFIRMED}})
    e["identity"]["ts"] = now
    assert ai.services_bound(e) is True
    e["identity"]["rule_version"] = ai.IDENTITY_RULE_VERSION - 1
    assert ai.rules_stale(e) is True
    assert ai.services_bound(e) is False


def test_shared_release_ids_join_the_same_person_under_different_titles():
    """Обратная ошибка дороже: потерять своего артиста. Два выпуска, которые
    обе стороны знают под ОДНИМИ id, — это один человек, даже если названия в
    витринах разошлись (переиздание, другой регистр, перевод). Без номеров те
    же самые данные дают одно совпавшее название, а одно название доказательством
    не считается — так и проверяем обе половины правила."""
    anchor = [{"title": "Terminal", "album": "4aQYd0ux2p"},
              {"title": "Atlas", "album": "6bRZq1ty9s"}]
    cand = [{"title": "Terminal", "album": "4aQYd0ux2p"},
            {"title": "Terminal (Live at Printworks)", "album": "6bRZq1ty9s"}]
    v = ai.classify(anchor, cand)
    assert v["evidence"]["titles_n"] == 1, "названия тут почти не совпадают"
    assert v["evidence"]["ids"]["album"] == 2
    assert v["verdict"] == ai.CONFIRMED
    thin = [{"title": r["title"]} for r in cand]
    assert ai.classify(anchor, thin)["verdict"] != ai.CONFIRMED


def test_duplicate_profiles_of_one_person_both_pass_the_feed_gate():
    """Витрина плодит вторые профили на того же человека («Noisia», Robert Hood
    живут по два-три id). Пустить только принятый id — значит выкинуть законные
    релизы подписки из ленты."""
    entries = [_entry("Noisia", "1", {"deezer": {"id": "1966",
                                                 "status": ai.CONFIRMED,
                                                 "aliases": ["14495"]}})]
    for aid in ("1966", "14495"):
        assert ai.stitch_shows({"via_xref": True, "service": "deezer",
                                "artist_id": aid, "title": "Terminal"}, entries)
    assert not ai.stitch_shows({"via_xref": True, "service": "deezer",
                                "artist_id": "999", "title": "Terminal"}, entries)


def test_two_people_under_one_name_in_one_store_ask_the_owner():
    """Два подтверждённых id, которые опровергают друг друга работами, —
    агента: кого из них подписывал человек, знает только он. Так «Sasha» у MB
    висит под двумя артистами."""
    e = _entry("Sasha", "1", {"deezer": {"id": "500", "status": ai.CONFIRMED,
                                         "namesakes": ["1099"]}})
    e["identity"]["ambiguous_names"] = {"deezer": ["1099"]}
    assert ai.needs_owner(e) is True
    assert ai.auto_pull_ok(e, {"title": "X"})[0] is False
    ai.set_choice(e, hide=["some label"])
    assert ai.needs_owner(e) is False


def test_repair_of_a_confirmed_id_is_recorded_once():
    ident = {}
    prev = {"id": "4949652", "status": ai.CONFIRMED}
    rec = {"id": "14495", "status": ai.CONFIRMED}
    ai._relink(prev, rec, "deezer", "Solomon Grey", ident)
    ai._relink(prev, rec, "deezer", "Solomon Grey", ident)
    assert [(r["service"], r["from"], r["to"]) for r in ident["repairs"]] == [
        ("deezer", "4949652", "14495")]


def test_recheck_of_the_same_id_keeps_siblings_it_already_knew():
    """Перепроверка не обязана находить дублей заново: витрина не перестаёт их
    носить от того, что в этом прогоне кандидаты не успели подтвердиться."""
    ident = {}
    prev = {"id": "1966", "status": ai.CONFIRMED, "aliases": ["14495"],
            "namesakes": ["77"]}
    rec = {"id": "1966", "status": ai.CONFIRMED}
    ai._relink(prev, rec, "deezer", "Noisia", ident)
    assert rec["aliases"] == ["14495"]
    assert rec["namesakes"] == ["77"]
    assert "repairs" not in ident


def test_summary_counts_what_is_still_unhealed():
    entries = [_entry("A", "1", {"deezer": {"id": "9", "status": ai.CONFIRMED}}),
               _entry("B", "2", {"deezer": {"id": "8", "status": ai.CONFIRMED}})]
    entries[1]["identity"]["rule_version"] = ai.IDENTITY_RULE_VERSION - 1
    entries[1]["identity"]["repairs"] = [{"service": "deezer", "from": "7",
                                          "to": "8"}]
    rep = ai.summary(entries)
    assert rep["stale"] == 1 and rep["repairs"] == 1
    assert [i["name"] for i in rep["items"] if i.get("repairs")] == ["B"]


def test_auto_pull_stays_shut_while_the_page_is_merged():
    """Показать карточку и скачать её — разные риски: скачанный чужой альбом
    остаётся в библиотеке. Поэтому на склеенной странице без решения владельца
    автозакачка закрыта, даже когда сам релиз выглядит своим."""
    entry, prof = _merged_entry()
    entry["identity"] = prof["identity"]
    entry["identity"]["profile"]["two_people"] = True
    assert ai.home_show(entry, {"title": "Human Music",
                                "label": "Mercury Classics"})[0] is True
    assert ai.auto_pull_ok(entry, {"title": "Human Music",
                                   "label": "Mercury Classics"})[0] is False
    ai.set_choice(entry, hide=["Spaceman Recordings"])
    assert ai.auto_pull_ok(entry, {"title": "Human Music",
                                   "label": "Mercury Classics"})[0] is True
