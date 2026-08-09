"""
Auth probe and token import routes.

  POST /api/test-auth/{service}      — verify saved credentials work
  POST /api/import-token/{service}   — import token.json from tidal-dl-ng etc.

Install: auth.install(app, cfg, save_config_fn)
"""
from __future__ import annotations

import json
import time
from datetime import datetime

import httpx
from fastapi import APIRouter, HTTPException

router = APIRouter()

_cfg: dict = {}
_save_config = None


def install(app, ctx) -> None:
    global _cfg, _save_config
    _cfg         = ctx.config
    _save_config = ctx.save_config
    app.include_router(router)


# ── Credential probe ──────────────────────────────────────────────────────────

def _view(overlay: dict | None) -> dict:
    """Config view for a probe: saved config with an optional candidate overlay."""
    return {**_cfg, **overlay} if overlay else _cfg


async def _probe_yandex(overlay: dict | None = None) -> dict:
    token = (_view(overlay).get("yandex-token") or "").strip()
    if not token:
        return {"ok": False, "error_key": "pr.ya_no_token", "error": "Токен не задан — Settings → Яндекс."}
    from fastapi.concurrency import run_in_threadpool

    def _do():
        from yandex_music import Client
        c = Client(token).init()
        acc = c.account_status()
        a = acc.account
        plus = bool(getattr(acc, "plus", None) and getattr(acc.plus, "has_plus", False))
        return {"ok": True, "user": {
            "login":    getattr(a, "login", "") or getattr(a, "display_name", "") or "?",
            "country":  getattr(a, "region", "") or "",
            "lossless": plus,        # Plus = FLAC доступен
            "hq":       not plus,
            "note_key": "pr.ya_plus" if plus else "pr.ya_noplus",
            "note":     "Яндекс Плюс ✓" if plus else "без Плюс — только превью",
        }}

    try:
        return await run_in_threadpool(_do)
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}


async def _probe_amazon(overlay: dict | None = None) -> dict:
    """Amazon Music: download goes through the `amz` CLI with a saved token.

    Previously this only checked "is a token string present" + "is the `amz`
    package importable" — it never actually asked Amazon whether the token
    still works. That made the Setup/services status page show a green
    checkmark for a long-expired token, so guests would pick Amazon, try to
    download, and fail — misleading everyone (found 2026-07-22). Now makes a
    real, cheap `GET /account` call (`amz.api.API.get_account_info`) and
    treats an expired/invalid/banned token as a genuine failure, same as the
    Apple decrypt-path health check."""
    token = (_view(overlay).get("amazon-token") or "").strip()
    if not token:
        return {"ok": False, "error_key": "pr.amz_no_token", "error": "Токен не задан — Settings → 🅰️ Amazon (amz.dezalty.com/login)."}
    from fastapi.concurrency import run_in_threadpool

    def _do():
        import os, importlib.util
        override = (_cfg.get("amazon-cli-path") or "").strip()
        if override:
            # An explicit override is a real exe path → check the file.
            ok_exe, detail = os.path.isfile(override), os.path.basename(override)
        else:
            # Default: _amz_exe() runs `python -c "from amz.cli import main"`, i.e.
            # an argv LIST, not a file path (the old os.path.isfile(list) blew up
            # with "path should be str… not list"). The real requirement is that
            # the `amz` (amazon-music) package is importable.
            ok_exe, detail = (importlib.util.find_spec("amz") is not None), "amz module"
        if not ok_exe:
            return {"ok": False,
                    "error_key": "pr.amz_no_cli", "error": "CLI `amz` не найден — pip install amazon-music "
                             "(или задай amazon-cli-path)."}

        # Real token check — cheap single GET, same client the actual downloader uses.
        try:
            from amz.api import API as _AmzAPI
            from amz.errors import InvalidAccessToken, UserBanned, RateLimitExceeded, ApiConnectionError
            api_url = (_cfg.get("amazon-api-url") or "https://amz.dezalty.com").strip()
            info = _AmzAPI(api_url, token).get_account_info()
        except InvalidAccessToken:
            return {"ok": False, "error_key": "pr.amz_expired", "error": "Токен Amazon протух — обнови в Settings → 🅰️ Amazon "
                                          "(amz.dezalty.com/login)."}
        except UserBanned:
            return {"ok": False, "error_key": "pr.amz_banned", "error": "Amazon-аккаунт заблокирован/забанен — токен не поможет, нужен другой."}
        except RateLimitExceeded:
            # A real, transient rate-limit (429) — the account/token themselves are
            # fine, just throttled right now. Don't flip the whole service red.
            return {"ok": True, "user": {
                "login": "token ✓", "lossless": True, "hq": True,
                "note_key": "pr.amz_429", "note": "Токен задан, но проверка сейчас rate-limited (429) — не про токен.",
            }}
        except ApiConnectionError as e:
            # amz.fetch() raises this for BOTH real network drops AND a bad HTTP
            # response from amz.dezalty.com (5xx / non-JSON) — either way the
            # third-party wrapper itself is down/unreachable right now, which
            # means a real download will fail too regardless of token validity.
            # Reporting this as green (as the old presence-only check did) was
            # exactly what misled everyone into trying and failing — say so
            # honestly instead (found 2026-07-22, amz.dezalty.com was returning
            # a Heroku "Application Error" page at the time).
            return {"ok": False, "error_key": "pr.amz_down", "error_args": {"e": str(e)}, "error": f"amz.dezalty.com сейчас недоступен ({e}) — "
                                          f"не проблема токена, сторонний сервис лежит, попробуй позже."}
        except Exception as e:
            return {"ok": False, "error_key": "pr.amz_check_fail", "error_args": {"e": f"{type(e).__name__}: {e}"}, "error": f"Не удалось проверить токен: {type(e).__name__}: {e}"}

        return {"ok": True, "user": {
            "login":    (info.get("email") or info.get("username") or "token ✓"),
            "lossless": True,         # Master/HD при наличии Unlimited
            "hq":       True,
            "note_key": "pr.amz_ok", "note_args": {"detail": detail},
            "note":     f"Токен проверен вживую, CLI: {detail} ✓",
        }}

    try:
        return await run_in_threadpool(_do)
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}


async def _probe_bbc(overlay: dict | None = None) -> dict:
    """BBC Sounds — ключей не требует вовсе, и раньше проба на него отвечала
    `400 Unsupported service: bbc`. Отсутствие учётных данных это НЕ ошибка
    настройки, и сообщать о нём как об ошибке — вводить в заблуждение.

    Заодно это канарейка: публичный API BBC может тихо умереть, и узнать об
    этом лучше здесь, чем по молчащему радару. Ровно так однажды умер
    RSS-эндпоинт вишлиста, и никто не замечал месяцами.
    """
    try:
        async with httpx.AsyncClient(timeout=10) as c:
            r = await c.get("https://www.bbc.co.uk/programmes/m002vbnb.json")
    except Exception as e:
        return {"ok": False, "error_key": "pr.bbc_down", "error_args": {"e": str(e)}, "error": f"BBC недоступен: {e}"}
    if r.status_code != 200:
        return {"ok": False,
                "error_key": "pr.bbc_http", "error_args": {"code": r.status_code}, "error": f"Публичный API BBC ответил {r.status_code} — "
                         f"это не про твои настройки, ключи ему не нужны."}
    return {"ok": True, "user": {
        "login": "public API", "hq": True,
        "note_key": "pr.bbc_ok", "note": "ключи не нужны — BBC Sounds открыт",
    }}


