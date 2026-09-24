"""Предзаказ: релиз ещё не вышел — ретраи бессмысленны, надо ждать выхода.

Факт 24.09.2026 (NORTHERN EXPOSURE, Qobuz + Tidal 555853091): до даты выхода
витрина отдаёт один instant-grat трек, раннер видит «1/22» и FOUR раз подряд
бессмысленно дозагружает. Здесь — честный детект по метаданным, которые
движки и метаданные-модули ПРИНОСЯТ САМИ (никаких новых тяжёлых запросов):

  • Qobuz  — album/get: release_date_original, streamable_at / purchasable_at
             (unix-моменты), per-track streamable.
  • Tidal  — albums/{id}: releaseDate + numberOfTracks; витрина определяется
             страной учётки (countryCode сессии).
  • Deezer — album/{id}: release_date (+ available_products у треков, если поле
             в ответе есть).
  • Apple  — releaseDate; на каталожном пути ещё isPreRelease /
             preReleaseReleaseDate.

Правило: предзаказ = момент доступности (точное поле движка или полночь СТРАНЫ
УЧЁТКИ по дате релиза) ещё не наступил. Полночь считается в часовом поясе
учётки; если настроено несколько живых учёток с разными странами, план
ставится на САМУЮ РАННЮЮ витрину (релиз придёт туда раньше — см. #3).

Часовые пояса: на этой машине zoneinfo без tzdata не находит ни одной зоны
(проверено, см. watchlist), поэтому к таблице смещений предусмотрен ручной
запас. Ошибку направления калим грейсом: неверно «раньше» — это повторный
план после наступившего, но ещё не доступного релиза, а не потерянные файлы.
"""
from __future__ import annotations

import datetime as _dt

#: Буфер после локальной полуночи — витрина применяет релиз не ровно в 00:00.
RELEASE_GRACE = _dt.timedelta(minutes=15)

#: Дальше года вперёд даты не верим: это брак метаданных, а не предзаказ.
_MAX_AHEAD = _dt.timedelta(days=366)

SUPPORTED = {"qobuz", "tidal", "deezer", "apple"}

#: Ключ конфига со страной текущей витрины — если измерение учётки недоступно.
_CFG_CC = {
    "tidal":  ("tidal-country",),
    "qobuz":  ("qobuz-country",),
    "deezer": ("deezer-country",),
    "apple":  ("apple-country", "storefront"),
}

#: Страна учётки → город, по которому витрина живёт в полуночи релиза.
_CITIES = {
    "NZ": "Pacific/Auckland", "AU": "Australia/Sydney", "JP": "Asia/Tokyo",
    "KR": "Asia/Seoul", "CN": "Asia/Shanghai", "SG": "Asia/Singapore",
    "HK": "Asia/Hong_Kong", "TW": "Asia/Taipei", "TH": "Asia/Bangkok",
    "MY": "Asia/Kuala_Lumpur", "ID": "Asia/Jakarta", "PH": "Asia/Manila",
    "IN": "Asia/Kolkata", "KZ": "Asia/Almaty", "IL": "Asia/Jerusalem",
    "RU": "Europe/Moscow", "UA": "Europe/Kyiv", "MD": "Europe/Chisinau",
    "GE": "Asia/Tbilisi", "AM": "Asia/Yerevan", "AZ": "Asia/Baku",
    "TR": "Europe/Istanbul", "GB": "Europe/London", "IE": "Europe/Dublin",
    "PT": "Europe/Lisbon", "ES": "Europe/Madrid", "FR": "Europe/Paris",
    "DE": "Europe/Berlin", "IT": "Europe/Rome", "NL": "Europe/Amsterdam",
    "BE": "Europe/Brussels", "LU": "Europe/Luxembourg", "CH": "Europe/Zurich",
    "AT": "Europe/Vienna", "CZ": "Europe/Prague", "SK": "Europe/Bratislava",
    "PL": "Europe/Warsaw", "HU": "Europe/Budapest", "SE": "Europe/Stockholm",
    "NO": "Europe/Oslo", "DK": "Europe/Copenhagen", "FI": "Europe/Helsinki",
    "EE": "Europe/Tallinn", "LV": "Europe/Riga", "LT": "Europe/Vilnius",
    "GR": "Europe/Athens", "RO": "Europe/Bucharest", "BG": "Europe/Sofia",
    "HR": "Europe/Zagreb", "SI": "Europe/Ljubljana", "RS": "Europe/Belgrade",
    "US": "America/New_York", "CA": "America/Toronto", "MX": "America/Mexico_City",
    "BR": "America/Sao_Paulo", "AR": "America/Argentina/Buenos_Aires",
    "CL": "America/Santiago", "CO": "America/Bogota", "PE": "America/Lima",
    "ZA": "Africa/Johannesburg", "NG": "Africa/Lagos", "KE": "Africa/Nairobi",
}

