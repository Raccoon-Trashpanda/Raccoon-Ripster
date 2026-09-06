"""Фоллбэк по учёткам: таблица «что повод взять следующую, а что нет».

Конфиги здесь синтетические, а не из config.yaml: тест про ПРАВИЛО, и он не
должен менять вердикт от того, что владелец добавил или убрал учётку.
"""
import pytest

from ripster import account_fallback as afb
from ripster import availability as av


def _cfg(qobuz=0, deezer=0):
    """Конфиг с нужным числом ДОПОЛНИТЕЛЬНЫХ учёток (основная задаётся отдельно)."""
    c = {
        "qobuz-user-id": "1", "qobuz-auth-token": "t",   # основная = слот 0
        "deezer-arl": "a" * 32,                          # основная = слот 0
        "qobuz-accounts": [{"user_id": str(i), "auth_token": "t", "label": f"q{i}"}
                           for i in range(qobuz)],
        "deezer-accounts": [{"arl": "b" * 32, "label": f"d{i}"} for i in range(deezer)],
    }
    return c


def _used(engine, n):
    """Задача, в которой n учёток этого сервиса уже отработали и отказали."""
    t = {}
    for i in range(n):
        afb.mark_tried(t, engine, i)
    return t


# ── Таблица причин ──────────────────────────────────────────────────────────────

@pytest.mark.parametrize("reason,expected", [
    ("entitlement", True),    # нет прав У ЭТОЙ учётки → следующая
    ("region",      True),    # витрина учётки → учётка другой страны
    ("postprocess", False),   # наша сторона
    ("decryption",  False),   # враппер/DRM — сожжём все учётки на одной аварии
    ("removed",     False),   # релиза нет ни у кого
    ("unavailable", False),
    ("no-flac",     False),
    ("",            False),
])
def test_reason_table(reason, expected):
    cfg = _cfg(qobuz=2)
    assert afb.should_try_next(_used("qobuz", 1), "qobuz", reason, cfg) is expected


# ── Пределы ────────────────────────────────────────────────────────────────────

def test_single_account_pool_never_retries():
    """Пул из ОДНОЙ учётки: перебирать нечего.

    Наивное `len(tried) < pool_size` при пустом tried разрешало повтор той же
    учётки без конца — `ripster-runaway-runs` в чистом виде.
    """
    cfg = _cfg(qobuz=0)                      # только основная
    assert afb.pool_size(cfg, "qobuz") == 1
    assert afb.should_try_next({}, "qobuz", "entitlement", cfg) is False
    assert afb.should_try_next(_used("qobuz", 1), "qobuz", "entitlement", cfg) is False


def test_empty_tried_means_pool_was_not_used():
    """Пустой tried — это «пула не было», а не «ещё не пробовали»."""
    cfg = _cfg(qobuz=3)
    assert afb.should_try_next({}, "qobuz", "entitlement", cfg) is False


def test_stops_at_pool_size():
    cfg = _cfg(qobuz=2)                      # основная + 2 = 3
    assert afb.pool_size(cfg, "qobuz") == 3
    t = _used("qobuz", 1)
    while afb.should_try_next(t, "qobuz", "entitlement", cfg):
        afb.mark_tried(t, "qobuz", len(afb.tried_slots(t, "qobuz")))
    assert len(afb.tried_slots(t, "qobuz")) == 3


def test_stops_at_attempt_cap_even_with_a_big_pool():
    cfg = _cfg(qobuz=20)
    t = _used("qobuz", afb.MAX_ACCOUNT_ATTEMPTS)
    assert afb.should_try_next(t, "qobuz", "entitlement", cfg) is False


def test_apple_is_not_in_the_generic_pool():
    """У Apple свой перебор — по СТРАНАМ слотов (runner: pick_slot_for).
    Общий «следующий свободный» потерял бы подбор витрины."""
    cfg = _cfg(qobuz=2)
    assert afb.pool_size(cfg, "apple") == 0
    assert afb.should_try_next(_used("apple", 1), "apple", "entitlement", cfg) is False


def test_mark_tried_is_idempotent_and_per_service():
    t = {}
    afb.mark_tried(t, "qobuz", 0)
    afb.mark_tried(t, "qobuz", 0)
    afb.mark_tried(t, "deezer", 0)
    assert afb.tried_slots(t, "qobuz") == [0]
    assert afb.tried_slots(t, "deezer") == [0]
    afb.mark_tried(t, "qobuz", None)          # слота нет — не пишем
    assert afb.tried_slots(t, "qobuz") == [0]


# ── Пулы действительно умеют исключать ──────────────────────────────────────────

def test_pools_honour_exclude(tmp_path):
    from ripster import qobuz_pool as qp
    pool = qp.QobuzPool([{"qobuz-user-id": str(i), "qobuz-auth-token": "t",
                          "qobuz-email": "", "qobuz-password": "", "label": f"q{i}"}
                         for i in range(3)], base_dir=tmp_path)
    assert pool.acquire()[0] == 0
    pool.release(0)
    # Без exclude повтор получил бы ТУ ЖЕ учётку и перебор не двинулся бы.
    assert pool.acquire(exclude=(0,))[0] == 1
    pool.release(1)
    assert pool.acquire(exclude=(0, 1))[0] == 2
    pool.release(2)
    assert pool.acquire(exclude=(0, 1, 2)) is None


# ── Итог загрузки → матрица доступности ────────────────────────────────────────

def test_record_outcome_only_takes_availability_verdicts(tmp_path):
    av.configure({}, tmp_path)
    av._cache, av._loaded = {}, True
    key = dict(upc="1234567890123", title="T", artist="A")

    assert av.record_outcome("beatport", "entitlement", **key) is True
    rec = av._cache[av._key("1234567890123", "", "", "")]["services"]["beatport"]
    assert rec["available"] is False and rec["reason"] == av.REASON_NO_RIGHTS
    assert rec["verified_by"] == "download"

    # Сетевая авария — не факт о витрине, записывать нельзя.
    assert av.record_outcome("beatport", "postprocess", **key) is False
    assert av._cache[av._key("1234567890123", "", "", "")]["services"]["beatport"]["reason"] == av.REASON_NO_RIGHTS

    assert av.record_outcome("beatport", "ok", **key) is True
    assert av._cache[av._key("1234567890123", "", "", "")]["services"]["beatport"]["available"] is True


def test_pick_source_falls_back_beyond_the_preference_list():
    """Предпочтения — ПОРЯДОК, а не белый список.

    В config.yaml лежит `[tidal, qobuz, apple]`, и релиз, доступный только в
    deezer или beatport, давал пустой ответ: «доступно, но качать неоткуда».
    """
    services = {"beatport": {"available": True}}
    assert av.pick_source(services, preference=["tidal", "qobuz", "apple"]) == "beatport"
    assert av.pick_source({"deezer": {"available": True}},
                          preference=["tidal", "qobuz", "apple"]) == "deezer"
    assert av.pick_source({"deezer": {"available": False}},
                          preference=["tidal"]) == ""