@router.get("/api/accounts/survey")
async def accounts_survey(fresh: int = 0):
    """Обойти ВСЕ настроенные учётки и сказать про каждую: жива или нет.

    Зачем: пробы существовали для каждого сервиса, но запускались только когда
    человек нажмёт кнопку. То есть обхода не было вовсе — была ручная проверка.
    09.08.2026 это стоило прямых убытков: подписка gamdl кончилась, ARL и токены
    жили неизвестно сколько, и узнавали мы об этом в момент падения загрузки.

    Пробы идут ПАРАЛЛЕЛЬНО: их десяток, а каждая ждёт сеть. Последовательно
    обход занял бы минуту и никто бы его не запускал.

    Учётки, которых нет в конфиге, пропускаем молча: «не настроено» — это не
    поломка, и мешать одно с другим нельзя.
    """
    import asyncio as _aio

    probes = _probes()
    # Что вообще настроено. Ключи те же, что проверяет панель настроек.
    keys = {
        "qobuz":      ("qobuz-auth-token", "qobuz-email"),
        "tidal":      ("tidal-token",),
        "deezer":     ("deezer-arl",),
        "spotify":    ("spotify-client-id", "spotify-sp-dc"),
        "soundcloud": ("soundcloud-oauth-token",),
        "apple":      ("wrapper-apple-id", "media-user-token"),
        "beatport":   ("beatport-username",),
        "yandex":     ("yandex-token",),
        "amazon":     ("amazon-cookies",),
        "bbc":        (),          # без учётных данных
    }

    def _configured(svc: str) -> bool:
        ks = keys.get(svc, ())
        if not ks:
            return True
        for k in ks:
            v = _cfg.get(k)
            if (v.strip() if isinstance(v, str) else v):
                return True
        return False

    todo = [s for s in probes if _configured(s)]

    async def _one(svc: str) -> dict:
        try:
            r = await probes[svc]()
        except Exception as e:
            # Исключение пробы — это НЕ «учётка мертва». Разные диагнозы.
            return {"service": svc, "ok": False, "error": f"проба упала: {type(e).__name__}: {e}"}
        out = {"service": svc, "ok": bool(r.get("ok"))}
        if r.get("error"):
            out["error"] = str(r["error"])[:200]
        u = r.get("user") or {}
        for k in ("country", "storefront", "plan", "offer", "subscription", "expires", "name"):
            if u.get(k):
                out[k] = u[k]
        return out

    rows = await _aio.gather(*(_one(s) for s in todo))
    rows = sorted(rows, key=lambda x: (x["ok"], x["service"]))
    dead = [r["service"] for r in rows if not r["ok"]]
    return {"checked": len(rows), "dead": dead, "accounts": rows}


def _probes() -> dict:
    """Все пробы сервисов одним словарём.

    Вынесена из обработчика: обход учёток (`/api/accounts/survey`) обязан ходить
    теми же пробами, что и кнопка «проверить». Две разные реализации проверки —
    это две разные правды о том, жива ли учётка.
    """
    return {
        "qobuz":      _probe_qobuz,
        "tidal":      _probe_tidal,
        "deezer":     _probe_deezer,
        "spotify":    _probe_spotify,
        "soundcloud": _probe_soundcloud,
        "apple":      _probe_apple,
        "beatport":   _probe_beatport,
        "yandex":     _probe_yandex,
        "amazon":     _probe_amazon,
        "bbc":        _probe_bbc,
    }


@router.post("/api/test-auth/{service}")
async def test_auth(service: str, body: dict | None = None):
    """Probe a service's API with saved credentials. Returns structured info:

    On success  ``{"ok": True,  "user": {...}, "note": "..."}``
    On failure  ``{"ok": False, "error": "human-readable message"}``

    Optional JSON body = CANDIDATE credentials overlay: config keys (whitelist-
    checked) probed ON TOP of the saved config WITHOUT persisting anything.
    Lets a caller (bot token wizard, UI) validate a pasted token BEFORE saving,
    so a dead candidate never clobbers a working credential.

    Never raises HTTPException for auth failures — an auth failure IS the
    normal response. Only returns non-200 for truly broken inputs
    (unknown service, missing httpx, etc).
    """
    s = service.lower()
    fn = _probes().get(s)
    if fn is None:
        raise HTTPException(400, f"Unsupported service: {service}")
    overlay: dict | None = None
    if isinstance(body, dict) and body:
        from ripster.security import config_key_allowed as _allowed
        overlay = {k: str(v) for k, v in body.items()
                   if isinstance(k, str) and _allowed(k) and isinstance(v, (str, int))}
        overlay = overlay or None
    try:
        result = await fn(overlay)
    except HTTPException:
        raise
    except Exception as e:
        print(f"[{s}] probe crashed: {type(e).__name__}: {e}", flush=True)
        return {"ok": False, "error": f"Probe crashed: {type(e).__name__}: {e}"}
    if not result.get("ok"):
        print(f"[{s}] probe failed: {(result.get('error') or '?')[:200]}", flush=True)
    elif overlay is None:
        # Каждая проба УЖЕ узнаёт страну аккаунта и окно подписки, но раньше это
        # жило одной строчкой рядом с кнопкой и умирало вместе с ней: обзор
        # аккаунтов писал «страна не определена» у всех, кроме Tidal (его страну
        # сохранял фронт). Складываем в config — обзор остаётся дешёвым (без
        # сети), а данные переживают перезагрузку. Только для СОХРАНЁННЫХ
        # учёток: overlay — это кандидат на проверке, ему писать в конфиг нечего.
        _remember_probe(s, result.get("user") or {})
    return result


def _remember_probe(svc: str, user: dict) -> None:
    """Запомнить страну и срок подписки, которые вернула проба сервиса."""
    import time as _time
    changed = {}
    cc = str(user.get("country") or "").strip().upper()
    if len(cc) == 2 and cc.isalpha():
        changed[f"{svc}-country"] = cc
    end = str(user.get("sub_end") or user.get("expiry") or "").strip()
    if end:
        changed[f"{svc}-sub-end"] = end[:10]
    dl = user.get("sub_days_left")
    if isinstance(dl, int):
        changed[f"{svc}-sub-days-left"] = dl
    if isinstance(user.get("sub_active"), bool):
        # Apple даты окончания не отдаёт вовсе — только «активна/нет». Это тоже
        # ответ, и он должен доезжать до обзора.
        changed[f"{svc}-sub-active"] = user["sub_active"]
    if changed:
        changed[f"{svc}-checked-at"] = int(_time.time())
        for k, v in changed.items():
            _cfg[k] = v
        if _save_config:
            try:
                _save_config(_cfg)
            except Exception as e:
                print(f"[{svc}] could not persist probe result: {e}", flush=True)


