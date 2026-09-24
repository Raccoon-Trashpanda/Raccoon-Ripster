"""API-ключ wm.wol.moe (`amd-wm-api-key`) — проба, квота, маскировка, вердикты.

С 10.09.2026 публичный wrapper ходит только по HTTP lite и требует
`Authorization: Bearer <key>` (@wm_auth_bot → /newkey). Здесь — то, что обязано
держать этот факт:

* заголовочная форма: ключ уходит заголовком, БЕЗ ключа заголовка нет;
* значение ключа НИ в одном возвращаемом словаре не появляется;
* 401/403 → `api_key`, 429 → `quota` (квота выбрана — это не «сервер упал»,
  долбить повторами нельзя);
* счётчики квоты из /status читаются только целыми числами;
* кэш пробы сбрасывается при появлении/исчезновении ключа («нет ключа» не
  должно прилипнуть к только что вписанному);
* маска для гостей и белый список конфиг-райтера;
* вердикты движка по сигнатурам amd_runner (`AMD_WM_NEED_KEY`/`AMD_WM_QUOTA`)
  — терминальные, не общие «AMD_FATAL» и не «сервер лёг».

Секретов в тестовых данных нет: вместо ключа — заведомо фейковая строка, и
тесты отдельно доказывают, что она не утекает в ответы функций.
"""
import pytest

from ripster import apple_router as ar
from ripster.engines.amd import AMDEngine
from ripster.routes.core import _SECRET_KEYS, _redact_config
from ripster.security import CONFIG_WRITABLE_PREFIXES

FAKE_KEY = "FAKE-not-a-key-0123456789"
CFG = {"amd-instance-url": "wm.example", "amd-instance-secure": True,
       "amd-wm-api-key": FAKE_KEY}


class _Resp:
    def __init__(self, status, body):
        self.status_code = status
        self._body = body

    def json(self):
        if self._body is None:
            raise ValueError("no json")
        return self._body


@pytest.fixture
def serve(monkeypatch):
    """Подмена httpx.get с записью фактических заголовков каждого запроса."""
    calls = []

    def _apply(status=None, body=None, exc=None):
        monkeypatch.setattr(ar, "_pool_env", {})
        monkeypatch.setattr(ar, "_pool_env_ts", 0.0)

        def _get(url, **kw):
            calls.append({"url": url, **kw})
            if exc is not None:
                raise exc
            return _Resp(status, body)

        monkeypatch.setattr(ar.httpx, "get", _get)
        return calls
    return _apply


# ── проба: заголовок и отсутствие утечек ────────────────────────────────────

def test_probe_sends_bearer_header(serve):
    calls = serve(status=200, body={"code": 0, "data": {"ready": True}})
    p = ar.public_wrapper_probe(CFG)
    assert p["state"] == "working"
    assert calls[0]["headers"]["Authorization"] == f"Bearer {FAKE_KEY}"


def test_probe_without_key_sends_no_authorization(serve):
    calls = serve(status=401, body={"code": -1, "msg": "invalid or missing API key"})
    p = ar.public_wrapper_probe({"amd-instance-url": "wm.example"})
    assert p["reason"] == "api_key"
    assert "Authorization" not in (calls[0]["headers"] or {})


def test_probe_never_echoes_the_key(serve):
    serve(status=200, body={"code": 0, "data": {"ready": True, "clientCount": 3}})
    p = ar.public_wrapper_probe(CFG)
    assert FAKE_KEY not in repr(p), "значение ключа уехало в словарь пробы"


# ── квота и 401/403 ─────────────────────────────────────────────────────────

def test_429_is_quota_not_down(serve):
    serve(status=429, body={"code": -1, "msg": "daily quota exceeded"})
    p = ar.public_wrapper_probe(CFG)
    assert p["state"] == "refusing" and p["reason"] == "quota"
    assert p["ready"] is False
    assert p["detail"] == "daily quota exceeded"


def test_quota_readout_integers_only(serve):
    serve(status=200, body={"code": 0, "data": {
        "ready": True, "quotaRemaining": 12, "quotaLimit": 100,
        "used": 88, "token": "нетких-секретов-здесь"}})
    p = ar.public_wrapper_probe(CFG)
    assert p["quota_remaining"] == 12 and p["quota_limit"] == 100
    assert p["quota_used"] == 88
    assert "token" not in p, "проба обязана поднимать счётчики, а не поля протокола"


def test_quota_readout_ignores_non_numbers(serve):
    serve(status=200, body={"code": 0, "data": {
        "ready": True, "quotaRemaining": "all", "quota": {"remaining": 5}}})
    p = ar.public_wrapper_probe(CFG)
    assert p.get("quota_remaining") == 5, "строка не должна затирать вложенное число"


# ── кэш: появление ключа не должно «не замечаться» ──────────────────────────

def test_cache_resets_when_key_appears(serve):
    calls = serve(status=401, body={"code": -1, "msg": "invalid or missing API key"})
    p1 = ar.public_wrapper_probe({"amd-instance-url": "wm.example"})
    assert p1["reason"] == "api_key"
    p2 = ar.public_wrapper_probe(CFG)          # ключ не было → есть: тот же хост
    assert len(calls) == 2, "кэш ответа про ключ не должен прилипать"
    assert p2["reason"] == "api_key"           # сервер всё ещё говорит 401
    assert calls[1]["headers"]["Authorization"] == f"Bearer {FAKE_KEY}"


# ── хозяин/гость: маска и право записи ──────────────────────────────────────

def test_key_is_masked_for_guests():
    out = _redact_config({"amd-wm-api-key": FAKE_KEY, "engine": "amd"})
    assert FAKE_KEY not in str(out)
    assert out["amd-wm-api-key"].startswith("••")


def test_key_in_secrets_and_writer_whitelist():
    assert "amd-wm-api-key" in _SECRET_KEYS
    assert "amd-wm-api-key" in CONFIG_WRITABLE_PREFIXES


# ── вердикты движка по сигнатурам amd_runner ────────────────────────────────

def test_engine_need_key_is_terminal_and_specific():
    eng = AMDEngine.__new__(AMDEngine)
    r = eng.is_finished("WM ERROR: AMD_WM_NEED_KEY: wm.wol.moe требует ключ", rc=1)
    assert not r.success
    assert "@wm_auth_bot" in r.error
    assert "Bento4" not in r.error, "ключевой отказ не должен притворяться Bento4"


def test_engine_quota_names_quota():
    eng = AMDEngine.__new__(AMDEngine)
    r = eng.is_finished("WM ERROR: AMD_WM_QUOTA: квота исчерпана", rc=1)
    assert not r.success
    assert "квота" in r.error.lower()
