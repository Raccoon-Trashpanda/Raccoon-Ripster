"""Одноразовые токены запуска внешнего окна плеера (трекер #37).

Зачем. Окно «Внешний плеер» — ОТДЕЛЬНЫЙ процесс pywebview/WebView2 со своим
профилем браузера. Хозяйская кука `ripster-session` в этом профиле есть не
всегда (владелец сидит в обычной вкладке), а без неё рукопожатие /ws
отвергается (`ws_allowed` → close 1008, HTTP 403) и окно никогда не
регистрируется панелью в релее — «живёт своей жизнью». Логин руками внутри
окна невозможен (окно — плеер, не страница входа), а раздать хозяйскую куку
всякому, кто попросит, — значит превратить loopback-порт в бесплатный вход в
хозяина.

Поэтому окно входит ОДНОРАЗОВЫМ токеном:
  • 32 байта `secrets.token_hex` (64 hex-символа), живёт ``TTL_S`` секунд;
  • в памяти сервера лежит ТОЛЬКО SHA-256 токена — утечка дампа памяти или
    логинов не даёт usable-токена, и сам токен не печатается нигде;
  • потребляется ровно один раз (запись удаётся ДО выдачи права — параллельный
    повтор уже не пройдёт);
  • принимается ТОЛЬКО с loopback-адреса: токен, утёкший через туннель, чужой
    машиной не обменяется.

Хранилище — память процесса: перезапуск сервера обнуляет невыданные токены,
окно при этом просто попросит запуск заново. Блокировка — `threading.Lock`:
роуты могут прилетать и в рабочий поток (`asyncio.to_thread`).
"""
from __future__ import annotations

import hashlib
import secrets
import threading
import time

TTL_S = 60                 # окно открывается секунды; минута — с запасом
_MAX_PENDING = 32          # потолок незакрытых выпусков (защита от разрастания)

_LOOPBACK = {"127.0.0.1", "::1", "localhost", "::ffff:127.0.0.1"}

_lock = threading.Lock()
_pending: dict[str, float] = {}   # sha256_hex(token) → unix-время смерти


def is_loopback(host: str | None) -> bool:
    """Loopback ли этоpeer. Маску IPv6-в-IPv4 сворачиваем к адресу."""
    h = (host or "").strip().lower()
    if h.startswith("::ffff:") and h[len("::ffff:"):].startswith("127."):
        return True
    return h in _LOOPBACK


def _prune(now: float) -> None:
    for h, exp in list(_pending.items()):
        if exp <= now:
            _pending.pop(h, None)


def mint(ttl: int = TTL_S) -> str:
    """Выпустить одноразовый токен. Наружу — только plaintext; в памяти — хэш."""
    token = secrets.token_hex(32)
    now = time.time()
    with _lock:
        _prune(now)
        while _pending and len(_pending) >= _MAX_PENDING:
            # очереди нет: сбрасываем самый старый невыпущенный токен
            oldest = min(_pending.values())
            for h, exp in list(_pending.items()):
                if exp == oldest:
                    _pending.pop(h, None)
                    break
        _pending[hashlib.sha256(token.encode()).hexdigest()] = now + ttl
    return token


def consume(token: str, host: str | None) -> bool:
    """Потребить токен: True — если он наш, живой, ещё не истрачен и запрос с
    loopback. Любой отказ — False, 401/403 решает вызывающий."""
    if not is_loopback(host):
        return False
    if not isinstance(token, str) or len(token) > 128:
        return False
    key = hashlib.sha256(token.encode("utf-8", "replace")).hexdigest()
    now = time.time()
    with _lock:
        exp = _pending.pop(key, None)      # однократность: убираем до решения
        if exp is None:
            return False
        if exp <= now:
            return False
        return True


def pending() -> int:
    """Сколько токенов ждёт своего окна (для теста на утечку)."""
    with _lock:
        _prune(time.time())
        return len(_pending)


def reset() -> None:
    """Забыть все выпуски — только для тестов."""
    with _lock:
        _pending.clear()