def _qobuz_user_block(user: dict, fallback_id: str = "") -> dict:
    user  = user or {}
    creds = user.get("credential") or {}
    # `parameters` can be JSON null (free accounts) — `.get("parameters", {})`
    # then returns None (default only applies when the key is ABSENT), so guard
    # with `or {}` to avoid `NoneType.get` crashing the probe.
    params = creds.get("parameters") or {}
    # streamrip raises IneligibleError when `credential.parameters` is falsy
    # (qobuz.py:193). Empty params == free account / no active subscription ==
    # downloads impossible, even though the token itself is valid.
    eligible = bool(params)

    # Subscription window — present (non-null) only on paid accounts. Qobuz puts
    # the period under user.subscription; some accounts also echo it in params.
    sub      = user.get("subscription") or {}
    sub_end  = sub.get("end_date") or params.get("end_date") or ""
    sub_start = sub.get("start_date") or ""
    offer    = (sub.get("offer") or creds.get("label")
                or creds.get("description") or params.get("short_label") or "")
    days_left = None
    expired   = False
    if sub_end:
        try:
            from datetime import date as _date
            y, m, d = (int(x) for x in str(sub_end)[:10].split("-"))
            days_left = (_date(y, m, d) - _date.today()).days
            expired   = days_left < 0
        except Exception:
            pass

    return {
        "id":           user.get("id", fallback_id),
        "login":        user.get("login") or user.get("email") or "?",
        "country":      user.get("country_code") or user.get("country") or "?",
        "zone":         user.get("zone") or "",
        "subscription": offer or "?",
        "hires":        bool(params.get("hires_streaming")),
        "lossless":     bool(params.get("lossless_streaming")),
        "eligible":     eligible,
        "sub_offer":    offer,
        "sub_start":    sub_start,
        "sub_end":      sub_end,            # "YYYY-MM-DD" or ""
        "sub_days_left": days_left,         # int or None (None = unknown / free)
        "sub_expired":  expired,
    }


def _qobuz_eligibility_error(user_block: dict) -> dict | None:
    """If the Qobuz token is valid but the account has no active subscription,
    return a probe result that fails loudly (matching streamrip's
    IneligibleError) instead of a misleading green check."""
    if user_block.get("eligible"):
        return None
    end = user_block.get("sub_end") or ""
    if end and user_block.get("sub_expired"):
        why = f"подписка Qobuz истекла {end}. Продли или вставь токен активного аккаунта."
    elif end:
        why = (f"подписка числится до {end}, но параметры стриминга пусты — "
               "Qobuz считает аккаунт неактивным.")
    else:
        why = ("у аккаунта нет активной подписки Qobuz (credential.parameters пуст). "
               "Нужна платная подписка Qobuz.")
    return {
        "ok": False,
        "user": user_block,
        "error": ("Токен валиден, НО " + why + " Скачивание невозможно — streamrip "
                  "упадёт с IneligibleError «Free accounts are not eligible to "
                  "download tracks»."),
    }


async def _probe_qobuz(overlay: dict | None = None) -> dict:
    """Validate Qobuz credentials. Supports both modes:
      A. user-id + user-auth-token  (cookie token)
      B. email + password           (logs in, then auto-saves the captured
         user-id + token so downloads use the reliable token path).
    """
    cfg = _view(overlay)
    user_id    = str(cfg.get("qobuz-user-id")    or "").strip()
    auth_token = str(cfg.get("qobuz-auth-token") or "").strip()
    email      = str(cfg.get("qobuz-email")      or "").strip()
    password   = str(cfg.get("qobuz-password")   or "").strip()
    custom_app = str(cfg.get("qobuz-app-id")     or "").strip()
    app_id     = custom_app or "312369995"

    if user_id and auth_token:
        res = await _qobuz_probe_token(user_id, auth_token, app_id)
        # Протухший токен НАВСЕГДА заслонял рабочие email+пароль: до ветки входа
        # ниже дело просто не доходило, и Qobuz лежал с 401, пока владелец не
        # обновит куку руками. Хуже того, сохранённая пара id+токен может быть
        # вообще от другого аккаунта (28.07.2026: в конфиге лежал id 11384795,
        # а вход по паролю отдавал 13637057). Раз пароль есть — перелогиниваемся
        # и перезаписываем пару; вход сам сохраняет свежий токен.
        if not res.get("ok") and email and password:
            fresh = await _qobuz_probe_password(email, password, app_id)
            if fresh.get("ok"):
                fresh["note_key"] = "pr.qb_relogged"
                fresh["note"] = "токен протух — перевошёл по паролю и обновил его"
                return fresh
        return res
    if email and password:
        return await _qobuz_probe_password(email, password, app_id)
    return {"ok": False,
            "error_key": "pr.qb_need_creds", "error": "Заполни user-id + user-auth-token ИЛИ email + пароль "
                     "в Settings → Qobuz."}


async def _qobuz_probe_token(user_id: str, auth_token: str, app_id: str) -> dict:
    try:
        async with httpx.AsyncClient(timeout=10) as c:
            r = await c.post(
                "https://www.qobuz.com/api.json/0.2/user/login",
                params={
                    "user_id":         user_id,
                    "user_auth_token": auth_token,
                    "app_id":          app_id,
                },
            )
    except Exception as e:
        return {"ok": False, "error_key": "pr.net", "error_args": {"e": str(e)}, "error": f"Сеть: {e}"}

    try:
        data = r.json()
    except Exception:
        return {"ok": False,
                "error_key": "pr.qb_not_json", "error_args": {"code": r.status_code}, "error": f"Qobuz ответил {r.status_code}, но не JSON (возможно блокировка)."}

    if r.status_code == 401:
        qerr = (data.get("message") or "Invalid credentials").strip()
        return {"ok": False,
                "error_key": "pr.qb_401", "error_args": {"msg": qerr}, "error": f"401 от Qobuz: {qerr}. "
                         "Проверь что user-auth-token из cookies на play.qobuz.com "
                         "и что токен не протух (обнови страницу и сними заново)."}
    if r.status_code == 400:
        return {"ok": False,
                "error_key": "pr.qb_400", "error": "400 от Qobuz: неверный app_id. "
                         "Очисти поле App ID в Settings → Qobuz → advanced, "
                         "чтобы использовать дефолтный streamrip."}
    if r.status_code != 200:
        return {"ok": False,
                "error_key": "pr.qb_http", "error_args": {"code": r.status_code, "body": str(data)[:120]},
                "error": f"Qobuz вернул HTTP {r.status_code}: {data}"}

    user_block = _qobuz_user_block(data.get("user") or {}, user_id)
    ineligible = _qobuz_eligibility_error(user_block)
    if ineligible:
        ineligible["app_id_used"] = app_id
        return ineligible
    return {
        "ok": True,
        "user": user_block,
        "app_id_used": app_id,
    }


