# -*- coding: utf-8 -*-
"""Хранилище ключей своего реле: хэш, квоты, отзыв, учёт.

Проверяем то, что реально гарантирует код, а не то, что красиво звучит:
плаинтекст ключа в базе не появляется НИКОГДА (это и есть смысл хэширования),
отозванный ключ перестаёт находиться, а суточный предел считается счётчиком
пейсинга, который переживает перезапуск.
"""
import time

import pytest

from ripster import pacing, relay_store as rs


@pytest.fixture(autouse=True)
def store(tmp_path, monkeypatch):
    """Изолированная база + счётчики пейсинга + фиксированный пепер.

    Оба модуля читают RIPSTER_BASE_DIR, поэтому «перезапуск» в тестах — это
    close() + повторный вызов того же пути."""
    monkeypatch.setenv("RIPSTER_BASE_DIR", str(tmp_path))
    rs.init(base_dir=tmp_path, secret_provider=lambda: "pepper-" + "s" * 31)
    rs._WINDOW.clear()
    rs._INFLIGHT.clear()
    yield rs
    rs.close()
    rs._WINDOW.clear()
    rs._INFLIGHT.clear()


CFG = {}   # пустой конфиг = дефолты FALLBACK_QUOTA


# ── хранение ────────────────────────────────────────────────────────────────

def test_issue_returns_plaintext_once_and_resolves_it(store):
    plain, ref = store.issue(label="phone", owner="123")
    assert plain.startswith(rs.KEY_PREFIX) and len(plain) > 30
    rec = store.resolve(plain)
    assert rec and rec["ref"] == ref and rec["label"] == "phone"
    assert store.resolve(plain + "x") is None
    assert store.resolve("") is None


def test_plaintext_never_reaches_the_database(store, tmp_path):
    """Главное обещание: скопированный файл базы не даёт ни одного ключа."""
    plain, _ = store.issue(label="kt", owner="1")
    store.note_hit(store.resolve(plain)["ref"], "key")
    store.close()
    blob = (tmp_path / "dist" / "relay" / "relay_keys.db").read_bytes()
    assert plain.encode() not in blob
    assert plain[:12].encode() not in blob          # даже длинный префикс не ищется


def test_pepper_changes_every_ref(store, tmp_path):
    """Поворот session-secret отзывает все ключи — и это надо знать, а не
    обнаружить в проде: без этого теста механизм «пепер из секрета» был бы
    обещанием, которое никто не проверял."""
    plain, ref = store.issue(label="a")
    assert store.resolve(plain)["ref"] == ref
    rs.init(base_dir=tmp_path, secret_provider=lambda: "other-" + "t" * 31)
    assert store.resolve(plain) is None


def test_revoke_by_ref_and_by_label(store):
    p1, r1 = store.issue(label="one")
    p2, _ = store.issue(label="two")
    assert store.revoke(r1) == {"ok": True, "count": 1}
    assert store.resolve(p1) is None
    assert store.resolve(p2) is not None
    # отзыв по метке снимает ВСЬИ одноимённые, а не «первый попавшийся»
    store.issue(label="dup")
    store.issue(label="dup")
    assert store.revoke("dup")["count"] == 2
    assert store.revoke("нет-такого") == {"ok": False, "count": 0}
    assert store.revoke("")["ok"] is False


def test_list_keys_carries_no_secret_material(store):
    plain, ref = store.issue(label="видимый", owner="42")
    row = next(k for k in store.list_keys() if k["ref"] == ref)
    assert row["label"] == "видимый" and row["owner"] == "42"
    assert row["hint"] == ref[:8]
    assert not any(plain in str(v) for v in row.values())
    assert " revoked" not in str(row) and row["revoked"] is False
    store.revoke(ref)
    assert next(k for k in store.list_keys() if k["ref"] == ref)["revoked"] is True
    assert all(k["ref"] != ref for k in store.list_keys(include_revoked=False))


# ── квоты ───────────────────────────────────────────────────────────────────

def test_defaults_are_conservative_and_owner_overridable(store):
    assert store.defaults({}) == rs.FALLBACK_QUOTA
    d = store.defaults({"relay-default-qps": 5, "relay-default-per-day": 0})
    assert d["qps"] == 5 and d["per_day"] == 0        # 0 = потолок снят
    assert store.defaults({"relay-default-qps": "мусор"})["qps"] == rs.FALLBACK_QUOTA["qps"]


def test_quota_inheritance_and_override(store):
    _, ref = store.issue(label="наследник")
    rec = store.get(ref)
    assert store.quota_of(rec, {}) == rs.FALLBACK_QUOTA
    store.set_quota(ref, per_day=10, qps=-1)
    rec = store.get(ref)
    assert store.quota_of(rec, {})["per_day"] == 10
    assert store.quota_of(rec, {})["qps"] == 0        # -1 = без предела
    assert store.set_quota(ref, evil_field=5) == {"ok": False, "changed": []}


