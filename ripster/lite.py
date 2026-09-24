# -*- coding: utf-8 -*-
"""Wrapper Lite: клиент key-сервера + шифрованный кэш ключей.

Wrapper Lite (``docs/WRAPPER_LITE_AUDIT_2026-09-24.md``) — второй локальный
бэкенд Apple: контейнер ``ripster-wrapper-lite`` на 127.0.0.1:12340 отдаёт
m3u8 и ключи расшифровки по HTTP; сама расшифровка идёт здесь, библиотекой
Temari (``ripster/lite_shim.py``). Учётные данные наружу не идут: lite сам
говорит с Apple, а мы — только с петлёй.

Кэш ``adamId+keyUri → ключ+шаблон`` нужен не для скорости, а против бана:
повторная закачка того же трека не должна дёргать лицензию Apple (совет
zhaarey в чате авторов). Ключи — это контент, их воруют так же охотно, как
пароли, поэтому файл кэма шифруется AES-256-GCM на ключе, выведенном из
``session-secret`` приложения (``ripster/auth.py``).
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import threading
import time
from pathlib import Path

import httpx

DEFAULT_URL = "http://127.0.0.1:12340"
#: общий prefetch-ключ (init-сегменты); lite принимает его только с adamId=0
PREFETCH_URI = "skd://itunes.apple.com/P000000000/s1/e1"

#: один запрос за ключом к Apple одновременно — и кэш не гоним двумя
#: потоками в одну цель, и Apple не видит пачки одинаковых лицензий
_key_gate = threading.Lock()

_SALT = b"ripster.apple.lite.keys.v1"
_MAX_ENTRIES = 5000


class LiteError(RuntimeError):
    """Ошибка lite-сервера (конверт code!=0) или невозможность до него дойти.

    `http_status` отделён от `code` намеренно: `code` — это поле конверта,
    которое реле отдаёт клиенту как есть, а по `http_status` решают про штраф
    (429/403 от туннеля или враппера в конверт не ложатся никогда)."""

    def __init__(self, msg: str, code: int = -1, http_status: int = 0):
        super().__init__(msg)
        self.code = code
        self.http_status = int(http_status or 0)


def lite_url(config: dict) -> str:
    """Адрес key-сервера из конфига; по умолчанию — строго петля."""
    raw = str((config or {}).get("apple-lite-url") or DEFAULT_URL).strip() or DEFAULT_URL
    if "://" not in raw:
        raw = "http://" + raw
    return raw.rstrip("/")


def _entry_key(adam_id: str, uri: str) -> str:
    return hashlib.sha256(f"{adam_id}|{uri}".encode("utf-8")).hexdigest()


def _default_secret() -> str:
    from ripster import auth
    return auth._ensure_session_secret()


class LiteKeyCache:
    """adamId+keyUri → {ключ, шаблон Temari}; на диске — только шифртекст."""

    def __init__(self, path: Path | str | None = None, secret_provider=None):
        self._lock = threading.Lock()
        self._secret_provider = secret_provider or _default_secret
        if path is None:
            from ripster.endpoint import cache_dir
            path = cache_dir() / "apple_lite_keys.v1.bin"
        self.path = Path(path)
        self._entries: dict[str, dict] | None = None

    # ── crypto ────────────────────────────────────────────────────────────
    def _aead_key(self) -> bytes:
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.kdf.hkdf import HKDF
        secret = str(self._secret_provider() or "").encode("utf-8")
        return HKDF(hashes.SHA256(), 32, salt=_SALT, info=b"content-keys").derive(secret)

    def _encrypt(self, blob: bytes) -> bytes:
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
        nonce = os.urandom(12)
        return nonce + AESGCM(self._aead_key()).encrypt(nonce, blob, None)

    def _decrypt(self, raw: bytes) -> bytes:
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
        nonce, ct = raw[:12], raw[12:]
        return AESGCM(self._aead_key()).decrypt(nonce, ct, None)

    # ── storage ───────────────────────────────────────────────────────────
    def _load(self) -> dict[str, dict]:
        if self._entries is not None:
            return self._entries
        entries: dict[str, dict] = {}
        try:
            raw = self.path.read_bytes()
            if raw[:4] == b"LITE":            # явный сигнал формата + версии
                entries = json.loads(self._decrypt(raw[4:]))
        except Exception:
            # битый/чужой/подделанный файл — кэш пустой, ключи дотребуются;
            # молча вернуть мусор от шифра, который не мы клали, нельзя
            entries = {}
        self._entries = entries
        return entries

    def _store(self, entries: dict[str, dict]) -> bool:
        blob = json.dumps(entries, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_bytes(b"LITE" + self._encrypt(blob))
            os.replace(tmp, self.path)
            try:
                os.chmod(self.path, 0o600)
            except OSError:
                pass
            self._entries = entries
            return True
        except Exception:
            return False

    def get(self, adam_id: str, uri: str) -> dict | None:
        with self._lock:
            ent = self._load().get(_entry_key(adam_id, uri))
        return dict(ent["data"]) if isinstance(ent, dict) and "data" in ent else None

    def put(self, adam_id: str, uri: str, data: dict) -> None:
        with self._lock:
            entries = self._load()
            entries[_entry_key(adam_id, uri)] = {"data": data, "ts": int(time.time())}
            if len(entries) > _MAX_ENTRIES:
                keep = sorted(entries.items(), key=lambda kv: kv[1].get("ts", 0))[-_MAX_ENTRIES:]
                entries = dict(keep)
            self._store(entries)

    def count(self) -> int:
        with self._lock:
            return len(self._load())


class LiteClient:
    """HTTP-клиент key-сервера lite (конверт ``{code,msg,data}``)."""

    def __init__(self, base_url: str, cache: LiteKeyCache | None = None,
                 transport: httpx.BaseTransport | None = None):
        self.base_url = base_url.rstrip("/")
        self._http = httpx.Client(transport=transport, timeout=20.0)
        self.cache = cache

    def close(self):
        try:
            self._http.close()
        except Exception:
            pass

    # ── transport ─────────────────────────────────────────────────────────
    def _envelope(self, resp: httpx.Response) -> dict:
        if resp.status_code >= 500:
            raise LiteError(f"lite HTTP {resp.status_code}",
                            code=resp.status_code, http_status=resp.status_code)
        try:
            env = resp.json()
        except ValueError:
            raise LiteError("lite вернул не-JSON", code=-1,
                            http_status=resp.status_code)
        if not isinstance(env, dict) or int(env.get("code", -1)) != 0:
            raise LiteError(str(env.get("msg") or "lite error"),
                            code=int(env.get("code", -1)) if isinstance(env, dict) else -1,
                            http_status=resp.status_code)
        return env.get("data") or {}

    def _get(self, path: str, params: dict, timeout: float | None = None) -> dict:
        try:
            r = self._http.get(self.base_url + path, params=params,
                               timeout=timeout or 20.0)
        except httpx.HTTPError as e:
            raise LiteError(f"Wrapper Lite недоступен: {e.__class__.__name__}", code=-1)
        return self._envelope(r)

    def _post(self, path: str, payload: dict, timeout: float | None = None) -> dict:
        try:
            r = self._http.post(self.base_url + path, json=payload,
                                timeout=timeout or 20.0)
        except httpx.HTTPError as e:
            raise LiteError(f"Wrapper Lite недоступен: {e.__class__.__name__}", code=-1)
        return self._envelope(r)

    def get_data(self, path: str, params: dict | None = None,
                 timeout: float | None = None) -> dict:
        """Общий GET: `data` из конверта дословно. Нужен реле, которое обязано
        вернуть клиенту ровно те поля, что отдал враппер, — отбросив половину
        конверта, мы рассинхронизируемся с AMDL и прочими lite-клиентами."""
        return self._get(path, params or {}, timeout=timeout)

    def post_data(self, path: str, payload: dict | None = None,
                  timeout: float | None = None) -> dict:
        return self._post(path, payload or {}, timeout=timeout)

    # ── API ───────────────────────────────────────────────────────────────
    def status(self) -> dict:
        return self._get("/status", {}, timeout=6.0)

    def regions(self) -> list[str]:
        data = self.status()
        return [str(r) for r in (data.get("regions") or [])]

    def m3u8_url(self, adam_id: str) -> str:
        data = self._get("/m3u8", {"adamId": adam_id}, timeout=60.0)
        url = str(data.get("m3u8") or "").strip()
        if not url:
            raise LiteError(f"lite не отдал m3u8 для {adam_id}")
        return url

    def webplayback(self, adam_id: str) -> dict:
        """Плейлист WebPlay (dev-токен стриминга): {adamId, m3u8}.

        Роут есть во враппере (`lite_main.cpp`, GET /webplayback), а в нашем
        Python-клиент его до сих пор никто не звал. Реле обязано его отдавать:
        это единственный путь для браузера, которому нужен список воспроизведения
        без хозяйского media-user-token."""
        return self._get("/webplayback", {"adamId": adam_id}, timeout=60.0)

    def key(self, adam_id: str, uri: str) -> dict:
        """data-объект /key: contentKey + шаблон Temari (ctx/state/регистры).

        Сначала — шифрованный кэш; к Apple идем только за новым ключом.
        """
        if self.cache is not None:
            hit = self.cache.get(adam_id, uri)
            if hit is not None:
                return hit
        with _key_gate:
            # пока ждали лок, победитель гонки уже положил результат в кэш
            if self.cache is not None:
                hit = self.cache.get(adam_id, uri)
                if hit is not None:
                    return hit
            data = self._get("/key", {"adamId": adam_id, "uri": uri}, timeout=120.0)
        if not data.get("contentKey"):
            raise LiteError(f"lite отдал ключ без contentKey для {adam_id}")
        if self.cache is not None:
            self.cache.put(adam_id, uri, data)
        return data

    def license(self, adam_id: str, challenge: str, uri: str,
                drm_type: str = "wv") -> dict:
        return self._post("/license", {"adamId": adam_id, "challenge": challenge,
                                       "uri": uri, "drm-type": drm_type},
                          timeout=60.0)

    def lyrics(self, adam_id: str, language: str = "en", syllable: bool = True) -> str:
        data = self._get("/lyrics", {"adamId": adam_id, "language": language,
                                     "syllable": "1" if syllable else "0"},
                         timeout=30.0)
        return str(data.get("lyrics") or "")


_default: LiteClient | None = None
_default_url: str | None = None
_default_lock = threading.Lock()


def get_client(config: dict) -> LiteClient:
    """Клиент на процесс; пересоздаётся при смене адреса в конфиге."""
    global _default, _default_url
    url = lite_url(config)
    with _default_lock:
        if _default is None or _default_url != url:
            if _default is not None:
                _default.close()
            _default = LiteClient(url, cache=LiteKeyCache())
            _default_url = url
        return _default
