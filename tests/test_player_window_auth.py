"""Пропуск наружу для внешнего окна плеера (ripster.launch_token +
POST /api/player-window/claim) — security-тесты.

Что здесь доказывается и почему это не «тест ради теста»:
  • окно панели — отдельный процесс WebView2 без хозяйской куки, и до 24.09
    его /ws отвергался (403) — внешний плеер был мёртв, хотя вкладки,
    поделившие куку, всё показывали;
  • лечение — разовый пропуск. Значит он обязан быть ОДНОРАЗОВЫМ, КОРОТКИМ,
    НЕПЕРЕДАВАЕМЫМ наружу и НИКОГДА не печататься;
  • и главное: кука, полученная по пропуску, должна давать РОВНО тот же
    допуск, что и обычный вход (тогда /ws принимает окно), а без неё — нет.
"""
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi import FastAPI                                # noqa: E402
from fastapi.testclient import TestClient                  # noqa: E402

from ripster import auth as _auth                          # noqa: E402
from ripster import launch_token as lt                     # noqa: E402
from ripster import player_window as pw                    # noqa: E402
from ripster.routes import player_window as R              # noqa: E402

LOOPBACK = ("127.0.0.1", 54321)
REMOTE = ("203.0.113.9", 54321)          # TEST-NET-3, заведомо не наш хост


@pytest.fixture(autouse=True)
def _clean_tokens():
    lt.reset()
    yield
    lt.reset()


# ── чистая механика токена ───────────────────────────────────────────────────
def test_mint_is_random_hex_of_the_expected_shape():
    a, b = lt.mint(), lt.mint()
    assert a != b
    assert len(a) == 64 and int(a, 16) >= 0
    assert lt.pending() == 2


def test_token_is_single_use_even_across_races():
    tok = lt.mint()
    assert lt.consume(tok, "127.0.0.1") is True
    assert lt.consume(tok, "127.0.0.1") is False
    assert lt.consume(tok, "127.0.0.1") is False       # и третья попытка


def test_token_expires():
    tok = lt.mint(ttl=0)
    time.sleep(0.01)
    assert lt.consume(tok, "127.0.0.1") is False


def test_unknown_or_malformed_token_is_refused():
    assert lt.consume("", "127.0.0.1") is False
    assert lt.consume(None, "127.0.0.1") is False
    assert lt.consume("deadbeef" * 8, "127.0.0.1") is False
    assert lt.consume("x" * 4096, "127.0.0.1") is False   # не хэшим помойку


@pytest.mark.parametrize("host", ["203.0.113.9", "testclient", "", None])
def test_non_loopback_never_consumes(host):
    """Токен, утёкший через туннель, чужой машине не обменять. Отказ ещё и
    не сжигает токен: проверка адреса стоит до списания."""
    tok = lt.mint()
    assert lt.consume(tok, host) is False
    assert lt.consume(tok, "127.0.0.1") is True


@pytest.mark.parametrize("host", ["127.0.0.1", "::1", "::ffff:127.0.0.5", "localhost"])
def test_loopback_shapes(host):
    assert lt.is_loopback(host) is True


def test_pending_is_bounded():
    for _ in range(lt._MAX_PENDING + 12):
        lt.mint()
    assert lt.pending() <= lt._MAX_PENDING


# ── HTTP-слой: заявка пропуска = хозяйская кука ──────────────────────────────
PASSWORD = "s3cr3t-owner-pw"


def _app(tmp_path, relay=None):
    """Настоящий auth + роуты окна: config с паролем, сохранение — в память."""
    cfg = {"app-password-hash": _auth._hash_password(PASSWORD)}
    app = FastAPI()
    _auth.install(app, cfg, lambda c: None)
    from types import SimpleNamespace
    R.install(app, SimpleNamespace(base_dir=tmp_path))
    R.set_relay(relay)
    return app, cfg


def _login(cli):
    r = cli.post("/api/login", json={"password": PASSWORD})
    assert r.status_code == 200
    return r.cookies.get(_auth.SESSION_COOKIE)


def _open_token(cli, cookie):
    r = cli.post("/api/player-window/open", json={"launcher": False},
                 headers={"Cookie": f"{_auth.SESSION_COOKIE}={cookie}"})
    assert r.status_code == 200, r.text
    return r.json()


def test_open_without_cookie_is_refused(tmp_path):
    app, _cfg = _app(tmp_path)
    cli = TestClient(app, client=LOOPBACK)
    assert cli.post("/api/player-window/open", json={}).status_code == 401


