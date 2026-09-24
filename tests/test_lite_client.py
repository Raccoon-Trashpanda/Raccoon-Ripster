# -*- coding: utf-8 -*-
"""Клиент Wrapper Lite и шифрованный кэш ключей (мок HTTP, без сети).

Конверт ответов — из реального ``lite/lite_main.cpp`` пина b7058ec5:
``{"code":0,"msg":"SUCCESS","data":{...}}`` / ``{"code":500,"msg":"key
retrieval failed"}``. Проверяется то, за чем кэш есть: повторный запрос
ключа НЕ доходит до lite-сервера, а на диске нет ни contentKey, ни шаблона.
"""
import json
import sys
from pathlib import Path

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from ripster import lite  # noqa: E402

SECRET = "a" * 64


def _env(data):
    return httpx.Response(200, json={"code": 0, "msg": "SUCCESS", "data": data})


KEY_DATA = {
    "adamId": "1720704575",
    "keyUri": "skd://itunes.apple.com/p683167073/c23",
    "contentKey": "00112233445566778899aabbccddeeff",
    "ctx": "AAAA", "state": "BBBB",
    "rcx": "0xa3", "rax": "0xb6", "rdx": "0x2480", "r9": "0xd383dde0",
    "rbp": "0xd38487e0",
}


@pytest.fixture()
def cache(tmp_path):
    return lite.LiteKeyCache(path=tmp_path / "keys.bin", secret_provider=lambda: SECRET)


def _client(handler, cache=None):
    return lite.LiteClient("http://lite.test", cache=cache,
                           transport=httpx.MockTransport(handler))


# ── клиент: конверт и ошибки ────────────────────────────────────────────────

def test_key_success_returns_data():
    seen = []

    def h(request):
        seen.append(dict(request.url.params))
        return _env(KEY_DATA)

    c = _client(h)
    assert c.key("1720704575", KEY_DATA["keyUri"]) == KEY_DATA
    assert seen == [{"adamId": "1720704575", "uri": KEY_DATA["keyUri"]}]


def test_key_500_chat_failure_is_named():
    def h(request):
        return httpx.Response(200, json={"code": 500, "msg": "key retrieval failed"})

    with pytest.raises(lite.LiteError) as e:
        _client(h).key("1", "skd://x")
    assert "key retrieval failed" in str(e.value)


def test_unreachable_wrapped_as_lite_error():
    def h(request):
        raise httpx.ConnectError("_connection refused")

    with pytest.raises(lite.LiteError) as e:
        _client(h).status()
    assert "недоступен" in str(e.value)


def test_m3u8_url_extracted():
    def h(request):
        return _env({"adamId": "7", "m3u8": "https://aod.m3u8?tok=x"})

    assert _client(h).m3u8_url("7") == "https://aod.m3u8?tok=x"


def test_license_posts_envelope():
    got = {}

    def h(request):
        got.update(json.loads(request.content))
        return _env({"licence": "zz"})

    c = _client(h)
    c.license("9", "chal", "skd://u", drm_type="pr")
    assert got == {"adamId": "9", "challenge": "chal", "uri": "skd://u",
                   "drm-type": "pr"}


def test_key_without_contentkey_is_refused():
    def h(request):
        return _env({"adamId": "5", "keyUri": "skd://x"})     # нет contentKey

    with pytest.raises(lite.LiteError):
        _client(h, cache=None).key("5", "skd://x")


# ── кэш:Hits, шифрование, порча ─────────────────────────────────────────────

def test_cache_hit_prevents_second_http_request(cache):
    calls = []

    def h(request):
        calls.append(request)
        return _env(KEY_DATA)

    c = _client(h, cache=cache)
    assert c.key("1720704575", KEY_DATA["keyUri"]) == KEY_DATA
    assert c.key("1720704575", KEY_DATA["keyUri"]) == KEY_DATA
    assert len(calls) == 1, "повтор не должен дёргать lite (и Apple)"


def test_cache_other_uri_is_a_miss(cache):
    calls = []

    def h(request):
        calls.append(request)
        return _env(KEY_DATA)

    c = _client(h, cache=cache)
    c.key("1720704575", KEY_DATA["keyUri"])
    c.key("1720704575", "skd://itunes.apple.com/p999/c9")
    assert len(calls) == 2


def test_cache_encrypted_at_rest(cache):
    c = _client(lambda r: _env(KEY_DATA), cache=cache)
    c.key("1720704575", KEY_DATA["keyUri"])
    raw = cache.path.read_bytes()
    assert raw[:4] == b"LITE"
    assert KEY_DATA["contentKey"].encode() not in raw
    assert b"ctx" not in raw


def test_cache_survives_reload_same_secret(tmp_path):
    c1 = lite.LiteKeyCache(path=tmp_path / "k.bin", secret_provider=lambda: SECRET)
    c1.put("1", "skd://a", KEY_DATA)
    c2 = lite.LiteKeyCache(path=tmp_path / "k.bin", secret_provider=lambda: SECRET)
    assert c2.get("1", "skd://a") == KEY_DATA


def test_cache_wrong_secret_reads_empty(tmp_path):
    c1 = lite.LiteKeyCache(path=tmp_path / "k.bin", secret_provider=lambda: SECRET)
    c1.put("1", "skd://a", KEY_DATA)
    c2 = lite.LiteKeyCache(path=tmp_path / "k.bin", secret_provider=lambda: "b" * 64)
    assert c2.get("1", "skd://a") is None


def test_cache_tampered_file_is_discarded(tmp_path):
    p = tmp_path / "k.bin"
    c1 = lite.LiteKeyCache(path=p, secret_provider=lambda: SECRET)
    c1.put("1", "skd://a", KEY_DATA)
    bad = bytearray(p.read_bytes())
    bad[-1] ^= 0xFF
    p.write_bytes(bytes(bad))
    c2 = lite.LiteKeyCache(path=p, secret_provider=lambda: SECRET)
    assert c2.get("1", "skd://a") is None
    assert c2.count() == 0


def test_cache_eviction_keeps_newest(tmp_path, monkeypatch):
    monkeypatch.setattr(lite, "_MAX_ENTRIES", 3)
    c = lite.LiteKeyCache(path=tmp_path / "k.bin", secret_provider=lambda: SECRET)
    for i in range(5):
        c.put(str(i), "skd://u", {"adamId": str(i), "contentKey": "k%d" % i,
                                  "ctx": "c", "state": "s"})
    assert c.count() <= 3
    assert c.get("4", "skd://u") is not None
    assert c.get("0", "skd://u") is None


def test_lite_url_defaults_to_loopback():
    assert lite.lite_url({}) == "http://127.0.0.1:12340"
    assert lite.lite_url({"apple-lite-url": "127.0.0.1:12340/"}) == "http://127.0.0.1:12340"