#: Ручные смещения для машин без tzdata: cc → (часы от UTC вне лета, режим).
#: Режимы: "eu" — последнее вс марта 01:00Z … последнее вс октября 01:00Z;
#: "us" — 2-е вс марта 07:00Z … 1-е вс ноября 06:00Z; "nz"/"au" — южное лето;
#: "cl" — южное лето Чили (апрель…сентябрь); "none" — без перевода.
#: Страны вне списка считаются без перевода часов: погрешность перекрывается
#: грейсом и повторным планом, молча потерять файлы это не может.
_FALLBACK = {
    "NZ": (12, "nz"), "AU": (10, "au"), "CL": (-4, "cl"),
    "US": (-5, "us"), "CA": (-5, "us"),
    "GB": (0, "eu"), "IE": (0, "eu"), "PT": (0, "eu"),
    "MX": (-6, "none"), "BR": (-3, "none"), "AR": (-3, "none"),
    "CO": (-5, "none"), "PE": (-5, "none"),
    "ZA": (2, "none"), "NG": (1, "none"), "KE": (3, "none"),
    "RU": (3, "none"), "TR": (3, "none"), "IL": (2, "eu"),
    "GE": (4, "none"), "AM": (4, "none"), "AZ": (4, "none"), "KZ": (5, "none"),
    "UA": (2, "eu"), "MD": (2, "eu"),
    "JP": (9, "none"), "KR": (9, "none"), "CN": (8, "none"),
    "SG": (8, "none"), "HK": (8, "none"), "TW": (8, "none"),
    "TH": (7, "none"), "MY": (8, "none"), "ID": (7, "none"), "PH": (8, "none"),
    "IN": (5.5, "none"),
}
# Вся континентальная Европа, кроме перечисленного выше, — CET +1 с общеевропейским летом.
_EU_SUMMER = ("ES FR DE IT NL BE LU CH AT CZ SK PL HU SE NO DK FI "
              "EE LV LT GR RO BG HR SI RS").split()


def utcnow() -> _dt.datetime:
    """Наивный UTC — тот же часы, что у watchlist/bbc_schedule."""
    return _dt.datetime.now(_dt.timezone.utc).replace(tzinfo=None)


# ── Смещения и полночь ───────────────────────────────────────────────────────

def _last_sunday(y: int, m: int) -> _dt.date:
    d = _dt.date(y, m, 28)
    return d + _dt.timedelta(days=(6 - d.weekday()) % 7)


def _first_sunday(y: int, m: int) -> _dt.date:
    d = _dt.date(y, m, 1)
    return d + _dt.timedelta(days=(6 - d.weekday()) % 7)


def _second_sunday(y: int, m: int) -> _dt.date:
    return _first_sunday(y, m) + _dt.timedelta(days=7)


def _sun_between(when_utc: _dt.datetime, start: _dt.datetime,
                 end: _dt.datetime) -> bool:
    """Окно южного лета, заданное двумя воскресными переходами одного года:
    активно ДО `end` (апрель–март) или ПОСЛЕ `start` (сентябрь–декабрь)."""
    return when_utc < end or when_utc >= start


