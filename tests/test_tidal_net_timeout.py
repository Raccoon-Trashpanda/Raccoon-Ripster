"""Tidal: прогон не должен висеть вечно на молча брошенном соединении.

Замер 01.10.2026. Владелец вставил ссылку Tidal, учётка живая (API отвечает за
1.8 с, сегменты аудио-CDN отдаются за 3 с) — а прогон встал и был снят сторожем
как зависший, альбом ушёл в «ошибку» с нулём файлов. Воспроизвели изолированно
(копия настроек, копия хранилища сессий): 1-й трек дошёл до 61 сегмента из 103
и замер; с 45-й секунды до 420-й — ноль байт ввода-вывода, ноль строк вывода,
два ESTABLISHED-сокета к аудио-CDN, ноль процессорного времени.

Причина: вендоренный OrpheusDL зовёт requests БЕЗ `timeout` (см.
`orpheus/utils/utils.py:download_file`, `modules/tidal/tidal_api.py:_get`), а
DASH-трек — это сотни сегментов; одного замершего соединения хватает, чтобы
`ThreadPoolExecutor.map` не вернулся никогда. `socket.setdefaulttimeout()` на
этом стеке НЕ спасает (проверено: чтение всё равно висит) — таймаут должен
прийти в сам вызов requests. Отсюда guard в бутстрапе прогона.

Тесты ниже проверяют ПРОВОД, а не только строку: без guard локально молчащий
сервер держит процесс до капа, с guard чтение прерывается за отведённое.
"""

import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from ripster.engines import orpheus_beatport as bp

GUARD_TIMEOUT = (1.0, 2.0)


def _child_source(url: str, with_guard: bool) -> str:
    guard = bp.orpheus_net_guard(GUARD_TIMEOUT) if with_guard else ""
    return (
        guard +
        "import time, requests\n"
        "t0 = time.monotonic()\n"
        f"try:\n"
        f"    r = requests.get({url!r}, stream=True, verify=False)\n"
        "    got = 0\n"
        "    for ch in r.iter_content(512):\n        got += len(ch)\n"
        "    print('DONE', got, round(time.monotonic() - t0, 2))\n"
        "except Exception as e:\n"
        "    print('RAISED', type(e).__name__, round(time.monotonic() - t0, 2))\n"
    )


@pytest.fixture
def silent_server():
    """HTTP-сервер, который принимает соединение и молчит (как брошенный TUN)."""
    holders = []

    def start(mode):
        srv = socket.socket()
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        srv.bind(("127.0.0.1", 0))
        srv.listen(4)
        srv.settimeout(0.3)
        stop = threading.Event()

        def handle(conn):
            try:
                conn.recv(4096)
                if mode == "body":
                    conn.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: 999999\r\n\r\n")
                    conn.sendall(b"X" * 1024)
                time.sleep(120)
            except OSError:
                pass

        def loop():
            while not stop.is_set():
                try:
                    conn, _ = srv.accept()
                except socket.timeout:
                    continue
                except OSError:                 # сокет закрыт на teardown
                    break
                threading.Thread(target=handle, args=(conn,), daemon=True).start()

        threading.Thread(target=loop, daemon=True).start()
        holders.append((srv, stop))
        return f"http://127.0.0.1:{srv.getsockname()[1]}/track.mp4"

    yield start
    for srv, stop in holders:
        stop.set()
        try:
            srv.close()
        except OSError:
            pass


def _run_child(source: str, cap: float):
    return subprocess.run([sys.executable, "-c", source], capture_output=True,
                          text=True, timeout=cap, encoding="utf-8", errors="replace")


def test_bez_guard_zavisanie_ne_vseknetsya_nikogda(silent_server):
    """Дефект живёт ровно там, где его замерили: молчаливое соединение вешает
    чтение навсегда. Если этот тест позеленеет сам — значит тест обесценился."""
    url = silent_server("headers")
    with pytest.raises(subprocess.TimeoutExpired) as exc:
        _run_child(_child_source(url, with_guard=False), cap=6.0)
    assert "RAISED" not in (exc.value.stdout or b"").decode(errors="replace")