async def _qobuz_probe_password(email: str, password: str, app_id: str) -> dict:
    """Log in to Qobuz with email + MD5(password). On success the captured
    user-id + user_auth_token are persisted so the engine uses token mode."""
    import hashlib
    pwd_md5 = hashlib.md5(password.encode()).hexdigest()
    try:
        async with httpx.AsyncClient(timeout=10) as c:
            r = await c.get(
                "https://www.qobuz.com/api.json/0.2/user/login",
                params={"email": email, "password": pwd_md5, "app_id": app_id},
                headers={"X-App-Id": app_id},
            )
    except Exception as e:
        return {"ok": False, "error_key": "pr.net", "error_args": {"e": str(e)}, "error": f"Сеть: {e}"}

    try:
        data = r.json()
    except Exception:
        return {"ok": False,
                "error_key": "pr.qb_not_json2", "error_args": {"code": r.status_code}, "error": f"Qobuz ответил {r.status_code}, но не JSON "
                         "(возможно блокировка или капча)."}

    if r.status_code == 401:
        qerr = (data.get("message") or "").strip()
        return {"ok": False,
                "error_key": "pr.qb_401_pw", "error_args": {"msg": qerr}, "error": f"401 от Qobuz: неверный email или пароль{(' — ' + qerr) if qerr else ''}."}
    if r.status_code == 400:
        return {"ok": False,
                "error_key": "pr.qb_400b", "error": "400 от Qobuz: неверный app_id — очисти поле App ID "
                         "в Settings → Qobuz → advanced."}
    if r.status_code != 200:
        return {"ok": False,
                "error_key": "pr.qb_http", "error_args": {"code": r.status_code, "body": str(data)[:120]},
                "error": f"Qobuz вернул HTTP {r.status_code}: {data}"}

    token = (data.get("user_auth_token") or "").strip()
    user  = data.get("user") or {}
    if not token or not user.get("id"):
        return {"ok": False,
                "error_key": "pr.qb_no_token", "error": "Qobuz принял вход, но не вернул токен — "
                         "возможно у аккаунта нет активной подписки."}

    # Auto-promote: persist user-id + token so downloads use the reliable
    # token path instead of re-logging-in with the password every run.
    _cfg["qobuz-user-id"]    = str(user.get("id"))
    _cfg["qobuz-auth-token"] = token
    if _save_config:
        try:
            _save_config(_cfg)
        except Exception:
            pass

    user_block = _qobuz_user_block(user, str(user.get("id")))
    ineligible = _qobuz_eligibility_error(user_block)
    if ineligible:
        ineligible["app_id_used"] = app_id
        ineligible["note_key"] = "pr.qb_no_sub"
        ineligible["note"] = "email/пароль приняты, но подписка не активна"
        return ineligible
    return {
        "ok": True,
        "user": user_block,
        "note_key": "pr.qb_saved", "note": "email/пароль приняты — user-id и токен сохранены автоматически",
        "app_id_used": app_id,
    }


async def _tidal_refresh_token() -> str:
    """Use stored refresh_token to mint a fresh access_token. Persists on success.
    Returns the new access_token, or "" on failure."""
    refresh = (_cfg.get("tidal-refresh") or "").strip()

    # FIRST: the OrpheusDL TV session, which is what actually works.
    # A refresh token belongs to the client that issued it — a TV-issued token
    # refreshed with a web client_id gets "This token was issued to another
    # client" (verified live 2026-07-27, all three web ids below failed, and
    # zU4XHVVkc3hFw1lb no longer exists at all: "Client with token not found").
    # The engine keeps a TV session that refreshes correctly with client_id +
    # SECRET, and downloads already rely on it — so reuse it instead of writing
    # a dead token into the config.
    try:
        from ripster.engines import tidal as _tid_engine
        tok, cc = await _tid_engine._orpheus_access_token()
        if tok:
            _cfg["tidal-token"] = tok
            if cc:
                _cfg["tidal-country"] = cc
            if _save_config:
                try: _save_config(_cfg)
                except Exception: pass
            return tok
    except Exception:
        pass

    if not refresh:
        return ""
    # Fallback: web-client refresh. Only works if the refresh token was itself
    # issued by one of these clients (i.e. a browser login, not the TV flow).
    for client_id in ("zU4XHVVkc3hFw1lb", "CzET4vdadNUFQ5JU", "aR7gUaTK1ihpXOEP"):
        try:
            async with httpx.AsyncClient(timeout=10) as c:
                r = await c.post(
                    "https://auth.tidal.com/v1/oauth2/token",
                    data={
                        "grant_type":    "refresh_token",
                        "refresh_token": refresh,
                        "client_id":     client_id,
                    },
                )
        except Exception:
            continue
        if r.status_code != 200:
            continue
        try:
            j = r.json()
        except Exception:
            continue
        access = (j.get("access_token") or "").strip()
        if not access:
            continue
        new_refresh = (j.get("refresh_token") or refresh).strip()
        exp = j.get("expires_in")
        _cfg["tidal-token"]   = access
        _cfg["tidal-refresh"] = new_refresh
        if isinstance(exp, int):
            _cfg["tidal-token-expiry"] = str(int(time.time()) + exp)
        if _save_config:
            try: _save_config(_cfg)
            except Exception: pass
        return access
    return ""


async def _probe_tidal(overlay: dict | None = None) -> dict:
    cfg = _view(overlay)
    candidate = bool(overlay)   # probing a pasted token → no refresh, no orpheus shortcut
    token   = (cfg.get("tidal-token")   or "").strip()
    user_id = (cfg.get("tidal-user-id") or "").strip()
    country = (cfg.get("tidal-country") or "US").strip().upper() or "US"

    # Downloads now run through OrpheusDL's self-refreshing session, NOT the
    # pasted access_token (which is only used for search/metadata and dies in
    # ~16 h). So Tidal counts as "available" whenever an OrpheusDL session
    # exists — even if the metadata access_token probe below fails. Otherwise a
    # stale metadata token would hide Tidal and the OrpheusDL path never runs.
    try:
        from ripster.engines.tidal import is_authenticated as _orph_tidal_authed
        _orph_ready = (not candidate) and bool(_orph_tidal_authed())
    except Exception:
        _orph_ready = False

    def _ok_via_orpheus(reason: str, key: str) -> dict:
        return {
            "ok": True,
            "via": "orpheus",
            "note_key": key,
            "note": reason,
            "user": {"id": user_id or "?", "login": "OrpheusDL session",
                     "country": country, "name": "Tidal (OrpheusDL)"},
        }

    if not token:
        # Try to mint one from the refresh_token before giving up.
        token = "" if candidate else await _tidal_refresh_token()
        if not token:
            if _orph_ready:
                return _ok_via_orpheus("access_token не задан — качаю через OrpheusDL-сессию",
                                       "pr.td_orpheus_notoken")
            return {"ok": False,
                    "error_key": "pr.td_no_token", "error": "Не заполнен access_token в Settings → Tidal."}

    async def _do_probe(t: str) -> "tuple[int, dict|str]":
        try:
            async with httpx.AsyncClient(timeout=10) as c:
                h = {"Authorization": f"Bearer {t}"}
                nonlocal user_id, country
                if not user_id:
                    rs = await c.get("https://api.tidal.com/v1/sessions", headers=h)
                    if rs.status_code == 200:
                        s = rs.json()
                        user_id = str(s.get("userId") or "")
                        country = (s.get("countryCode") or country).upper()
                    else:
                        return rs.status_code, rs.text[:200]
                if not user_id:
                    return 0, "no user_id"
                rr = await c.get(
                    f"https://api.tidal.com/v1/users/{user_id}",
                    headers=h,
                    params={"countryCode": country},
                )
                return rr.status_code, (rr.json() if rr.status_code == 200 else rr.text[:200])
        except Exception as e:
            return -1, str(e)

    code, payload = await _do_probe(token)

    if code == 401 and not candidate:
        # Try one auto-refresh and retry exactly once before reporting failure.
        new_tok = await _tidal_refresh_token()
        if new_tok:
            token = new_tok
            user_id = (_cfg.get("tidal-user-id") or "").strip()
            country = (_cfg.get("tidal-country") or country).strip().upper() or country
            code, payload = await _do_probe(token)

    # Metadata token failed, but OrpheusDL session can still download → available.
    if code != 200 and _orph_ready:
        return _ok_via_orpheus("metadata-токен истёк, но качаю через OrpheusDL-сессию",
                               "pr.td_orpheus_stale")

    if code == -1:
        return {"ok": False, "error_key": "pr.net", "error_args": {"e": str(payload)}, "error": f"Сеть: {payload}"}
    if code == 0:
        return {"ok": False, "error_key": "pr.td_no_user", "error": "Не удалось получить user_id из сессии."}
    if code == 401:
        return {"ok": False,
                "error_key": "pr.td_401", "error": "401 от Tidal: access_token истёк, refresh не сработал. "
                         "Обнови токены на listen.tidal.com → DevTools → Local Storage."}
    if code != 200:
        return {"ok": False, "error": f"Tidal HTTP {code}: {payload}"}

    u = payload if isinstance(payload, dict) else {}
    return {
        "ok": True,
        "user": {
            "id":      user_id,
            "login":   u.get("username") or u.get("email") or "?",
            "country": country,
            "name":    f"{u.get('firstName','')} {u.get('lastName','')}".strip() or "?",
        },
    }


