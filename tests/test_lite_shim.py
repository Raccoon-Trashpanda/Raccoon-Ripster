"""TCP-оракл LiteShim: протокол runv2 ↔ lite + Temari.

Проверяем два слоя:

1. раз framing (``utils/runv2/runv2.go``): первый ключ без SwitchKeys,
   смена ключа — четыре нуля + строка, запрос — uint32LE + байты,
   закрытие — пять нулей; на питоновской стороне — фейковый клиент,
   никакой сети и учётки;
2. живой end-to-end: фикстура golden (template/ct/pt) через НАСТОЯЩИЕ
   сокет и Temari — если пойдёт, значит Go-загрузчик получит ровно то же.
"""
import json
import socket
import struct
from pathlib import Path

import pytest

from ripster import lite, lite_shim

FIXTURES = Path(__file__).parent / "fixtures" / "lite_golden"

_TIMEOUT = 10.0


class FakeClient:
    def __init__(self, urls=None, keys=None, fail_url=False):
        self.urls = urls or {}
        self.keys = keys or {}
        self.fail_url = fail_url
        self.key_calls: list[tuple] = []
        self.url_calls: list[str] = []

    def m3u8_url(self, adam):
        self.url_calls.append(adam)
        if self.fail_url:
            raise lite.LiteError("нет токенов")
        return self.urls.get(adam, "")

    def key(self, adam, uri):
        self.key_calls.append((adam, uri))
        if (adam, uri) not in self.keys:
            raise lite.LiteError(f"lite не отдал ключ для {adam}")
        return self.keys[(adam, uri)]


class FakeCrypto:
    """decrypt = метка текущих данных + инверсия байтов (обратимо)."""

    def __init__(self):
        self.calls: list[tuple] = []

    def decrypt(self, data, chunk):
        self.calls.append((data.get("adamId"), len(chunk)))
        return bytes(255 - b for b in chunk)


@pytest.fixture
def shim():
    client = FakeClient(urls={"111": "https://example.test/master.m3u8"},
                        keys={("111", "skd://a/1"): {"adamId": "111", "contentKey": "K1", "tag": "A"},
                              ("222", "skd://b/2"): {"adamId": "222", "contentKey": "K2", "tag": "B"},
                              ("999", "skd://c/3"): {"adamId": "999", "keyUri": "skd://c/3",
                                                     "contentKey": "K9"}})
    s = lite_shim.LiteShim(client, FakeCrypto())
    s.start("127.0.0.1:0", "127.0.0.1:0")
    yield s
    s.stop()


def _connect(addr):
    host, port = addr
    conn = socket.create_connection((host, port), timeout=_TIMEOUT)
    conn.settimeout(_TIMEOUT)
    return conn


def _recv_exact(conn, n):
    buf = bytearray()
    while len(buf) < n:
        part = conn.recv(n - len(buf))
        assert part, "сервер закрыл соединение раньше, чем прислал ответ"
        buf += part
    return bytes(buf)


def _expect_closed(conn):
    """Windows закрывает сокет с непрочитанными данными как RST — для нас
    исход безразличен: сервер сессию отпустил."""
    try:
        return conn.recv(1) == b""
    except (ConnectionResetError, ConnectionAbortedError):
        return True


def _key_line(adam: str, uri: str) -> bytes:
    return bytes([len(adam)]) + adam.encode() + bytes([len(uri)]) + uri.encode()


# ── get-m3u8-port ─────────────────────────────────────────────────────────
def test_m3u8_url_line_content(shim):
    conn = _connect(shim.m3u_addr)
    adam = "111"
    conn.sendall(bytes([len(adam)]) + adam.encode())
    data = b""
    while not data.endswith(b"\n"):
        chunk = conn.recv(4096)
        if not chunk:
            break
        data += chunk
    conn.close()
    assert data == b"https://example.test/master.m3u8\n"


def test_m3u8_error_is_honest_empty_line(shim):
    shim.client.fail_url = True
    conn = _connect(shim.m3u_addr)
    conn.sendall(bytes([3]) + b"404")
    data = b""
    while True:
        chunk = conn.recv(4096)
        if not chunk or data.endswith(b"\n"):
            break
        data += chunk
    conn.close()
    assert data == b"\n"


# ── decrypt-m3u8-port ─────────────────────────────────────────────────────
def test_shim_refuses_non_loopback_bind():
    s = lite_shim.LiteShim(FakeClient(), FakeCrypto())
    with pytest.raises(lite.LiteError):
        s.start("0.0.0.0:0", "127.0.0.1:0")
    s.stop()


