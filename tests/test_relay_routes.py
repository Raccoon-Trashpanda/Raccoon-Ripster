# -*- coding: utf-8 -*-
"""Безопасность своего реле: что обязано отвечать `/relay/…`.

Тесты ровно из постановки владельца (24.09): без ключа → 401, отозванный → 401
(неотличимо от «такого нет»), поверх квоты → 429 с Retry-After, учётные данные
Apple наружу не уходят никогда, /status показывает только витрины и готовность.

Мини-приложение: ставим только роутер реле, без сессионного замка — сам замок
тестируется в test_auth*, здесь проверяется то, на что он не влияет: ключевая
авторизация, квоты и формат ответов.
"""
import time

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from ripster import pacing, relay_pool, relay_store
from ripster.routes import relay as relay_routes


class _Ctx:
    def __init__(self, config):
        self.config = config
        self.save_config = lambda cfg: None


@pytest.fixture()
def env(tmp_path, monkeypatch):
    """Живое хранилище ключей в tmp + фиктивные upstream-ответы."""
    monkeypatch.setenv("RIPSTER_BASE_DIR", str(tmp_path))
    relay_store.init(base_dir=tmp_path,
                     secret_provider=lambda: "pepper-" + "s" * 31)
    relay_store._WINDOW.clear()
    relay_store._INFLIGHT.clear()
    cfg = {"relay-enabled": True, "relay-unauth-per-minute": 100}
    relay_routes._unauth_window.clear()
    app = FastAPI()
    relay_routes.install(app, _Ctx(cfg))
    calls = []

    async def fake_call(endpoint, config=None, params=None, attempts=2):
        calls.append((endpoint, dict(params or {})))
        return {"endpoint": endpoint, "contentKey": "aabbccdd" * 4,
                "adamId": str((params or {}).get("adamId") or "")}

    monkeypatch.setattr(relay_pool, "call", fake_call)
    client = TestClient(app)
    yield client, cfg, relay_store, calls
    relay_store.close()
    relay_store._WINDOW.clear()
    relay_store._INFLIGHT.clear()
    relay_routes._unauth_window.clear()


def _key(store, **quota):
    plain, ref = store.issue(label="t", owner="owner", quota=quota or None)
    return plain


def _bearer(k):
    return {"Authorization": f"Bearer {k}"}


# ── 401 ──────────────────────────────────────────────────────────────────────

def test_no_key_is_401(env):
    client, cfg, store, calls = env
    r = client.get("/relay/key", params={"adamId": "1", "uri": "skd://x"})
    assert r.status_code == 401
    assert r.json()["code"] == 401
    assert not calls, "отказ не должен доходить до враппера"


def test_garbage_key_is_401(env):
    client, cfg, store, calls = env
    r = client.get("/relay/m3u8", params={"adamId": "1"},
                   headers=_bearer("rk_ne_nash_klyuch_0000000000"))
    assert r.status_code == 401


def test_revoked_key_answer_is_indistinguishable_from_unknown(env):
    """«Отозван» и «такого не выпускали» — один ответ слово в слово: иначе
    админка утекает под перебор (нашёл живой ref — узнал про отзыв)."""
    client, cfg, store, calls = env
    plain = _key(store)
    store.revoke(relay_store.key_ref(plain))
    r_revoked = client.get("/relay/m3u8", params={"adamId": "1"},
                           headers=_bearer(plain))
    r_unknown = client.get("/relay/m3u8", params={"adamId": "1"},
                           headers=_bearer("rk_nochnoy-vor"))
    assert r_revoked.status_code == r_unknown.status_code == 401
    assert r_revoked.json() == r_unknown.json()


def test_relay_disabled_answers_503_to_everything(env):
    client, cfg, store, calls = env
    cfg["relay-enabled"] = False
    plain = _key(store)
    for path in ("/relay/status", "/relay/m3u8?adamId=1"):
        r = client.get(path, headers=_bearer(plain))
        assert r.status_code == 503, path
    assert not calls


# ── basic-форма ключа ────────────────────────────────────────────────────────

