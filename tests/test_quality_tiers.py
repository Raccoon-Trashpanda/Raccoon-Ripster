"""Тариф → потолок качества: строки обзора аккаунтов и карточка SoundCloud.

Повод (22.09.2026). Владелец получил 160 kbps с «RA.1057 Marina Herlop» и принял
это за баг. Бага не было: Go+ покупает AAC 256 только там, где релиз отдаёт
`aac_hq`, — у этого трека его не было вовсе (`aac_160k, aac_96k, abr_sq,
mp3_1_0`). Ни строка аккаунта, ни карточка релиза не отвечали на вопрос
«что мне даёт моя подписка и что даст ЭТА запись» — вопрос выглядел как поломка.

Что охраняет этот файл:

  1. Маппинг тариф→потолок живёт на сервере (ripster/quality_tiers.py) и
     молчит там, где не уверен: неизвестный тариф — «не определено», а не
     самая смелая догадка.
  2. Честность строки: ИСТЁКШАЯ подписка читается сломанной, а не старым
     тарифом. «Go+» на аккаунте без подписки = обещание AAC 256, которого
     никто не получит.
  3. Разбор реальных транскодов релиза и вердикт «что скачается» — на форме
     ответа из инцидента.
"""
import asyncio
from datetime import date, timedelta

import pytest

from ripster import quality_tiers as qt


# ── 1. Маппинг тарифа ────────────────────────────────────────────────────────

def test_soundcloud_go_plus_buys_aac256_and_free_caps_at_aac160():
    assert qt.ceiling("soundcloud", "Go+") == {"codec": "AAC", "kbps": 256}
    assert qt.ceiling("soundcloud", "", {"go_plus": True}) == {"codec": "AAC", "kbps": 256}
    # без Go+ поток всё равно доходит до AAC 160 — это замер движка, не догадка
    assert qt.ceiling("soundcloud", "Free (128 kbps)") == {"codec": "AAC", "kbps": 160}
    assert qt.ceiling("soundcloud", "", {"go_plus": False}) == {"codec": "AAC", "kbps": 160}


def test_unknown_tier_has_no_ceiling_not_a_brave_guess():
    """Промежуточный «Go», незнакомый сервис, пустой тариф → None.

    Именно `None` панель показывает как «не определено». Выдуманный потолок
    стоит дороже молчания: под «AAC 256» человек будет ждать 256 там, где
    сервис отдаёт 160, и снова решит, что это баг.
    """
    assert qt.ceiling("soundcloud", "Go") is None
    assert qt.ceiling("soundcloud", "") is None
    assert qt.ceiling("beatport", "Beatport Studio Link") is None   # чем платит — не мерали
    assert qt.ceiling("no-such-service", "Premium") is None


def test_amazon_lossless_flag_is_not_trusted():
    """Проба Amazon пишет lossless=True БЕЗ проверки Unlimited — не верим."""
    assert qt.ceiling("amazon", "", {"lossless": True}) is None


def test_tidal_quality_and_names():
    assert qt.ceiling("tidal", "PREMIUM") == {"kbps": 320}
    # кодек у 320-го слоя разный у разных клиентов — не додумываем,
    # поэтому у ответа НЕТ поля codec, и UI рисует «потолок 320 кбит/с»
    assert "codec" not in qt.ceiling("tidal", "PREMIUM")
    assert qt.ceiling("tidal", "PREMIUM", {"quality": "HI_RES_LOSSLESS"}) == \
        {"codec": "FLAC", "bits": 24, "khz": 192}
    assert qt.ceiling("tidal", "INTRO") is None


def test_qobuz_deezer_apple_spotify():
    assert qt.ceiling("qobuz", "studio") == {"codec": "FLAC", "bits": 24, "khz": 192}
    assert qt.ceiling("qobuz", "Music Pass") == {"codec": "FLAC", "kbps": 1411}
    assert qt.ceiling("deezer", "Deezer Free") == {"codec": "MP3", "kbps": 128}
    assert qt.ceiling("deezer", "Deezer Family") == {"codec": "MP3", "kbps": 320}
    assert qt.ceiling("deezer", "", {"lossless": True}) == {"codec": "FLAC", "kbps": 1411}
    # Apple: lossless входит в каждую платную подписку, но показываем только
    # когда подписка измеренно активна
    assert qt.ceiling("apple", "", {"sub_active": True}) == \
        {"codec": "ALAC", "bits": 24, "khz": 192}
    assert qt.ceiling("apple", "", {"sub_active": False}) is None
    assert qt.ceiling("spotify", "Premium (OGG)") == {"codec": "Ogg Vorbis", "kbps": 320}
    assert qt.ceiling("spotify", "free") is None


# ── 2. Честность строки обзора ───────────────────────────────────────────────

