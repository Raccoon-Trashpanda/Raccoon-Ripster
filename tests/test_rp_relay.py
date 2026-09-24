"""Чистая логика rp-релея (трекер #37, relay-транспорт панели).

Сервер перекладывает {rp:1,…} между панелью и главной страницей. Главное,
что здесь доказывается — ВЫБОРЫ ХОСТА по активности (исправление 24.09.2026,
«внешний плеер живёт своей жизнью»):

  • адресат панели — вкладка, КОТОРАЯ ИГРАЕТ, а не «самая свежая по
    регистрации»: молчаливая вторая вкладка больше не перехватывает мост;
  • заигравший перехватывает, а ушедший на паузу «прилипает» (last-played);
  • неактивный хост не имеет права затопкать состояние играющего — его
    state релей выбрасывает;
  • хост, подключившийся к живому окну, узнаёт о панели (panel-present) и,
    если он активен, начинает вещать сразу, без чужого hello;
  • при смерти активного хоста мост молча передаётся выжившему, а panel
    врёт «host-gone» только когда хостов не осталось вовсе.

Панель командует ОДНИМ хостом (иначе «дальше» перематывает два трека), хост
вещает всем панелям. Разрывы честятся другой стороне (bye / host-gone).
"""
import pytest

from ripster.rp_relay import RpRelay


class FakeWs:
    """Адресат в паре (ws, payload) сравнивается по объекту — достаточно ведра."""
    def __init__(self, n="?"):
        self.n = n
    def __repr__(self):
        return f"<ws {self.n}>"


def rp(**msg):
    return {"rp": 1, **msg}


def hb(relay, ws, playing, ts=1):
    """Хост шлёт heartbeat с состоянием игры — то, по чему релей выбирает активного."""
    return relay.route(ws, rp(k="hb", playing=playing, ts=ts))


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


# ── выборы: играет = адресат, молчун мост не перехватывает ────────────────────
def test_idle_second_tab_does_not_steal_the_bridge(relay):
    """В точности дефект владельца: играет T1, открылась пустая T2. Панель
    обязана остаться на T1. Раньше маршрутизация шла по «самому свежему
    хосту» — и T2 уводила мост, показывая тишину."""
    t1, t2, panel = FakeWs("T1"), FakeWs("T2"), FakeWs("panel")
    relay.register(t1, "host")
    hb(relay, t1, True, ts=10)          # T1 играет
    relay.register(panel, "panel")
    relay.register(t2, "host")               # T2 открылась ПОЗЖЕ и молчит
    targets = [tgt for tgt, _ in relay.route(panel, rp(k="cmd", cmd="toggle"))]
    assert targets == [t1]
    assert relay.elected_host() is t1


def test_playing_host_preempts_and_others_state_is_dropped(relay):
    """Как только молчаливая вкладка заиграла — мост переходит к ней, а
    состояние прежнего (теперь неактивного) хоста не доходит до панели."""
    t1, t2, panel = FakeWs("T1"), FakeWs("T2"), FakeWs("panel")
    relay.register(t1, "host")
    hb(relay, t1, True, ts=10)
    relay.register(t2, "host")
    relay.register(panel, "panel")
    out = hb(relay, t2, True, ts=20)    # T2 начинает играть (позже по ts)
    # sync: t1 деактивируется, t2 активируется
    assert {tgt for tgt, _ in out} == {t1, t2}
    assert relay.elected_host() is t2
    assert relay.route(panel, rp(k="cmd", cmd="next")) == [(t2, {"type": "rp", "msg": rp(k="cmd", cmd="next")})]
    assert relay.route(t1, rp(k="state", seq=1)) == []            # неактивный — тишина
    assert [p for p, _ in relay.route(t2, rp(k="state", seq=2))] == [panel]


def test_paused_last_player_stays_elected(relay):
    """«last played»: ушёл на паузу — всё ещё хозяин окна, пока никто другой
    не заиграл. Иначе мост прыгал бы при каждой паузе."""
    t1, t2, panel = FakeWs("T1"), FakeWs("T2"), FakeWs("panel")
    relay.register(t1, "host")
    hb(relay, t1, True, ts=10)
    relay.register(t2, "host")
    relay.register(panel, "panel")
    hb(relay, t1, False, ts=11)         # T1 на паузе, T2 молчит
    assert relay.elected_host() is t1
    assert [t for t, _ in relay.route(panel, rp(k="hello"))] == [t1]


