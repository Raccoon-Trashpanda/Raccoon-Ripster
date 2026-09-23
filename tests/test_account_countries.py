"""Страна каждой учётки: разбор реальных ответов, честный отказ, таймзоны.

Повод (трекер #9005, 21.09.2026). Панель «страны аккаунтов» показывала «страна
не определена» там, где страна была известна: клиент считал признаком страны
часовой offset (`known = a.offset != null`), а offset не считывался на Windows
без IANA-базы. Плюс панель знала одну страну на сервис, хотя шесть аккаунтов
владельца живут в шести странах — а на этом держится вся стратегия ранней
доступности (релиз выходит в НЗ на полсуток раньше Европы).

Здесь три вещи, которые надо охранять тестом, а не словом:

  1. РАЗБОР реального ответа каждого сервиса (сниппеты сняты с живых форм
     ответов; секреты вырезаны, поля и регистр — подлинные).
  2. ОТСУТСТВИЕ страны в ответе → честный отказ, а НЕ дефолт. `us` в качестве
     «страны по умолчанию» стоит владельцу суток ожидания релиза, который уже
     вышел.
  3. Сбой таймзонной базы НЕ имеет права стирать известную страну.
"""
import asyncio
import sys
import time
import types

import pytest

from ripster import account_country as ac


# ── 1. Разбор реальных ответов ───────────────────────────────────────────────
# Каждый тест: живой формат ответа → страна учётки. Формы сняты с endpoints,
# которые реально опрашивает `*_accounts` (см. модули), значения — из пула
# владельца, обезличенные.

TIDAL_SESSIONS = {                      # GET api.tidal.com/v1/sessions
    "userId": "170000000",
    "countryCode": "nz",                # сервис отдаёт нижним регистром
    "subscription": {"premium_access": True},
}
DEEZER_GWLIGHT = {                      # POST api.deezer.com/q/gw-light deezer.getUserData
    "results": {
        "USER": {
            "USER_ID": 1500000000,
            "BLOG_NAME": "bulgarian-listener",
            "COUNTRY": "BG",
            "OPTIONS": {"license_country": "BG", "offer_name": "Deezer Family",
                        "expiration_timestamp": 1789000000},
        }
    }
}
QOBUZ_USER = {                          # GET /user/auth + /user GET
    "user": {"id": 14232934, "country_code": "NZ",
             "label": "streaming-studio", "lossless_streaming": True},
}
SOUNDCLOUD_ME = {                       # GET api.soundcloud.com/me
    "id": 700000000, "username": "nameless", "country_code": "ES",
    "plan": {"_id": "app-mono-go-plus-rich", "name": "Go+"},
}
APPLE_ACCOUNT_JSON = {                  # контейнер слота, порт 30020 изнутри
    "storefront_id": "143455-ca-1-2",
    "dev_token": "REDACTED", "music_token": "REDACTED",
}


def test_tidal_country_from_real_session_payload(monkeypatch):
    """Сессия Tidal → страна учётки в обзоре.

    Ответ `/v1/sessions` реальный: Tidal отдаёт `countryCode` нижним регистром
    («nz»), а `tidal_accounts` поднимает его до ISO-2 ещё на записи в кэш —
    обзор обязан принимать и то, и другое, иначе одна и та же учётка выглядит
    по-разному до и после перезапуска.
    """
    assert TIDAL_SESSIONS["countryCode"] == "nz"
    rows = _tidal_slots(monkeypatch, {"REF-A": {"country": "NZ", "alive": True,
                                                "plan": "PREMIUM"}})
    assert rows[0]["country"] == "NZ" and rows[0]["country_known"]
    # нижний регистр из чужого кэша — не «нет страны»: нормализуем у себя
    loose = _tidal_slots(monkeypatch, {"REF-A": {"country": "nz", "alive": True}})
    assert loose[0]["country"] == "NZ"


def test_deezer_country_from_real_gwlight_payload(monkeypatch):
    user = DEEZER_GWLIGHT["results"]["USER"]
    opts = user["OPTIONS"]
    # ровно та формула, что в deezer_accounts.arl_info
    cc = str(user.get("COUNTRY") or opts.get("license_country") or "").lower()
    assert cc == "bg"
    rows = _deezer_slots(monkeypatch, {"ARL1": {"country": cc, "alive": True,
                                                "plan": opts["offer_name"]}})
    assert rows[0]["country"] == "BG", "код страны нормализуется у нас, а не у сервиса"


