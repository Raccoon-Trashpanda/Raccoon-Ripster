"""Режим источника ПК-версии: свои ключи / Премиум Ripster / оба (как в мобилке).

Единый источник правды о том, какие сервисы показывать в поиске и списках. Правило владельца
(11.10.2026): показываем только то, что внедрено И доступно в выбранном режиме; Amazon и Beatport
без ключей не показываем нигде, чтобы не вводить в заблуждение. Спецификация и фазы —
docs/PC_PREMIUM_MODE_PLAN_2026-10-11.md.
"""
from __future__ import annotations

MODES = ("own", "premium", "both")
CONFIG_KEY = "source-mode"

# Что умеет отдать Flex (Премиум): сервисы с рабочим серверным путём.
PREMIUM_SERVICES = ("qobuz", "deezer", "tidal")      # доступны и в поиске, и по ссылке
# Яндекс по Премиуму скачивается ТОЛЬКО по ссылке (поиска по нему у нас нет), поэтому в список поиска не входит.
# Публичные сервисы без учётки — доступны в любом режиме.
PUBLIC_SERVICES = ("bbc", "jiosaavn")

_flex_active_hook = None


def set_flex_active_hook(fn) -> None:
    """Фаза 2 подключает сюда проверку «ключ поддержки активирован на этом ПК»."""
    global _flex_active_hook
    _flex_active_hook = fn


def flex_active() -> bool:
    try:
        return bool(_flex_active_hook()) if _flex_active_hook else False
    except Exception:
        return False


def effective_mode(configured: str | None, own_has: dict) -> str | None:
    """Режим, по которому работаем. None — человек ещё не выбирал и спросить надо.

    Установка, где уже есть свои токены, считается режимом «own» молча: существующих
    пользователей не заставляем проходить выбор заново.
    """
    m = (configured or "").strip().lower()
    if m in MODES:
        return m
    if any(bool(v) for k, v in own_has.items() if k not in PUBLIC_SERVICES and k != "apple"):
        return "own"
    return None


def apply_mode(mode: str | None, own_has: dict, flex_ok: bool) -> dict:
    """-> {сервис: доступен}. own_has — то, что доступно по собственным ключам."""
    if mode in (None, "own"):
        return dict(own_has)
    out: dict = {}
    prem = {s: flex_ok for s in PREMIUM_SERVICES}
    if mode == "premium":
        out = {s: False for s in own_has}
        out.update(prem)
        for s in PUBLIC_SERVICES:
            out[s] = True
        return out
    # both: свои ключи плюс то, что добирает Премиум
    out = dict(own_has)
    for s, ok in prem.items():
        out[s] = bool(out.get(s)) or ok
    return out


# Ключи конфига, по которым видно, что у человека есть СВОИ токены сервиса.
OWN_KEYS = {
    "qobuz": "qobuz-auth-token", "deezer": "deezer-arl", "tidal": "tidal-token",
    "soundcloud": "soundcloud-oauth-token", "yandex": "yandex-token", "beatport": "beatport-username",
}
FLEX_DOWNLOAD_SERVICES = ("qobuz", "deezer", "tidal", "yandex")


def has_own(cfg, svc: str) -> bool:
    key = OWN_KEYS.get(svc)
    if key and str(cfg.get(key) or "").strip():
        return True
    if svc == "tidal":                               # живая сессия движка тоже считается своей учёткой
        try:
            from ripster.engines.tidal import session_refresh_token
            return bool(session_refresh_token())
        except Exception:
            return False
    return False


def use_flex(cfg, svc: str) -> bool:
    """Качать этот сервис через Премиум? premium — всегда (если ключ активен); both — когда своих токенов нет."""
    if svc not in FLEX_DOWNLOAD_SERVICES or not flex_active():
        return False
    mode = str(cfg.get(CONFIG_KEY) or "").strip().lower()
    if mode == "premium":
        return True
    return mode == "both" and not has_own(cfg, svc)
