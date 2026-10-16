"""Телефон должен получать лучшую учётку сервиса, а не первое поле конфига.

04.09.2026. Сопряжение отдавало на телефон `deezer-arl`, `qobuz-auth-token` и
`soundcloud-oauth-token` — то есть ОСНОВНЫЕ поля. У владельца основной Deezer
оказался бесплатным аккаунтом (BG), а две Deezer Family с lossless лежали в
пуле: телефон качал 128 kbps при двух доступных lossless-учётках, и по списку
полей это было незаметно.

У Qobuz та же беда в другом виде — по срокам: основной токен NZ действует до
28.09, соседний NZ до 14.10. После первой даты телефон молча остался бы без
Qobuz, хотя рабочая учётка той же зоны рядом.

Правило: берём слот с наименьшим рангом здоровья, при равенстве — тот, что
раньше в конфиге (зона NZ так сохраняется). Ничего не измерено — ведём себя
как раньше и отдаём основную: догадка хуже прежнего поведения.
"""
import pytest

from ripster import deezer_accounts as da
from ripster import deezer_pool as dp
from ripster import qobuz_accounts as qa
from ripster import qobuz_pool as qp
from ripster import soundcloud_accounts as sa
from ripster import soundcloud_pool as sp
from ripster.routes import pairing as pr


@pytest.fixture(autouse=True)
def _health(monkeypatch, tmp_path):
    monkeypatch.setenv("RIPSTER_BASE_DIR", str(tmp_path))
    for mod in (dp, qp, sp):
        monkeypatch.setattr(mod, "_warm_health", lambda *a, **k: None, raising=False)
    known: dict[str, dict] = {}
    monkeypatch.setattr(da, "known", lambda s, **k: known.get(s))
    monkeypatch.setattr(sa, "known", lambda s, **k: known.get(s))
    monkeypatch.setattr(qa, "known", lambda s, **k: known.get(s))
    return known


DEEZER = {"deezer-arl": "free", "deezer-accounts": [{"arl": "family-ca"}, {"arl": "family-br"}]}


def test_phone_gets_the_lossless_deezer_not_the_free_primary(_health):
    """Ровно раскладка владельца: основной — бесплатный, lossless в пуле."""
    _health.update({
        "free": {"alive": True, "lossless": False},
        "family-ca": {"alive": True, "lossless": True},
        "family-br": {"alive": True, "lossless": True},
    })
    assert pr._best_slots(DEEZER)["deezer.arl"] == "family-ca"


def test_tie_keeps_the_earlier_slot(_health):
    """При равном здоровье порядок конфига решает — так сохраняется зона."""
    _health.update({"free": {"alive": True, "lossless": True},
                    "family-ca": {"alive": True, "lossless": True},
                    "family-br": {"alive": True, "lossless": True}})
    assert pr._best_slots(DEEZER)["deezer.arl"] == "free"


def test_nothing_measured_means_no_override(_health):
    """Неизвестность не повод менять поведение: вызывающий отдаст основную."""
    assert "deezer.arl" not in pr._best_slots(DEEZER)


def test_dead_accounts_are_never_sent(_health):
    _health.update({"free": {"alive": False, "reason": "отвергнут"},
                    "family-ca": {"alive": False, "reason": "отвергнут"},
                    "family-br": {"alive": False, "reason": "отвергнут"}})
    assert "deezer.arl" not in pr._best_slots(DEEZER)


def test_single_account_is_left_alone(_health):
    """Выбирать не из чего — не вмешиваемся."""
    _health["only"] = {"alive": True, "lossless": True}
    assert pr._best_slots({"deezer-arl": "only"}) == {}


def test_expired_qobuz_hands_over_to_the_next_of_the_same_zone(_health):
    """28.09 основной NZ-токен истечёт — телефон должен уехать на NZ до 14.10,
    а не остаться без Qobuz."""
    cfg = {"qobuz-user-id": "1", "qobuz-auth-token": "nz-expired",
           "qobuz-accounts": [{"qobuz-user-id": "2", "qobuz-auth-token": "nz-fresh"}]}
    _health.update({
        "nz-expired": {"alive": True, "eligible": False, "reason": "подписка истекла"},
        "nz-fresh": {"alive": True, "eligible": True, "hires": True},
    })
    assert pr._best_slots(cfg)["qobuz.token"] == "nz-fresh"


def test_soundcloud_prefers_go_plus(_health):
    cfg = {"soundcloud-oauth-token": "free-tok",
           "soundcloud-accounts": [{"token": "goplus-tok"}]}
    _health.update({"free-tok": {"alive": True, "go_plus": False},
                    "goplus-tok": {"alive": True, "go_plus": True}})
    assert pr._best_slots(cfg)["soundcloud.oauth"] == "goplus-tok"


def test_broken_pool_does_not_break_pairing(monkeypatch):
    """Сопряжение важнее оптимизации: сломался выбор — отдаём основную."""
    monkeypatch.setattr(dp, "_configured_accounts",
                        lambda cfg: (_ for _ in ()).throw(RuntimeError("сломалось")))
    assert "deezer.arl" not in pr._best_slots(DEEZER)