def _overview(monkeypatch, cfg, slots_map=None):
    from ripster import account_country as ac
    from ripster import wrapper_pool as wp
    from ripster.routes import setup as S
    monkeypatch.setattr(wp, "_configured_accounts", lambda c: [], raising=False)
    monkeypatch.setattr(ac, "schedule_heal", lambda svc, rows: False)
    empty = {s: [] for s in ("apple", "tidal", "deezer", "qobuz", "spotify", "soundcloud")}
    monkeypatch.setattr(ac, "slots",
                        lambda c, svc: (slots_map or empty).get(svc, []))
    S._cfg = cfg
    return asyncio.run(S.accounts_overview())


def test_overview_shows_tier_and_ceiling_from_probe(monkeypatch):
    d = _overview(monkeypatch, {
        "soundcloud-oauth-token": "FAKE-NOT-A-TOKEN",
        "soundcloud-country": "ES",
        "soundcloud-tier": "Go+",
    })
    row = next(a for a in d["accounts"] if a["service"] == "soundcloud")
    assert row["tier"] == "Go+"
    assert row["ceiling"] == {"codec": "AAC", "kbps": 256}


def test_overview_expired_account_reads_broken_not_old_tier(monkeypatch):
    """Главный honesty-кейс: вчерашняя дата окончания + закешированный «Go+».

    Строка обязана читаться сломанной: показать тариф — пообещать AAC 256
    учётке, у которой подписки уже нет.
    """
    yesterday = (date.today() - timedelta(days=1)).isoformat()
    d = _overview(monkeypatch, {
        "soundcloud-oauth-token": "FAKE-NOT-A-TOKEN",
        "soundcloud-tier": "Go+",
        "soundcloud-sub-end": yesterday,
    })
    row = next(a for a in d["accounts"] if a["service"] == "soundcloud")
    assert row["tier_hidden"] == "expired"
    assert row["tier"] == "" and row["ceiling"] is None


def test_overview_dead_slots_hide_tier(monkeypatch):
    """Все слоты мертвы — это нет доступа, а не «тариф был»."""
    d = _overview(monkeypatch,
                  {"soundcloud-oauth-token": "FAKE-NOT-A-TOKEN",
                   "soundcloud-tier": "Go+"},
                  {"soundcloud": [{"slot": 0, "label": "a0", "alive": False,
                                   "plan": "Go+", "lossless": True}]})
    row = next(a for a in d["accounts"] if a["service"] == "soundcloud")
    assert row["tier_hidden"] == "dead" and row["ceiling"] is None


def test_overview_never_probed_reads_unknown(monkeypatch):
    d = _overview(monkeypatch, {"soundcloud-oauth-token": "FAKE-NOT-A-TOKEN"})
    row = next(a for a in d["accounts"] if a["service"] == "soundcloud")
    assert row["tier"] == "" and row["ceiling"] is None
    assert "tier_hidden" not in row      # не сломана — просто ещё не измерено


def test_overview_tier_from_measured_slot_without_probe(monkeypatch):
    """Сторож померил пул, ручной пробы не было: план слота доезжает до строки."""
    d = _overview(monkeypatch, {"soundcloud-oauth-token": "FAKE-NOT-A-TOKEN"},
                  {"soundcloud": [{"slot": 0, "label": "a0", "alive": True,
                                   "plan": "Go+", "lossless": True}]})
    row = next(a for a in d["accounts"] if a["service"] == "soundcloud")
    assert row["tier"] == "Go+"
    assert row["ceiling"] == {"codec": "AAC", "kbps": 256}


def test_overview_mixed_pool_ceiling_follows_the_tier_it_names(monkeypatch):
    """Разношёрстный пул: «тариф free · потолок AAC 256» — это ложь.

    Снято живым прогоном на конфиге владельца (22.09): SoundCloud-пул —
    «free», «free», «Go+». Строка брала тариф из пробы основной учётки
    («free»), а флаги — OR'ом со всего пула, и обещала 256 кбит/с там, где
    показанный тариф даёт 160. Потолок обязан соответствовать названному
    тарифу; что умеет каждый слот — видно деревом под строкой.
    """
    pool = [{"slot": 0, "label": "a0", "alive": True, "plan": "free", "lossless": False},
            {"slot": 1, "label": "a1", "alive": True, "plan": "free", "lossless": False},
            {"slot": 2, "label": "a2", "alive": True, "plan": "Go+", "lossless": True}]
    d = _overview(monkeypatch, {"soundcloud-oauth-token": "FAKE-NOT-A-TOKEN",
                                "soundcloud-tier": "free"}, {"soundcloud": pool})
    row = next(a for a in d["accounts"] if a["service"] == "soundcloud")
    assert row["tier"] == "free"
    assert row["ceiling"] == {"codec": "AAC", "kbps": 160}
    assert row["slots"][2]["plan"] == "Go+", "пул виден деревом, а не слит в одну строку"

    # зеркальный случай: пробы нет, тариф берём из слота — и потолок того же слота
    d = _overview(monkeypatch, {"soundcloud-oauth-token": "FAKE-NOT-A-TOKEN"},
                  {"soundcloud": pool})
    row = next(a for a in d["accounts"] if a["service"] == "soundcloud")
    assert row["tier"] == "free" and row["ceiling"] == {"codec": "AAC", "kbps": 160}