def test_no_playback_elects_first_registered(relay):
    """Никто не играл — окна кому-то служить надо: берём первую вкладку.
    Порядок регистрации тут не «победитель навечно», а разумный дефолт."""
    a, b, panel = FakeWs("A"), FakeWs("B"), FakeWs("panel")
    relay.register(a, "host")
    relay.register(b, "host")
    relay.register(panel, "panel")
    assert [t for t, _ in relay.route(panel, rp(k="hello"))] == [a]


def test_heartbeat_is_consumed_not_broadcast(relay):
    """hb — служебный сигнал выборов: он НЕ должен улетать на панель."""
    host, panel = FakeWs(), FakeWs()
    relay.register(host, "host")
    relay.register(panel, "panel")
    out = hb(relay, host, True, ts=1)
    assert all(tgt is not panel for tgt, _ in out)


# ── panel-present: хост при живом окне узнаёт о панели сам ───────────────────
def test_on_register_announces_panel_to_new_host(relay):
    """Панель открыта, потом (пере)загрузилась вкладка. Регистрируемся — и
    сервер СРАЗУ шлёт panel-present + host-active, не дожидаясь hello окна."""
    panel = FakeWs("panel")
    relay.register(panel, "panel")
    host = FakeWs("T1")
    relay.register(host, "host")
    out = relay.on_register(host)
    assert {t for t, _ in out} == {host}              # адресат — только что пришедший хост
    msgs = [p["msg"] for _, p in out]
    kinds = {m["k"] for m in msgs}
    assert kinds == {"panel-present", "host-active"}
    active = next(m for m in msgs if m["k"] == "host-active")
    assert active["on"] == 1                          # он единственный хост → активен


def test_on_register_silent_without_panel(relay):
    host = FakeWs()
    relay.register(host, "host")
    assert relay.on_register(host) == []


def test_host_active_synced_only_on_change(relay):
    """host-active — флаг про «ты хозяин окна»: шлётся только при СМЕНЕ статуса,
    а не на каждом heartbeat (иначе вкладки гоняли бы рассылку впустую)."""
    host, panel = FakeWs("H"), FakeWs("P")
    relay.register(host, "host")
    relay.register(panel, "panel")
    relay.on_register(host)                         # помечает host активным
    out = hb(relay, host, True, ts=5)               # статус не меняется
    assert [m for _, m in out] == []                # ни panel-present, ни host-active
    h2 = FakeWs("H2")
    relay.register(h2, "host")
    out2 = hb(relay, h2, True, ts=9)                # h2 заиграла позже — перехват
    flags = {t: m["msg"].get("on") for t, m in out2 if m["msg"]["k"] == "host-active"}
    assert flags == {host: 0, h2: 1}


# ── маршрутизация (общее) ─────────────────────────────────────────────────────
def test_host_broadcasts_state_to_all_panels(relay):
    h = FakeWs()
    relay.register(h, "host")                       # единственный → elected
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
    h1, h2 = FakeWs("h1"), FakeWs("h2")
    relay.register(h1, "host")
    relay.register(h2, "host")
    p = FakeWs()
    relay.register(p, "panel")
    out = relay.drop(p)
    assert {t for t, _ in out} == {h1, h2}
    for _, payload in out:
        assert payload == {"type": "rp", "msg": rp(k="bye")}
    assert relay.role(p) is None


def test_survivor_host_death_rehandso_silently(relay):
    """Умер НЕ активный хост — панель ничего не замечает: у неё есть активный.
    Никакого host-gone и лишнего переподключения."""
    t1, t2, panel = FakeWs("T1"), FakeWs("T2"), FakeWs("P")
    relay.register(t1, "host")
    hb(relay, t1, True, ts=10)                 # T1 активен
    relay.register(t2, "host")
    relay.register(panel, "panel")
    out = relay.drop(t2)                            # умер молчун
    assert out == []                                # панели и активу — тишина
    assert relay.elected_host() is t1


