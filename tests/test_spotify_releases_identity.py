"""Однофамильцы в Spotify-ветке ленты «Релизы».

Путь карточек (разбор 21.09.2026): фронт тянет `/api/spotify/releases`
(app.js), роут отдаёт `_build_feed` из `spotify_artist_state.json` — полки
артистов, на которые подписан Spotify-аккаунт владельца. Полки эти однажды
пополнились и ПО ИМЕНИ вишлиста (артефакт identity_bindall_state.json), поэтому
«BOP» 719709163 (хип-хоп) получал полку drum&bass-однофамильца, а «Solomon
Grey» — испанский госпел. Фильтр `_sp_identity_filter` повторяет правило
radar-ветки: имя — только кандидат, в ленту пускается подтверждённый общими
работами id; «не знаю» — скрыть и пометить владельцу, склад НЕ трогается.
"""
from datetime import datetime, timedelta

import pytest

import ripster.routes.spotify as sp
from ripster import artist_identity as ai


def _days_ago(n):
    return (datetime.now() - timedelta(days=n)).strftime("%Y-%m-%d")


def _entry(name, aid, spotify=None):
    """Подписка вишлиста; spotify — ("id"|"pending", статус) в identity.services."""
    e = {"name": name, "kind": "artist", "artist_id": aid, "service": "apple"}
    if spotify:
        sid, status = spotify
        rec = {"status": status, "ts": 1.0}
        if sid:
            rec["id"] = sid
        e["identity"] = {"services": {"spotify": rec}}
    return e


def _rel(rid, artist, artist_id, title="T", date=None, **kw):
    return {"id": rid, "artist": artist, "artist_id": artist_id, "title": title,
            "type": "single", "date": date or _days_ago(5), **kw}


@pytest.fixture(autouse=True)
def _isolate(monkeypatch):
    monkeypatch.setattr(sp, "_sp_watchlist", [], raising=False)
    monkeypatch.setattr(sp, "_sp_save_watchlist", None, raising=False)


def _confirmed(name, apple_id, sp_id):
    return _entry(name, apple_id, (sp_id, ai.CONFIRMED))


# ── ловушки, на которых попадала name-подписка ───────────────────────────────

def test_conflicting_homonym_shelf_is_hidden_and_marked(monkeypatch):
    # BOP: подписка ПРИВЯЗАНА к своему id, а карточка пришла с чужого — вот это
    # и есть однофамилец. Скрываем и помечаем владельцу.
    bop = _confirmed("BOP", "719709163", "sp_hiphop")
    monkeypatch.setattr(sp, "_sp_watchlist", [bop])
    saved = []
    monkeypatch.setattr(sp, "_sp_save_watchlist", saved.append)
    rel = _rel("s1", "BOP", "sp_dnb")
    assert sp._sp_identity_filter([rel]) == []
    hidden = (bop["identity"]["hidden"])
    assert hidden and hidden[0]["key"] == ai.tkey(rel["title"])
    assert saved, "пометка должна дожить до перезапуска — save_watchlist вызван"


def test_confirmed_id_passes_own_shelf(monkeypatch):
    # Подтверждённый id той же полки — пропуск, ни одна карточка не теряется.
    e = _confirmed("Ronski Speed", "44162464", "sp_real")
    monkeypatch.setattr(sp, "_sp_watchlist", [e])
    rel = _rel("s1", "Ronski Speed", "sp_real")
    assert sp._sp_identity_filter([rel]) == [rel]
    assert not (e.get("identity") or {}).get("hidden")


def test_different_followed_id_is_hidden_even_when_bound(monkeypatch):
    # Подписка подтверждена на sp_real, а в followed кто-то другой с тем же
    # именем — это ровно name-follow чужого человека.
    monkeypatch.setattr(sp, "_sp_watchlist", [_confirmed("BOP", "719709163", "sp_real")])
    out = sp._sp_identity_filter([_rel("s1", "BOP", "sp_wrong")])
    assert out == []


def test_own_follows_without_watchlist_collision_pass_silently(monkeypatch):
    # 5762 подписки аккаунта vs 425 вишлиста: то, чего в вишлисте нет, —
    # собственные follow'ы владельца, судить нечем и незачем.
    monkeypatch.setattr(sp, "_sp_watchlist", [_entry("BOP", "719709163")])
    rel = _rel("s9", "Some Random Producer", "sp_x")
    assert sp._sp_identity_filter([rel]) == [rel]