def test_basic_auth_username_form_of_key_is_accepted(env):
    """`https://<key>@host` — браузер шлёт Basic base64("<key>:"),wm.wol.moe
    понимает именно так; мы обязаны понять тоже."""
    client, cfg, store, calls = env
    import base64
    plain = _key(store)
    blob = base64.b64encode(f"{plain}:".encode()).decode()
    r = client.get("/relay/m3u8", params={"adamId": "123"},
                   headers={"Authorization": f"Basic {blob}"})
    assert r.status_code == 200 and r.json()["code"] == 0


def test_key_in_query_is_not_accepted(env):
    """URL'ы оседают в логах прокси и истории браузера — ключом из query мы
    не платим даже ценой совместимости."""
    client, cfg, store, calls = env
    plain = _key(store)
    r = client.get("/relay/m3u8", params={"adamId": "1", "key": plain})
    assert r.status_code == 401


# ── 429 / квоты ──────────────────────────────────────────────────────────────

def test_over_qps_is_429_with_retry_after(env):
    client, cfg, store, calls = env
    plain = _key(store, qps=1)
    r1 = client.get("/relay/m3u8", params={"adamId": "1"}, headers=_bearer(plain))
    r2 = client.get("/relay/m3u8", params={"adamId": "2"}, headers=_bearer(plain))
    assert r1.status_code == 200
    assert r2.status_code == 429 and r2.json()["code"] == 429
    assert int(r2.headers.get("retry-after", "0")) >= 1


def test_over_daily_quota_is_429(env):
    client, cfg, store, calls = env
    plain = _key(store, qps=-1, per_day=2)
    h = _bearer(plain)
    ok = [client.get("/relay/m3u8", params={"adamId": str(i)}, headers=h)
          for i in range(2)]
    assert all(r.status_code == 200 for r in ok)
    r = client.get("/relay/m3u8", params={"adamId": "9"}, headers=h)
    assert r.status_code == 429
    assert "day" in r.json()["msg"]
    assert len(calls) == 2, "отказ по квоте не должен трогать Apple"


def test_denied_requests_do_not_burn_the_quota(env):
    """Отказ по нашей же квоте не двигает суточный счётчик: иначе ключ,
    перегруженный клиентом, добивает сам себя до нуля."""
    client, cfg, store, calls = env
    plain, ref = store.issue(label="x", quota={"qps": 1, "per_hour": 5})
    h = _bearer(plain)
    for _ in range(20):
        client.get("/relay/m3u8", params={"adamId": "1"}, headers=h)
    c = pacing.counts("relay", ref)
    assert c["hour"] <= 3, f"отказов засчитано больше, чем принято: {c}"


# ── floods без ключа ─────────────────────────────────────────────────────────

def test_unauthenticated_flood_gets_429(env):
    client, cfg, store, calls = env
    cfg["relay-unauth-per-minute"] = 3
    codes = [client.get("/relay/m3u8", params={"adamId": "1"}).status_code
             for _ in range(6)]
    assert codes[:3] == [401, 401, 401]
    assert 429 in codes[3:], codes
    assert not calls


# ── что наружу не уходит ─────────────────────────────────────────────────────

def test_upstream_error_text_never_reaches_the_client(env, monkeypatch):
    """В тексте ошибки враппера бывает что угодно — в ответе реле его быть не
    должно: только код и нейтральное сообщение."""
    client, cfg, store, calls = env

    async def boom(endpoint, config=None, params=None, attempts=2):
        raise relay_pool.RelayUpstreamError(
            "media-user-token=SECRET-TOKEN-123 apple-account@example.com",
            code=403, http_status=403)

    monkeypatch.setattr(relay_pool, "call", boom)
    plain = _key(store)
    r = client.get("/relay/key", params={"adamId": "1", "uri": "skd://x"},
                   headers=_bearer(plain))
    body = r.text
    assert r.status_code == 403
    assert "SECRET-TOKEN-123" not in body and "example.com" not in body


