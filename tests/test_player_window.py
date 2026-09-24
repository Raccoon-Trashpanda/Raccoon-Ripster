"""ripster/player_window.py — чистая логика внешнего окна: single-instance,
память геометрии, оркестрация «фокус → лаунчер → standalone», честные причины
отказа. pywebview не импортируем: GUI проверяется живой проверкой, здесь —
только механика, которую можно сломать молча.
"""
import json
import os
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ripster import player_window as pw  # noqa: E402


# ── пути и адрес ─────────────────────────────────────────────────────────────
def test_win_paths_layout(tmp_path):
    p = pw.win_paths(tmp_path)
    assert p["lock"] == tmp_path / "logs" / "player_window.lock"
    assert p["state"] == tmp_path / "logs" / "player_window.json"
    assert p["launcher_req"] == tmp_path / "logs" / "launcher.rpwin"


def test_panel_url_single_transport_relay():
    assert pw.panel_url("http://127.0.0.1:7805/") == \
        "http://127.0.0.1:7805/static/panel/index.html?transport=relay"


# ── PID-механика (single-instance основа) ────────────────────────────────────
def test_pid_alive_self_and_dead():
    assert pw.pid_alive(os.getpid()) is True
    assert pw.pid_alive(2_000_000_000) is False    # заведомо не этот процесс
    assert pw.pid_alive(None) is False
    assert pw.pid_alive(0) is False


def test_read_pid_garbage_is_none(tmp_path):
    f = tmp_path / "x.lock"
    f.write_text("не число", encoding="utf-8")
    assert pw.read_pid(f) is None
    assert pw.read_pid(tmp_path / "нет.lock") is None


def test_running_pid_ignores_stale_lock(tmp_path):
    p = pw.win_paths(tmp_path)
    p["lock"].parent.mkdir(parents=True)
    p["lock"].write_text("2000000000", encoding="utf-8")   # мёртвый владелец
    assert pw.running_pid(p) is None
    p["lock"].write_text(str(os.getpid()), encoding="utf-8")
    assert pw.running_pid(p) == os.getpid()


# ── геометрия: память окна ───────────────────────────────────────────────────
def test_geo_roundtrip_and_bounds(tmp_path):
    p = pw.win_paths(tmp_path)
    assert pw.load_geo(p) == {"width": 420, "height": 800}   # дефолт без файла
    pw.save_geo(p, {"width": 500, "height": 700, "x": 40, "y": 60})
    assert pw.load_geo(p) == {"width": 500, "height": 700, "x": 40, "y": 60}
    # враньё по границам не должно превращать окно в полоску или за экран
    pw.save_geo(p, {"width": 10, "height": 999999, "x": -5000})
    assert pw.load_geo(p) == {"width": 420, "height": 800}
    p["state"].write_text("{ битый json", encoding="utf-8")
    assert pw.load_geo(p) == {"width": 420, "height": 800}


def test_on_top_remembers_across_geo_saves(tmp_path):
    p = pw.win_paths(tmp_path)
    assert pw.load_on_top(p) is False
    pw.save_geo(p, {"width": 430, "height": 810}, on_top=True)
    assert pw.load_on_top(p) is True
    pw.save_geo(p, {"width": 440, "height": 820})           # без on_top — не теряет
    assert pw.load_on_top(p) is True


# ── оркестрация кнопки: фокус → лаунчер → standalone ─────────────────────────
def test_open_external_focuses_live_window(tmp_path, monkeypatch):
    monkeypatch.setattr(pw, "running_pid", lambda p: 4242)
    calls = {}
    monkeypatch.setattr(pw, "touch", lambda path: calls.setdefault("touched", path))
    res = pw.open_external(tmp_path, "http://h/p")
    assert res == {"ok": True, "how": "focus"}
    assert Path(calls["touched"]).name == "player_window.show"


def test_open_external_asks_launcher_first(tmp_path, monkeypatch):
    monkeypatch.setattr(pw, "running_pid", lambda p: None)
    monkeypatch.setattr(pw, "request_launcher",
                        lambda p, url, **k: url == "http://h/p")
    res = pw.open_external(tmp_path, "http://h/p")
    assert res == {"ok": True, "how": "launcher"}


def test_open_external_falls_back_to_standalone(tmp_path, monkeypatch):
    # Старый frozen-exe протокола не знает → request_launcher честен=False,
    # и окно поднимается своим процессом.
    monkeypatch.setattr(pw, "focus_existing", lambda p: False)
    monkeypatch.setattr(pw, "request_launcher", lambda p, url, **k: False)
    monkeypatch.setattr(pw, "spawn_standalone",
                        lambda p, base, url, **k: {"ok": True, "how": "standalone"})
    assert pw.open_external(tmp_path, "http://h/p") == {"ok": True, "how": "standalone"}


