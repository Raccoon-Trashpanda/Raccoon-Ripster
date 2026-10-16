"""Поиск ПК-версии в режиме «Премиум Ripster»: одноразовые поисковые учётки с сервера владельца.

Правило владельца (10.10.2026): поиск идёт отдельно от рабочих учёток. ПК-клиент просит у сервера
поисковую учётку (`/api/flex/search-lease`, только по ключу Flex) и ищет сам; рабочие учётки нужны
лишь за реальной ссылкой на звук. Своя учётка человека всегда главнее выданной.

Qobuz — токен и app_id уходят в запрос поиска. Tidal — из refresh-токена учётки выпускается
короткоживущий access (общий механизм движка Tidal), страна берётся из выдачи.
Выданные данные живут только в памяти процесса и в логи не попадают.
"""
from __future__ import annotations

import time

from ripster import flex_client, source_mode

_LEASE_TTL_S = 600
_leases: dict[str, tuple[float, dict]] = {}
_tidal_at: dict = {"token": "", "exp": 0.0, "refresh": ""}


def premium_search_on(cfg, service: str) -> bool:
    """Нужно ли брать выданную учётку: режим premium/both, ключ активен, своих токенов сервиса нет."""
    if str(cfg.get(source_mode.CONFIG_KEY) or "").strip().lower() not in ("premium", "both"):
        return False
    return flex_client.is_active() and service in ("qobuz", "tidal")


async def lease(service: str) -> dict:
    """Кэшированная выдача на 10 минут. Пусто — сервер не отдал (нет поисковой учётки или нет связи)."""
    now = time.time()
    hit = _leases.get(service)
    if hit and hit[0] > now:
        return hit[1]
    try:
        cred = await flex_client.search_lease(service)
    except flex_client.FlexClientError:
        cred = {}
    if cred:
        _leases[service] = (now + _LEASE_TTL_S, cred)
    return cred or {}


def forget() -> None:
    _leases.clear()
    _tidal_at.update(token="", exp=0.0, refresh="")


async def qobuz_creds() -> tuple[str, str]:
    """-> (user_auth_token, app_id) поисковой учётки Qobuz; пусто, если не выдана."""
    c = await lease("qobuz")
    return str(c.get("token") or ""), str(c.get("app_id") or "")


async def tidal_token() -> tuple[str, str]:
    """-> (access_token, country) поисковой учётки Tidal; пусто, если не выдана или не освежилась."""
    c = await lease("tidal")
    refresh = str(c.get("refresh") or "")
    country = str(c.get("country") or "US").upper() or "US"
    if not refresh:
        return "", country
    now = time.time()
    if _tidal_at["refresh"] == refresh and _tidal_at["exp"] > now + 60 and _tidal_at["token"]:
        return _tidal_at["token"], country
    from ripster.engines.tidal import _refresh_async
    j, _why, _revoked = await _refresh_async(refresh)
    tok = str((j or {}).get("access_token") or "").strip()
    if not tok:
        return "", country
    ttl = int((j or {}).get("expires_in") or 3600)
    _tidal_at.update(token=tok, exp=now + ttl, refresh=refresh)
    return tok, country


async def tidal_get(c, url: str, params: dict):
    """GET с выданным токеном → (response, country); (None, country), если токена нет.
    На 401 выпускает access заново и повторяет ровно один раз."""
    tok, cc = await tidal_token()
    if not tok:
        return None, cc
    p = dict(params or {})
    p.setdefault("countryCode", cc)
    r = await c.get(url, headers={"Authorization": f"Bearer {tok}"}, params=p)
    if r.status_code == 401:
        _tidal_at.update(token="", exp=0.0)
        tok2, _ = await tidal_token()
        if tok2:
            r = await c.get(url, headers={"Authorization": f"Bearer {tok2}"}, params=p)
    return r, cc


async def cfg_view(cfg, service: str):
    """Конфиг для вызова движка (get_album/get_artist): в режиме Премиум и без своих токенов сервиса
    подставляем выданную поисковую учётку. Исходный словарь не меняется; в остальных случаях — он же."""
    if service not in ("qobuz", "tidal") or not premium_search_on(cfg, service):
        return cfg
    if source_mode.has_own(cfg, service):
        return cfg
    view = dict(cfg)
    if service == "qobuz":
        tok, aid = await qobuz_creds()
        if tok:
            view["qobuz-auth-token"] = tok
            if aid and not str(view.get("qobuz-app-id") or "").strip():
                view["qobuz-app-id"] = aid
    else:
        tok, cc = await tidal_token()
        if tok:
            view["tidal-token"] = tok
            view["tidal-country"] = cc
    return view
