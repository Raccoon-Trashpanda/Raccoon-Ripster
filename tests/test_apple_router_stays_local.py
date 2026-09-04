"""Ripster не должен уезжать с дефолтного движка сам по себе.

04.09.2026, жалоба владельца: «почему-то Рипстер постоянно уезжает то на
публичный, то на этот, надоело». Повод — письмо об ошибке gamdl у гостя.

`ripster_stats.db` показал, что произошло на самом деле: ОДНА И ТА ЖЕ ссылка
`music.apple.com/nz/song/aurora/6801096662` дала за минуту две записи —

    16:34:08  gamdl    aac    error   ← это письмо и увидел владелец
    16:35:28  zhaarey  alac   done    ← а трек на самом деле скачался

Причина не в gamdl. Правило от 03.09 («чужая витрина разруливается перебором
Apple-аккаунтов, а не сменой движка») применили только к lossless-ветке. Ветка
AAC по-прежнему считала чужой регион поводом уйти на куки — при витрине
аккаунта `ca` и ссылке `/nz/` любой AAC-запрос улетал в gamdl, чьи cookies
протухли. Отсюда и ощущение постоянного «съезда».

Тесты держат главное: движок меняется только тогда, когда локальный wrapper
действительно недоступен, и НИКОГДА не переключается на публичный сам.
"""
import pytest

from ripster import apple_router as ar

NZ = "https://music.apple.com/nz/song/aurora/6801096662"
CA = "https://music.apple.com/ca/album/x/123"


@pytest.fixture(autouse=True)
def _no_network(monkeypatch):
    """Витрина аккаунта — 'ca', как у владельца; в сеть не ходим."""
    monkeypatch.setattr(ar, "local_wrapper_storefront", lambda cfg: "ca")
    monkeypatch.setattr(ar, "_cookies_ok", lambda cfg: True)
    monkeypatch.setattr(ar, "_local_wrapper_ok", lambda cfg: True)
    monkeypatch.setattr(ar, "_public_wrapper_ok", lambda cfg: True)


def _cfg(**kw):
    return {"storefront": "us", "apple-wrapper": "local", **kw}


@pytest.mark.parametrize("quality", ["aac", "aac-legacy", ""])
def test_foreign_region_no_longer_moves_aac_to_cookies(quality):
    """Ровно случай гостя: ссылка /nz/, аккаунт ca."""
    r = ar.route_apple(quality, _cfg(), NZ)
    assert r["engine"] == "zhaarey", f"AAC снова уехал на {r['engine']}"
    assert "перебор учёток" in r["note"]


@pytest.mark.parametrize("quality", ["alac", "alac-hires", "atmos"])
def test_lossless_stays_local_as_before(quality):
    assert ar.route_apple(quality, _cfg(), NZ)["engine"] == "zhaarey"


def test_own_region_aac_unchanged():
    r = ar.route_apple("aac", _cfg(), CA)
    assert r["engine"] == "zhaarey" and "перебор" not in r["note"]


def test_cookies_used_only_when_the_local_wrapper_is_down(monkeypatch):
    """Смена движка допустима — но по реальной причине, а не из-за региона."""
    monkeypatch.setattr(ar, "_local_wrapper_ok", lambda cfg: False)
    r = ar.route_apple("aac", _cfg(**{"apple-wrapper": "auto"}), NZ)
    assert r["engine"] == "gamdl"
    assert "не отвечает" in r["note"]


def test_pinned_local_never_falls_to_cookies(monkeypatch):
    """Владелец выбрал локальный — значит локальный, даже если он лёг."""
    monkeypatch.setattr(ar, "_local_wrapper_ok", lambda cfg: False)
    r = ar.route_apple("aac", _cfg(), NZ)
    assert r["engine"] == "zhaarey"


def test_public_is_never_chosen_automatically(monkeypatch):
    """Жёсткое правило 03.09: публичный wrapper — только вручную."""
    monkeypatch.setattr(ar, "_local_wrapper_ok", lambda cfg: False)
    monkeypatch.setattr(ar, "_cookies_ok", lambda cfg: False)
    for q in ("aac", "alac", "alac-hires", ""):
        assert ar.route_apple(q, _cfg(**{"apple-wrapper": "auto"}), NZ)["engine"] != "amd"


def test_public_still_available_when_explicitly_asked():
    r = ar.route_apple("alac", _cfg(**{"apple-wrapper": "public"}), NZ)
    assert r["engine"] == "amd" and "вручную" in r["note"]


def test_music_video_still_goes_to_gamdl():
    """У клипов альтернативы нет: Widevine CDM есть только у gamdl."""
    r = ar.route_apple("alac", _cfg(), "https://music.apple.com/nz/music-video/x/999")
    assert r["engine"] == "gamdl" and r["quality"] == "mv"


# ── структурная защита ───────────────────────────────────────────────────────
# Владелец 04.09.2026: «прибей гвоздями, надоело править это уже не первый раз».
# Тесты выше проверяют ПОВЕДЕНИЕ, но новая ветка маршрутизатора может вернуть
# словарь напрямую и обойти правило — именно так оно и ломалось дважды. Поэтому
# отдельно держим форму кода: выход из модуля ровно один.

def test_router_has_exactly_one_exit():
    """Все ветки обязаны выпускать решение через `_decide`."""
    import pathlib as _pl
    src = (_pl.Path(__file__).resolve().parents[1] / "ripster" / "apple_router.py").read_text(encoding="utf-8")
    direct = src.count('return {"engine"')
    assert direct == 1, (
        f"прямых выходов {direct}, а должен быть один — внутри самого _decide. "
        f"Новая ветка вернула маршрут в обход проверки, и запрет на публичный "
        f"wrapper/куки на неё не действует."
    )


def test_guard_still_knows_both_prohibitions():
    src_fn = ar._decide
    assert src_fn is not None
    # Публичный — только вручную.
    r = src_fn("amd", "alac", pref="auto", local_ok=True, is_video=False)
    assert r["engine"] == "zhaarey"
    # Куки — не вместо живого локального враппера.
    r = src_fn("gamdl", "aac", pref="auto", local_ok=True, is_video=False)
    assert r["engine"] == "zhaarey"
    # Но видео у gamdl не отнимаем.
    r = src_fn("gamdl", "mv", pref="auto", local_ok=True, is_video=True)
    assert r["engine"] == "gamdl"


def test_guard_says_out_loud_when_it_corrects(capsys):
    """Тихая коррекция сделала бы сторожа украшением: разъехавшуюся ветку
    надо было бы искать глазами."""
    ar._decide("amd", "alac", pref="auto", local_ok=True, is_video=False)
    assert "маршрут исправлен" in capsys.readouterr().out
