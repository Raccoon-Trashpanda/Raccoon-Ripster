# -*- coding: utf-8 -*-
"""TCP-оракл: загрузчик apple-music-downloader ↔ Wrapper Lite + Temari.

Go-загрузчик (``utils/runv2/runv2.go``) умеет расшифровывать только через
TCP-«оракул» локального враппера: порты ``get-m3u8-port`` (одна строка —
URL мастер-плейлиста) и ``decrypt-m3u8-port`` (состояния: установка ключа
``[len][adamId][len][uri]``, смена ключа — четыре нуля перед ней, запрос
расшифровки — ``uint32LE`` длины + байты, ответ — те же байты в claro;
закрытие — пять нулей). Протокол без ключей и без пикосекундных тонкостей:
всё это воспроизводим здесь, а ключ и шаблон берём у lite по HTTP
(``ripster/lite.py``), сами сэмплы кладём локально Temari.

Плюс: никакого нового MP4-кода — проверенный Go-пайплайн (качалка,
фрагментация, обложки, тексты) остается как есть.
"""
from __future__ import annotations

import json
import socket
import struct
import threading
from collections import OrderedDict
from pathlib import Path

from ripster import lite

_MAX_CHUNK = 64 * 1024 * 1024        # uint32-запрос больше этого — точно мусор
_TEMPLATES_MAX = 64


class TemariCrypto:
    """Ленивая обёртка над Temari: json шаблона /key → decrypt(chunk)."""

    def __init__(self):
        self._lock = threading.Lock()
        self._cache: OrderedDict[str, object] = OrderedDict()
        self._mod = None

    def _module(self):
        if self._mod is None:
            try:
                import temari
            except Exception as e:
                raise lite.LiteError(
                    "Temari не установлен — pip install temari==0.3.1 "
                    f"(аудит: docs/WRAPPER_LITE_AUDIT_2026-09-24.md); {e}")
            self._mod = temari
        return self._mod

    def available(self) -> bool:
        try:
            self._module()
            return True
        except lite.LiteError:
            return False

    def _template(self, data: dict):
        if not data.get("ctx") or not data.get("state"):
            raise lite.LiteError(
                "lite не снял шаблон (нет ctx/state) — перезапустите контейнер "
                "Wrapper Lite или выполните логин заново")
        key = lite._entry_key(str(data.get("adamId") or ""),
                              str(data.get("keyUri") or ""))
        with self._lock:
            t = self._cache.get(key)
            if t is not None:
                self._cache.move_to_end(key)
                return t
            t = self._module().Temari.from_json(json.dumps(data).encode("utf-8"))
            self._cache[key] = t
            while len(self._cache) > _TEMPLATES_MAX:
                _k, old = self._cache.popitem(last=False)
                try:
                    old.close()
                except Exception:
                    pass
            return t

    def decrypt(self, data: dict, chunk: bytes) -> bytes:
        return self._template(data).decrypt(chunk)


_crypto = TemariCrypto()