def test_unbound_subscription_lets_the_card_through(monkeypatch):
    # «Не знаю» ≠ «чужой». Сухой прогон 21.09.2026: строгое правило скрыло бы
    # 299 карточек у 123 артистов, и почти все — настоящие подписки владельца,
    # просто ни разу не привязанные к Spotify. Пустая лента хуже чужой карточки,
    # поэтому до появления подтверждённого id карточка проходит, а склад цел.
    state = {"sp_dnb": {"name": "BOP",
                        "releases": [_rel("s1", "BOP", "sp_dnb")], "ts": 0}}
    monkeypatch.setattr(sp, "_sp_artist_state", state, raising=False)
    monkeypatch.setattr(sp, "_sp_followed_cache",
                        {"artists": [{"id": "sp_dnb", "name": "BOP"}]}, raising=False)
    e = _entry("BOP", "719709163", (None, ai.PENDING))
    monkeypatch.setattr(sp, "_sp_watchlist", [e])
    feed = sp._build_feed(90, "album,single,compilation")
    assert len(feed["releases"]) == 1, "непривязанную подписку скрывать нечем"
    assert state["sp_dnb"]["releases"], "склад чистить нельзя"


def test_merged_page_owner_choice_hides_cluster_from_spotify_shelf(monkeypatch):
    # Solomon Grey: id подтверждён (тот же, что и followed), но страница
    # склеена и владелец уже указал чужую группу — госпел уходит и отсюда.
    e = _confirmed("Solomon Grey", "668535482", "sp_sg")
    e["identity"]["profile"] = {
        "titles": [ai.tkey("Human Music"), ai.tkey("Refugio en la Tormenta")],
        "hidden_titles": [ai.tkey("Refugio en la Tormenta")],
        "labels": ["mercury classics"],
        "merged": True,
    }
    monkeypatch.setattr(sp, "_sp_watchlist", [e])
    good = _rel("s1", "Solomon Grey", "sp_sg", title="Human Music",
                label="Mercury Classics")
    gospel = _rel("s2", "Solomon Grey", "sp_sg", title="Refugio en la Tormenta")
    out = sp._sp_identity_filter([good, gospel])
    assert out == [good]


def test_labels_and_nameless_entries_are_not_collisions(monkeypatch):
    # kind=label без artist_id и подписка с пустым именем не создают ловушку.
    monkeypatch.setattr(sp, "_sp_watchlist",
                        [{"name": "Any", "kind": "label", "artist_id": "l1"},
                         {"name": "  ", "kind": "artist", "artist_id": "9"}])
    rel = _rel("s1", "Any", "sp_x")
    assert sp._sp_identity_filter([rel]) == [rel]


def test_two_subscriptions_same_name_one_confirmed_passes(monkeypatch):
    a = _entry("BOP", "111")
    b = _confirmed("BOP", "222", "sp_ok")
    monkeypatch.setattr(sp, "_sp_watchlist", [a, b])
    rel = _rel("s1", "BOP", "sp_ok")
    out = sp._sp_identity_filter([rel])
    assert out == [rel]
    # не подтверждено — помечены ОБЕ подписки того же имени
    assert sp._sp_identity_filter([_rel("s2", "BOP", "sp_no")]) == []
    assert (a["identity"]["hidden"]) and (b["identity"]["hidden"])


def test_case_and_spacing_variants_still_collide(monkeypatch):
    # «bop», «B.o.p» vs «BOP»: норма та же, что у bind/confirmed_map. Скрывает
    # только КОНФЛИКТ: подписка привязана к одному id, а карточка несёт другой.
    bound = _confirmed("BOP", "719709163", "sp_hiphop")
    monkeypatch.setattr(sp, "_sp_watchlist", [bound])
    assert sp._sp_identity_filter([_rel("s1", " bOp ", "sp_dnb")]) == []
    kept = sp._sp_identity_filter([_rel("s1", " bOp ", "sp_hiphop")])
    assert len(kept) == 1, "свой же подтверждённый id обязан проходить"


def test_empty_watchlist_is_noop(monkeypatch):
    monkeypatch.setattr(sp, "_sp_watchlist", [])
    rels = [_rel("s1", "BOP", "sp_dnb")]
    assert sp._sp_identity_filter(rels) == rels
