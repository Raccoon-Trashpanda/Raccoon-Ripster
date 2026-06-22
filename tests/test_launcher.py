"""Source launcher pure helpers: port/URL resolution and bootstrap-interpreter
pick. The runtime parts (start_server / open_window / waits) are integration
(subprocess + system webview) and not unit-tested here."""
import sys

from ripster import launcher


def test_config_port_default(tmp_path):
    assert launcher.config_port(tmp_path) == 7799     # no config.yaml → default


def test_config_port_from_yaml(tmp_path):
    (tmp_path / "config.yaml").write_text("port: 8080\nengine: tidal\n", encoding="utf-8")
    assert launcher.config_port(tmp_path) == 8080


def test_server_url(tmp_path):
    assert launcher.server_url(tmp_path) == "http://127.0.0.1:7799"
    (tmp_path / "config.yaml").write_text("port: 9001\n", encoding="utf-8")
    assert launcher.server_url(tmp_path) == "http://127.0.0.1:9001"


def test_bootstrap_python_prefers_venv(tmp_path):
    venv = tmp_path / ".venv" / "Scripts"
    venv.mkdir(parents=True)
    exe = venv / "python.exe"
    exe.write_text("", encoding="utf-8")
    assert launcher.bootstrap_python(tmp_path) == str(exe)


def test_bootstrap_python_fallback(tmp_path):
    # no .venv → current interpreter (portable install)
    assert launcher.bootstrap_python(tmp_path) == sys.executable


# ── open_window: own native window vs browser fallback ───────────────────────
def test_open_window_uses_webview(monkeypatch):
    import types
    calls = {}
    fake = types.ModuleType("webview")
    fake.create_window = lambda *a, **k: calls.setdefault("created", a)
    fake.start = lambda *a, **k: calls.setdefault("started", True)
    monkeypatch.setitem(sys.modules, "webview", fake)
    assert launcher.open_window("http://x", "T") == "webview"
    assert calls["created"] == ("T", "http://x")   # create_window(title, url, ...)
    assert calls.get("started") is True


def test_open_window_falls_back_to_browser(monkeypatch):
    # force `import webview` to fail → browser path
    monkeypatch.setitem(sys.modules, "webview", None)
    opened = {}
    monkeypatch.setattr(launcher.webbrowser, "open", lambda u: opened.setdefault("url", u))
    assert launcher.open_window("http://y") == "browser"
    assert opened["url"] == "http://y"