async def _probe_deezer(overlay: dict | None = None) -> dict:
    arl = (_view(overlay).get("deezer-arl") or "").strip()
    if not arl:
        return {"ok": False, "error_key": "pr.dz_no_arl", "error": "Не заполнен ARL в Settings → Deezer."}

    try:
        async with httpx.AsyncClient(timeout=10, cookies={"arl": arl}) as c:
            r = await c.post(
                "https://www.deezer.com/ajax/gw-light.php",
                params={"method": "deezer.getUserData", "api_version": "1.0", "api_token": ""},
            )
    except Exception as e:
        return {"ok": False, "error_key": "pr.net", "error_args": {"e": str(e)}, "error": f"Сеть: {e}"}

    try:
        data = r.json()
    except Exception:
        return {"ok": False, "error_key": "pr.dz_not_json", "error_args": {"body": r.text[:200]}, "error": f"Deezer вернул не-JSON: {r.text[:200]}"}

    results = data.get("results") or {}
    user    = results.get("USER") or {}
    if not user.get("USER_ID"):
        return {"ok": False,
                "error_key": "pr.dz_bad_arl", "error": "ARL недействителен или истёк. "
                         "Обнови ARL в cookies на deezer.com."}

    # Options live under results.USER.OPTIONS (the top-level USER_OPTIONS key is
    # empty/partial). Country is results.COUNTRY (Deezer's account region, same
    # source deemix reads), with license_country / USER.COUNTRY as fallbacks.
    opts = user.get("OPTIONS") or {}
    country = (results.get("COUNTRY")
               or opts.get("license_country")
               or user.get("COUNTRY") or "?")
    return {
        "ok": True,
        "user": {
            "id":       user.get("USER_ID"),
            "login":    user.get("BLOG_NAME") or user.get("EMAIL") or "?",
            "country":  country,
            "hq":       bool(opts.get("web_hq")),
            "lossless": bool(opts.get("web_lossless")),
        },
    }


async def _probe_spotify(overlay: dict | None = None) -> dict:
    """What "Spotify works" actually means for Ripster — and what it does NOT.

    Until 31.07.2026 this probe tested exactly ONE thing: the optional Developer-
    App OAuth token against ``api.spotify.com/v1/me``. That is the one Spotify
    path Ripster cannot rely on:

      * ``/v1`` answers our web tokens with a permanent 429 (see the
        ripster-spotify-tokens skill) — waiting never clears it;
      * a Developer App owned by a non-Premium account is refused outright
        ("Your application is blocked from accessing the Web API since you do
        not have a Spotify Premium subscription");
      * and none of it is involved in a single download.

    So a user who had signed in properly — durable librespot session, downloads
    working — got "Не авторизован. Нажми «Подключить»" every time, and the only
    way to make the probe green was to go create a Developer App that Ripster
    has no use for. Now the probe asks the components that actually do the work,
    in the order they matter, and only falls back to the dev-app OAuth last.
    """
    cfg = _view(overlay)

    # 1. The download session (librespot). This is what makes Spotify usable.
    kind = ""
    try:
        from ripster.engines.orpheus_spotify import session_kind
        kind = session_kind()
    except Exception:
        pass

    # 2. The web-player Bearer the keeper mints from that same session — what the
    #    radar/metadata actually query (api-partner, never rate-limited for us).
    bearer_fresh = False
    try:
        from ripster.routes.spotify import _sp_minted_bearer
        bearer_fresh = bool(_sp_minted_bearer())
    except Exception:
        pass

    if kind == "blob":
        note = ("сессия Spotify (librespot) активна"
                + (" · web-токен свежий" if bearer_fresh else ""))
        return {"ok": True, "via": "librespot",
                "user": {"login": "Spotify session", "subscription": "Premium (OGG)",
                         "hq": True,
                         "note_key": "pr.sp_blob_fresh" if bearer_fresh else "pr.sp_blob",
                         "note": note}}

    if kind == "oauth":
        # Signed in, but with the WEAK credential: metadata-grade OAuth, no
        # durable librespot blob. Downloads may work and may die at any moment
        # when Spotify revokes the token. Say so instead of a flat green ✓.
        return {"ok": True, "via": "pkce", "user": {
            "login": "PKCE-сессия", "hq": True,
            "note_key": "pr.sp_pkce_weak",
            "note": ("вход есть, но это слабая PKCE-сессия — Spotify её периодически "
                     "отзывает. Нажми «🎧 Войти в Spotify» (первый блок вкладки) в Settings → Spotify, "
                     "чтобы получить постоянную сессию."),
        }}

    if bearer_fresh:
        return {"ok": True, "via": "web-token", "user": {
            "login": "web-токен", "hq": True,
            "note_key": "pr.sp_meta_only", "note": "качать нечем (нет сессии), но метаданные и радар работают",
        }}

    if (cfg.get("spotify-sp-dc") or "").strip():
        return {"ok": True, "via": "sp_dc", "user": {
            "login": "sp_dc", "hq": True,
            "note_key": "pr.sp_spdc_only", "note": "есть только кука sp_dc — хватает радару, для загрузок нужен вход",
        }}

    # 3. Last resort: the optional Developer-App OAuth. Nothing downloads through
    #    it; it only ever mattered for the legacy /v1 release scan.
    try:
        from ripster.routes.spotify import get_access_token as _sp_token
    except ImportError:
        return {"ok": False, "error_key": "pr.sp_no_module", "error": "Модуль Spotify не загружен."}

    token = await _sp_token()
    if not token:
        return {"ok": False,
                "error_key": "pr.sp_no_login", "error": "Нет входа в Spotify. Settings → Spotify → «🎧 Войти в Spotify» (первый блок вкладки) "
                         "(логин/пароль Spotify, Developer App и Client ID НЕ нужны)."}

    try:
        async with httpx.AsyncClient(timeout=10) as c:
            r = await c.get(
                "https://api.spotify.com/v1/me",
                headers={"Authorization": f"Bearer {token}"},
            )
    except Exception as e:
        return {"ok": False, "error_key": "pr.net", "error_args": {"e": str(e)}, "error": f"Сеть: {e}"}

    if r.status_code == 401:
        return {"ok": False,
                "error_key": "pr.sp_expired", "error": "Токен Spotify истёк — переподключись в Settings → Spotify."}
    if r.status_code in (403, 429):
        return {"ok": False, "error": _spotify_devapi_hint(r)}
    if r.status_code != 200:
        return {"ok": False, "error": f"Spotify API → HTTP {r.status_code}"}

    u = r.json()
    product = u.get("product", "")
    sub_map = {"premium": "Premium", "free": "Free", "open": "Free"}
    return {
        "ok": True,
        "user": {
            "login":        u.get("display_name") or u.get("id") or "?",
            "country":      u.get("country") or "?",
            "subscription": sub_map.get(product, product or "?"),
        },
    }


