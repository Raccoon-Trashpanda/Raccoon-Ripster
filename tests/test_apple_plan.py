"""Конкурентность дешифровки по тарифу Apple (`ripster/apple_plan.py`).

Владелец 24.09.2026 (выжимка чата @apple_music_alac, #343764/#348845): личная
подписка Apple Music выдерживает ОДИН поток расшифровки, семейная — до шести.
На личной второй поток встречает диалогом «More than one device is trying to
play music» и `end lease code 3084`.

Здесь проверяются три вещи, которые без тестов снова стали бы верой:

  * тариф распознаётся из `/v1/me/account?meta=subscription`, а неизвестный
    тариф = ОДИН поток (угадать «family» дороже, чем недоиспользовать слот);
  * журнал враппера с 3084 разбирается в сигнал, который сузил учётку до
    одного потока, — и НЕ в приговор учётке (в отличие от 3062);
  * пул считает потоки на УЧЁТКУ, а не на контейнер: два слота с одной личной
    подпиской — это один поток, и полоса планировщика обязана знать именно это.
"""

import pathlib
import sys
import time

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from ripster import apple_plan as ap                      # noqa: E402
from ripster import apple_accounts as aa                   # noqa: E402
from ripster import wrapper_pool as wp                     # noqa: E402


# ── изоляция состояния ────────────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def base_dir(tmp_path, monkeypatch):
    """Всё состояние — в tmp: файл тарифа переживает запуск приложения, и
    тесты не имеют права читать `dist/` рабочей машины."""
    monkeypatch.setenv("RIPSTER_BASE_DIR", str(tmp_path))
    ap._MEM.clear()
    monkeypatch.setattr(wp, "_blocks_path", lambda: tmp_path / "login_blocks.json")
    return tmp_path


ACCOUNTS = [
    # Одна и та же учётка, записанная дважды: так человек вполне может
    # оставить её и в «primary», и в списке пула.
    {"id": "ind@example.invalid", "password": "p0", "label": "primary"},
    {"id": "ind@example.invalid", "password": "p0", "label": "тот же apple id"},
    {"id": "fam@example.invalid", "password": "p1", "label": "family"},
]


def _pool(size=3):
    return wp.WrapperPool(ACCOUNTS, size=size)


def _ident(i: int) -> str:
    return wp.account_identity(ACCOUNTS[i])


# ── тариф ────────────────────────────────────────────────────────────────────

def test_streams_follow_the_plan():
    assert ap.streams_for_plan("INDIVIDUAL") == 1
    assert ap.streams_for_plan("student") == 1
    assert ap.streams_for_plan("Family") == ap.MAX_STREAMS
    # Неизвестное имя — не «сколько попросили», а один.
    assert ap.streams_for_plan("duo") == ap.UNKNOWN_STREAMS
    assert ap.streams_for_plan("") == ap.UNKNOWN_STREAMS


def test_parse_plan_reads_the_names_apple_actually_sends():
    assert ap.parse_plan({"plan": "FAMILY"}) == "family"
    assert ap.parse_plan({"type": "INDIVIDUAL"}) == "individual"
    assert ap.parse_plan({"productType": " Student "}) == "student"
    # Семейный план бывает помечен только флагом участия.
    assert ap.parse_plan({"isFamilyHost": True}) == "family"
    assert ap.parse_plan({"familySharing": False}) == ""
    assert ap.parse_plan({}) == ""
    assert ap.parse_plan(None) == ""


def test_record_plan_accepts_the_shape_the_caller_holds():
    """Один вызывающий держит `meta.subscription`, другой — весь ответ amp-api.
    Плоский ответ молча дал бы «тарифа нет», то есть учётку на одном потоке."""
    ident = _ident(2)
    ap.record_plan(ident, {"meta": {"subscription": {"plan": "FAMILY", "active": True}}})
    assert ap.cap_for(ident) == ap.MAX_STREAMS


