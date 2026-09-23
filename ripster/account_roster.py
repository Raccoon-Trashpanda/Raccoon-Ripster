"""Единый вид учётки: флаг страны, тип подписки, дата окончания, статус.

Владелец 13.09.2026: «у каждого аккаунта есть страна и дата окончания подписки,
это я и любой пользователь ПК/мобилки должны видеть — чётко с флагом страны, с
датой окончания и прочими реквизитами».

Один источник правды для всех трёх поверхностей (отчёт боту, настройки ПК,
мобилка), чтобы «активна/в запасе/снята» и реквизиты рисовались одинаково и не
разъезжались. Меру берём из уже существующих `*_accounts.account_info`
(country, plan, valid_until/expiry, признак премиума) — здесь только показ.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any


def country_flag(cc: str) -> str:
    """ISO-3166 alpha-2 → эмодзи-флаг. Пусто/мусор → 🏳 (не роняем строку)."""
    cc = (cc or "").strip().upper()
    if len(cc) != 2 or not cc.isalpha():
        return "\U0001F3F3"  # белый флаг = страна неизвестна
    base = 0x1F1E6  # regional indicator 'A'
    return chr(base + (ord(cc[0]) - 65)) + chr(base + (ord(cc[1]) - 65))


def _expiry_epoch(valid_until: Any) -> float | None:
    """Дату окончания → unix, из ISO-строки или миллисекунд. None — не знаем."""
    if not valid_until:
        return None
    if isinstance(valid_until, (int, float)):
        # Tidal отдаёт миллисекунды, иногда — секунды. > 1e12 ⇒ мс.
        v = float(valid_until)
        return v / 1000.0 if v > 1e12 else v
    s = str(valid_until).strip()
    for fmt in ("%Y-%m-%dT%H:%M:%S.%f%z", "%Y-%m-%dT%H:%M:%S%z",
                "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d"):
        try:
            dt = datetime.strptime(s.replace("+0000", "+00:00")
                                   if fmt.endswith("%z") else s[:19], fmt)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.timestamp()
        except Exception:  # noqa: BLE001
            continue
    return None


def expiry_date(valid_until: Any) -> str:
    """Человекочитаемая дата 'YYYY-MM-DD' или '' если не знаем."""
    ep = _expiry_epoch(valid_until)
    if ep is None:
        return ""
    return datetime.fromtimestamp(ep, tz=timezone.utc).strftime("%Y-%m-%d")


def is_expired(valid_until: Any) -> bool:
    ep = _expiry_epoch(valid_until)
    return ep is not None and ep < datetime.now(tz=timezone.utc).timestamp()


#: Статусы учётки в пуле — ровно те, что утвердил владелец 13.09.2026.
ACTIVE = "active"    # используется сейчас
BENCH = "bench"      # живая, премиум, в запасе
RETIRED = "retired"  # истекла / не премиум / мертва — из автоотбора вон, в UI серым


def classify(info: dict, *, is_active: bool, premium: bool) -> str:
    """Статус учётки по РОЛИ, а не только по здоровью.

    Активная — это ФАКТ (её сейчас использует сервис), и прятать её нельзя,
    даже если она не премиум: наоборот, владелец обязан это видеть. Поэтому
    активная всегда `active` (а «активна без премиума» показывается отдельной
    пометкой через [is_degraded]). Не активная: живая премиум → `bench`;
    мёртвая / истёкшая / не премиум → `retired` (серым, вне автоотбора).
    """
    if is_active:
        return ACTIVE
    alive = info.get("alive")
    if alive is False:
        return RETIRED
    if is_expired(_expiry_raw(info)):
        return RETIRED
    if not premium:
        return RETIRED
    return BENCH


def is_degraded(info: dict, *, premium: bool) -> bool:
    """Активная учётка «не тянет»: жива, но не премиум, срок вышел — либо ЕЮ НЕ
    КАЧАЮТ. Последнее важнее всех: 23.09.2026 активная карточка была зелёной при
    мёртвой сессии движка, и «✅ активна» означало ровно противоположное тому,
    что видел пользователь в логе загрузок."""
    if info.get("session_drift"):
        return True
    return info.get("alive") is not False and (not premium or is_expired(_expiry_raw(info)))


_BADGE = {ACTIVE: "✅ активна", BENCH: "🟢 запас", RETIRED: "⚪ снята"}


def line(service: str, label: str, info: dict, status: str, *, premium: bool = True) -> str:
    """Одна строка ростера для текстового отчёта.

    Пример: `🇳🇿 Tidal · NZ · PREMIUM · до 2026-10-07 · ✅ активна`
    """
    cc = (info.get("country") or "").strip().upper()
    plan = (info.get("plan") or info.get("type") or "").strip() or "?"
    exp = expiry_date(_expiry_raw(info))
    exp_s = f"до {exp}" if exp else "срок ?"
    badge = _BADGE.get(status, status)
    drift = str(info.get("session_drift") or "")
    if drift:
        # Зелёную галочку поверх этой строки ставить нельзя: она и была ложью.
        badge = f"⚠️ {drift}"
    elif status == ACTIVE and is_degraded(info, premium=premium):
        badge += " ⚠️ без премиума" if not premium else " ⚠️ истёк"
    who = f" · {label}" if label else ""
    return f"{country_flag(cc)} {service}{who} · {cc or '??'} · {plan} · {exp_s} · {badge}"


#: Поля срока окончания у разных сервисов называются по-разному. Берём первое
#: непустое — так один рендер обслуживает все.
_EXPIRY_KEYS = ("valid_until", "expiry", "expires", "options_until_ts")


def _expiry_raw(info: dict):
    for k in _EXPIRY_KEYS:
        v = info.get(k)
        if v:
            return v
    return None


def as_dict(service: str, label: str, info: dict, status: str, *,
            masked: str = "", premium: bool = True) -> dict:
    """Машиночитаемая карточка учётки — для JSON API (ПК/мобилка рисуют сами)."""
    cc = (info.get("country") or "").strip().upper()
    exp_raw = _expiry_raw(info)
    return {
        "service": service,
        "label": label,
        "country": cc,
        "flag": country_flag(cc),
        "plan": (info.get("plan") or info.get("type") or "").strip(),
        "expiry": expiry_date(exp_raw),
        "expired": is_expired(exp_raw),
        "status": status,
        "alive": info.get("alive"),
        "premium": bool(premium),
        "degraded": (status == ACTIVE and is_degraded(info, premium=premium)),
        # Чем КАЧАЕТ эта учётка: 'same' — сессия движка совпадает, 'other' —
        # движок сидит на другой, 'none' — сессии движка нет. Пусто для
        # сервисов без такого разделения.
        "engine_session": info.get("engine_session") or "",
        "session_drift": str(info.get("session_drift") or ""),
        "masked": masked,
    }


# ── Сбор карточек по ВСЕМ сервисам ───────────────────────────────────────────
#
# Владелец 13.09.2026: «для всех сервисов, на ПК и мобилке». Каждый сервис меряет
# себя по-своему (поля premium и „кто активен“ разные), поэтому здесь маленький
# адаптер на сервис, а показ — общий. Любой адаптер обёрнут в try/except: один
# упавший сервис не должен уносить весь ростер.


def _card(service: str, label: str, info: dict, *, is_active: bool, premium: bool,
          masked: str = "") -> dict:
    status = classify(info, is_active=is_active, premium=premium)
    d = as_dict(service, label, info, status, masked=masked, premium=premium)
    # Для строкового отчёта пригодится готовая строка.
    d["line"] = line(service, label, info, status, premium=premium)
    return d


def roster_cards(cfg: dict) -> dict:
    """{service: [card, ...]} по всем сервисам с учётками. Меру берём из кэша
    (`fresh=False`) — показ не должен гонять сеть на каждый рендер."""
    import asyncio

    out: dict[str, list] = {}

    def _run(coro):
        return asyncio.run(coro)

    # ── Tidal ────────────────────────────────────────────────────────────────
    try:
        from . import tidal_accounts as ta, tidal_pool as tp
        active = (cfg.get("tidal-refresh") or "").strip()
        # Мера — сессия движка, а не refresh из config.yaml: качает OrpheusDL
        # первой, и именно она умерла 23.09 в 19:37, пока карточка была зелёной.
        sess = ta.engine_session_secret()
        cards = []
        for i, acct in enumerate(tp.configured_accounts(cfg) or []):
            sec = ta.account_secret(acct)
            is_active = bool(sec and sec == active)
            uses = "none" if not sess else ("same" if sess == sec else "other")
            # Активную карточку не берём из вчерашнего кэша: «жива» со вчерашнего
            # дня переживает сегодняшнюю блокировку учётки.
            info = dict(_run(ta.account_info(acct, fresh=is_active)))
            info["engine_session"] = uses
            if is_active and uses != "same":
                # Заголовок «активна» при мертвой качалке — та же ложь. Скажем,
                # чем кончится следующая загрузка, если это известно.
                why = ("у движка нет сессии Tidal — качать нечем" if uses == "none"
                       else "движок качает другой учёткой")
                if sess:
                    live = _run(ta.account_info({"tidal-refresh": sess}, fresh=True))
                    if live.get("alive") is False:
                        why += f": {live.get('reason') or 'отклонена Tidal'}"
                        info["alive"] = False
                    elif live.get("country"):
                        info.setdefault("engine_country", live["country"])
                info["session_drift"] = why
            cards.append(_card("tidal", acct.get("label") or f"слот {i}", info,
                               is_active=is_active,
                               premium=bool(info.get("lossless")), masked=_mask(sec)))
        if cards:
            out["tidal"] = cards
    except Exception as e:  # noqa: BLE001
        out.setdefault("_errors", []).append(f"tidal: {type(e).__name__}")

    # ── SoundCloud ───────────────────────────────────────────────────────────
    try:
        from . import soundcloud_accounts as sa
        from . import soundcloud_pool as _scp
        # При пуле активна не primary-строка конфига, а учётка, которую пул
        # отдаёт загрузкам (тот же выбор, что `acquire()`).
        active = (_scp.active_token(cfg)
                  or (cfg.get("soundcloud-oauth-token") or "").strip())
        cards = []
        for a in sa.configured_accounts(cfg) or []:
            info = _run(sa.account_info(a["token"], fresh=False))
            cards.append(_card("soundcloud", a.get("label") or "?", info,
                               is_active=bool(a["token"] == active),
                               premium=bool(info.get("go_plus")), masked=_mask(a["token"])))
        if cards:
            out["soundcloud"] = cards
    except Exception as e:  # noqa: BLE001
        out.setdefault("_errors", []).append(f"soundcloud: {type(e).__name__}")

    # ── Qobuz ────────────────────────────────────────────────────────────────
    try:
        from . import qobuz_accounts as qa
        appid = (cfg.get("qobuz-app-id") or "").strip()
        active = (cfg.get("qobuz-auth-token") or "").strip()
        cards = []
        for i, acct in enumerate(qa.configured_accounts(cfg) or []):
            info = _run(qa.account_info(acct, appid, fresh=False))
            sec = qa.account_secret(acct)
            cards.append(_card("qobuz", acct.get("label") or f"слот {i}", info,
                               is_active=bool(sec and sec == active),
                               premium=bool(info.get("lossless") or info.get("hires")),
                               masked=_mask(sec)))
        if cards:
            out["qobuz"] = cards
    except Exception as e:  # noqa: BLE001
        out.setdefault("_errors", []).append(f"qobuz: {type(e).__name__}")

    # ── Deezer ───────────────────────────────────────────────────────────────
    try:
        from . import deezer_accounts as da
        rows = _run(da.survey(cfg, fresh=False))
        cards = []
        for r in rows or []:
            cards.append(_card("deezer", r.get("label") or "?", r,
                               is_active=bool(r.get("primary")),
                               premium=bool(r.get("lossless")),
                               masked=_mask(r.get("arl") or "")))
        if cards:
            out["deezer"] = cards
    except Exception as e:  # noqa: BLE001
        out.setdefault("_errors", []).append(f"deezer: {type(e).__name__}")

    # ── Yandex ───────────────────────────────────────────────────────────────
    try:
        from . import yandex_accounts as ya
        rows = _run(ya.survey(cfg, fresh=False))
        cards = []
        for r in rows or []:
            # survey не проносит флаг primary — активной считаем слот с таким
            # лейблом (его ставит _configured для основного токена).
            is_primary = bool(r.get("primary")) or (r.get("label") == "primary")
            cards.append(_card("yandex", r.get("label") or "?", r,
                               is_active=is_primary,
                               premium=bool(r.get("plus")),
                               masked=_mask(r.get("token") or "")))
        if cards:
            out["yandex"] = cards
    except Exception as e:  # noqa: BLE001
        out.setdefault("_errors", []).append(f"yandex: {type(e).__name__}")

    # ── Beatport (тариф из orpheus-модуля, одна учётка) ──────────────────────
    try:
        from .engines import orpheus_beatport as bp
        tier = bp._tier_now()
        if tier or cfg.get("beatport-username"):
            info = {"alive": bool(tier) or None,
                    "plan": tier or "?",
                    "country": (cfg.get("beatport-country") or "").strip()}
            out["beatport"] = [_card(
                "beatport", cfg.get("beatport-username") or "аккаунт", info,
                is_active=True, premium=bool(tier and tier.startswith("bp_link_pro")),
                masked=_mask(cfg.get("beatport-username") or ""))]
    except Exception as e:  # noqa: BLE001
        out.setdefault("_errors", []).append(f"beatport: {type(e).__name__}")

    return out


def _mask(secret: str) -> str:
    s = (secret or "").strip()
    return f"...{s[-6:]}" if len(s) > 6 else (s or "?")