def test_elected_host_death_rehandso_without_host_gone(relay):
    """Умер активный, но есть другой — мост тихо передаём выжившему
    (panel-present + host-active), панели не врём про «host-gone»."""
    t1, t2, panel = FakeWs("T1"), FakeWs("T2"), FakeWs("P")
    relay.register(t1, "host")
    hb(relay, t1, True, ts=10)
    relay.register(t2, "host")
    relay.register(panel, "panel")
    out = relay.drop(t1)                            # умер активный
    tgts = {t for t, _ in out}
    assert panel not in tgts                        # host-gone НЕ слан панели
    assert relay.elected_host() is t2
    assert t2 in tgts                               # выживший получил мост
    ks = {p["msg"]["k"] for _, p in out}
    assert "panel-present" in ks or "host-active" in ks


def test_all_hosts_dead_says_host_gone_to_panels(relay):
    """Хостов не осталось вовсе — панель обязана честно погасить мост
    (panel.js: host-gone → failToast + повторные рукопожатия)."""
    h = FakeWs()
    relay.register(h, "host")
    p = FakeWs()
    relay.register(p, "panel")
    assert relay.drop(h) == [(p, {"type": "rp", "msg": rp(k="host-gone")})]


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


# ── диагностика моста: relay-status (лечение внешнего окна, 24.09) ───────────
def test_panel_ack_is_consumed_and_remembered(relay):
    """«ack» — не команда хосту, а отчёт панели: состояние ДОШЛО и показано.
    Хосту оно не пересылается (иначе на каждое состояние было бы два)."""
    h, p = FakeWs("host"), FakeWs("panel")
    relay.register(h, "host")
    relay.register(p, "panel")
    assert relay.route(p, rp(k="ack", title="Nightcall", artist="Kavinsky",
                             have=True, live=True, nolink=False,
                             keys=3, cmds=1)) == []
    st = relay.status()
    assert st["last_panel_ack"]["title"] == "Nightcall"
    assert st["last_panel_ack"]["panel"] == st["panel_ids"][0]
    assert st["last_panel_ack"]["age_s"] < 2
    # live/nolink — как панель видит свой мост: снаружи этого не измерить;
    # keys/cmds — счётчики панели: клавиша долетела / команда послана
    ack = st["last_panel_ack"]
    assert (ack["live"], ack["nolink"], ack["keys"], ack["cmds"]) == (True, False, 3, 1)


def test_status_counts_hosts_panels_and_elected(relay):
    h1, h2, p = FakeWs("h1"), FakeWs("h2"), FakeWs("p")
    relay.register(h1, "host")
    relay.register(h2, "host")
    relay.register(p, "panel")
    relay.route(h2, rp(k="hb", playing=True, ts=10))
    st = relay.status()
    assert (st["hosts"], st["panels"], st["active_host"], st["playing"]) == \
        (2, 1, st["host_ids"][1], True)


def test_status_ids_are_stable_labels_not_object_reprs(relay):
    """Наружу — 'h1'/'p1': по ним стенд и опознаёт, кто есть кто."""
    h, p = FakeWs(), FakeWs()
    relay.register(h, "host")
    relay.register(p, "panel")
    assert relay.status()["host_ids"] == ["h1"]
    assert relay.status()["panel_ids"] == ["p1"]


def test_ack_dies_with_the_panel(relay):
    """Панель отвалилась — вранья про «последнее подтверждение» не остаётся:
    иначе relay-status вёл бы себя как живой мост."""
    h, p = FakeWs(), FakeWs()
    relay.register(h, "host")
    relay.register(p, "panel")
    relay.route(p, rp(k="ack", title="Nightcall", have=True))
    relay.drop(p)
    assert relay.status()["last_panel_ack"] is None
    assert relay.status()["panels"] == 0


def test_ack_from_a_host_is_not_a_panel_ack(relay):
    """Хост, приславший ack, — не панель: картотека не должна врать о том,
    кто подтверждает показ (роль проверяется до записи)."""
    h = FakeWs()
    relay.register(h, "host")
    relay.route(h, rp(k="ack", title="подделка", have=True))
    assert relay.status()["last_panel_ack"] is None