def test_record_plan_sets_the_cap_and_survives_an_empty_answer():
    ident = _ident(2)
    assert ap.record_plan(ident, {"plan": "FAMILY"}) == "family"
    assert ap.cap_for(ident) == ap.MAX_STREAMS
    # Ответ без имени тарифа («не дослушали») не имеет права стирать tariff.
    assert ap.record_plan(ident, {}) == ""
    assert ap.cap_for(ident) == ap.MAX_STREAMS
    # А вот явное имя нового тарифа — имеет.
    assert ap.record_plan(ident, {"plan": "INDIVIDUAL"}) == "individual"
    assert ap.cap_for(ident) == 1


def test_unknown_account_gets_one_stream():
    assert ap.cap_for("никогда@не.измерена") == 1
    assert ap.plan_of("никогда@не.измерена") == ""


def test_stale_plan_falls_back_to_one_stream(monkeypatch):
    ident = _ident(2)
    ap.record_plan(ident, {"plan": "FAMILY"})
    monkeypatch.setattr(ap, "PLAN_TTL_S", 60.0)
    later = time.time() + 3600
    assert ap.cap_for(ident, now=later) == 1, \
        "протухшее «family» дало бы шесть потоков учётке, которая могла стать личной"


# ── 3084 ─────────────────────────────────────────────────────────────────────

def test_block_reason_recognises_the_second_stream_dialog(monkeypatch):
    """Журнал враппера с «More than one device…» обязан читаться в причину.
    До этого учётка молча числилась «причина не выяснена», а веер треков
    возвращался туда же на следующей задаче."""
    log = ('2026-09-24T12:00:03Z  WARN  playback: "More than one device is '
           'trying to play music" (end lease code 3084)')
    monkeypatch.setattr(aa.subprocess, "run",
                        lambda *a, **k: type("R", (), {"stdout": log, "stderr": ""})())
    assert aa.container_block_reason("rip-wrapper-1") == "plan_single_stream"


def test_lease_conflict_is_not_a_death_sentence():
    """3062 (лимит устройств) — повод не трогать учётку 12 часов. 3084 — повод
    перестать давать ей второй поток. Путать их нельзя: в первом случае
    помогает только время, во втором — наш порядок."""
    assert "plan_single_stream" not in aa.HARD_BLOCK_REASONS
    assert "plan_single_stream" not in wp._HARD_LOGIN_REASONS
    assert "plan_single_stream" in wp._SOFT_LOGIN_REASONS


def test_lease_conflict_narrows_to_one_and_reports_once():
    ident = _ident(2)
    ap.record_plan(ident, {"plan": "FAMILY"})
    now = time.time()

    assert ap.note_lease_conflict(ident, now=now) is True, "первый сигнал — владельцу"
    assert ap.cap_for(ident, now=now + 10) == 1
    assert ap.lease_cooling(ident, now=now + 10) is True
    assert ap.note_lease_conflict(ident, now=now + 20) is False, \
        "журнал враппера повторяет это на каждом треке — владелец не должен слушать это дважды"
    # Каждый новый отказ отодерживает остывание: лизинг держат, а не освобождают.
    assert ap.lease_cooling(ident, now=now + ap.LEASE_COOLDOWN_S + 5) is True
    # Остыло — учётка снова берётся за дело, НО одним потоком: право на шесть
    # даёт не поле `plan`, а ответ Apple, а он сказал обратное.
    after = now + 20 + ap.LEASE_COOLDOWN_S + 1
    assert ap.lease_cooling(ident, now=after) is False
    assert ap.cap_for(ident, now=after) == 1, \
        "шесть потоков вернулись бы сами — то есть ровно тот отказ, из-за которого их забрали"
    # Возвращает их только свежее измерение тарифа (явная переоценка учётки).
    assert ap.record_plan(ident, {"plan": "FAMILY"}, now=after + 60, force=True) == "family"
    assert ap.cap_for(ident, now=after + 61) == ap.MAX_STREAMS


def test_snapshot_hides_nothing_and_reveals_no_secrets():
    ident = _ident(0)
    ap.record_plan(ident, {"plan": "INDIVIDUAL"})
    ap.note_lease_conflict(ident)
    snap = ap.snapshot(ident)
    assert snap["plan"] == "individual" and snap["streams"] == 1
    assert snap["cooling_until"] > time.time() and snap["hits"] == 1
    assert "password" not in str(snap) and "p0" not in str(snap)


