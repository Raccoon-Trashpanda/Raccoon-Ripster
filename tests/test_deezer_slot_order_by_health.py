"""Перебор учёток Deezer обязан начинаться с той, что реально может скачать.

04.09.2026, живой отказ у гостя на https://www.deezer.com/ru/album/1001943021:

    account-fallback deezer: слот [0, 1, 2] → следующий (причина session)
    ✗ Deezer: ARL не задан или протух

Слоты 0–2 — бесплатная учётка и две отвергнутые; ровно на них ушёл весь лимит
попыток (MAX_ACCOUNT_ATTEMPTS = 4), и до двух Deezer Family с lossless, стоявших
в конце списка, очередь не дошла. Порядок «как в конфиге» разумен, только пока
про учётки ничего не известно. Когда известно — вести должен факт.

Второе требование теста важнее первого: недоступность НЕ приговор. Если до
Deezer не достучались, учётка не двигается в конец и не снимается — иначе один
сетевой сбой стоил бы владельцу рабочих токенов.
"""
import pytest

from ripster import deezer_accounts as da
from ripster import deezer_pool


@pytest.fixture(autouse=True)
def _clean_health(monkeypatch):
    """Судим только по тому, что подложено в тесте: ни сети, ни диска."""
    monkeypatch.setattr(da, "_MEM", {}, raising=False)
    monkeypatch.setattr(deezer_pool, "_warm_health", lambda *_a, **_k: None)
    known: dict[str, dict] = {}
    monkeypatch.setattr(da, "known", lambda arl, **_k: known.get(arl))
    return known


def test_rank_orders_lossless_then_alive_then_unknown_then_rejected(_clean_health):
    _clean_health.update({
        "family": {"alive": True, "lossless": True},
        "free": {"alive": True, "lossless": False},
        "dead": {"alive": False, "reason": "ARL отвергнут (гостевая сессия)"},
    })
    assert deezer_pool.health_rank("family") == 0
    assert deezer_pool.health_rank("free") == 1
    assert deezer_pool.health_rank("never-asked") == 2
    assert deezer_pool.health_rank("dead") == 3


def test_unreachable_is_not_treated_as_dead(_clean_health):
    """Таймаут до Deezer говорит о канале, а не об учётке."""
    _clean_health["flaky"] = {"alive": False, "unreachable": True,
                              "reason": "запрос не прошёл: ConnectTimeout"}
    assert deezer_pool.health_rank("flaky") == 2


def test_working_account_is_tried_before_the_dead_ones(_clean_health, tmp_path):
    """Та самая гостевая раскладка: рабочая учётка стоит последней в конфиге."""
    _clean_health.update({
        "arl-free": {"alive": True, "lossless": False},
        "arl-dead1": {"alive": False, "reason": "отвергнут"},
        "arl-dead2": {"alive": False, "reason": "отвергнут"},
        "arl-family": {"alive": True, "lossless": True},
    })
    cfg = {
        "deezer-arl": "arl-free",
        "deezer-accounts": [{"arl": "arl-dead1"}, {"arl": "arl-dead2"},
                            {"arl": "arl-family"}],
    }
    pool = deezer_pool.DeezerPool(deezer_pool._configured_accounts(cfg), tmp_path)
    got = pool.acquire()
    assert got is not None, "пул не выдал ни одной учётки"
    slot, arl, _cfg_dir = got
    assert arl == "arl-family", f"первым взят слот {slot} ({arl})"


def test_dead_slots_are_skipped_entirely_while_a_live_one_exists(_clean_health, tmp_path):
    """Каждая попытка на мёртвой учётке — попытка, не доставшаяся рабочей."""
    _clean_health.update({
        "arl-dead1": {"alive": False, "reason": "отвергнут"},
        "arl-dead2": {"alive": False, "reason": "отвергнут"},
        "arl-ok": {"alive": True, "lossless": False},
    })
    cfg = {"deezer-arl": "arl-dead1",
           "deezer-accounts": [{"arl": "arl-dead2"}, {"arl": "arl-ok"}]}
    pool = deezer_pool.DeezerPool(deezer_pool._configured_accounts(cfg), tmp_path)
    seen = []
    while True:
        got = pool.acquire(exclude=[s for s, _a, _c in seen])
        if got is None:
            break
        seen.append(got)
    assert [arl for _s, arl, _c in seen] == ["arl-ok"]


def test_all_dead_still_yields_a_slot(_clean_health, tmp_path):
    """Отказ должен приходить от Deezer, а не от нашей догадки: если живых нет,
    пробуем что есть — вдруг наше знание устарело."""
    _clean_health.update({
        "a": {"alive": False, "reason": "отвергнут"},
        "b": {"alive": False, "reason": "отвергнут"},
    })
    cfg = {"deezer-arl": "a", "deezer-accounts": [{"arl": "b"}]}
    pool = deezer_pool.DeezerPool(deezer_pool._configured_accounts(cfg), tmp_path)
    assert pool.acquire() is not None


def test_owner_priority_wins_over_measured_health(_clean_health, tmp_path):
    """Ручная настройка старше автоматики: если владелец задал приоритет —
    он и решает."""
    _clean_health.update({
        "arl-free": {"alive": True, "lossless": False},
        "arl-family": {"alive": True, "lossless": True},
    })
    cfg = {"deezer-arl": "arl-free",
           "deezer-accounts": [{"arl": "arl-family", "priority": 50}]}
    accounts = deezer_pool._configured_accounts(cfg)
    assert accounts[0]["priority"] == 100      # free: ранг 1, слот 0
    assert accounts[1]["priority"] == 50       # задано владельцем, не трогали
    pool = deezer_pool.DeezerPool(accounts, tmp_path)
    assert pool.acquire()[1] == "arl-family"


def test_slot_index_still_maps_to_its_own_deemix_dir(_clean_health, tmp_path):
    """Сортировка меняет ПОРЯДОК перебора, но не тождество слота: у слота своя
    папка конфига deemix, и её подмена сломала бы чужую сессию."""
    _clean_health.update({"x": {"alive": True, "lossless": True},
                          "y": {"alive": True, "lossless": False}})
    cfg = {"deezer-arl": "y", "deezer-accounts": [{"arl": "x"}]}
    pool = deezer_pool.DeezerPool(deezer_pool._configured_accounts(cfg), tmp_path)
    slot, arl, cfg_dir = pool.acquire()
    assert (slot, arl) == (1, "x")
    assert cfg_dir == tmp_path / "acct1"
