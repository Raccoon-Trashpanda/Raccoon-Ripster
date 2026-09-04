"""«Подписка не видна» — два разных состояния, и совет у них разный.

04.09.2026, гостю не отдали `music.apple.com/nz/song/aurora/…`, а владельцу
пришло: «протухли cookies (cookies.txt). Экспортируй их заново». Совет был
ложным: сессия отвечала 200, storefront gb, а мертва была ПОДПИСКА. Выполнить
его значило потратить время и получить тот же отказ.

Обидно, что правду система знала: сторож здоровья различал эти состояния с
01.08.2026 — но знание жило внутри отчёта, а сообщение движка советовало своё.
Разбор вынесен в `ripster/apple_cookies.py`, и оба спрашивают там.

Тесты держат ровно это: каждому состоянию — свой совет, а незнание не выдаётся
за диагноз.
"""
import pytest

from ripster.engines.gamdl import GamdlEngine

LOG = "ERROR   No active Apple Music subscription found"


@pytest.fixture
def engine():
    return GamdlEngine()


def _verdict(monkeypatch, state, **extra):
    from ripster import apple_cookies as ac
    monkeypatch.setattr(ac, "verdict", lambda p: {"state": state, "storefront": "gb", **extra})


def test_dead_subscription_does_not_advise_re_export(engine, monkeypatch):
    """Главный случай: экспорт тех же куки не изменит ничего."""
    _verdict(monkeypatch, "no_subscription")
    msg = engine.is_finished(LOG, 1).error
    assert "НЕТ активной подписки" in msg
    assert "не поможет" in msg
    assert "gb" in msg
    # И не должно быть совета, ради которого владелец пошёл бы зря.
    assert "экспортируй файл заново" not in msg.lower()


def test_expired_session_does_advise_re_export(engine, monkeypatch):
    """А вот здесь экспорт как раз лечит — и совет обязан прозвучать."""
    _verdict(monkeypatch, "expired")
    msg = engine.is_finished(LOG, 1).error
    assert "протухла" in msg and "экспортируй" in msg.lower()
    assert "подписк" in msg.lower()          # объясняем, чего именно нет


def test_missing_token_names_the_actual_problem(engine, monkeypatch):
    _verdict(monkeypatch, "no_token")
    assert "media-user-token" in engine.is_finished(LOG, 1).error


def test_missing_file_says_so(engine, monkeypatch):
    _verdict(monkeypatch, "no_file")
    assert "не найден" in engine.is_finished(LOG, 1).error


def test_unknown_state_offers_both_instead_of_guessing(engine, monkeypatch):
    """Не смогли спросить — называем оба варианта. Угадать и ошибиться хуже."""
    _verdict(monkeypatch, "unknown")
    msg = engine.is_finished(LOG, 1).error
    assert "не удалось" in msg
    assert "сесси" in msg.lower() and "подписк" in msg.lower()


def test_probe_failure_never_breaks_the_verdict(engine, monkeypatch):
    """Сломанная проверка не должна съедать сообщение об ошибке целиком."""
    from ripster import apple_cookies as ac
    monkeypatch.setattr(ac, "verdict",
                        lambda p: (_ for _ in ()).throw(RuntimeError("сломалось")))
    msg = engine.is_finished(LOG, 1).error
    assert msg and "gamdl" in msg


def test_one_source_of_truth():
    """Сторож и движок обязаны спрашивать один модуль, а не расходиться."""
    import pathlib
    root = pathlib.Path(__file__).resolve().parents[1]
    eng = (root / "ripster" / "engines" / "gamdl.py").read_text(encoding="utf-8")
    hc = (root / "tools" / "ripster_healthcheck.py").read_text(encoding="utf-8")
    assert "apple_cookies" in eng, "движок больше не спрашивает общий модуль"
    assert "apple_cookies" in hc, "сторож больше не спрашивает общий модуль"
    # Старый безусловный совет не должен вернуться. Ищем именно ту фразу, что
    # уходила человеку, а не упоминание её в комментарии: разбор истории дефекта
    # в коде полезен и мешать ему нечего.
    assert "(cookies.txt). Экспортируй" not in eng, (
        "в движке снова безусловное «протухли cookies (cookies.txt), экспортируй "
        "заново» — это и был ложный совет"
    )