def _spotify_devapi_hint(r) -> str:
    """Turn the two dead-end Developer-App answers into something actionable.

    Both look like a Ripster failure and are not: Spotify refuses Web-API access
    to apps owned by non-Premium accounts, and it permanently 429s our web
    tokens on /v1. Neither blocks downloading — that path doesn't touch /v1."""
    body = ""
    try:
        body = (r.json().get("error", {}) or {}).get("message", "") or ""
    except Exception:
        body = (r.text or "")[:200]
    low = body.lower()
    if "premium" in low:
        return ("Spotify не пускает Developer App без Premium у владельца приложения "
                f"({body}). Это НЕ нужно для скачивания: Ripster качает через вход "
                "«🎧 Войти в Spotify» (первый блок вкладки), а не через Developer App. Client ID/Secret "
                "можно вообще не заполнять.")
    if r.status_code == 429:
        return ("Spotify держит api.spotify.com/v1 в постоянном 429 для наших токенов — "
                "ожидание не помогает и на скачивание не влияет. Используй вход "
                "«🎧 Войти в Spotify» (первый блок вкладки) в Settings → Spotify.")
    return (f"Spotify Developer API → HTTP {r.status_code}: {body or '—'}. "
            "На скачивание не влияет — оно идёт через вход «🎧 Войти в Spotify» (первый блок вкладки).")


async def _probe_soundcloud(overlay: dict | None = None) -> dict:
    token = (_view(overlay).get("soundcloud-oauth-token") or "").strip()
    if not token:
        return {"ok": False,
                "error_key": "pr.sc_no_token", "error": "Не заполнен OAuth токен в Settings → SoundCloud."}

    try:
        async with httpx.AsyncClient(timeout=10) as c:
            r = await c.get(
                "https://api-v2.soundcloud.com/me",
                headers={"Authorization": f"OAuth {token}"},
            )
    except Exception as e:
        return {"ok": False, "error_key": "pr.net", "error_args": {"e": str(e)}, "error": f"Сеть: {e}"}

    if r.status_code == 401:
        return {"ok": False,
                "error_key": "pr.sc_bad_token", "error": "OAuth токен недействителен или истёк. Обнови в DevTools → Local Storage."}
    if r.status_code != 200:
        return {"ok": False, "error": f"SoundCloud API → HTTP {r.status_code}"}

    u    = r.json()
    # consumer_subscription is the actual Go+ field (not "subscription")
    prod = (
        (u.get("consumer_subscription") or {}).get("product")
        or (u.get("subscription") or {}).get("product")
        or {}
    )
    plan_id   = prod.get("id", "")
    _LABEL = {
        "consumer-high-tier": "Go+",
        "consumer-mid-tier":  "Go",
        "soundcloud-go-plus": "Go+",
        "soundcloud-go":      "Go",
    }
    plan_name = _LABEL.get(plan_id, prod.get("name", ""))

    free_ids  = {"", "free", "free-tier", "trial", "free-v01"}
    has_go    = (plan_id.lower() not in free_ids and bool(plan_id)) or u.get("go_plus", False)
    sub_label = plan_name or (plan_id if plan_id else "") or ("Go+" if has_go else "Free (128 kbps)")
    if not sub_label:
        sub_label = "Free (128 kbps)"
    print(f"[soundcloud] probe sub: plan_id={plan_id!r} plan_name={plan_name!r} "
          f"go_plus_field={u.get('go_plus')} → {sub_label}", flush=True)
    return {
        "ok": True,
        "user": {
            "login":        u.get("username") or "?",
            "country":      u.get("country_code") or u.get("country") or "?",
            "subscription": sub_label,
        },
    }


async def _probe_apple(overlay: dict | None = None) -> dict:
    cfg = _view(overlay)
    mut       = (cfg.get("media-user-token")    or "").strip()
    bearer    = (cfg.get("authorization-token") or "").strip()
    storefront = (cfg.get("storefront") or "us").strip().lower()

    if not mut:
        return {"ok": False,
                "error_key": "pr.ap_no_mut", "error": "Не заполнен media-user-token в Settings → Apple Music → Токены."}
    if not bearer:
        # Try server-side auto-fetch from music.apple.com JS — no browser needed.
        try:
            from ripster.metadata.apple import auto_fetch_bearer
            bearer = (await auto_fetch_bearer()) or ""
            if bearer and not overlay:      # candidate probe must not persist
                _cfg["authorization-token"] = bearer
                if _save_config:
                    try: _save_config(_cfg)
                    except Exception: pass
        except Exception as _e:
            pass
        if not bearer:
            return {"ok": False,
                    "error_key": "pr.ap_no_bearer", "error": "Bearer не получен авто-фетчем. "
                             "Открой /apple/login и проверь токен вручную."}

    try:
        async with httpx.AsyncClient(timeout=10) as c:
            r = await c.get(
                "https://api.music.apple.com/v1/me/storefront",
                headers={
                    "Authorization":    f"Bearer {bearer}",
                    "Media-User-Token": mut,
                    # The web Bearer is origin-bound (JWT root_https_origin=apple.com).
                    # Without these headers Apple returns 401 even for valid tokens.
                    "Origin":  "https://music.apple.com",
                    "Referer": "https://music.apple.com/",
                },
            )
    except Exception as e:
        return {"ok": False, "error_key": "pr.net", "error_args": {"e": str(e)}, "error": f"Сеть: {e}"}

    if r.status_code == 401:
        return {"ok": False,
                "error_key": "pr.ap_bad_tokens", "error": "Токены недействительны. Обнови MUT и Bearer в Settings → Apple Music."}
    if r.status_code == 403:
        return {"ok": False,
                "error_key": "pr.ap_403", "error": "403 от Apple — MUT протух или неверный storefront."}
    if r.status_code != 200:
        return {"ok": False, "error": f"Apple Music API → HTTP {r.status_code}"}

    data  = r.json().get("data", [{}])[0]
    sf_id = (data.get("id") or storefront).upper()

    # A valid media-user-token/bearer only proves METADATA access (search,
    # catalog lookups) — it says nothing about whether either decrypt path
    # can actually deliver a download right now. Without this check the probe
    # (and therefore the bot's svc_unavailable gate) reports Apple "ok" while
    # every real download dies downstream — exactly what happened 2026-07-22
    # while the local wrapper was stuck on Apple's own device-limit lease
    # error. Check both decrypt paths the router can pick between; either one
    # being healthy is enough (zhaarey uses the local docker wrapper, AMD
    # uses the public wm.wol.moe-style wrapper).
    decrypt_ok, decrypt_note, (why_key, why_args) = await _apple_decrypt_path_ok()
    if not decrypt_ok:
        return {"ok": False,
                # Причина вложена в общую фразу, поэтому у неё свой ключ — иначе
                # переводу достался бы только каркас, а суть осталась русской.
                "error_key": "pr.ap_no_decrypt",
                "error_args": {"why_key": why_key, "why_args": why_args, "why": decrypt_note},
                "error": f"Метаданные доступны, но расшифровка недоступна: {decrypt_note}"}

    # Подписка. Возраст cookies.txt и сроки самих кук про неё НЕ говорят ничего —
    # единственный источник правды у Apple это /v1/me/account?meta=subscription
    # (01.08.2026: cookies отвечали 200, а подписка кончилась). Best-effort:
    # ошибка здесь не должна ронять пробу, у которой всё остальное сошлось.
    sub = {}
    try:
        async with httpx.AsyncClient(timeout=10) as c:
            sr = await c.get(
                "https://api.music.apple.com/v1/me/account?meta=subscription",
                headers={"Authorization": f"Bearer {bearer}", "Media-User-Token": mut,
                         "Origin": "https://music.apple.com",
                         "Referer": "https://music.apple.com/"},
            )
        if sr.status_code == 200:
            meta = (sr.json().get("meta") or {}).get("subscription") or {}
            sub["sub_active"] = bool(meta.get("active"))
            for k in ("expirationDate", "endDate", "expiration", "renewalDate"):
                if meta.get(k):
                    sub["sub_end"] = str(meta[k])[:10]
                    break
    except Exception as e:
        print(f"[apple] subscription meta unavailable: {e}", flush=True)

    return {
        "ok": True,
        "user": {
            "country": sf_id,
            "lossless": True,
            "hires":    True,
            "note_key": why_key, "note_args": why_args,
            "note": decrypt_note,
            **sub,
        },
    }