def _dst_active(rule: str, when_utc: _dt.datetime) -> bool:
    """Летнее ли время в момент `when_utc` (наивный UTC) для режима `rule`."""
    y = when_utc.year
    if rule == "eu":
        start = _last_sunday(y, 3)  + _dt.timedelta(hours=1)    # 01:00 UTC
        end   = _last_sunday(y, 10) + _dt.timedelta(hours=1)
        return start <= when_utc < end
    if rule == "us":
        start = _second_sunday(y, 3) + _dt.timedelta(hours=7)   # 07:00 UTC
        end   = _first_sunday(y, 11) + _dt.timedelta(hours=6)
        return start <= when_utc < end
    if rule == "nz":
        # NZDT: последнее вс сентября 02:00 NZST (+12) → первое вс апреля
        # 03:00 NZDT (+13). Те же вычитания, что в watchlist (проверено на
        # реальных переходах): переход — «суббота, вечер по UTC».
        start = _last_sunday(y, 9) - _dt.timedelta(hours=10)    # 02:00 вс −12ч
        end   = _first_sunday(y, 4) - _dt.timedelta(hours=11)   # 03:00 вс −13ч
        return _sun_between(when_utc, start, end)
    if rule == "au":
        # AEDT: первое вс октября 02:00 AEST (+10) … первое вс апреля
        # 03:00 AEDT (+11).
        start = _first_sunday(y, 10) - _dt.timedelta(hours=8)
        end   = _first_sunday(y, 4)  - _dt.timedelta(hours=11)
        return _sun_between(when_utc, start, end)
    if rule == "cl":
        # Чили: первое вс сентября 00:00 … первое вс апреля 24:00 (местное).
        # Окно по датам с тем же трюком: активно до апреля и после сентября.
        start = _first_sunday(y, 9) + _dt.timedelta(hours=4)    # 00:00 −(−4)
        end   = _first_sunday(y, 4) + _dt.timedelta(hours=21)   # 24:00 апр −(−3)
        return _sun_between(when_utc, start, end)
    return False


def offset_hours(cc: str, when_utc: _dt.datetime) -> float:
    """Часы от UTC для страны учётки в момент `when_utc` (наивный UTC).

    Сначала zoneinfo (если на машине есть tzdata — на этой её нет), затем
    ручная таблица смещений. Никогда не бросает: неизвестная страна — это +0,
    худший случай сдвинет план на пару часов, файлы это не теряет.
    """
    cc = (cc or "").strip().upper()
    z = _CITIES.get(cc)
    if z:
        try:
            from zoneinfo import ZoneInfo
            off = (when_utc.replace(tzinfo=_dt.timezone.utc)
                   .astimezone(ZoneInfo(z)).utcoffset())
            if off is not None:
                return off.total_seconds() / 3600.0
        except Exception:                                      # noqa: BLE001
            pass
    if cc in _EU_SUMMER:
        std, rule = 1.0, "eu"
    else:
        std, rule = _FALLBACK.get(cc, (0.0, "none"))
    if rule == "none":
        return float(std)
    if rule == "eu":
        return float(std + 1) if _dst_active("eu", when_utc) else float(std)
    if rule == "us":
        return float(std + 1) if _dst_active("us", when_utc) else float(std)
    if rule == "nz":
        return 13.0 if _dst_active("nz", when_utc) else 12.0
    if rule == "au":
        return 11.0 if _dst_active("au", when_utc) else 10.0
    if rule == "cl":
        return -3.0 if _dst_active("cl", when_utc) else -4.0
    return float(std)


def local_midnight_utc(cc: str, day: _dt.date) -> _dt.datetime:
    """Naive-UTC-момент полуночи `day` в часовом поясе страны `cc`.
    Два прохода, как в watchlist.nz_friday_target_utc: пояс у границы
    перевода часов мог измениться между догадкой и ответом."""
    local = _dt.datetime(day.year, day.month, day.day)
    off = offset_hours(cc, local - _dt.timedelta(hours=12))
    guess = local - _dt.timedelta(hours=off)
    off2 = offset_hours(cc, guess)
    return local - _dt.timedelta(hours=off2) if off2 != off else guess


def release_moment_utc(cc: str, day: _dt.date) -> _dt.datetime:
    """Когда релиз с датой `day` станет доступен учётке страны `cc`
    (полуночь + буфер)."""
    return local_midnight_utc(cc, day) + RELEASE_GRACE


# ── Страны учёток ────────────────────────────────────────────────────────────

def account_candidates(svc: str, cfg: dict) -> list[dict]:
    """Живые учётки сервиса с известной страной: [{cc, slot}].

    Берём из account_country — там уже различены «нет страны», «мёртвый
    токен» и измеренное значение. Мёртвые исключаем: план на учётку, которой
    нет, не сработает. Если ни одна учётка не измерена — страна из конфига
    (слот 0), это витрина, которой реально качали.
    """
    out: list[dict] = []
    try:
        from ripster import account_country as _ac
        for r in _ac.slots(cfg or {}, svc) or []:
            cc = str(r.get("country") or "").upper()
            if cc and r.get("alive") is not False:
                out.append({"cc": cc, "slot": r.get("slot")})
    except Exception:                                          # noqa: BLE001
        pass
    if out:
        return out
    for key in _CFG_CC.get(svc, ()):
        cc = str((cfg or {}).get(key) or "").strip().upper()
        if len(cc) == 2 and cc.isalpha():
            return [{"cc": cc, "slot": None}]
    return []