def test_open_external_from_browser_tab_never_asks_launcher(tmp_path, monkeypatch):
    # Вкладка браузера (ask_launcher=False): лаунчерово окно управлялось бы
    # главным окном ЛАУНЧЕРА, а не этой вкладкой — просим сразу standalone.
    monkeypatch.setattr(pw, "focus_existing", lambda p: False)
    asked = {"launcher": False}
    monkeypatch.setattr(pw, "request_launcher",
                        lambda p, url, **k: asked.__setitem__("launcher", True) or True)
    monkeypatch.setattr(pw, "spawn_standalone",
                        lambda p, base, url, **k: {"ok": True, "how": "standalone"})
    res = pw.open_external(tmp_path, "http://h/p", ask_launcher=False)
    assert res == {"ok": True, "how": "standalone"}
    assert asked["launcher"] is False


# ── HTTP-слой: флаг вкладами в теле запроса ─────────────────────────────────
def _open_route(monkeypatch, tmp_path):
    """Ставит роут с заглушками auth и open_external; возвращает (клиент, вызовы)."""
    from types import SimpleNamespace
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    import ripster.routes.player_window as R

    calls = []
    monkeypatch.setattr(R._auth, "is_owner_request", lambda req: True)
    monkeypatch.setattr(R._auth, "is_enabled", lambda: True)
    monkeypatch.setattr(pw, "open_external",
                        lambda base, url, ask=True: (
                            calls.append({"base": base, "url": url, "ask": ask}),
                            {"ok": True, "how": "standalone"})[1])
    app = FastAPI()
    R.install(app, SimpleNamespace(base_dir=tmp_path))
    return TestClient(app), calls


@pytest.mark.parametrize("body,ask", [
    ({"launcher": False}, False),        # вкладка браузера — только standalone
    ({"launcher": True}, True),
    ({}, True),                          # старая вкладка без тела не меняет поведения
], ids=["browser-tab", "explicit", "no-flag"])
def test_open_route_passes_launcher_flag(monkeypatch, tmp_path, body, ask):
    cli, calls = _open_route(monkeypatch, tmp_path)
    r = cli.post("/api/player-window/open", json=body)
    assert r.status_code == 200 and r.json()["ok"] is True
    origin = str(r.request.url).rsplit("/api/", 1)[0]   # окно грузит ТОТ ЖЕ сервер
    assert calls == [{"base": tmp_path, "url": origin + pw.PANEL_QUERY, "ask": ask}]


def test_open_route_garbage_body_still_asks_launcher(monkeypatch, tmp_path):
    cli, calls = _open_route(monkeypatch, tmp_path)
    r = cli.post("/api/player-window/open", content="не json".encode("utf-8"),
                 headers={"Content-Type": "application/json"})
    assert r.status_code == 200
    assert calls[0]["ask"] is True


def test_open_route_rejects_non_owner(monkeypatch, tmp_path):
    from types import SimpleNamespace
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    import ripster.routes.player_window as R

    monkeypatch.setattr(R._auth, "is_owner_request", lambda req: False)
    monkeypatch.setattr(R._auth, "is_enabled", lambda: True)
    app = FastAPI()
    R.install(app, SimpleNamespace(base_dir=tmp_path))
    assert TestClient(app).post("/api/player-window/open", json={}).status_code == 403


def test_request_launcher_dead_launcher_says_no(tmp_path):
    p = pw.win_paths(tmp_path)
    assert pw.request_launcher(p, "http://h", timeout=0.2) is False   # lock нет


def test_request_launcher_live_but_no_ack_times_out(tmp_path, monkeypatch):
    p = pw.win_paths(tmp_path)
    p["launcher_lock"].parent.mkdir(parents=True)
    p["launcher_lock"].write_text("2000000000", encoding="utf-8")
    monkeypatch.setattr(pw, "pid_alive", lambda pid: pid == 2000000000)
    t0 = time.monotonic()
    assert pw.request_launcher(p, "http://h", timeout=0.3, poll=0.05) is False
    assert time.monotonic() - t0 >= 0.3 - 0.05
    # запрос за забеспокоившийся лаунчер не должен остаться навечно
    assert not p["launcher_req"].exists()


def test_request_launcher_ack_roundtrip(tmp_path, monkeypatch):
    p = pw.win_paths(tmp_path)
    monkeypatch.setattr(pw, "pid_alive", lambda pid: True)

    def fake_pony():
        # «лаунчер» съел запрос и ответил — как новый launcher_exe
        time.sleep(0.05)
        req = p["launcher_req"]
        if req.exists():
            req.unlink()
            p["launcher_ack"].write_text("window", encoding="utf-8")
    import threading
    threading.Thread(target=fake_pony, daemon=True).start()
    assert pw.request_launcher(p, "http://h", timeout=1.5, poll=0.05) is True
    assert not p["launcher_ack"].exists()          # ack забран, не гниёт в logs/


# ── spawn: честные причины отказа ────────────────────────────────────────────
class ProcStub:
    def __init__(self, rc=0):
        self._rc = rc
    def poll(self):
        return self._rc


