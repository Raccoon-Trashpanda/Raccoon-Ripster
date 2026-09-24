"""Уникальный отпечаток устройства (`-I` device-info) для каждой учётки wrapper'а.

Проблема (из переписки авторов wrapper'а, NPGamma 31.08.2026): все, кто входит
враппером, сидят на ОДНОМ дефолтном `--device-info`
(`Music/4.9/Android/10/Samsung S9/7663313/en-US/en-US/dc28071e981c439e`). Apple
на этом профиле блокирует учётки массово («Your account is disabled»). Новый
wrapper-lite выводит уникальный device-info из имени учётки — делаем то же, но
детерминированно из засоленного хеша идентификатора аккаунта:

  · AndroidID (16 hex)   — из sha256(salt | account_id),
  · DeviceModel/Build    — из небольшого списка РЕАЛЬНЫХ Android-устройств,
                           тоже по хешу (разные аккаунты → разные модели),
  · Locale/Language      — по витрине аккаунта (gb → en-GB, ru → ru-RU, …),
                           боковые поля 7–8 `-I`, в выбор витрины авторизации не
                           входят (см. docs/APPLE_STOREFRONT_MISMATCH_2026-09-24).

Стабильность. Меняющийся device-info = для Apple НОВОЕ устройство = ещё один
сожжённый слот (device_limit). Поэтому отпечаток назначается ОДИН РАЗ и больше
не пересчитывается: значение кладётся в реестр `dist/docker/wrapper_device_info.json`
(по идентификатору аккаунта) и переиспользуется после рестартов даже если
список устройств или солёмка изменятся. Солёмка — отдельный секрет в конфиге
(`apple-device-salt`), пишется штатным конфиг-райтером и НИКОГДА не печатается.

Слот 0 (единственная живая учётка, `amd-wrapper`) этот модуль молча НЕ трогает:
`get_assigned()` для него пустой, пока владелец явно не нажмёт «Сменить отпечаток
устройства» (тогда `owner_reroll()` кладёт значение в реестр). Новые аккаунты
пула получают уникальный отпечаток автоматически (`assign_new_account`).

Формат строки — ровно 8 слэшей, 9 полей (docs/APPLE_WRAPPER_UPSTREAM.md):
    Music/<ver>/Android/<api>/<model>/<build>/<locale>/<lang>/<androidId>
"""
from __future__ import annotations

import hashlib
import json
import os
import re
from pathlib import Path

# Не менять без крайней нужды: любое изменение сдвигает отпечатки НОВЫХ учёток.
# Уже назначенные защищены реестром, но сам формат завязан на разбор wrapper'ом.
APP_NAME = "Music"
APP_VERSION = "4.9"
OS_NAME = "Android"

#: Реальные Android-устройства: (api, модель, build). Модель и build берутся
#: парой, чтобы отпечаток выглядел согласованно (прошивка соответствует модели),
#: а не случайным набором полей. Дефолтный «Samsung S9» намеренно ИСКЛЮЧЁН —
#: именно он у всех одинаковый и именно за него банят.
ANDROID_DEVICES: tuple[tuple[str, str, str], ...] = (
    ("10", "SM-G960F",  "PPR1.180610.011"),   # Galaxy S9
    ("11", "SM-G975F",  "RP1A.200720.012"),   # Galaxy S10+
    ("12", "Pixel 4a",  "SD1A.210817.036"),   # Pixel 4a
    ("13", "Pixel 6",   "TD1A.220804.031"),   # Pixel 6
    ("12", "LE2121",    "SKQ1.210923.025"),   # OnePlus 9
    ("11", "M2101K6G",  "RKQ1.200826.002"),   # Redmi Note 10 Pro
    ("13", "SM-S908B",  "TP1A.220624.014"),   # Galaxy S22 Ultra
    ("12", "SM-A525F",  "SP1A.210812.016"),   # Galaxy A52 5G
    ("13", "Pixel 7",   "TQ1A.230205.001"),   # Pixel 7
    ("12", "2201117TG", "SP1A.210812.016"),   # Redmi Note 11
    ("11", "RMX3085",   "RKQ1.201217.002"),   # Realme 8 Pro
    ("13", "CPH2449",   "TP1A.220905.001"),   # OPPO Reno8
)

#: Код витрины аккаунта → (LocaleIdentifier, LanguageIdentifier) для `-I`.
#: ISO-код страны в верхнем регистре после дефиса; язык — до. Дефолт — en-US.
STOREFRONT_LOCALE: dict[str, tuple[str, str]] = {
    "us": ("en-US", "en-US"), "gb": ("en-GB", "en-GB"), "ru": ("ru-RU", "ru-RU"),
    "ua": ("uk-UA", "uk-UA"), "de": ("de-DE", "de-DE"), "fr": ("fr-FR", "fr-FR"),
    "es": ("es-ES", "es-ES"), "it": ("it-IT", "it-IT"), "jp": ("ja-JP", "ja-JP"),
    "kr": ("ko-KR", "ko-KR"), "cn": ("zh-CN", "zh-CN"), "ca": ("en-CA", "en-CA"),
    "au": ("en-AU", "en-AU"), "nz": ("en-NZ", "en-NZ"), "in": ("en-IN", "en-IN"),
    "br": ("pt-BR", "pt-BR"), "pt": ("pt-PT", "pt-PT"), "mx": ("es-MX", "es-MX"),
    "nl": ("nl-NL", "nl-NL"), "se": ("sv-SE", "sv-SE"), "no": ("nb-NO", "nb-NO"),
    "dk": ("da-DK", "da-DK"), "fi": ("fi-FI", "fi-FI"), "pl": ("pl-PL", "pl-PL"),
    "tr": ("tr-TR", "tr-TR"), "tw": ("zh-TW", "zh-TW"), "hk": ("zh-HK", "zh-HK"),
    "sg": ("en-SG", "en-SG"), "my": ("ms-MY", "ms-MY"), "th": ("th-TH", "th-TH"),
    "vn": ("vi-VN", "vi-VN"), "id": ("id-ID", "id-ID"), "at": ("de-AT", "de-AT"),
    "ch": ("de-CH", "de-CH"), "be": ("nl-BE", "nl-BE"), "ie": ("en-IE", "en-IE"),
}

