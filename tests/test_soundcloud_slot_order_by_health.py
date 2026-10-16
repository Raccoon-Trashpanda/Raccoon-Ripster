"""Маршрутизация SoundCloud-пула обязана начинаться с Go+-учётки.

Тот же сюжет, что у Deezer (04.09.2026, test_deezer_slot_order_by_health),
но здесь он особенно незаметен: токен без Go+ полностью валиден — `/me`
отвечает 200, загрузка проходит и даже заканчивается успехом, просто в
128 kbps вместо AAC 256. Если primary бесплатный, а живая Go+ лежит в пуле,
порядок «как в конфиге» отдаёт худшее качество при доступном лучшем.

Поэтому первым идёт измеренное здоровье (soundcloud_pool.health_rank),
а ручная настройка владельца — старше автоматики: заданный вручную приоритет
не переопределяется. Недоступный Go+ (401) с бесплатной учётки не снимает:
128 kbps — рабочая загрузка, а не отказ.
"""
import pytest

from ripster import soundcloud_accounts as sa
from ripster import soundcloud_pool as sp


@pytest.fixture(autouse=True)
def _clean_health(monkeypatch, tmp_path):
    """Судим только по тому, что подложено в тесте: ни сети, ни диска."""
    monkeypatch.setenv("RIPSTER_BASE_DIR", str(tmp_path))
    monkeypatch.setattr(sa, "_MEM", {}, raising=False)
    monkeypatch.setattr(sp, "_warm_health", lambda *_a, **_k: None)
    known: dict[str, dict] = {}
    monkeypatch.setattr(sa, "known", lambda tok, **_k: known.get(tok))
    return known


def _pool(cfg):
    return sp.SoundcloudPool(sp._configured_accounts(cfg))


def test_rank_orders_goplus_then_free_then_unknown_then_rejected(_clean_health):
    _clean_health.update({
        "goplus": {"alive": True, "go_plus": True},
        "free": {"alive": True, "go_plus": False},
        "dead": {"alive": False, "reason": "SoundCloud отверг токен (401)"},
        "flaky": {"alive": False, "unreachable": True,
                  "reason": "SoundCloud: лимит запросов (429)"},
    })
    assert sp.health_rank("goplus") == 0
    assert sp.health_rank("free") == 1          # 128 kbps — не отказ
    assert sp.health_rank("never-asked") == 2   # не спрашивали — не судим
    assert sp.health_rank("dead") == 3
    assert sp.health_rank("flaky") == 2         # авария канала — не приговор


def test_go_plus_slot_is_tried_before_the_free_primary(_clean_health):
    """Раскладка владельца из отчёта healthcheck: primary — free, Go+ — запас."""
    _clean_health.update({
        "free-primary": {"alive": True, "go_plus": False},
        "goplus-es": {"alive": True, "go_plus": True},
    })
    cfg = {"soundcloud-oauth-token": "free-primary",
           "soundcloud-accounts": [{"token": "goplus-es"}]}
    slot, token = _pool(cfg).acquire()
    assert (slot, token) == (1, "goplus-es"), "первым должен идти Go+"


def test_owner_priority_wins_over_auto_preference(_clean_health):
    """Ручная настройка старше автоматики: если владелец задал приоритет —
    он и решает, даже когда это значит «качать с бесплатной»."""
    _clean_health.update({
        "free-primary": {"alive": True, "go_plus": False},
        "goplus-es": {"alive": True, "go_plus": True},
    })
    cfg = {"soundcloud-oauth-token": "free-primary",
           "soundcloud-accounts": [{"token": "goplus-es", "priority": 300}]}
    accounts = sp._configured_accounts(cfg)
    assert accounts[0]["priority"] == 100      # free: ранг 1, слот 0 — авто
    assert accounts[1]["priority"] == 300      # задано владельцем, не трогали
    assert _pool(cfg).acquire() == (0, "free-primary")


def test_dead_go_plus_falls_back_to_the_free_primary(_clean_health):
    """Отвергнутый Go+ не тягает попытки: перебор отдаёт рабочую бесплатную."""
    _clean_health.update({
        "free-primary": {"alive": True, "go_plus": False},
        "goplus-es": {"alive": False, "reason": "SoundCloud отверг токен (401)"},
    })
    cfg = {"soundcloud-oauth-token": "free-primary",
           "soundcloud-accounts": [{"token": "goplus-es"}]}
    pool = _pool(cfg)
    seen = []
    while (got := pool.acquire(exclude=[s for s, _t in seen])):
        seen.append(got)
    assert [t for _s, t in seen] == ["free-primary"]


def test_active_token_for_the_report_follows_the_pool(_clean_health):
    """Отчёт («какая учётка активна») обязан показывать выбор пула, а не
    primary-ключ конфига: загрузку обслуживает именно он."""
    _clean_health.update({
        "free-primary": {"alive": True, "go_plus": False},
        "goplus-es": {"alive": True, "go_plus": True},
    })
    cfg = {"soundcloud-oauth-token": "free-primary",
           "soundcloud-accounts": [{"token": "goplus-es"}]}
    assert sp.active_token(cfg) == "goplus-es"
    # Пула нет — выбирать не из чего, вызывающий остаётся на primary-ключе.
    assert sp.active_token({"soundcloud-oauth-token": "solo"}) == ""