# ── мера тарифа: одна сетевая проба на учётку в час ──────────────────────────

def test_measure_container_asks_once_per_hour(monkeypatch):
    ident = _ident(0)
    calls = []
    monkeypatch.setattr(aa, "_account_json",
                        lambda c, timeout=8.0: (calls.append(c),
                                                {"music_token": "x" * 60,
                                                 "dev_token": "y" * 60})[1])
    monkeypatch.setattr("ripster.credential_health.subscription_meta",
                        lambda raw: {"plan": "FAMILY", "active": True})
    assert ap.measure_container(ident, "rip-wrapper-0") == "family"
    assert ap.measure_container(ident, "rip-wrapper-0") == "family"
    assert len(calls) == 1, "свежий тариф спрашивается повторно — лишние ходы к amp-api"
    # Учётка, у которой тариф не прочитался, переспрашивается не чаще раза в час,
    # а не на каждой задаче: лишний ход к amp-api дешёв ровно до того момента,
    # пока он повторяется на каждом старте закачки.
    other = _ident(2)
    monkeypatch.setattr("ripster.credential_health.subscription_meta", lambda raw: {})
    for _ in range(5):
        assert ap.measure_container(other, "rip-wrapper-2") == ""
    assert len(calls) == 2, "невнятный ответ amp-api заставил лезть в сеть на каждой задаче"


# ── пул: потоки считаются на учётку ──────────────────────────────────────────

def _make_pool_usable(monkeypatch, pool, serving=None, logged=()):
    monkeypatch.setattr(pool, "_start", lambda c, i: pool._ports(i))
    monkeypatch.setattr(pool, "_running", lambda c, i: True)
    monkeypatch.setattr(pool, "_serving",
                        lambda i, timeout=1.5: (serving is None or i in serving))
    monkeypatch.setattr(pool, "_session_country",
                        lambda i, max_age=15.0: ("ca" if i in logged else ""))
    monkeypatch.setattr(pool, "_wait_listening", lambda c, i, timeout=25.0: True)


def test_two_slots_of_one_individual_account_are_one_stream(monkeypatch):
    """Регресс, из-за которого личная подписка и ловила 3084: `acquire` знал
    только «слот занят», а два слота с ОДНОЙ учёткой считались двумя местами."""
    pool = _pool()
    _make_pool_usable(monkeypatch, pool, logged=(0, 1, 2))
    monkeypatch.setattr(wp, "_client", lambda: object())

    assert pool.plan_limited_size() == 2, \
        "три слота, две учётки, обе без семейного тарифа → два потока, а не три"
    first = pool.acquire()
    second = pool.acquire()
    assert first and second
    # Слоты 0 и 1 — одна учётка: второй из них может достаться только один раз.
    assert {first[0], second[0]} in ({0, 2}, {1, 2}), (first, second)
    assert pool.acquire() is None, "третья задача села бы третьим потоком на ту же учётку"


def test_family_plan_opens_the_second_slot_of_the_same_account(monkeypatch):
    pool = _pool()
    _make_pool_usable(monkeypatch, pool, logged=(0, 1, 2))
    monkeypatch.setattr(wp, "_client", lambda: object())
    ap.record_plan(_ident(0), {"plan": "FAMILY"})

    assert pool.plan_limited_size() == 3
    taken = [pool.acquire()[0] for _ in range(3)]
    assert sorted(taken) == [0, 1, 2], taken


def test_reserve_slot_refuses_the_second_stream_and_frees_on_release(monkeypatch):
    """Дорога закреплённого слота (`_force_slot`) раньше шла мимо учёта занятости."""
    pool = _pool()
    assert pool.reserve_slot(0) is True
    assert pool.reserve_slot(1) is False, "тот же apple id, второй слот — тариф не даёт"
    assert pool.reserve_slot(2) is True, "чужая учётка не должна страдать за соседку"
    pool.release(0)
    assert pool.reserve_slot(1) is True