_DEFAULT_LOCALE = ("en-US", "en-US")

_HEX16_RE = re.compile(r"^[0-9a-f]{16}$")


def storefront_locale(cc: str) -> tuple[str, str]:
    """Витрина ('gb', 'US', 'en-GB', 143444…) → (locale, language).

    Терпит и ISO-код, и уже собранный locale, и пустоту. Неизвестная страна →
    en-US: отпечаток всё равно уникальный (его несёт AndroidID), а некорректная
    локаль дороже — её wrapper может не принять."""
    raw = str(cc or "").strip().lower().replace("_", "-")
    if not raw:
        return _DEFAULT_LOCALE
    if raw in STOREFRONT_LOCALE:
        return STOREFRONT_LOCALE[raw]
    # «en-gb», «engb», «gb» уже разобраны выше; оставляем саму страну, если это
    # два символа — язык неизвестен, берём английский носитель этой страны.
    if len(raw) == 2 and raw.isalpha():
        region = raw.upper()
        return (f"en-{region}", f"en-{region}")
    # Если передали полноценный «xx-YY», вернём его как есть в оба поля.
    if "-" in raw:
        lang, _, region = raw.partition("-")
        loc = f"{lang.lower()}-{region.upper()}"
        return (loc, loc)
    return _DEFAULT_LOCALE


def android_id(account_id: str, salt: str) -> str:
    """Стабильный 16-ричный AndroidID из засоленного хеша идентификатора."""
    return hashlib.sha256(f"{salt}|{account_id}".encode("utf-8")).hexdigest()[:16]


def derive_device_info(account_id: str, salt: str, cc: str = "") -> str:
    """Собрать `-I` device-info для одной учётки. Чистая функция: тот же ввод →
    то же значение на любой машине и после любого рестарта (стабильность —
    требование Apple, см. шапку модуля)."""
    acct = str(account_id or "").strip()
    digest = hashlib.sha256(f"{salt}|{acct}".encode("utf-8")).hexdigest()
    api, model, build = ANDROID_DEVICES[int(digest[16:24], 16) % len(ANDROID_DEVICES)]
    locale, lang = storefront_locale(cc)
    aid = digest[:16]
    return f"{APP_NAME}/{APP_VERSION}/{OS_NAME}/{api}/{model}/{build}/{locale}/{lang}/{aid}"


# ── Реестр назначенных отпечатков (per-account persistence) ──────────────────
# Живёт рядом с identity-каталогами и login_blocks (тот же precedent), а не в
# config.yaml: это не секрет-пароль, но значение должно пережить рестарт и
# перемену кода, чтобы не двигать устройство у уже работающих учёток.

def _registry_path() -> Path:
    base = Path(os.environ.get("RIPSTER_BASE_DIR")
                or Path(__file__).resolve().parent.parent)
    return base / "dist" / "docker" / "wrapper_device_info.json"


def _key(account_id: str) -> str:
    return str(account_id or "").strip().lower()


def _load() -> dict:
    try:
        return json.loads(_registry_path().read_text(encoding="utf-8"))
    except Exception:
        return {}


def _store(data: dict) -> None:
    try:
        p = _registry_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
        tmp.replace(p)
    except Exception as e:                              # noqa: BLE001
        print(f"[device-info] реестр не записан: {e}", flush=True)


def get_assigned(account_id: str) -> str | None:
    """Раньше назначенный отпечаток или None. НИКОГДА не вычисляет сам —
    так слот 0 остаётся без `-I`, пока владелец явно не назначит."""
    return _load().get(_key(account_id))


def assign_new_account(account_id: str, salt: str, cc: str = "") -> str:
    """Уникальный отпечаток для НОВОГО аккаунта: если уже есть в реестре — он;
    иначе вывести детерминированно и запомнить (дальше не меняется)."""
    key = _key(account_id)
    reg = _load()
    if key in reg:
        return reg[key]
    value = derive_device_info(account_id, salt, cc)
    reg[key] = value
    _store(reg)
    return value


def owner_reroll(account_id: str, salt: str, cc: str = "") -> str:
    """Явное действие владельца («Сменить отпечаток»): ПРИНУДИТЕльно вывести
    заново и перезаписать в реестре. Для слота 0 — единственный путь получить
    `-I`. Считается новым устройством у Apple: применять осознанно, с предупреждением."""
    key = _key(account_id)
    value = derive_device_info(account_id, salt, cc)
    reg = _load()
    reg[key] = value
    _store(reg)
    return value


def looks_valid(device_info: str) -> bool:
    """Sanity-проверка собранной строки: 9 полей, AndroidID — 16 hex, поля
    локализации похожи на locale. Нужна тестам и, главное, чтобы в `-I` не
    уехал мусор, который wrapper отвергнет при логине."""
    parts = str(device_info or "").split("/")
    if len(parts) != 9:
        return False
    if not _HEX16_RE.match(parts[8]):
        return False
    for loc in parts[6:8]:
        if not re.match(r"^[a-z]{2}-[A-Z]{2}$", loc):
            return False
    return parts[0] == APP_NAME and parts[2] == OS_NAME
