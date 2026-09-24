"""Чистая логика rp-релея (трекер #37, relay-транспорт панели).

Сервер перекладывает {rp:1,…} между панелью и главной страницей. Здесь
доказывается главное: панель командует ОДНИМ хостом (не всеми вкладками
сразу), хост вещает всем панелям, а при разрыве каждая сторона честится
другой стороне (bye / host-gone), а не крутит видимость живого моста.
"""
import pytest

from ripster.rp_relay import RpRelay


class FakeWs:
    """Адресат в паре (ws, payload) сравнивается по объекту — достаточно ведра."""
    def __repr__(self):
        return f"<ws {id(self) % 1000:03d}>"


def rp(**msg):
    return {"rp": 1, **msg}


@pytest.fixture
def relay():
    return RpRelay()


# ── регистрация ролей ────────────────────────────────────────────────────────
def test_register_only_known_roles(relay):
    ws = FakeWs()
    assert relay.register(ws, "host") is True
    assert relay.role(ws) == "host"
    assert relay.register(FakeWs(), "guest") is False
    assert relay.register(FakeWs(), None) is False


def test_reregistration_moves_client_to_the_back_of_the_queue(relay):
    """Переглота — новый хост: он и становится адресатом панелей."""
    h1, h2 = FakeWs(), FakeWs()
    relay.register(h1, "host")
    relay.register(h2, "host")
    relay.register(h1, "host")           # h1 перезагрузился — он свежай
    panel = FakeWs()
    relay.register(panel, "panel")
    targets = [t for t, _ in relay.route(panel, rp(k="hello"))]
    assert targets == [h1]


# ── маршрутизация ─────────────────────────────────────────────────────────────
def test_panel_message_goes_to_latest_host_only(relay):
    """Два хоста = две вкладки одного Ripster. «Дальше» от панели не должно
    перематывать трек дважды — строго самый свежий хост."""
    h1, h2 = FakeWs(), FakeWs()
    relay.register(h1, "host")
    relay.register(h2, "host")
    p1, p2 = FakeWs(), FakeWs()
    relay.register(p1, "panel")
    relay.register(p2, "panel")
    out = relay.route(p1, rp(k="cmd", cmd="next"))
    assert out == [(h2, {"type": "rp", "msg": rp(k="cmd", cmd="next")})]


def test_host_broadcasts_state_to_all_panels(relay):
    h = FakeWs()
    relay.register(h, "host")
    p1, p2 = FakeWs(), FakeWs()
    relay.register(p1, "panel")
    relay.register(p2, "panel")
    msg = rp(k="state", seq=7)
    out = relay.route(h, msg)
    assert [t for t, _ in out] == [p1, p2]
    assert all(p == {"type": "rp", "msg": msg} for _, p in out)


def test_unregistered_and_junk_are_silent(relay):
    stranger = FakeWs()
    assert relay.route(stranger, rp(k="state")) == []
    h = FakeWs()
    relay.register(h, "host")
    assert relay.route(h, {"k": "state"}) == []          # нет метки rp:1
    assert relay.route(h, None) == []
    assert relay.route(h, "text") == []
    assert relay.route(h, rp(k="state")) == []           # панелей нет — слать некому


def test_no_host_yet_panel_message_is_dropped(relay):
    """Хоста нет (главная страница закрыта) — сообщение некуда доставить;
    релею не на кого ругаться, и тишина здесь честнее ошибки."""
    p = FakeWs()
    relay.register(p, "panel")
    assert relay.route(p, rp(k="hello")) == []


# ── разрывы ───────────────────────────────────────────────────────────────────
def test_panel_death_says_bye_to_hosts(relay):
    h1, h2 = FakeWs(), FakeWs()
    relay.register(h1, "host")
    relay.register(h2, "host")
    p = FakeWs()
    relay.register(p, "panel")
    out = relay.drop(p)
    assert {(t,) for t, _ in out} == {(h1,), (h2,)}
    for _, payload in out:
        assert payload == {"type": "rp", "msg": rp(k="bye")}
    assert relay.role(p) is None


def test_host_death_says_host_gone_to_panels(relay):
    """Панель не должна притворяться мостом: 'host-gone' включает ей честный
    статус и повторные рукопожатия (panel.js rpHandshakeRetry)."""
    h = FakeWs()
    relay.register(h, "host")
    p = FakeWs()
    relay.register(p, "panel")
    out = relay.drop(h)
    assert out == [(p, {"type": "rp", "msg": rp(k="host-gone")})]


def test_drop_of_unrelated_client_is_silent(relay):
    h = FakeWs()
    relay.register(h, "host")
    stranger = FakeWs()
    assert relay.drop(stranger) == []
    assert relay.drop(stranger) == []
    assert relay.role(h) == "host"


def test_drop_twice_does_not_renotify(relay):
    """app.py зовёт drop в finally; повторного bye быть не может: роль снята."""
    h, p = FakeWs(), FakeWs()
    relay.register(h, "host")
    relay.register(p, "panel")
    assert relay.drop(p) != []
    assert relay.drop(p) == []