async def _apple_decrypt_path_ok() -> tuple[bool, str, tuple[str, dict]]:
    """True if AT LEAST ONE Apple decrypt path (local zhaarey wrapper OR the
    public AMD wrapper) looks usable right now. Best-effort — any internal
    error here counts as "can't confirm", not a hard failure of the probe
    itself, so a broken health-check never masks otherwise-working tokens.

    Returns `(ok, русский текст, (ключ, аргументы))`. Текст остаётся для бота и
    healthcheck, ключ — для интерфейса, который говорит на своём языке."""
    local_ok = False
    try:
        from ripster.amd import check_wrapper_running
        local_ok = await check_wrapper_running()
    except Exception:
        pass
    if local_ok:
        return True, "локальный wrapper готов", ("pr.ap_wrap_local", {})

    try:
        from ripster.amd import amd_wrapper_status
        instance = _cfg.get("amd-instance-url", "wm.wol.moe")
        secure   = _cfg.get("amd-instance-secure", True)
        r = await amd_wrapper_status(instance, secure)
        if r.get("ready"):
            n = r.get("client_count", 0)
            return True, f"публичный AMD-wrapper готов ({n} клиентов)", ("pr.ap_wrap_pub", {"n": n})
        if "error" in r:
            return (False, f"локальный wrapper не отвечает, публичный: {r['error']}",
                    ("pr.ap_wrap_pub_err", {"e": str(r["error"])}))
        return (False, "локальный wrapper не отвечает, публичный не готов (0 клиентов/регионов)",
                ("pr.ap_wrap_none", {}))
    except Exception as e:
        return (False, f"локальный wrapper не отвечает, публичный не проверить: {type(e).__name__}",
                ("pr.ap_wrap_unknown", {"e": type(e).__name__}))


async def _probe_beatport(overlay: dict | None = None) -> dict:
    cfg = _view(overlay)
    username = (cfg.get("beatport-username") or "").strip()
    password = (cfg.get("beatport-password") or "").strip()
    if not username or not password:
        return {"ok": False,
                "error_key": "pr.bp_no_creds", "error": "Не заполнены email/пароль в Settings → Beatport."}

    API         = "https://api.beatport.com/v4/"
    CLIENT_ID   = "Zy2K9Wvy6DkUds7g8s1GNMHfk17E5Ch2BWHlyaGY"  # Serato DJ Lite
    REDIRECT    = "seratodjlite://beatport"
    UA          = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")

    def _err(step: str, r) -> dict:
        try:
            j = r.json()
            msg = j.get("detail") or j.get("error_description") or j.get("error") or f"HTTP {r.status_code}"
        except Exception:
            msg = f"HTTP {r.status_code}: {r.text[:80]}"
        return {"ok": False, "error": f"Beatport {step}: {msg}"}

    try:
        async with httpx.AsyncClient(timeout=20, follow_redirects=False) as c:
            # Step 1: start OAuth — server sets session cookie
            r = await c.get(f"{API}auth/o/authorize/", params={
                "client_id": CLIENT_ID, "response_type": "code", "redirect_uri": REDIRECT,
            }, headers={"User-Agent": UA})
            if r.status_code != 302:
                return _err("step1", r)

            loc      = r.headers.get("location", "")
            base_url = f"{r.request.url.scheme}://{r.request.url.host}"
            referer  = (base_url + loc) if loc.startswith("/") else loc

            # Step 2: post credentials
            r = await c.post(f"{API}auth/login/", json={"username": username, "password": password},
                             headers={"User-Agent": UA, "Referer": referer})
            if r.status_code != 200:
                return _err("логин", r)

            # Step 3: get authorization code
            r = await c.get(f"{API}auth/o/authorize/", params={
                "client_id": CLIENT_ID, "response_type": "code", "redirect_uri": REDIRECT,
            }, headers={"User-Agent": UA})
            if r.status_code != 302:
                return _err("step3", r)

            loc = r.headers.get("location", "")
            if "code=" not in loc:
                return {"ok": False, "error_key": "pr.bp_no_code", "error": "Beatport: code не получен из redirect"}
            code = loc.split("code=")[1].split("&")[0]

            # Step 4: exchange code for tokens
            r = await c.post(f"{API}auth/o/token/", data={
                "client_id": CLIENT_ID, "code": code,
                "grant_type": "authorization_code", "redirect_uri": REDIRECT,
            })
            if r.status_code != 200:
                return _err("token", r)

            token = r.json().get("access_token", "")
            if not token:
                return {"ok": False, "error_key": "pr.bp_no_token", "error": "Beatport: токен не найден в ответе"}

            # Step 5: get account info
            subscription = "OK"
            try:
                me = await c.get(f"{API}auth/o/introspect",
                                 headers={"Authorization": f"Bearer {token}"})
                if me.status_code == 200:
                    subscription = (me.json().get("subscription", {}) or {}).get("name") or "Link"
            except Exception:
                pass

    except Exception as e:
        return {"ok": False, "error_key": "pr.bp_net", "error_args": {"e": str(e)}, "error": f"Beatport сеть: {e}"}

    return {
        "ok": True,
        "user": {
            "login":        username,
            "subscription": subscription,
        },
    }


# ── Token import ──────────────────────────────────────────────────────────────

