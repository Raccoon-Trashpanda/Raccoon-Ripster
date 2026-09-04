"""Удаление дублей учёток: что считается доказательством, а что нет.

Повод (04.09.2026): в пуле SoundCloud владельца три токена, все живые, все с
Go+ — но два из них принадлежат ОДНОМУ аккаунту `goku`. Пул из трёх записей даёт
две независимые учётки; «следующая учётка» при отказе оказывается той же самой.

Владелец, ставя задачу, предупредил: «но с проверкой, а то ты можешь сам себя
обмануть». Поэтому тесты здесь охраняют не столько удаление, сколько ОТКАЗ от
удаления: пустой id, недоступность, разошедшаяся перепроверка и основной слот —
всё это причины не трогать ничего.
"""
import asyncio

import pytest

from ripster import dedup_accounts as dd


def _e(slot, secret, *, primary=False, acc="", alive=True, unreachable=False, display=""):
    return dd.Entry(slot=slot, secret=secret, label=f"acct{slot}", primary=primary,
                    account_id=acc, alive=alive, unreachable=unreachable, display=display)


def _rank(_e):
    return 0


def test_same_account_under_two_keys_is_a_duplicate():
    """Ровно случай владельца: разные токены, один аккаунт."""
    a = _e(0, "tok-A-xxxxxxxxxx", primary=True, acc="777", display="goku")
    b = _e(1, "tok-B-yyyyyyyyyy", acc="777", display="goku")
    c = _e(2, "tok-C-zzzzzzzzzz", acc="999", display="nameless")
    dups = dd._group([a, b, c], _rank)
    assert len(dups) == 1
    assert dups[0].keep is a and [x.slot for x in dups[0].drop] == [1]


def test_primary_slot_is_never_the_one_removed():
    """Основная запись лежит в главном поле конфига — её потеря дороже."""
    pool_first = _e(1, "tok-B-yyyyyyyyyy", acc="777")
    primary = _e(0, "tok-A-xxxxxxxxxx", primary=True, acc="777")
    dups = dd._group([pool_first, primary], _rank)
    assert dups[0].keep is primary
    assert all(not x.primary for x in dups[0].drop)


def test_unknown_account_id_is_never_a_duplicate():
    """«Не знаю, чей это аккаунт» ≠ «тот же самый»."""
    a = _e(0, "tok-A-xxxxxxxxxx", primary=True, acc="")
    b = _e(1, "tok-B-yyyyyyyyyy", acc="")
    assert dd._group([a, b], _rank) == []


def test_unreachable_entry_is_left_alone():
    """Молчание сети — не доказательство ни в одну сторону."""
    a = _e(0, "tok-A-xxxxxxxxxx", primary=True, acc="777")
    b = _e(1, "tok-B-yyyyyyyyyy", acc="777", alive=False, unreachable=True)
    assert dd._group([a, b], _rank) == []


def test_dead_entry_is_not_deduplicated():
    """Мёртвую учётку снимает сторож здоровья со своим порогом, не этот модуль."""
    a = _e(0, "tok-A-xxxxxxxxxx", primary=True, acc="777")
    b = _e(1, "tok-B-yyyyyyyyyy", acc="777", alive=False)
    assert dd._group([a, b], _rank) == []


def test_exact_copy_needs_no_network():
    """Одинаковая строка — это одна и та же запись по определению."""
    a = _e(0, "same-token-value-1", primary=True, acc="", alive=False, unreachable=True)
    b = _e(1, "same-token-value-1", acc="", alive=False, unreachable=True)
    dups = dd._group([a, b], _rank)
    assert len(dups) == 1 and dups[0].exact_copy
    assert dups[0].keep is a and [x.slot for x in dups[0].drop] == [1]


def test_more_capable_key_wins_when_neither_is_primary():
    a = _e(1, "tok-A-xxxxxxxxxx", acc="777")
    b = _e(2, "tok-B-yyyyyyyyyy", acc="777")
    ranks = {"tok-A-xxxxxxxxxx": 1, "tok-B-yyyyyyyyyy": 0}
    dups = dd._group([a, b], lambda e: ranks[e.secret])
    assert dups[0].keep is b and [x.slot for x in dups[0].drop] == [1]


def test_three_keys_one_account_leave_exactly_one():
    g = [_e(0, "tok-A-xxxxxxxxxx", primary=True, acc="777"),
         _e(1, "tok-B-yyyyyyyyyy", acc="777"),
         _e(2, "tok-C-zzzzzzzzzz", acc="777")]
    dups = dd._group(g, _rank)
    assert len(dups[0].drop) == 2 and dups[0].keep.slot == 0


