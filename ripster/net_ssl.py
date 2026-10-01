"""ripster.net_ssl — единый SSL-контекст для всех загрузок проекта.

01.10.2026 (задача 079): на этом ПК системный магазин Windows содержит
просроченную кросс-подписанную копию ISRG Root X2 (истекла 15.09.2025), и
OpenSSL строит цепочку через неё вместо валидной ISRG Root X1. Хосты с
Let's Encrypt (bok.net, gyan.dev, codeberg.org…) падали с «certificate has
expired», хотя сертификаты живые. certifi-бандл эту проблему обходит.

Этот модуль — единственное место, где создаётся ssl-контекст для загрузок.
Все urllib-операции в ripster/ и tools/ берут контекст здесь, без дублей
``ssl.create_default_context(cafile=certifi.where())`` по файлам.
"""
from __future__ import annotations

import ssl
from functools import lru_cache
from typing import Optional


@lru_cache(maxsize=1)
def ssl_context() -> ssl.SSLContext:
    """Вернуть лучший доступный контекст для проверки TLS.

    Порядок: certifi-бандл (Mozilla root store, свежий) → системный контекст
    Python (хранилище Windows). Никогда не отключает проверку сертификатов.
    """
    try:
        import certifi
        return ssl.create_default_context(cafile=certifi.where())
    except Exception:
        return ssl.create_default_context()


def ssl_context_or_none() -> Optional[ssl.SSLContext]:
    """Как ssl_context(), но None если certifi недоступен — вызывающий код
    сам решает, падать или использовать системный контекст."""
    try:
        import certifi
        return ssl.create_default_context(cafile=certifi.where())
    except Exception:
        return None