def test_status_reveals_only_regions_and_counts(env, monkeypatch):
    client, cfg, store, calls = env

    def fake_snapshot(config=None, force=False):
        return {"ready": True, "regions": ["USA", "GBM"],
                "reason": "",
                "instances": [
                    {"label": "127.0.0.1:12340", "url": "http://127.0.0.1:12340",
                     "account": "host@example.com", "ready": True},
                    {"label": "port-2", "url": "http://192.168.10.20:12341",
                     "account": "second@example.com", "ready": False},
                ]}

    monkeypatch.setattr(relay_pool, "snapshot", fake_snapshot)
    r = client.get("/relay/status")
    assert r.status_code == 200
    body = r.text
    for secret in ("127.0.0.1", "192.168", "example.com", "host", "url"):
        assert secret not in body, f"статус показывает лишнее: {secret}"
    data = r.json()["data"]
    assert data["regions"] == ["USA", "GBM"]
    assert data["instances"] == 2 and data["readyInstances"] == 1


def test_key_never_appears_in_body_of_successful_answer(env):
    client, cfg, store, calls = env
    plain = _key(store)
    r = client.get("/relay/key", params={"adamId": "7", "uri": "skd://7"},
                   headers=_bearer(plain))
    assert r.status_code == 200
    assert plain not in r.text


# ── license ──────────────────────────────────────────────────────────────────

def test_license_closed_by_default_and_gated_by_owner(env):
    client, cfg, store, calls = env
    plain = _key(store)
    r = client.post("/relay/license", json={"adamId": "1", "challenge": "c"})
    assert r.status_code == 403, "без ключа тоже 403: аноним не картирует настройки"
    cfg["relay-license-allow"] = True
    r = client.post("/relay/license", json={"adamId": "1", "challenge": "c"},
                    headers=_bearer(plain))
    assert r.status_code == 200 and calls[-1][0] == "license"


# ── админка ──────────────────────────────────────────────────────────────────

def test_admin_endpoints_need_owner(env):
    client, cfg, store, calls = env
    for path in ("/api/relay/admin/keys", "/api/relay/admin/pool"):
        r = client.get(path)
        assert r.status_code == 401, path
    for path in ("/api/relay/admin/issue", "/api/relay/admin/revoke",
                 "/api/relay/admin/quota"):
        r = client.post(path, json={})
        assert r.status_code == 401, path


def test_admin_issue_shows_key_once_and_it_works(env, monkeypatch):
    client, cfg, store, calls = env
    monkeypatch.setattr(relay_routes, "_is_owner", lambda request: True)
    r = client.post("/api/relay/admin/issue",
                    json={"label": "ноут", "quota": {"qps": 5}})
    assert r.status_code == 200
    plain = r.json()["data"]["key"]
    assert plain.startswith("rk_")
    ok = client.get("/relay/m3u8", params={"adamId": "1"}, headers=_bearer(plain))
    assert ok.status_code == 200
    # второй раз тот же ключ админка не отдаёт — в списке только ref/hint
    lst = client.get("/api/relay/admin/keys").json()["data"]["keys"]
    assert all(plain not in str(row) for row in lst)
    ref = r.json()["data"]["ref"]
    assert client.post("/api/relay/admin/revoke", json={"ref": ref}).status_code == 200
    assert client.get("/relay/m3u8", params={"adamId": "1"},
                      headers=_bearer(plain)).status_code == 401


def test_quota_patch_changes_limits(env, monkeypatch):
    client, cfg, store, calls = env
    monkeypatch.setattr(relay_routes, "_is_owner", lambda request: True)
    plain, ref = store.issue(label="x", quota={"qps": 1})
    h = _bearer(plain)
    assert client.get("/relay/m3u8", params={"adamId": "1"}, headers=h).status_code == 200
    assert client.get("/relay/m3u8", params={"adamId": "2"}, headers=h).status_code == 429
    r = client.post("/api/relay/admin/quota", json={"ref": ref, "qps": 10})
    assert r.json()["data"]["ok"] is True
    # окно в 1 с ещё занято первым запросом — ждём его закрытия
    time.sleep(1.1)
    assert client.get("/relay/m3u8", params={"adamId": "3"}, headers=h).status_code == 200