def test_first_key_has_no_switchkeys_prefix(shim):
    """runv2.go шлёт четыре нуля только ПРИ СМЕНЕ ключа (i != 0)."""
    conn = _connect(shim.dec_addr)
    conn.sendall(_key_line("111", "skd://a/1"))
    ct = b"\x01\x02\x03\x04"
    conn.sendall(struct.pack("<I", len(ct)) + ct)
    plain = _recv_exact(conn, len(ct))
    conn.close()
    assert shim.client.key_calls == [("111", "skd://a/1")]
    assert plain == bytes(255 - b for b in ct)
    assert shim.crypto.calls == [("111", 4)]


def test_chunks_reuse_current_key_without_refetch(shim):
    conn = _connect(shim.dec_addr)
    conn.sendall(_key_line("111", "skd://a/1"))
    for payload in (b"aaaa", b"bb"):
        conn.sendall(struct.pack("<I", len(payload)) + payload)
        _recv_exact(conn, len(payload))
    conn.close()
    assert shim.client.key_calls == [("111", "skd://a/1")]
    assert [c[1] for c in shim.crypto.calls] == [4, 2]


def test_switch_keys_then_close(shim):
    conn = _connect(shim.dec_addr)
    conn.sendall(_key_line("111", "skd://a/1"))
    ct = b"xyz"
    conn.sendall(struct.pack("<I", len(ct)) + ct)
    _recv_exact(conn, len(ct))
    # SwitchKeys + новый ключ
    conn.sendall(b"\x00\x00\x00\x00" + _key_line("222", "skd://b/2"))
    ct2 = b"ww"
    conn.sendall(struct.pack("<I", len(ct2)) + ct2)
    _recv_exact(conn, len(ct2))
    assert shim.client.key_calls == [("111", "skd://a/1"), ("222", "skd://b/2")]
    assert shim.crypto.calls[-1][0] == "222"
    # Close: пять нулей — сервер дожидается и закрывает
    conn.sendall(b"\x00\x00\x00\x00\x00")
    assert _expect_closed(conn)
    conn.close()


def test_oversized_request_closes_connection(shim):
    conn = _connect(shim.dec_addr)
    conn.sendall(_key_line("999", "skd://c/3"))
    conn.sendall(struct.pack("<I", lite_shim._MAX_CHUNK + 1))
    assert _expect_closed(conn)
    conn.close()


def test_failed_key_fetch_closes_connection(shim):
    """Клиент не отдал ключ (_read_key → None после первого) — сессия гаснет,
    Go получит обрыв и честно сообщит о провале, вместо зависания."""
    conn = _connect(shim.dec_addr)
    conn.sendall(_key_line("111", "skd://a/1"))
    ct = b"q"
    conn.sendall(struct.pack("<I", len(ct)) + ct)
    _recv_exact(conn, 1)
    conn.sendall(b"\x00\x00\x00\x00" + _key_line("???", "skd://net-v-tablitse"))
    assert _expect_closed(conn)
    conn.close()


# ── живой end-to-end: сокет + настоящая Temari на golden-векторе ──────────
requires_temari = pytest.mark.skipif(
    not lite_shim._crypto.available(),
    reason="Temari не установлена (pip install temari==0.3.1)")


@requires_temari
def test_golden_decrypt_through_real_socket():
    """Ровно производственная форма запроса (runv2.go, cbcsFullSubsampleDecrypt):
    весь субсэмпл одним запросом, длина усечена до 16. Temari decrypt валиден
    для целого субсэмпла от нулевого байта — не для произвольных срезов
    (проверено: префикс [:992] сходится, серединный чанк — уже нет)."""
    template = json.loads((FIXTURES / "template.json").read_bytes())
    ct = (FIXTURES / "sample.ct").read_bytes()
    pt = (FIXTURES / "sample.pt").read_bytes()
    aligned = len(ct) & ~0xf

    class GoldenClient(FakeClient):
        def key(self, adam, uri):
            self.key_calls.append((adam, uri))
            return dict(template, adamId=adam, keyUri=uri)

    s = lite_shim.LiteShim(GoldenClient(keys={}))  # настоящая TemariCrypto
    s.start("127.0.0.1:0", "127.0.0.1:0")
    try:
        conn = _connect(s.dec_addr)
        conn.sendall(_key_line("777", "skd://golden/1"))
        conn.sendall(struct.pack("<I", aligned) + ct[:aligned])
        got = _recv_exact(conn, aligned)
        conn.close()
        assert got == pt[:aligned], \
            "сквозное расшифрование через оракул разошлось с golden"
    finally:
        s.stop()