def test_reserve_slot_refuses_a_busy_slot(monkeypatch):
    pool = _pool()
    _make_pool_usable(monkeypatch, pool, logged=(0,))
    monkeypatch.setattr(wp, "_client", lambda: object())
    assert pool.acquire()[0] == 0
    assert pool.reserve_slot(0) is False


def test_up_slot_pauses_login_softly_on_3084(monkeypatch):
    """Причина из журнала должна привести к КОРОТКОЙ паузе входа и одному
    потоку, а не к 12-часовой блокировке живой учётки."""
    pool = _pool()
    monkeypatch.setattr(wp, "_client", lambda: object())
    monkeypatch.setattr(pool, "_start", lambda c, i: pool._ports(i))
    monkeypatch.setattr(pool, "_running", lambda c, i: True)
    monkeypatch.setattr(pool, "_serving", lambda i, timeout=1.5: True)
    monkeypatch.setattr(pool, "_session_country", lambda i, max_age=15.0: "")
    monkeypatch.setattr(pool, "blocked_reason", lambda i: "plan_single_stream")
    monkeypatch.setattr(pool, "_wait_listening", lambda c, i, timeout=25.0: True)

    assert pool.up_slot(1) is False
    pause = wp.login_pause(ACCOUNTS[1])
    assert pause and pause["reason"] == "plan_single_stream"
    left = pause["until"] - time.time()
    assert left <= wp.LEASE_PAUSE_S + 5, f"пауза на {left:.0f} с — это не остывание лизинга"
    assert ap.cap_for(_ident(1)) == 1


def test_up_slot_hard_reason_still_pauses_for_twelve_hours(monkeypatch):
    """Мягкая пауза не должна случайно съесть и старое лечение 3062: там учётка
    правда жжёт слот устройства на каждой новой попытке входа."""
    pool = _pool()
    monkeypatch.setattr(wp, "_client", lambda: object())
    monkeypatch.setattr(pool, "_start", lambda c, i: pool._ports(i))
    monkeypatch.setattr(pool, "_running", lambda c, i: True)
    monkeypatch.setattr(pool, "_serving", lambda i, timeout=1.5: True)
    monkeypatch.setattr(pool, "_session_country", lambda i, max_age=15.0: "")
    monkeypatch.setattr(pool, "blocked_reason", lambda i: "device_limit")
    monkeypatch.setattr(pool, "_wait_listening", lambda c, i, timeout=25.0: True)

    assert pool.up_slot(2) is False
    pause = wp.login_pause(ACCOUNTS[2])
    assert pause and pause["reason"] == "device_limit"
    assert pause["until"] - time.time() > wp.LEASE_PAUSE_S * 10


def test_cooling_account_leaves_the_track_fanout(monkeypatch):
    """Веер треков по пулу не возвращает в ту же учётку второй поток, пока
    лизинг не остыл, — иначе следующая же задача получила бы тот же диалог."""
    pool = _pool()
    _make_pool_usable(monkeypatch, pool, logged=(0, 1, 2))
    monkeypatch.setattr(wp, "_client", lambda: object())
    monkeypatch.setattr(wp, "_POOL", pool)
    monkeypatch.setattr(wp, "pool_enabled", lambda cfg: True)
    assert len(wp.ensure_all_decrypt_ports({})) == 2      # 0 и 1 — одна учётка

    ap.note_lease_conflict(_ident(2))
    ports = wp.ensure_all_decrypt_ports({})
    assert f"127.0.0.1:{wp.DECRYPT_BASE + 2}" not in ports, ports
    assert pool.cooling_down(2) and not pool.cooling_down(0)


def test_health_shows_the_plan_of_each_slot(monkeypatch):
    pool = _pool()
    _make_pool_usable(monkeypatch, pool, logged=(0, 1, 2))
    monkeypatch.setattr(wp, "_client", lambda: object())
    ap.record_plan(_ident(2), {"plan": "FAMILY"})
    h = {row["slot"]: row for row in pool.health()}
    assert h[2]["plan"] == "family" and h[2]["streams"] == ap.MAX_STREAMS
    assert h[0]["plan"] == "" and h[0]["streams"] == 1, "неизвестный тариф = один поток"