class LiteShim:
    """Два loopback-TCP сервера для Go-загрузчика."""

    def __init__(self, client: lite.LiteClient, crypto: TemariCrypto | None = None):
        self.client = client
        self.crypto = crypto or _crypto
        self._sockets: list[socket.socket] = []
        self._threads: list[threading.Thread] = []
        self._stopping = threading.Event()
        self.dec_addr: tuple[str, int] | None = None
        self.m3u_addr: tuple[str, int] | None = None

    # ── lifecycle ─────────────────────────────────────────────────────────
    def start(self, dec_addr: str = "127.0.0.1:12345",
              m3u_addr: str = "127.0.0.1:12346") -> None:
        self.dec_addr = self._serve(dec_addr, self._handle_decrypt)
        self.m3u_addr = self._serve(m3u_addr, self._handle_m3u8)

    def _serve(self, addr: str, handler) -> tuple[str, int]:
        host, _, port_s = addr.rpartition(":")
        host = "127.0.0.1" if host in ("", "127.0.0.1", "localhost") else host
        if host != "127.0.0.1":
            # оракул без авторизации живёт только на петле — см. аудит §6.2
            raise lite.LiteError(f"Wrapper Lite: порт можно слушать только на "
                                 f"127.0.0.1, а не на {addr}")
        srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        srv.bind(("127.0.0.1", int(port_s)))
        srv.listen(16)
        self._sockets.append(srv)
        th = threading.Thread(target=self._accept_loop, args=(srv, handler),
                              name=f"lite-shim-{srv.getsockname()[1]}", daemon=True)
        th.start()
        self._threads.append(th)
        return srv.getsockname()[:2]

    def _accept_loop(self, srv: socket.socket, handler):
        while not self._stopping.is_set():
            try:
                conn, _ = srv.accept()
            except OSError:
                return
            threading.Thread(target=self._guarded, args=(handler, conn),
                             daemon=True).start()

    def _guarded(self, handler, conn: socket.socket):
        try:
            handler(conn)
        except Exception:
            # обрыв на стороне Go (EOF среди фразмента) — не авария приложения;
            # Go сам честно сообщит о провале расшифровки
            pass
        finally:
            try:
                conn.close()
            except Exception:
                pass

    def stop(self):
        self._stopping.set()
        for s in self._sockets:
            try:
                s.close()
            except Exception:
                pass
        self._sockets.clear()

    # ── wire helpers ──────────────────────────────────────────────────────
    @staticmethod
    def _read_exact(conn: socket.socket, n: int) -> bytes | None:
        buf = bytearray()
        while len(buf) < n:
            part = conn.recv(n - len(buf))
            if not part:
                return None
            buf += part
        return bytes(buf)

    @staticmethod
    def _read_byte(conn: socket.socket) -> int | None:
        b = conn.recv(1)
        return None if not b else b[0]

    # ── get-m3u8-port: [len][adamId] → "URL\n" ───────────────────────────
    def _handle_m3u8(self, conn: socket.socket):
        n = self._read_byte(conn)
        if not n:
            return
        adam = self._read_exact(conn, n)
        if adam is None:
            return
        try:
            url = self.client.m3u8_url(adam.decode("ascii", "replace"))
        except lite.LiteError:
            url = ""                     # честная пустая строка: Go заропорится сам
        conn.sendall(url.encode("utf-8") + b"\n")

    # ── decrypt-m3u8-port: ключевые строки + потоковые запросы ───────────
    def _read_key(self, conn: socket.socket) -> dict | None:
        """[len][adamId][len][uri] → данные ключа; None — обрыв/ошибка."""
        adam_n = self._read_byte(conn)
        if not adam_n:
            return None
        return self._read_key_with(conn, adam_n)

    def _handle_decrypt(self, conn: socket.socket):
        # первый ключ приходит БЕЗ SwitchKeys-префикса (runv2.go: префикс
        # только при смене ключа, i != 0)
        data = self._read_key(conn)
        if data is None:
            return
        while True:
            head = self._read_exact(conn, 4)
            if head is None:
                return
            (n,) = struct.unpack("<I", head)
            if n == 0:
                # четыре нуля — SwitchKeys; пятый ноль без продолжения — Close
                nxt = conn.recv(1)
                if not nxt or nxt[0] == 0:
                    return
                data = self._read_key_with(conn, nxt[0])
                if data is None:
                    return
                continue
            if n > _MAX_CHUNK:
                return
            chunk = self._read_exact(conn, n)
            if chunk is None:
                return
            try:
                plain = self.crypto.decrypt(data, chunk)
            except Exception:
                return
            conn.sendall(plain)

    def _read_key_with(self, conn: socket.socket, adam_n: int) -> dict | None:
        """Как _read_key, но длина adamId уже прочитана."""
        adam = self._read_exact(conn, adam_n)
        uri_n = self._read_byte(conn)
        if adam is None or uri_n is None:
            return None
        uri = self._read_exact(conn, uri_n)
        if uri is None:
            return None
        try:
            return self.client.key(adam.decode("ascii", "replace"),
                                   uri.decode("ascii", "replace"))
        except lite.LiteError:
            return None


# ── синглтон на процесс + рабочий каталог для Go ──────────────────────────
_shim: LiteShim | None = None
_shim_lock = threading.Lock()
_cwd: str | None = None


def ensure(config: dict) -> LiteShim:
    """Поднять оракул на портах из конфига (или вернуть поднятый).

    Идемпотентно: повторные вызовы с теми же адресами переиспользуют shim и
    только перезаписывают ``.lite_cwd/config.yaml`` — Go читает config.yaml
    из своей cwd (см. wrapper_pool.slot_cwd, тот же трюк).
    """
    global _shim, _cwd
    with _shim_lock:
        dec = str(config.get("apple-lite-decrypt-port") or "127.0.0.1:12345")
        m3u = str(config.get("apple-lite-m3u8-port") or "127.0.0.1:12346")
        if _shim is None:
            if not _crypto.available():
                raise lite.LiteError("Temari не доступен — движок Lite не работает")
            client = lite.get_client(config)
            _shim = LiteShim(client)
            try:
                _shim.start(dec, m3u)
            except OSError as e:
                _shim = None
                raise lite.LiteError(
                    f"не удалось поднять оракул Lite на {dec}/{m3u}: {e}")
        _cwd = write_cwd(config, dec, m3u)
        return _shim


def cwd_dir() -> str | None:
    return _cwd


def write_cwd(config: dict, dec_addr: str, m3u_addr: str) -> str:
    """Копия корневого config.yaml с переставленными портами враппера."""
    import re as _re
    root = Path(__file__).resolve().parent.parent
    text = (root / "config.yaml").read_text(encoding="utf-8")

    def _set(t: str, key: str, val: str) -> str:
        pat = _re.compile(rf"^{_re.escape(key)}:.*$", _re.M)
        return pat.sub(f"{key}: {val}", t) if pat.search(t) else t + f"\n{key}: {val}\n"

    for k in ("decrypt-m3u8-port", "decrypt-port"):
        text = _set(text, k, dec_addr)
    for k in ("get-m3u8-port", "m3u8-port"):
        text = _set(text, k, m3u_addr)
    # все параллельные дорожки Go размазывает по `decrypt-ports` — пусть все
    # идут в наш единственный оракул, а не в пул контейнеров из корневого конфига
    text = _set(text, "decrypt-ports", dec_addr)
    d = root / ".lite_cwd"
    d.mkdir(parents=True, exist_ok=True)
    (d / "config.yaml").write_text(text, encoding="utf-8")
    return str(d)
