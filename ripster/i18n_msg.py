"""i18n-контракт API-сообщений (#27).

Проблема: ~263 сообщения в ripster/routes отдавались пользователю по-русски
(`raise HTTPException(400, detail="русский текст")`), и клиент показывал их как
есть — на любом языке интерфейса.

Контракт: сервер отдаёт detail как ОБЪЕКТ ``{key, params, msg}``:
  - ``key``   — ключ i18n (в static/js/i18n.js), который клиент резолвит на язык UI;
  - ``params``— подстановки для ``ti(key, params)`` (интерполяция ``{name}``);
  - ``msg``   — русский fallback: показывается, если ключа в i18n ещё нет.

Клиент (``app.js`` → ``errText`` + нормализация в ``api()``) разворачивает объект
в локализованную строку ЦЕНТРАЛЬНО, поэтому все существующие места
``res.detail`` / ``err.detail`` получают готовую строку без правок.

Обратная совместимость: старые строковые ``detail`` проходят как есть — можно
конвертировать сообщения ПАРТИЯМИ, ничего не ломая.

Пример:
    from ripster.i18n_msg import imsg
    raise HTTPException(400, detail=imsg("err.no_url", "Не указана ссылка"))
    raise HTTPException(404, detail=imsg("err.track_gone",
                                         "Трек {id} недоступен", id=track_id))
"""
from __future__ import annotations

from typing import Any, Dict


def imsg(key: str, ru: str, **params: Any) -> Dict[str, Any]:
    """Собрать detail-объект i18n-контракта. ``key`` — ключ i18n, ``ru`` — русский
    fallback (обязателен, чтобы сообщение оставалось читаемым без перевода)."""
    return {"key": key, "params": params, "msg": ru}
