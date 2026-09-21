"""Apple-ветка радара отдаёт МИКСЫ, а не обычные релизы вишлиста.

Ловушки, ради которых тест и написан:
  * концертник «Live at Wembley» и «The Abbey Road Sessions» — не миксы,
    хотя слова в названии те же самые;
  * ремастер «(2012 Mix/Master)» содержит «Mix», но миксом не является;
  * один и тот же микс находится через треки нескольких артистов, и у каждого
    свой url («?i=<трек>») — в ленту он обязан попасть РАЗ.
"""
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from ripster.routes.radar import _apple_mix_item, _apple_mix_verdict  # noqa: E402

MIN = 60_000


def _rel(name, genres=(), times=()):
    return {"name": name, "genres": list(genres), "track_times": list(times)}


def test_direct_naming_is_enough():
    for name in ("Group Therapy 694 (DJ Mix)",
                 "25 Years Of Anjuna Mixed By James Grant",
                 "Balance presents… (Continuous Mix)"):
        ok, why = _apple_mix_verdict(_rel(name))
        assert ok, (name, why)


def test_genre_alone_is_enough():
    ok, why = _apple_mix_verdict(_rel("Essential Mix 2026-09-19", ["DJ-Mixes"]))
    assert ok and why == "genre:dj-mixes"


def test_weak_words_need_proof():
    """«Live»/«Sessions»/«Mixtape» в одиночку не пускают."""
    ok, why = _apple_mix_verdict(_rel("Live at Wembley", times=12 * [4 * MIN]))
    assert not ok and "weak title" in why

    ok, _ = _apple_mix_verdict(_rel("The Abbey Road Sessions", times=14 * [3 * MIN]))
    assert not ok

    ok, why = _apple_mix_verdict(_rel("Summer Mixtape 2026"))
    assert not ok and "no proof yet" in why   # длительностей ещё нет — добьём лукупом


def test_duration_confirms_a_set():
    ok, why = _apple_mix_verdict(_rel("HAYLA at EDC Las Vegas 2026 (Live)",
                                      times=[75 * MIN]))
    assert ok and "continuous" in why

    ok, why = _apple_mix_verdict(_rel("Boiler Room: Berlin", times=4 * [20 * MIN]))
    assert ok and "long-form" in why


def test_remaster_with_the_word_mix_is_not_a_mix():
    ok, _ = _apple_mix_verdict(_rel("Blue Lines (2012 Mix/Master)"))
    assert not ok


def test_regular_album_is_rejected_with_numbers():
    ok, why = _apple_mix_verdict(_rel("II Reworked", times=11 * [4 * MIN]))
    assert not ok and why.startswith("regular:11 tracks")


def test_item_id_is_the_collection_not_the_track_url():
    """Один микс, найденный через треки двух артистов, = один id и чистый url."""
    alb = {"id": "1799556655", "name": "JCM 2026, Vol. 11 (DJ Mix)",
           "artist": "Lane 8", "date": "2026-08-21", "cover": "", "track_count": 1,
           "url": "https://music.apple.com/us/album/jcm/1799556655?i=1799556700"}
    a = _apple_mix_item(alb, {"name": "Lane 8", "artist_id": "1"}, "title:«dj mix»")
    b = _apple_mix_item({**alb, "url": alb["url"].replace("700", "701")},
                        {"name": "Mat Zo", "artist_id": "2"}, "title:«dj mix»")
    assert a["id"] == b["id"] == "1799556655"
    assert "?i=" not in a["url"] and a["url"].endswith("1799556655")
