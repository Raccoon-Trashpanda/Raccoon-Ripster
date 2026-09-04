"""«Нет FLAC» иногда про релиз, а иногда про учётку — и это разные ответы.

04.09.2026, проверено живьём. Deezer на бесплатную учётку отвечает
«Your account can't stream the track at the desired bitrate» на ЛЮБОЙ битрейт,
включая 128; две учётки Deezer Family рядом качают тот же трек в FLAC (29 МБ,
трек 66609426). Формулировка одна и та же и для «у релиза нет FLAC», и для «у
этой учётки нет прав».

Раннер классифицировал её как `no-flac`, а `no-flac` не входит в
RETRY_WITH_NEXT_ACCOUNT — перебор учёток не начинался вовсе. Человек получал
совет «выбери MP3 320/128», который на бесплатной учётке приводит ровно туда же.

Правило теперь такое: перебирать, только если в пуле осталась НЕ пробованная
учётка с бо́льшими правами. Если все одинаковы — у релиза действительно нет
FLAC, и тратить попытки не на что.
"""
import pytest

from ripster import account_fallback as afb
from ripster import deezer_accounts as da
from ripster import deezer_pool as dp


@pytest.fixture(autouse=True)
def _health(monkeypatch):
    known: dict[str, dict] = {}
    monkeypatch.setattr(da, "known", lambda arl, **_k: known.get(arl))
    monkeypatch.setattr(dp, "_warm_health", lambda *_a, **_k: None)
    return known


CFG = {"deezer-arl": "free", "deezer-accounts": [{"arl": "family"}]}


def _task(tried):
    return {"_accts_tried": {"deezer": list(tried)}}


def test_free_account_refusal_moves_on_to_the_lossless_one(_health):
    _health.update({"free": {"alive": True, "lossless": False},
                    "family": {"alive": True, "lossless": True}})
    assert afb.should_try_next(_task([0]), "deezer", "no-flac", CFG) is True


def test_release_without_flac_does_not_burn_attempts(_health):
    """Обе учётки одинаковы — значит дело в релизе, а не в правах."""
    _health.update({"free": {"alive": True, "lossless": True},
                    "family": {"alive": True, "lossless": True}})
    assert afb.should_try_next(_task([0]), "deezer", "no-flac", CFG) is False


def test_no_retry_once_the_best_account_was_already_tried(_health):
    _health.update({"free": {"alive": True, "lossless": False},
                    "family": {"alive": True, "lossless": True}})
    assert afb.should_try_next(_task([1]), "deezer", "no-flac", CFG) is False


def test_pool_was_not_involved_at_all(_health):
    """Пустой список пробованных — пула в задаче не было; правило прежнее."""
    _health.update({"free": {"alive": True, "lossless": False},
                    "family": {"alive": True, "lossless": True}})
    assert afb.should_try_next(_task([]), "deezer", "no-flac", CFG) is False


def test_other_reasons_are_unchanged(_health):
    _health.update({"free": {"alive": True, "lossless": False},
                    "family": {"alive": True, "lossless": True}})
    assert afb.should_try_next(_task([0]), "deezer", "entitlement", CFG) is True
    assert afb.should_try_next(_task([0]), "deezer", "postprocess", CFG) is False
    assert afb.should_try_next(_task([0]), "deezer", "removed", CFG) is False


def test_a_service_without_the_hook_keeps_the_old_behaviour():
    """Пул, не умеющий отвечать про качество, ничего не должен ломать."""
    assert afb._better_account_untried(_task([0]), "qobuz", {}) is False
    assert afb.should_try_next(_task([0]), "tidal", "no-flac", {}) is False