def test_spawn_standalone_dying_process_says_pywebview(tmp_path, monkeypatch):
    p = pw.win_paths(tmp_path)
    monkeypatch.setattr(pw, "spawn_interpreter", lambda b: "python.exe")
    class FakePopen:
        def __init__(self, *a, **k):
            for f in (k.get("stdout"), k.get("stderr")):
                try: f.close()
                except Exception: pass
        def poll(self): return 3
    monkeypatch.setattr(pw.subprocess, "Popen", FakePopen)
    res = pw.spawn_standalone(p, tmp_path, "http://h/p", wait_s=0.3, poll=0.05)
    assert res["ok"] is False and res["reason"] == "pywebview"


def test_spawn_standalone_popen_error_says_spawn(tmp_path, monkeypatch):
    p = pw.win_paths(tmp_path)
    monkeypatch.setattr(pw, "spawn_interpreter", lambda b: "python.exe")
    def boom(*a, **k): raise OSError("отказано в доступе")
    monkeypatch.setattr(pw.subprocess, "Popen", boom)
    res = pw.spawn_standalone(p, tmp_path, "http://h/p", wait_s=0.2)
    assert res["ok"] is False and res["reason"] == "spawn"


def test_spawn_standalone_registers_lock(tmp_path, monkeypatch):
    p = pw.win_paths(tmp_path)
    class OkProc:
        def poll(self): return None
    def fake_popen(args, **k):
        p["lock"].parent.mkdir(parents=True, exist_ok=True)
        p["lock"].write_text("777", encoding="utf-8")       # «окно зарегистрировалось»
        return OkProc()
    monkeypatch.setattr(pw, "spawn_interpreter", lambda b: "python.exe")
    monkeypatch.setattr(pw, "running_pid", lambda pp: 777 if pp["lock"].exists() else None)
    monkeypatch.setattr(pw.subprocess, "Popen", fake_popen)
    res = pw.spawn_standalone(p, tmp_path, "http://h/p", wait_s=1.0, poll=0.05)
    assert res == {"ok": True, "how": "standalone"}


def test_close_external_only_when_window_lives(tmp_path, monkeypatch):
    p = pw.win_paths(tmp_path)
    monkeypatch.setattr(pw, "running_pid", lambda pp: None)
    assert pw.close_external(tmp_path)["ok"] is False
    monkeypatch.setattr(pw, "running_pid", lambda pp: 5)
    assert pw.close_external(tmp_path) == {"ok": True}
    assert p["close"].exists()


# ── само окно: single-instance без GUI ───────────────────────────────────────
def test_main_second_instance_only_focuses(tmp_path, monkeypatch):
    monkeypatch.setattr(pw, "running_pid", lambda p: os.getpid())
    focus = {}
    monkeypatch.setattr(pw, "focus_existing", lambda p: focus.setdefault("f", True) or True)
    assert pw.main(["--url", "http://h/p", "--base", str(tmp_path)]) == 0
    assert focus.get("f") is True


def test_main_without_pywebview_exits_3(tmp_path, monkeypatch):
    monkeypatch.setattr(pw, "running_pid", lambda p: None)
    monkeypatch.setitem(sys.modules, "webview", None)      # ImportError, как без пакета
    assert pw.main(["--url", "http://h/p", "--base", str(tmp_path)]) == 3
    # lock от несостоявшегося окна не должен остаться
    assert not (tmp_path / "logs" / "player_window.lock").exists()


def test_window_api_pin_persists(tmp_path):
    p = pw.win_paths(tmp_path)
    class Win: on_top = False
    win = Win()
    api = pw.WindowApi(p, lambda: win)
    assert api.pin(True) == "ok"
    assert win.on_top is True and pw.load_on_top(p) is True
    assert api.pin(False) == "ok" and pw.load_on_top(p) is False


def test_watch_flags_show_and_close(tmp_path):
    p = pw.win_paths(tmp_path)
    p["show"].parent.mkdir(parents=True)
    class Win:
        def __init__(self): self.calls = []
        def show(self): self.calls.append("show")
        def restore(self): self.calls.append("restore")
        def focus(self): self.calls.append("focus")
        def close(self): self.calls.append("close")
    win = Win()
    import threading
    stop = threading.Event()
    th = threading.Thread(target=pw._watch_flags, args=(p, win, stop), daemon=True)
    th.start()
    pw.touch(p["show"])
    deadline = time.monotonic() + 3
    while "focus" not in win.calls and time.monotonic() < deadline:
        time.sleep(0.05)
    assert win.calls[:3] == ["show", "restore", "focus"]
    assert not p["show"].exists()
    pw.touch(p["close"])
    deadline = time.monotonic() + 3
    while "close" not in win.calls and time.monotonic() < deadline:
        time.sleep(0.05)
    assert stop.is_set()
    th.join(3)
