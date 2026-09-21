"""Панель SoundCloud описывает ту учётку, которой РЕАЛЬНО едут загрузки.

20.09.2026: первой в конфиге стояла free-учётка, Go+ лежала третьей в пуле —
проба каждые 5 минут писала «Free (128 kbps)», хотя качало на Go+. Панель,
которая врёт про тариф, стоит потом часов разбирательств «почему 128 kbps».
"""
import asyncio
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from ripster.routes import auth as A  # noqa: E402

CFG = {
    "soundcloud-oauth-token": "free-primary",
    "soundcloud-accounts": [
        {"token": "free-primary", "label": "goku"},
        {"token": "free-second", "label": "nameless"},
        {"token": "goplus", "label": "Ipa48"},
    ],
}

PLANS = {
    "free-primary": {"alive": True, "go_plus": False, "login": "goku"},
    "free-second": {"alive": True, "go_plus": False, "login": "nameless"},
    "goplus": {"alive": True, "go_plus": True, "login": "Ipa48", "plan": "Go+"},
}


def _patch(monkeypatch):
    from ripster import soundcloud_accounts as sa

    async def _info(token, fresh=False):
        return PLANS[token]

    monkeypatch.setattr(sa, "account_info", _info)
    monkeypatch.setattr(sa, "known", lambda t, max_age=None: PLANS.get(t))


def test_probe_follows_the_downloading_account(monkeypatch):
    _patch(monkeypatch)
    token, who = asyncio.run(A._sc_probe_token(CFG, "free-primary"))
    assert token == "goplus", "проба обязана смотреть Go+, а не первую строку конфига"
    assert who == "Ipa48"


def test_single_account_stays_on_primary(monkeypatch):
    """Без пула описывать нечего, кроме единственной учётки."""
    _patch(monkeypatch)
    cfg = {"soundcloud-oauth-token": "free-primary"}
    token, _ = asyncio.run(A._sc_probe_token(cfg, "free-primary"))
    assert token == "free-primary"


def test_broken_pool_does_not_break_the_probe(monkeypatch):
    """Сломанный выбор учётки не должен ронять пробу — остаёмся на primary."""
    from ripster import soundcloud_pool as scp

    def _boom(cfg):
        raise RuntimeError("пул сломан")

    monkeypatch.setattr(scp, "_configured_accounts", _boom)
    token, who = asyncio.run(A._sc_probe_token(CFG, "free-primary"))
    assert token == "free-primary" and who == ""