def test_probe_remembers_tier_and_flags(monkeypatch):
    """_remember_probe: то, что вернула проба, переживает перезагрузку."""
    from ripster.routes import auth as A
    cfg = {}
    monkeypatch.setattr(A, "_cfg", cfg)
    monkeypatch.setattr(A, "_save_config", lambda c: None)
    A._remember_probe("soundcloud", {"login": "x", "country": "ES",
                                     "subscription": "Go+", "hires": True})
    assert cfg["soundcloud-tier"] == "Go+"
    assert cfg["soundcloud-hires"] is True
    assert cfg["soundcloud-country"] == "ES"
    # «?» — это не тариф (Qobuz на пустой подписке отвечает именно так)
    cfg2 = {}
    monkeypatch.setattr(A, "_cfg", cfg2)
    A._remember_probe("qobuz", {"subscription": "?", "country": "NZ"})
    assert "qobuz-tier" not in cfg2


# ── 3. Потолок конкретного релиза (SoundCloud) ───────────────────────────────

def _trans(*presets):
    return [{"preset": p, "url": f"https://api-v2.soundcloud.com/i/{p}",
             "format": {"protocol": "hls", "mime_type": "audio/mpeg"}}
            for p in presets]


def test_release_quality_reads_the_incident_track_list():
    """Сниппет формы — RA.1057 Marina Herlop: HQ-транскода нет вовсе."""
    from ripster.routes.soundcloud import _sc_release_quality, _sc_quality_out
    rel = _sc_release_quality(_trans("abr_sq", "aac_96k", "aac_160k", "mp3_1_0"))
    assert rel["best"] == {"preset": "aac_160k", "codec": "AAC", "kbps": 160}
    assert {f["preset"] for f in rel["formats"]} == {"aac_160k", "abr_sq", "mp3_1_0", "aac_96k"}
    verdict = _sc_quality_out(rel, {"codec": "AAC", "kbps": 256})   # Go+
    # тот самый ответ владельцу: 160 — не баг, релиз столько отдаёт
    assert verdict["why"] == "no_hq"
    assert verdict["delivered"]["kbps"] == 160


def test_release_quality_with_hq_and_go_plus():
    from ripster.routes.soundcloud import _sc_release_quality, _sc_quality_out
    rel = _sc_release_quality(_trans("aac_hq", "aac_160k", "mp3_1_0"))
    v = _sc_quality_out(rel, {"codec": "AAC", "kbps": 256})
    assert v["why"] == "ok" and v["delivered"]["preset"] == "aac_hq"


def test_release_quality_free_account_capped_by_tier():
    """HQ у релиза есть, но аккаунт без Go+ — врать про 256 не будем."""
    from ripster.routes.soundcloud import _sc_release_quality, _sc_quality_out
    rel = _sc_release_quality(_trans("aac_hq", "aac_160k", "mp3_1_0"))
    v = _sc_quality_out(rel, {"codec": "AAC", "kbps": 160})
    assert v["why"] == "tier_limited"
    assert v["delivered"]["preset"] == "aac_160k"


def test_release_quality_unknown_tier_stays_unknown():
    from ripster.routes.soundcloud import _sc_release_quality, _sc_quality_out
    rel = _sc_release_quality(_trans("aac_160k", "mp3_1_0"))
    v = _sc_quality_out(rel, None)
    assert v["why"] == "tier_unknown" and v["delivered"] is None
    assert v["release_best"]["kbps"] == 160     # про релиз-то мы знаем честно


def test_release_quality_unknown_preset_is_listed_but_not_counted():
    """Незнакомый пресет показывается как есть, но потолок по нему не считаем."""
    from ripster.routes.soundcloud import _sc_release_quality
    rel = _sc_release_quality(_trans("opus_a_0", "mp3_1_0"))
    assert rel["unknown"] == ["opus_a_0"]
    assert rel["best"]["preset"] == "mp3_1_0"


def test_release_quality_empty_transcodings():
    from ripster.routes.soundcloud import _sc_release_quality, _sc_quality_out
    rel = _sc_release_quality([])
    assert rel["best"] is None
    assert _sc_quality_out(rel, {"codec": "AAC", "kbps": 256})["why"] == "no_streams"