def test_qobuz_country_from_real_user_payload(monkeypatch):
    u = QOBUZ_USER["user"]
    rows = _qobuz_slots(monkeypatch, {"QTOK": {"country": u["country_code"],
                                               "alive": True, "plan": u["label"]}})
    assert rows[0]["country"] == "NZ" and rows[0]["plan"] == "streaming-studio"


def test_soundcloud_country_from_real_me_payload(monkeypatch):
    u = SOUNDCLOUD_ME
    rows = _sc_slots(monkeypatch, {"SCTOK": {"country": u["country_code"],
                                             "alive": True, "login": u["username"]}})
    assert rows[0]["country"] == "ES" and rows[0]["account"] == "nameless"


def test_apple_storefront_id_maps_to_the_account_country(monkeypatch):
    """Apple: витрина подписки из контейнера слота, а не из ссылки/конфига."""
    from ripster import apple_accounts as aa
    # настоящая форма `storefront_id` — «143443-de-…» (разделитель дефис)
    monkeypatch.setattr(aa, "_account_json", lambda c, timeout=8.0:
                        {"storefront_id": "143443-de-1-2", "dev_token": "REDACTED"})
    monkeypatch.setattr(aa, "_CACHE", {})
    assert aa.container_storefront("rip-wrapper-1") == "de"


def test_apple_unparseable_storefront_id_stays_unknown(monkeypatch):
    """Незнакомый магазин остаётся «не выяснили» — US не подставляется.

    Форма id бывает и `143443:de:1` (двоеточия) — у旧 code такой id молча
    раскрывался в '', и это единственное честное поведение: выдуманная витрина
    стоит суток ожидания релиза.
    """
    from ripster import apple_accounts as aa
    for sid in ("143443:de:1", "", "999999-de-1", None):
        monkeypatch.setattr(aa, "_account_json",
                            lambda c, timeout=8.0, s=sid: {"storefront_id": s})
        monkeypatch.setattr(aa, "_CACHE", {})
        assert aa.container_storefront("amd-wrapper") == ""


# ── кэш-заглушки: сеть не трогаем, формат ответа — подлинный ────────────────

def _tidal_slots(monkeypatch, stored, cfg=None):
    import ripster.tidal_accounts as ta
    import ripster.tidal_pool as tp
    keys = list(stored)
    monkeypatch.setattr(ta, "known",
                        lambda s, max_age=24 * 3600.0: stored.get(s))
    monkeypatch.setattr(ta, "account_secret",
                        lambda a: a.get("tidal-refresh") or a.get("refresh") or "")
    monkeypatch.setattr(tp, "configured_accounts",
                        lambda config: [{"tidal-refresh": k, "label": f"a{i}"}
                                        for i, k in enumerate(keys)])
    return ac.slots(cfg or {"tidal-refresh": keys[0]}, "tidal")


def _deezer_slots(monkeypatch, stored):
    import ripster.deezer_accounts as da
    import ripster.deezer_pool as dp
    keys = list(stored)
    monkeypatch.setattr(da, "known", lambda s, max_age=24 * 3600.0: stored.get(s))
    monkeypatch.setattr(dp, "_configured_accounts",
                        lambda config: [{"arl": k, "label": f"a{i}"} for i, k in enumerate(keys)])
    return ac.slots({"deezer-arl": keys[0]}, "deezer")


def _qobuz_slots(monkeypatch, stored):
    import ripster.qobuz_accounts as qa
    import ripster.qobuz_pool as qp
    keys = list(stored)
    monkeypatch.setattr(qa, "known", lambda s, max_age=24 * 3600.0: stored.get(s))
    monkeypatch.setattr(qa, "account_secret", lambda a: a.get("auth_token") or "")
    monkeypatch.setattr(qp, "_configured_accounts",
                        lambda config: [{"auth_token": k, "label": f"a{i}"} for i, k in enumerate(keys)])
    return ac.slots({"qobuz-auth-token": keys[0]}, "qobuz")


def _sc_slots(monkeypatch, stored):
    import ripster.soundcloud_accounts as sa
    import ripster.soundcloud_pool as sp
    keys = list(stored)
    monkeypatch.setattr(sa, "known", lambda s, max_age=24 * 3600.0: stored.get(s))
    monkeypatch.setattr(sp, "_configured_accounts",
                        lambda config: [{"token": k, "label": f"a{i}"} for i, k in enumerate(keys)])
    return ac.slots({"soundcloud-oauth-token": keys[0]}, "soundcloud")