def test_guard_obyryvaet_molchanie_po_taymouta(silent_server):
    url = silent_server("headers")        # сервер не отвечает вовсе
    out = _run_child(_child_source(url, with_guard=True), cap=15.0).stdout
    assert out.startswith("RAISED"), out
    assert float(out.split()[-1]) <= 8.0   # 2 с на чтение + запас на запуск


def test_guard_obyryvaet_obryv_posle_nachala_otdachi(silent_server):
    url = silent_server("body")            # отдал кусок и замолчал — типичный TUN
    out = _run_child(_child_source(url, with_guard=True), cap=15.0).stdout
    assert out.startswith("RAISED"), out


def test_guard_podyavlivaet_timeout_tolko_gde_ego_net():
    """Явный `timeout=` от вызывающего оставляем как был — guard не перекрикивает,
    и молчаливый `timeout=None` тоже лечится (именно так зовёт OrpheusDL)."""
    import types

    calls = []

    class FakeSession:
        def request(self, method, url, *a, **kw):
            calls.append(kw.get("timeout", "нет аргумента"))
            return "ok"

    fake_sessions = types.ModuleType("requests.sessions")
    fake_sessions.Session = FakeSession
    fake_requests = types.ModuleType("requests")
    fake_requests.sessions = fake_sessions

    saved = {name: sys.modules.get(name) for name in ("requests", "requests.sessions")}
    sys.modules["requests"] = fake_requests
    sys.modules["requests.sessions"] = fake_sessions
    try:
        exec(compile(bp.orpheus_net_guard((15.0, 60.0)), "<guard>", "exec"), {})
        FakeSession().request("GET", "https://x/1")            # без таймаута
        FakeSession().request("GET", "https://x/2", timeout=None)
        FakeSession().request("GET", "https://x/3", timeout=3)  # явный — не трогаем
    finally:
        for name, mod in saved.items():
            if mod is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = mod

    assert calls == [(15.0, 60.0), (15.0, 60.0), 3]


def test_provod_tidal_beryot_guard_a_sosed_nyet(monkeypatch, tmp_path):
    """Не «модуль правильный», а провод: build_cmd Tidal передаёт guard, а
    соседние движки Orpheus (Beatport/JioSaavn) получают прежнюю команду."""
    from ripster.engines import tidal as td

    writes = []
    monkeypatch.setattr(td, "is_authenticated", lambda cfg: True)
    monkeypatch.setattr(td, "ensure_run_session", lambda cfg=None: "")
    monkeypatch.setattr(td, "_update_orpheus_settings",
                        lambda *a, **k: writes.append(a))
    monkeypatch.setattr(td, "_config_dir_for", lambda cfg: tmp_path)
    monkeypatch.setattr(td, "_slot_session_path", lambda cfg=None: tmp_path / "loginstorage.bin")
    monkeypatch.setattr(td, "_orpheus_dir", lambda: tmp_path)
    monkeypatch.setattr(td, "_module_path", lambda: tmp_path)
    (tmp_path / "orpheus.py").write_text("", encoding="utf-8")
    (tmp_path / "interface.py").write_text("", encoding="utf-8")

    eng = td.TidalEngine()
    cmd = eng.build_cmd("https://listen.tidal.com/album/125079029", "hi_res", {})
    boot = cmd[cmd.index("-c") + 1]
    assert "_orp_timed_request" in boot, "прогон Tidal остался без таймаута"
    assert f"{td._NET_TIMEOUT[0]}, {td._NET_TIMEOUT[1]}" in boot

    plain = bp.orpheus_cmd(tmp_path, "https://beatport.com/x", str(tmp_path))
    assert "_orp_timed_request" not in plain[plain.index("-c") + 1]