@router.post("/api/import-token/{service}")
async def import_token(service: str, body: dict):
    """Parse a saved token.json file and populate the relevant config keys.

    Expected body:
        {"content": "<raw JSON text>"}    OR
        {"token": {...already-parsed object...}}

    Supported formats:
        * tidal-dl-ng / tidalapi        — ``{access_token, refresh_token,
          expiry_time (ISO date), token_type, is_pkce}``
        * streamrip's own export        — ``{user_id, access_token,
          refresh_token, country_code, token_expiry}``
        * Raw Tidal OAuth response      — ``{access_token, refresh_token,
          expires_in (seconds), token_type}``
    """
    s = service.lower()
    if s != "tidal":
        raise HTTPException(400, f"Импорт токена не поддерживается для {service}. "
                                 "Пока есть только Tidal.")

    token_obj = (body or {}).get("token")
    raw       = (body or {}).get("content", "")
    if token_obj is None:
        if not isinstance(raw, str) or not raw.strip():
            raise HTTPException(400, "Пустое тело. Передай 'content' с содержимым token.json.")
        try:
            token_obj = json.loads(raw)
        except json.JSONDecodeError as e:
            raise HTTPException(400,
                f"Невалидный JSON: {e.msg} (строка {e.lineno}, колонка {e.colno})")

    if not isinstance(token_obj, dict):
        raise HTTPException(400, "Ожидался JSON-объект с токенами")

    access  = (token_obj.get("access_token")  or token_obj.get("accessToken")  or "").strip()
    refresh = (token_obj.get("refresh_token") or token_obj.get("refreshToken") or "").strip()
    user    = str(token_obj.get("user_id") or token_obj.get("userId") or "").strip()
    country = (token_obj.get("country_code") or token_obj.get("countryCode") or "").strip().upper()

    if not access:
        raise HTTPException(400, "В token.json не найдено поле 'access_token'.")

    expiry_unix = ""
    if token_obj.get("token_expiry"):
        expiry_unix = str(token_obj["token_expiry"])
    elif token_obj.get("expiry_time"):
        et = str(token_obj["expiry_time"])
        try:
            dt = datetime.fromisoformat(et.replace("Z", "+00:00"))
            expiry_unix = str(int(dt.timestamp()))
        except ValueError:
            expiry_unix = ""
    elif token_obj.get("expires_in"):
        try:
            expiry_unix = str(int(time.time()) + int(token_obj["expires_in"]))
        except (ValueError, TypeError):
            expiry_unix = ""

    if access and (not user or not country):
        try:
            async with httpx.AsyncClient(timeout=8) as c:
                r = await c.get(
                    "https://api.tidal.com/v1/sessions",
                    headers={"Authorization": f"Bearer {access}"},
                )
            if r.status_code == 200:
                sess = r.json()
                if not user:
                    user = str(sess.get("userId") or "")
                if not country:
                    country = (sess.get("countryCode") or "").upper()
        except Exception:
            pass

    updates = {
        "tidal-token":        access,
        "tidal-refresh":      refresh,
        "tidal-user-id":      user,
        "tidal-country":      country or "US",
        "tidal-token-expiry": expiry_unix,
    }
    _cfg.update({k: v for k, v in updates.items() if v or k in ("tidal-token", "tidal-refresh")})
    if _save_config:
        _save_config(_cfg)

    return {
        "ok": True,
        "imported": {
            "access_token":  f"{access[:8]}…{access[-4:]}" if access else "",
            "refresh_token": f"{refresh[:8]}…{refresh[-4:]}" if refresh else "",
            "user_id":       user,
            "country":       country,
            "expiry_unix":   expiry_unix,
        },
    }


# ── Выход из аккаунта сервиса ────────────────────────────────────────────────
# Выхода не было вовсе: войти можно, выйти — нет. Это не косметика, а рабочая
# нехватка, и 01.08.2026 она стоила вечера. У Apple ID кончились слоты устройств
# («device limit»), и единственным способом что-то изменить было лезть руками в
# docker и config.yaml — при том, что рядом лежал живой второй аккаунт.
#
# ЧТО ИМЕННО ЧИСТИМ. Для каждого сервиса — ровно те ключи, по которым он себя
# опознаёт, и ничего сверх: путь загрузок, качество, шаблоны имён и прочие
# настройки человек терять не должен. Отдельно от ключей стоит Apple: у него
# сессия живёт не в конфиге, а внутри контейнера и его identity-папки.
#
# ЧЕГО ЗДЕСЬ СОЗНАТЕЛЬНО НЕТ — «выйти отовсюду». Одна кнопка, стирающая все
# входы разом, слишком легко нажимается по ошибке, а восстановление стоит часов.
_LOGOUT_KEYS: dict[str, tuple[str, ...]] = {
    "yandex":     ("yandex-token",),
    "amazon":     ("amazon-token",),
    "deezer":     ("deezer-arl",),
    "soundcloud": ("soundcloud-oauth-token",),
    "tidal":      ("tidal-token", "tidal-token-expiry", "tidal-user-id"),
    "qobuz":      ("qobuz-auth-token", "qobuz-token", "qobuz-user-id",
                   "qobuz-email", "qobuz-password"),
    "beatport":   ("beatport-email", "beatport-password", "beatport-token"),
    "spotify":    ("spotify-sp-dc",),
    "apple":      ("media-user-token", "authorization-token"),
}


def _logout_files(service: str) -> list:
    """Файлы сессии, которые конфигом не стираются."""
    from pathlib import Path
    base = Path(__file__).resolve().parent.parent.parent
    if service == "spotify":
        # Долговременный librespot-блоб и минтованные токены: без них Spotify
        # действительно разлогинен, а не «до первого обновления токена».
        cfgdir = base / "orpheus" / "config"
        return [cfgdir / ".librespot_cache" / "reusable_credentials.json",
                cfgdir / "credentials.json",
                cfgdir / "spotify-token.txt",
                cfgdir / "spotify-client-token.txt"]
    return []


@router.post("/api/logout/{service}")
async def logout_service(service: str, body: dict | None = None):
    """Выйти из аккаунта сервиса.

    Возвращает, что именно очищено — чтобы человек видел последствие, а не
    получал молчаливое «готово».
    """
    svc = (service or "").strip().lower()
    if svc not in _LOGOUT_KEYS and svc != "apple":
        raise HTTPException(400, f"Unsupported service: {svc}")

    cleared, removed = [], []
    for k in _LOGOUT_KEYS.get(svc, ()):
        if (_cfg.get(k) or "") != "":
            _cfg[k] = ""
            cleared.append(k)

    for p in _logout_files(svc):
        try:
            if p.exists():
                p.unlink()
                removed.append(p.name)
        except Exception as e:
            print(f"[logout] {p}: {e}", flush=True)

    wrapper = ""
    if svc == "apple":
        # У Apple сессия живёт в контейнере и его identity-папке. Контейнер
        # гасим ВСЕГДА: пока он поднят, он держит вход. Identity сносим только
        # по явной просьбе — она привязана к устройству на аккаунте, и лишний
        # снос заводит НОВОЕ устройство, а слоты у Apple ID конечны
        # ([[project_apple_device_limit_exhausted_2026-08-01]]).
        try:
            from ripster import amd as _amd
            ok, dp = _amd.check_docker_installed()
            if ok:
                import subprocess
                subprocess.run([dp, "rm", "-f", _amd.WRAPPER_CONTAINER_NAME],
                               capture_output=True, timeout=20)
                wrapper = "stopped"
        except Exception as e:
            wrapper = f"error: {type(e).__name__}"
        if (body or {}).get("forget_device"):
            try:
                import shutil
                from ripster import amd as _amd
                d = _amd._rootfs_data(_amd._wrapper_mode())
                if d.exists():
                    shutil.rmtree(d, ignore_errors=True)
                    removed.append(str(d))
                    wrapper = (wrapper or "") + " +identity"
            except Exception as e:
                print(f"[logout] apple identity: {e}", flush=True)

    if cleared and _save_config:
        _save_config(_cfg)
    print(f"[logout] {svc}: keys {len(cleared)}, files {len(removed)}"
          + (f", wrapper {wrapper}" if wrapper else ""), flush=True)
    return {"ok": True, "service": svc, "cleared": cleared,
            "removed": removed, "wrapper": wrapper}