# ── 2. Честный отказ вместо выдуманной страны ────────────────────────────────

def test_no_country_in_the_response_yields_a_reason_not_a_default():
    """Отрицательный случай: в ответе страны нет → пустая строка + причина.

    Именно здесь раньше рождался «us»: молчаливый дефолт вместо признанного
    «не знаю».
    """
    r = ac.result("")
    assert r["country"] == "" and r["country_known"] is False
    assert r["country_reason"] == ac.UNKNOWN            # не «US», не «?»-код
    assert r["country_source"] == ""


def test_garbage_is_not_a_country():
    for junk in ("us ".strip() + "a", "USA", "1", "", "??", None, 42):
        assert ac.result(str(junk) if junk is not None else "")["country"] in ("", "US")
    # две буквы проходят, три — уже нет
    assert ac.result("usa")["country"] == ""
    assert ac.result("ca")["country"] == "CA"


def test_dead_token_is_reported_as_dead_token_not_as_no_country(monkeypatch):
    """Мёртвый токен — это нет доступа, а не «страны не бывает»."""
    import ripster.tidal_accounts as ta
    import ripster.tidal_pool as tp

    class _TA:
        @staticmethod
        def known(secret, max_age=0):
            return {"alive": False, "reason": "токен не обновляется (HTTP 401)"}

        @staticmethod
        def account_secret(a):
            return a.get("tidal-refresh") or ""

    class _TP:
        @staticmethod
        def configured_accounts(config):
            return [{"label": "primary", "tidal-refresh": "REF"}]

    monkeypatch.setattr(ta, "known", _TA.known)
    monkeypatch.setattr(ta, "account_secret", _TA.account_secret)
    monkeypatch.setattr(tp, "configured_accounts", _TP.configured_accounts)
    row = ac.slots({"tidal-refresh": "REF"}, "tidal")[0]
    assert row["country"] == ""
    assert row["country_reason"] == ac.DEAD_TOKEN


def test_overview_never_invents_a_country_for_apple():
    """Свежая установка без пробы: Apple обязан остаться «не известно».

    До правки строка была `(apple-country or storefront or "us")`, и панель
    показывала «США» учётке, которую никто не спрашивал.
    """
    from ripster.routes import setup as S

    cfg = {"wrapper-apple-id": "a@b.c", "wrapper-password": "x",
           "wrapper-accounts": []}
    S._cfg = cfg
    d = asyncio.run(S.accounts_overview())
    apple = next(a for a in d["accounts"] if a["service"] == "apple")
    assert apple["country"] == ""
    assert apple["country_known"] is False
    assert apple["country_reason"] == ac.NEVER_PROBED
    # и ни одна строка ответа не содержит выдуманного US
    assert "US" not in {a.get("country") for a in d["accounts"] if not a.get("country_known")}


# ── 3. Таймзонная база не имеет права стирать страну ────────────────────────

def _no_tz(monkeypatch):
    """Симулируем машину БЕЗ `tzdata`: zoneinfo не находит ни одну зону.

    Именно это происходило на Windows: `ZoneInfo('Pacific/Auckland')` →
    ZoneInfoNotFoundError, и строка с реальной страной уезжала с панели.
    """
    import zoneinfo

    def boom(key):
        raise zoneinfo.ZoneInfoNotFoundError(f"No time zone found with key {key}")

    monkeypatch.setattr(zoneinfo, "ZoneInfo", boom)


def test_missing_tz_database_keeps_the_known_country(monkeypatch):
    from ripster.routes import setup as S
    _no_tz(monkeypatch)
    info = S._country_info("NZ")
    assert info["country"] == "NZ", "известная страна не должна исчезать из-за пояса"
    assert info["flag"] == "\U0001F1F3\U0001F1FF"
    assert info["country_known"] is True
    assert info["offset"] is None and info["tz_known"] is False
    assert info["tz_reason"] == "tz_unavailable"


def test_country_and_timezone_are_independent_signals(monkeypatch):
    """Разные отказы → разные ответы клиента: «нет пояса» ≠ «нет страны»."""
    from ripster.routes import setup as S
    _no_tz(monkeypatch)
    no_tz = S._country_info("CA")
    no_cc = S._country_info("")
    assert (no_tz["country_known"], no_tz["tz_known"]) == (True, False)
    assert (no_cc["country_known"], no_cc["tz_known"]) == (False, False)
    assert no_cc["country_reason"] and not no_tz["country_reason"]