def test_claim_roundtrip_gives_a_working_owner_cookie(tmp_path, monkeypatch):
    app, _cfg = _app(tmp_path)
    cli = TestClient(app, client=LOOPBACK)
    cookie = _login(cli)
    held = {}
    monkeypatch.setattr(pw, "open_external",
                        lambda b, u, ask=True, token="": held.update(t=token, u=u)
                        or {"ok": True, "how": "focus"})
    _open_token(cli, cookie)
    tok = held["t"]
    assert tok and len(tok) == 64
    # пропуск едет окну во фрагменте адреса, а не в query (не светится в логах)
    assert held["u"].endswith(pw.LAUNCH_PREFIX + tok)

    r = cli.post("/api/player-window/claim", json={"token": tok})
    assert r.status_code == 200, r.text
    assert r.json() == {"ok": True, "owner": True}
    window_cookie = r.cookies.get(_auth.SESSION_COOKIE)
    assert window_cookie and _auth.verify_session_cookie(window_cookie)
    setc = r.headers["set-cookie"]
    assert "HttpOnly" in setc and "SameSite=strict" in setc
    # сам токен в ответе не светится
    assert tok not in r.text

    # …и эта кука — полноценный хозяйский вход в закрытые ручки
    ok = cli.get("/api/player-window/relay-status",
                 headers={"Cookie": f"{_auth.SESSION_COOKIE}={window_cookie}"})
    assert ok.status_code == 200
    # …а без всякой куки те же ручки закрыты (TestClient хранит куки сам,
    # поэтому спрашиваем чистым клиентом)
    assert TestClient(app, client=LOOPBACK).post(
        "/api/player-window/open", json={}).status_code == 401


def test_claim_reuse_is_401(tmp_path, monkeypatch):
    app, _cfg = _app(tmp_path)
    cli = TestClient(app, client=LOOPBACK)
    cookie = _login(cli)
    held = {}
    monkeypatch.setattr(pw, "open_external",
                        lambda b, u, ask=True, token="": held.update(t=token)
                        or {"ok": True, "how": "focus"})
    _open_token(cli, cookie)
    tok = held["t"]
    assert cli.post("/api/player-window/claim", json={"token": tok}).status_code == 200
    again = cli.post("/api/player-window/claim", json={"token": tok})
    assert again.status_code == 401


def test_claim_expired_is_401(tmp_path):
    app, _cfg = _app(tmp_path)
    cli = TestClient(app, client=LOOPBACK)
    tok = lt.mint(ttl=0)
    time.sleep(0.01)
    assert cli.post("/api/player-window/claim", json={"token": tok}).status_code == 401


def test_claim_from_remote_address_is_403(tmp_path):
    """Дальше, чем «неверно»: запрос с чужого адреса — это не окно, это
    кто-то нашёл токен. 403, и токен остаётся жив для законного предъявления."""
    app, _cfg = _app(tmp_path)
    loop = TestClient(app, client=LOOPBACK)
    remote = TestClient(app, client=REMOTE)
    tok = lt.mint()
    r = remote.post("/api/player-window/claim", json={"token": tok})
    assert r.status_code == 403
    assert loop.post("/api/player-window/claim", json={"token": tok}).status_code == 200


@pytest.mark.parametrize("body", [{}, {"token": ""}, {"token": None},
                                  {"token": "zz" * 32}, "не объект", {"other": 1}],
                         ids=["empty", "blank", "none", "wrong-shape", "garbage", "no-key"])
def test_claim_garbage_is_401(tmp_path, body):
    app, _cfg = _app(tmp_path)
    cli = TestClient(app, client=LOOPBACK)
    r = cli.post("/api/player-window/claim", json=body)
    assert r.status_code == 401


def test_claim_never_needs_a_session(tmp_path):
    """Путь публичный: окно идёт за кукой БЕЗ куки. Проверка — на 401, а не
    на 302/403 от прослойки."""
    app, _cfg = _app(tmp_path)
    cli = TestClient(app, client=LOOPBACK)
    assert cli.post("/api/player-window/claim", json={"token": "0" * 64}).status_code == 401


# ── WebSocket: ровно то, из-за чего всё было ────────────────────────────────
class _WS:
    """Минимум, который читает ws_allowed: куки."""
    def __init__(self, cookies):
        self.cookies = cookies