# ── Детект ───────────────────────────────────────────────────────────────────

def _parse_date(raw) -> "_dt.date | None":
    s = str(raw or "").strip()[:10]
    try:
        return _dt.date.fromisoformat(s)
    except ValueError:
        return None


def _ts(raw) -> float:
    try:
        return float(raw or 0)
    except (TypeError, ValueError):
        return 0.0


def detect(task: dict, cfg: dict, now: "_dt.datetime | None" = None) -> "dict | None":
    """Инфо о предзаказе по задаче или None (релиз вышел / не поняли).

    Только для альбомных задач поддерживаемых сервисов. Поля ответа:
      service, cc, slot — витрина, на которую смотрим;
      release_date      — «YYYY-MM-DD»;
      release_utc       — наивный UTC момента доступности (+буфер);
      exact             — True, если момент точный (Qobuz streamable_at),
                          а не «полуночь по стране учётки»;
      go_now            — True, когда у одной живой учётки релиз УЖЕ вышел,
                          а у другой ещё в будущем: ждать нечего, докачать
                          сейчас через ту, где вышло (план сгорит сразу).
    """
    now = now or utcnow()
    svc = str(task.get("service") or "").lower()
    if svc not in SUPPORTED:
        return None
    meta = task.get("meta") or {}
    typ = str(meta.get("type") or "").lower()
    if typ and typ not in ("album", "albums"):
        return None

    # Единственный момент, не зависящий от страны учётки — точные unix-поля
    # Qobuz. Есть и в будущем → предзаказ без всяких часовых поясов.
    if svc == "qobuz":
        ts = _ts(meta.get("streamableAt")) or _ts(meta.get("purchasableAt"))
        if ts > 0:
            when = _dt.datetime.fromtimestamp(ts, _dt.timezone.utc).replace(tzinfo=None)
            if when > now:
                cands = account_candidates(svc, cfg)
                cc = cands[0]["cc"] if cands else ""
                slot = cands[0]["slot"] if cands else None
                return {"service": svc, "cc": cc, "slot": slot,
                        "release_date": str(meta.get("date") or "")[:10],
                        "release_utc": (when + RELEASE_GRACE).isoformat(timespec="seconds"),
                        "exact": True, "go_now": False}
            # streamable_at прошёл, а треков нет — это не предзаказ, а авария.
            return None

    day = _parse_date(meta.get("date") or meta.get("releaseDate"))
    if svc == "apple" and meta.get("isPreRelease"):
        # Каталог сказал «предзаказ» — дата открытия важнее releaseDate:
        # у instant-grat сингла releaseDate уже в прошлом.
        day = _parse_date(meta.get("preReleaseReleaseDate")) or day
    if day is None:
        return None

    cands = account_candidates(svc, cfg)
    if not cands:
        # Не знаем страну учётки — нечего обещать пользователю в сообщении,
        # а молча планировать «в UTC» — значит докачать на 12 часов раньше/позже.
        return None
    moments = []
    for c in cands:
        moments.append((release_moment_utc(c["cc"], day), c["cc"], c.get("slot")))
    future = [m for m in moments if m[0] > now]
    if not future:
        return None                                   # у всех учёток уже вышло
    past = [m for m in moments if m[0] <= now]
    if past:
        # Релиз уже доступен на другой живой учётке (например, NZ встречает
        # его раньше Европы) — ждать нельзя, надо докачать СЕЙЧАС через неё.
        when, cc, slot = min(moments)
        return {"service": svc, "cc": cc, "slot": slot,
                "release_date": day.isoformat(),
                "release_utc": when.isoformat(timespec="seconds"),
                "exact": False, "go_now": True}
    when, cc, slot = min(future)                      # самая ранняя витрина
    if when - now > _MAX_AHEAD:
        return None                                   # год вперёд — брак данных
    return {"service": svc, "cc": cc, "slot": slot,
            "release_date": day.isoformat(),
            "release_utc": when.isoformat(timespec="seconds"),
            "exact": False, "go_now": False}


def human_date(iso: str) -> str:
    """«2026-09-25» → «25.09» — для сообщений человеку."""
    d = _parse_date(iso)
    return f"{d.day:02d}.{d.month:02d}" if d else (iso or "")