def test_distinct_accounts_are_left_alone():
    g = [_e(0, "tok-A-xxxxxxxxxx", primary=True, acc="1"),
         _e(1, "tok-B-yyyyyyyyyy", acc="2"),
         _e(2, "tok-C-zzzzzzzzzz", acc="3")]
    assert dd._group(g, _rank) == []


def test_apply_without_confirm_changes_nothing(monkeypatch):
    """План — это рассказ, а не действие."""
    removed = []
    entries = [_e(0, "tok-A-xxxxxxxxxx", primary=True, acc="777"),
               _e(1, "tok-B-yyyyyyyyyy", acc="777")]

    async def probe(e):
        return {"alive": True, "account_id": "777", "login": "goku"}

    monkeypatch.setitem(dd.SERVICES, "fake", lambda cfg: (
        lambda: [dd.Entry(**vars(x)) for x in entries], probe,
        _rank, lambda e: removed.append(e.secret), "login"))
    monkeypatch.setitem(dd.RETIRE_KIND, "fake", "fake_kind")

    lines = asyncio.run(dd.apply({}, "fake"))
    assert removed == []
    assert any("удаление не выполнялось" in l for l in lines)


def test_apply_aborts_when_the_recheck_disagrees(monkeypatch):
    """Главная защита: план минутной давности — не доказательство."""
    removed = []
    entries = [_e(0, "tok-A-xxxxxxxxxx", primary=True, acc="777"),
               _e(1, "tok-B-yyyyyyyyyy", acc="777")]
    calls = {"n": 0}

    async def probe(e):
        # На повторном проходе сервис «передумал»: это уже другой аккаунт.
        calls["n"] += 1
        if calls["n"] > 2 and e.secret.startswith("tok-B"):
            return {"alive": True, "account_id": "999", "login": "other"}
        return {"alive": True, "account_id": "777", "login": "goku"}

    monkeypatch.setitem(dd.SERVICES, "fake", lambda cfg: (
        lambda: [dd.Entry(**vars(x)) for x in entries], probe,
        _rank, lambda e: removed.append(e.secret), "login"))
    monkeypatch.setitem(dd.RETIRE_KIND, "fake", "fake_kind")

    lines = asyncio.run(dd.apply({}, "fake", confirm=True))
    assert removed == [], "удалили, хотя перепроверка разошлась с планом"
    assert any("повторная проверка" in l for l in lines)


def test_apply_removes_only_the_extra_key(monkeypatch, tmp_path):
    removed = []
    entries = [_e(0, "tok-A-xxxxxxxxxx", primary=True, acc="777"),
               _e(1, "tok-B-yyyyyyyyyy", acc="777"),
               _e(2, "tok-C-zzzzzzzzzz", acc="999")]

    async def probe(e):
        acc = "999" if e.secret.startswith("tok-C") else "777"
        return {"alive": True, "account_id": acc, "login": "goku"}

    monkeypatch.setenv("RIPSTER_BASE_DIR", str(tmp_path))
    monkeypatch.setitem(dd.SERVICES, "fake", lambda cfg: (
        lambda: [dd.Entry(**vars(x)) for x in entries], probe,
        _rank, lambda e: removed.append(e.secret), "login"))
    monkeypatch.setitem(dd.RETIRE_KIND, "fake", "fake_kind")

    lines = asyncio.run(dd.apply({}, "fake", confirm=True))
    assert removed == ["tok-B-yyyyyyyyyy"]
    assert any("удалён" in l for l in lines)


def test_ledger_reason_says_duplicate_not_dead(monkeypatch, tmp_path):
    """Владелец не должен решить, что рабочая учётка умерла."""
    from ripster import retired_credentials as retired
    monkeypatch.setenv("RIPSTER_BASE_DIR", str(tmp_path))
    entries = [_e(0, "tok-A-xxxxxxxxxx", primary=True, acc="777"),
               _e(1, "tok-B-yyyyyyyyyy", acc="777")]

    async def probe(e):
        return {"alive": True, "account_id": "777", "login": "goku"}

    monkeypatch.setitem(dd.SERVICES, "fake", lambda cfg: (
        lambda: [dd.Entry(**vars(x)) for x in entries], probe, _rank,
        lambda e: None, "login"))
    monkeypatch.setitem(dd.RETIRE_KIND, "fake", "fake_kind")

    asyncio.run(dd.apply({}, "fake", confirm=True))
    rec = [r for r in retired.listing() if r.get("kind") == "fake_kind"]
    assert rec, "запись в реестр не попала"
    assert "дубль" in rec[0]["reason"]
    assert "отверг" not in rec[0]["reason"]


@pytest.mark.parametrize("service", ["deezer", "qobuz", "soundcloud"])
def test_every_supported_service_is_wired(service):
    assert service in dd.SERVICES and service in dd.RETIRE_KIND