def test_overview_reports_countries_even_with_broken_tz(monkeypatch):
    from ripster.routes import setup as S
    import ripster.wrapper_pool as wp
    _no_tz(monkeypatch)
    monkeypatch.setattr(wp, "_configured_accounts", lambda cfg: [], raising=False)
    S._cfg = {"apple-country": "CA", "tidal-country": "NZ", "tidal-refresh": "r",
              "tidal-token": "t", "tidal-accounts": []}
    d = asyncio.run(S.accounts_overview())
    by = {a["service"]: a for a in d["accounts"]}
    assert by["apple"]["country"] == "CA" and by["apple"]["country_known"]
    assert by["apple"]["offset"] is None            # пояс не посчитан
    assert by["apple"]["country_reason"] == ""      # а стране это не мешает
    assert by["apple"]["local_time"] == ""


# ── 4. Разбивка по аккаунтам, а не по сервису ────────────────────────────────

def test_breakdown_reports_differing_accounts():
    rows = [ac.result("CA"), ac.result("GB"), ac.result("GB"), ac.result("")]
    bd = ac.breakdown(rows)
    assert bd["countries"] == ["CA", "GB"]
    assert bd["varies"] is True            # смысл панели: 6 аккаунтов ≠ 1 страна
    assert bd["unknown"] == 1


def test_breakdown_says_quietly_when_accounts_agree():
    bd = ac.breakdown([ac.result("nz"), ac.result("NZ")])
    assert bd["countries"] == ["NZ"] and bd["varies"] is False and bd["unknown"] == 0


# ── 5. Кэш и самопочинка: «unknown» не должно прилипнуть ────────────────────

def test_unknown_slot_is_due_for_a_recheck():
    assert ac.stale_slots([ac.result("")]) == [{"country": "", "country_known": False,
                                                "country_reason": ac.UNKNOWN,
                                                "country_source": "", "checked_at": ""}]


def test_fresh_measurement_is_not_resent():
    fresh = ac.result("nz", at=time.time())
    assert ac.stale_slots([fresh]) == []


def test_stale_measurement_is_resent():
    old = ac.result("nz", at=time.time() - 30 * 3600)
    assert ac.stale_slots([old]) != []


def test_heal_is_throttled_per_service(monkeypatch):
    monkeypatch.setitem(ac._heal_at, "tidal", time.time())
    assert ac.needs_heal("tidal", [ac.result("")], now=time.time()) is False


def test_heal_is_offered_when_unknown_and_cooldown_passed(monkeypatch):
    monkeypatch.setattr(ac, "_heal_at", {})
    assert ac.needs_heal("tidal", [ac.result("")], now=time.time()) is True


def test_heal_does_not_reprobe_a_service_that_has_no_country_at_all(monkeypatch):
    """Spotify: переспрашивать бессмысленно — поля нет. Не ждём токены зря."""
    monkeypatch.setattr(ac, "_heal_at", {})
    assert "spotify" not in ac._HEALERS
    assert ac.needs_heal("spotify", [ac.result("", ac.NO_FIELD)], now=time.time()) is False


def test_reauth_forgets_the_stale_unknown(monkeypatch):
    """После перевхода час ожидания не должен прятать уже известное."""
    monkeypatch.setitem(ac._heal_at, "qobuz", time.time())
    ac.forget_heal("qobuz")
    assert ac.needs_heal("qobuz", [ac.result("")], now=time.time()) is True


def test_schedule_heal_never_touches_the_network_when_disabled(monkeypatch):
    monkeypatch.setattr(ac, "needs_heal", lambda *a, **k: True)
    monkeypatch.setenv("RIPSTER_COUNTRY_HEAL", "0")
    started = []
    monkeypatch.setattr(ac.threading, "Thread", lambda **kw: started.append(kw))
    assert ac.schedule_heal("tidal", [ac.result("")]) is False
    assert started == []


def test_slots_reader_failure_degrades_to_unknown_not_an_exception(monkeypatch):
    """Один недоступный источник не должен ронять весь обзор (500 вместо строки)."""
    import ripster.tidal_pool as tp

    def boom(config):
        raise RuntimeError("пул не читается")

    monkeypatch.setattr(tp, "configured_accounts", boom)
    assert ac.slots({"tidal-refresh": "r"}, "tidal") == []
