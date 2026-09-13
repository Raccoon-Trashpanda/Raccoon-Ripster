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
    """Статус учётки по измеренному здоровью.

    `premium` — сервис-специфичный «даёт максимум» (Tidal lossless / SC Go+):
    вычисляет вызывающий, потому что имя поля у сервисов разное.
    Истёкшая ИЛИ не-премиум ИЛИ мёртвая → retired (серым, не в автоотборе).
    Живая премиум и активная → active; живая премиум в запасе → bench.
    """
    alive = info.get("alive")
    if alive is False:
        return RETIRED
    if is_expired(info.get("valid_until") or info.get("expiry")):
        return RETIRED
    if not premium:
        return RETIRED
    return ACTIVE if is_active else BENCH


_BADGE = {ACTIVE: "✅ активна", BENCH: "🟢 запас", RETIRED: "⚪ снята"}


def line(service: str, label: str, info: dict, status: str) -> str:
    """Одна строка ростера для текстового отчёта.

    Пример: `🇳🇿 Tidal · NZ · PREMIUM · до 2026-10-07 · ✅ активна`
    """
    cc = (info.get("country") or "").strip().upper()
    plan = (info.get("plan") or info.get("type") or "").strip() or "?"
    exp = expiry_date(info.get("valid_until") or info.get("expiry"))
    exp_s = f"до {exp}" if exp else "срок ?"
    badge = _BADGE.get(status, status)
    who = f" · {label}" if label else ""
    return f"{country_flag(cc)} {service}{who} · {cc or '??'} · {plan} · {exp_s} · {badge}"


def as_dict(service: str, label: str, info: dict, status: str, *, masked: str = "") -> dict:
    """Машиночитаемая карточка учётки — для JSON API (ПК/мобилка рисуют сами)."""
    cc = (info.get("country") or "").strip().upper()
    return {
        "service": service,
        "label": label,
        "country": cc,
        "flag": country_flag(cc),
        "plan": (info.get("plan") or info.get("type") or "").strip(),
        "expiry": expiry_date(info.get("valid_until") or info.get("expiry")),
        "expired": is_expired(info.get("valid_until") or info.get("expiry")),
        "status": status,
        "alive": info.get("alive"),
        "masked": masked,
    }
