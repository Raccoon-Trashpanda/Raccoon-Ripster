"""Перебор учёток Qobuz обязан доходить до тех, что реально могут скачать.

04.09.2026, замер по живым учёткам владельца (`qobuz_accounts.survey`): из
восьми ТРИ отвечают 401 и стоят сразу за основной, у четвёртой подписка истекла
15.08.2026. Лимит попыток на задачу — `MAX_ACCOUNT_ATTEMPTS = 4`, то есть отказ
на слоте 0 съедал бы весь бюджет на мёртвых и ни разу не дошёл до четырёх
рабочих. Тот же дефект, что в тот же день закрыт у Deezer, — и найден он был
потому, что владелец попросил проверить остальные сервисы «вдруг там тоже
насрано».

Отдельно держим различие, которого у Deezer нет: у Qobuz токен бывает ВАЛИДНЫМ
при погасшей подписке. Качать таким нельзя (`credential.parameters` пуст,
streamrip падает с IneligibleError), но и удалять его нельзя — подписку
продлевают.
"""
import pytest

from ripster import account_fallback as afb
from ripster import qobuz_accounts as qa
from ripster import qobuz_pool as qp


@pytest.fixture(autouse=True)
def _health(monkeypatch, tmp_path):
    """Судим только по подложенному: ни сети, ни диска, ни чужого реестра."""
    monkeypatch.setenv("RIPSTER_BASE_DIR", str(tmp_path))
    monkeypatch.setattr(qp, "_warm_health", lambda *_a, **_k: None)
    known: dict[str, dict] = {}
    monkeypatch.setattr(qa, "known", lambda secret, **_k: known.get(secret))
    return known


def _cfg(*tokens):
    """Первый токен — основная учётка, остальные — пул."""
    head, *rest = tokens
    return {"qobuz-user-id": "1", "qobuz-auth-token": head,
            "qobuz-accounts": [{"qobuz-user-id": "2", "qobuz-auth-token": t} for t in rest]}


def _acct(token):
    return {"qobuz-auth-token": token, "qobuz-email": ""}


def test_rank_reflects_what_the_account_can_actually_do(_health):
    _health.update({
        "hires":   {"alive": True, "eligible": True, "hires": True, "lossless": True},
        "lossy":   {"alive": True, "eligible": True, "hires": False, "lossless": False},
        "expired": {"alive": True, "eligible": False, "reason": "подписка истекла"},
        "dead":    {"alive": False, "reason": "Qobuz отверг учётные данные (401)"},
        "flaky":   {"alive": False, "unreachable": True, "reason": "таймаут"},
    })
    assert qp.health_rank(_acct("hires")) == 0
    assert qp.health_rank(_acct("lossy")) == 1
    assert qp.health_rank(_acct("never-asked")) == 2
    assert qp.health_rank(_acct("flaky")) == 2      # не достучались ≠ приговор
    assert qp.health_rank(_acct("expired")) == 3    # токен жив, качать нечем
    assert qp.health_rank(_acct("dead")) == 3


def test_working_account_is_reached_past_three_dead_ones(_health, tmp_path):
    """Ровно раскладка владельца: мёртвые стоят сразу за основной."""
    _health.update({
        "primary": {"alive": True, "eligible": True, "hires": True},
        "d1": {"alive": False, "reason": "401"},
        "d2": {"alive": False, "reason": "401"},
        "d3": {"alive": False, "reason": "401"},
        "good": {"alive": True, "eligible": True, "hires": True},
    })
    cfg = _cfg("primary", "d1", "d2", "d3", "good")
    accounts = qp._configured_accounts(cfg)
    pool = qp.QobuzPool(accounts, tmp_path)
    taken = []
    while True:
        got = pool.acquire(exclude=[s for s, _a, _c in taken])
        if got is None:
            break
        taken.append(got)
    slots = [s for s, _a, _c in taken]
    assert slots == [0, 4], f"мёртвые слоты всё ещё в переборе: {slots}"


def test_expired_subscription_is_skipped_but_not_deleted(_health, tmp_path):
    """Из очереди убираем, из конфига — нет: подписку продлевают."""
    from ripster import retired_credentials as retired
    _health.update({"ok": {"alive": True, "eligible": True, "lossless": True},
                    "expired": {"alive": True, "eligible": False}})
    cfg = _cfg("ok", "expired")
    pool = qp.QobuzPool(qp._configured_accounts(cfg), tmp_path)
    taken = []
    while True:
        got = pool.acquire(exclude=[s for s, _a, _c in taken])
        if got is None:
            break
        taken.append(got)
    assert [s for s, _a, _c in taken] == [0]
    assert retired.strip_from_config(cfg) == []
    assert len(cfg["qobuz-accounts"]) == 1


def test_all_unusable_still_yields_a_slot(_health, tmp_path):
    """Если годных нет, пусть откажет Qobuz, а не наша догадка."""
    _health.update({"a": {"alive": False, "reason": "401"},
                    "b": {"alive": True, "eligible": False}})
    pool = qp.QobuzPool(qp._configured_accounts(_cfg("a", "b")), tmp_path)
    assert pool.acquire() is not None


def test_owner_priority_still_wins(_health, tmp_path):
    _health.update({"lossy": {"alive": True, "eligible": True, "lossless": False},
                    "hires": {"alive": True, "eligible": True, "hires": True}})
    cfg = _cfg("lossy", "hires")
    cfg["qobuz-accounts"][0]["priority"] = 50
    accounts = qp._configured_accounts(cfg)
    assert accounts[0]["priority"] == 100     # lossy: ранг 1, слот 0
    assert accounts[1]["priority"] == 50      # задано владельцем
    pool = qp.QobuzPool(accounts, tmp_path)
    assert pool.acquire()[0] == 1


def test_retired_account_never_comes_back(_health, tmp_path):
    from ripster import retired_credentials as retired
    _health.update({"good": {"alive": True, "eligible": True, "hires": True},
                    "gone": {"alive": True, "eligible": True, "hires": True}})
    retired.retire("qobuz_account", "gone", "Qobuz отверг учётные данные")
    cfg = _cfg("good", "gone")
    assert qp.health_rank(_acct("gone")) == 3
    notes = retired.strip_from_config(cfg)
    assert notes and cfg["qobuz-accounts"] == []


def test_no_flac_moves_on_only_when_a_better_account_remains(_health):
    """Тот же вопрос, что у Deezer: отказ по качеству — про релиз или про права."""
    _health.update({"lossy": {"alive": True, "eligible": True, "lossless": False},
                    "hires": {"alive": True, "eligible": True, "hires": True}})
    cfg = _cfg("lossy", "hires")
    task = {"_accts_tried": {"qobuz": [0]}}
    assert afb.should_try_next(task, "qobuz", "no-flac", cfg) is True
    # Лучшую уже пробовали — перебирать больше не на что.
    assert afb.should_try_next({"_accts_tried": {"qobuz": [1]}}, "qobuz", "no-flac", cfg) is False