def test_ws_accepts_window_cookie_and_rejects_without_it(tmp_path, monkeypatch):
    """Ровно то место, где внешний плеер и умирал: без куки /ws отвергался, и
    вкладка-свидетель этого не замечала."""
    app, _cfg = _app(tmp_path)
    cli = TestClient(app, client=LOOPBACK)
    cookie = _login(cli)
    held = {}
    monkeypatch.setattr(pw, "open_external",
                        lambda b, u, ask=True, token="": held.update(t=token)
                        or {"ok": True, "how": "standalone"})
    _open_token(cli, cookie)
    r = cli.post("/api/player-window/claim", json={"token": held["t"]})
    assert r.status_code == 200
    window_cookie = r.cookies.get(_auth.SESSION_COOKIE)
    assert _auth.ws_allowed(_WS({})) is False                     # окно без куки — смерть
    assert _auth.ws_allowed(_WS({_auth.SESSION_COOKIE: window_cookie})) is True
    assert _auth.ws_allowed(_WS({_auth.SESSION_COOKIE: "1." + "0" * 64})) is False


def test_relay_status_is_owner_only(tmp_path):
    class Relay:
        def status(self):
            return {"hosts": 1, "panels": 1, "host_ids": ["h1"], "panel_ids": ["p1"],
                    "active_host": "h1", "playing": True,
                    "last_panel_ack": {"panel": "p1", "title": "Тест", "age_s": 1.0}}
    app, _cfg = _app(tmp_path, relay=Relay())
    cli = TestClient(app, client=LOOPBACK)
    assert cli.get("/api/player-window/relay-status").status_code == 401
    cookie = _login(cli)
    r = cli.get("/api/player-window/relay-status",
                headers={"Cookie": f"{_auth.SESSION_COOKIE}={cookie}"})
    assert r.status_code == 200
    body = r.json()
    assert body["hosts"] == 1 and body["panels"] == 1
    assert body["active_host"] == "h1" and body["last_panel_ack"]["title"] == "Тест"


def test_relay_status_without_relay_is_honest(tmp_path):
    """Неподключённый релей — честные нули и relay: false, а не «всё хорошо»."""
    app, _cfg = _app(tmp_path, relay=None)
    cli = TestClient(app, client=LOOPBACK)
    cookie = _login(cli)
    body = cli.get("/api/player-window/relay-status",
                   headers={"Cookie": f"{_auth.SESSION_COOKIE}={cookie}"}).json()
    assert body["relay"] is False and body["hosts"] == 0 and body["panels"] == 0


# ── «кука окна села»: окно просит допуск, сервер отвечает ────────────────────
def test_reauth_flag_becomes_a_fresh_launch_token(tmp_path, monkeypatch):
    """Флаг reauth поднимает панель (у окна нет права самой себе выдать
    вход), сторож снимает флаг и кладёт окно новый пропуск в launch-флаг."""
    from types import SimpleNamespace
    R._ctx = SimpleNamespace(base_dir=tmp_path)
    p = pw.win_paths(tmp_path)
    p["reauth"].parent.mkdir(parents=True, exist_ok=True)
    p["reauth"].write_text("1", encoding="utf-8")
    monkeypatch.setattr(pw, "running_pid", lambda paths: 4242)
    import asyncio
    assert asyncio.run(_once()) is True
    assert not p["reauth"].exists()
    tok = pw._read_launch_flag(p["launch"])
    assert len(tok) == 64 and lt.consume(tok, "127.0.0.1") is True


async def _once():
    return await R._reauth_once()


def test_reauth_of_a_dead_window_is_dropped_not_answered(tmp_path, monkeypatch):
    """Окна нет — токенов не раздаём, флаг не живёт вечно."""
    from types import SimpleNamespace
    R._ctx = SimpleNamespace(base_dir=tmp_path)
    p = pw.win_paths(tmp_path)
    p["reauth"].parent.mkdir(parents=True, exist_ok=True)
    p["reauth"].write_text("1", encoding="utf-8")
    monkeypatch.setattr(pw, "running_pid", lambda paths: None)
    import asyncio
    assert asyncio.run(_once()) is False
    assert not p["reauth"].exists()
    assert not p["launch"].exists()


def test_watcher_is_wired_into_the_app_lifespan():
    """Модуль может быть безупречен и не вызван ниоткуда (так было со
    session_feedback в станциях). Здесь единственный провод — lifespan app.py:
    со СВОИМ lifespan-контекстом on_startup-хуки FastAPI не звучат вовсе."""
    src = (Path(__file__).resolve().parent.parent / "app.py").read_text(encoding="utf-8")
    body = src[src.index("async def lifespan("):]
    body = body[:body.index("\n    yield")]
    assert "start_reauth_watcher" in body, "сторож reauth не запущен из lifespan"
