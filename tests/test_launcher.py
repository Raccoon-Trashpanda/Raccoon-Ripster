"""Source launcher pure helpers: port/URL resolution and bootstrap-interpreter
pick. The runtime parts (start_server / open_window / waits) are integration
(subprocess + system webview) and not unit-tested here."""
import sys

from ripster import launcher

import pytest


@pytest.fixture(autouse=True)
def _log_stays_in_tmp(tmp_path, monkeypatch):
    """Тесты не пишут в боевой logs/launcher.log: 542 записи отсюда
    («webview halted; None in sys.modules» — ровно то, что подставляет
    monkeypatch ниже) завалили лог и на фоне них не видно настоящих ошибок
    лаунчера (docs/BOOT_AUTOSTART_2026-09-24.md). Путь теперь читается через
    launcher_log_path(), которому можно указать tmp и переменной."""
    monkeypatch.setattr(launcher, "BASE_DIR", tmp_path)
    monkeypatch.delenv("RIPSTER_LAUNCHER_LOG", raising=False)


def test_launcher_log_path_default(tmp_path, monkeypatch):
    monkeypatch.setattr(launcher, "BASE_DIR", tmp_path)
    assert launcher.launcher_log_path() == tmp_path / "logs" / "launcher.log"


def test_launcher_log_path_env_override(tmp_path, monkeypatch):
    # Ровно этот механизм держит тестовый прогон вне боевого лога.
    target = tmp_path / "elsewhere" / "launch.log"
    monkeypatch.setenv("RIPSTER_LAUNCHER_LOG", str(target))
    assert launcher.launcher_log_path() == target
    launcher._log("hello")
    assert target.exists() and "hello" in target.read_text(encoding="utf-8")


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