def test_qps_limit_engages_within_one_second(store):
    plain, ref = store.issue(label="qps")
    store.set_quota(ref, qps=2, concurrency=-1, per_hour=-1, per_day=-1)
    rec = store.get(ref)
    now = time.time()
    assert store.check(rec, now=now)["ok"] is True
    store.acquire(rec, now=now)
    assert store.check(rec, now=now)["ok"] is True    # второй разрешён
    store.acquire(rec, now=now)
    verdict = store.check(rec, now=now)
    assert verdict["ok"] is False and verdict["reason"] == "qps"
    assert 0 < verdict["retry_after"] <= 1.0
    # через секунду окно освобождается
    assert store.check(rec, now=now + 1.05)["ok"] is True


def test_unlimited_quota_is_a_real_option(store):
    """-1 из админки = «не считать». Ноль так не умеет (он значит
    «наследовать хозяйский дефолт»), и путать их нельзя."""
    _, ref = store.issue(label="unlim")
    store.set_quota(ref, per_day=-1, per_hour=-1)
    rec = store.get(ref)
    assert store.quota_of(rec, {})["per_day"] == 0
    for _ in range(40):
        assert store.check(rec)["ok"] is True
        store.note_hit(ref, "key")


def test_concurrency_counts_inflight(store):
    _, ref = store.issue(label="conc")
    store.set_quota(ref, qps=-1, concurrency=1, per_hour=-1, per_day=-1)
    rec = store.get(ref)
    assert store.check(rec)["ok"] is True
    store.acquire(rec)
    v = store.check(rec)
    assert v["ok"] is False and v["reason"] == "concurrency"
    assert [k for k in store.list_keys() if k["ref"] == ref][0]["inflight"] == 1
    store.release(rec)
    assert store.check(rec)["ok"] is True


def test_hour_and_day_limits_use_pacing_counters(store):
    """Счётчик — общий с ripster/pacing.py, поэтому проверка и на «сработало»,
    и на «записано туда, где его увидят в отчёте»."""
    _, ref = store.issue(label="day")
    store.set_quota(ref, qps=-1, concurrency=-1, per_hour=5, per_day=8)
    rec = store.get(ref)
    for _ in range(5):
        assert store.check(rec)["ok"] is True
        store.note_hit(ref, "key")
    v = store.check(rec)
    assert v["ok"] is False and v["reason"] == "hour"
    assert v["retry_after"] > 0
    assert pacing.counts(rs.PACE_SERVICE, ref)["hour"] == 5
    for _ in range(3):
        store.note_hit(ref, "key")
    assert pacing.counts(rs.PACE_SERVICE, ref)["day"] == 8
    assert store.check(rec)["ok"] is False


def test_denied_request_does_not_burn_the_quota(store):
    """Отказ по квоте не должен приближать суточный потолок: иначе один
    настойчивый клиент выбивает себе доступ сам."""
    _, ref = store.issue(label="deny")
    store.set_quota(ref, per_day=3, qps=-1, concurrency=-1, per_hour=-1)
    rec = store.get(ref)
    for _ in range(3):
        store.note_hit(ref, "m3u8")
    assert store.check(rec)["ok"] is False
    for _ in range(20):
        store.note_hit(ref, "m3u8", ok=False)
    assert pacing.counts(rs.PACE_SERVICE, ref)["day"] == 3
    assert [k for k in store.list_keys() if k["ref"] == ref][0]["denied"] == 20


def test_usage_breakdown_per_endpoint_and_day(store):
    _, ref = store.issue(label="u")
    store.note_hit(ref, "key")
    store.note_hit(ref, "key")
    store.note_hit(ref, "m3u8")
    store.note_hit(ref, "lyrics", ok=False)
    assert {r["endpoint"]: r for r in store.usage_today(ref)} == {
        "key": {"endpoint": "key", "n": 2, "denied": 0},
        "m3u8": {"endpoint": "m3u8", "n": 1, "denied": 0},
        "lyrics": {"endpoint": "lyrics", "n": 0, "denied": 1},
    }
    today = time.strftime("%Y-%m-%d")
    assert store.usage_summary(1)[today] == {"n": 3, "denied": 1}


def test_survives_restart(store, tmp_path):
    """close() + повторный open() на том же файле — это и есть перезапуск
    приложения: база и счётчики должны остаться на месте."""
    plain, ref = store.issue(label="после рестарта")
    store.note_hit(ref, "key")
    store.close()
    rs._WINDOW.clear()
    rs._INFLIGHT.clear()
    rs.init(base_dir=tmp_path, secret_provider=lambda: "pepper-" + "s" * 31)
    rec = store.resolve(plain)
    assert rec["ref"] == ref and rec["label"] == "после рестарта"
    assert pacing.counts(rs.PACE_SERVICE, ref)["day"] == 1
