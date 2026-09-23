"""Тариф учётки → потолок качества, который этот тариф реально покупает.

Повод (22.09.2026). Владелец получил с SoundCloud 160 kbps на «RA.1057 Marina
Herlop» и принял это за баг. Бага не было: Go+ покупает AAC 256 только там, где
релиз отдаёт транскод ``aac_hq``; у этого трека его нет вовсе — сервис listing'ом
зоны ``aac_160k, aac_96k, abr_sq, mp3_1_0``. Аккаунт живой: probe
``/api/test-auth/soundcloud`` отвечает ``subscription: "Go+"``.

Почему маппинг здесь, а не в UI. Панель обязана показывать то, что измерено
пробой или обещано движком загрузки (``ripster/engines/*``). Две независимые
оценки качества — серверная и клиентская — разъезжаются на первом же изменении
тарифной сетки; UI получает пару ``tier`` + ``ceiling`` в готовом виде.

Правила честности (из AGENTS.md):

  • неизвестный тариф → ``None`` («не определено»), а не самая смелая догадка;
  • маппится только то, что подтверждено ответом сервиса или текстом движка;
  • флаг, выставленный «по умолчанию», а не измеренный, не участвует
    (см. ветку amazon — проба пишет lossless=True без проверки Unlimited).

Форма ceiling: ``{"codec": "AAC", "kbps": 256}`` — lossy;
``{"codec": "FLAC", "bits": 24, "khz": 192}`` — hi-res;
``{"kbps": 320}`` — когда битрейт известен, а кодек у тарифа бывает разный
у разных клиентов (Tidal Premium: MP3/AAC — не выдумываем).
"""
from __future__ import annotations


def _aac(kbps: int) -> dict: return {"codec": "AAC", "kbps": kbps}
def _mp3(kbps: int) -> dict: return {"codec": "MP3", "kbps": kbps}


def _flac_cd(codec: str = "FLAC") -> dict:
    return {"codec": codec, "kbps": 1411}


def _flac_hr(codec: str = "FLAC") -> dict:
    return {"codec": codec, "bits": 24, "khz": 192}


def _flag(flags: dict, name: str):
    v = (flags or {}).get(name)
    return v if isinstance(v, bool) else None


def _soundcloud(tier: str, flags: dict) -> dict | None:
    t = tier.lower()
    # Go+ — единственный тариф, которому сервис отдаёт aac_hq (AAC 256).
    if "go+" in t or _flag(flags, "go_plus") is True:
        return _aac(256)
    # Без Go+ поток всё равно доходит до AAC 160 — это не покупка тарифа,
    # а то, что отдаёт бесплатный слой (замер движка: «иначе AAC 160 или
    # MP3 128 — что отдаст сервис»). Промежуточный «Go» не маппим: чем он
    # платит в 2026, подтверждений нет.
    if _flag(flags, "go_plus") is False or t.startswith("free"):
        return _aac(160)
    return None


def _tidal(tier: str, flags: dict) -> dict | None:
    q = str((flags or {}).get("quality") or "").upper()
    t = tier.upper()
    if q in ("HI_RES_LOSSLESS", "HI_RES") or t in ("MAX", "HI_FI_PLUS", "HIFI_PLUS"):
        return _flac_hr()
    if _flag(flags, "hires") is True:
        return _flac_hr()
    if q == "LOSSLESS" or _flag(flags, "lossless") is True or t in ("HI_FI", "HIFI", "PLUS"):
        return _flac_cd()
    # INTRO — проморка с ограниченным качеством, чем именно платит — не мерали.
    if q == "HIGH" or t == "PREMIUM":
        return {"kbps": 320}
    return None


def _qobuz(tier: str, flags: dict) -> dict | None:
    t = tier.lower()
    if _flag(flags, "hires") is True or "studio" in t:
        return _flac_hr()
    if _flag(flags, "lossless") is True or "pass" in t:
        return _flac_cd()
    return None


def _deezer(tier: str, flags: dict) -> dict | None:
    t = tier.lower()
    if _flag(flags, "lossless") is True:
        return _flac_cd()
    if "free" in t:
        return _mp3(128)
    # Платные тарифы Deezer (Premium/Family и бандлы вроде «Itau bundle — …
    # Premium») — 320 kbps MP3; lossless сверху уже пойман флагом выше.
    if any(w in t for w in ("premium", "family", "abo")) or _flag(flags, "lossless") is False:
        return _mp3(320)
    return None


def _apple(tier: str, flags: dict) -> dict | None:
    # Каждая платная подписка Apple Music включает lossless до 24 бит/192 кГц —
    # это не свойство тарифа, а свойство сервиса; но показывать потолок можно
    # только когда подписка измеренно активна (иначе — см. honesty-правило
    # обзора: истёкшая учётка читается сломанной, а не старым тарифом).
    if _flag(flags, "sub_active") is True or _flag(flags, "hires") is True \
            or _flag(flags, "lossless") is True:
        return _flac_hr("ALAC")
    return None


def _spotify(tier: str, flags: dict) -> dict | None:
    # «Premium (OGG)» — метка, которую проба снимает с живой librespot-сессии.
    if "premium" in tier.lower():
        return {"codec": "Ogg Vorbis", "kbps": 320}
    return None


def _yandex(tier: str, flags: dict) -> dict | None:
    if _flag(flags, "lossless") is True:      # Яндекс Плюс = FLAC доступен
        return _flac_cd()
    return None


def _amazon(tier: str, flags: dict) -> dict | None:
    return None   # проба пишет lossless=True без проверки Unlimited — не верим


_HANDLERS = {
    "soundcloud": _soundcloud,
    "tidal":      _tidal,
    "qobuz":      _qobuz,
    "deezer":     _deezer,
    "apple":      _apple,
    "spotify":    _spotify,
    "yandex":     _yandex,
    "amazon":     _amazon,
}


def ceiling(svc: str, tier: str = "", flags: dict | None = None) -> dict | None:
    """Что покупает тариф ``tier`` сервиса ``svc`` — максимум, не обещание по
    каждому релизу. ``None`` — «не определено»; молчание честнее догадки."""
    fn = _HANDLERS.get((svc or "").lower())
    if fn is None:
        return None
    try:
        return fn(str(tier or "").strip(), flags or {})
    except Exception:                               # noqa: BLE001
        return None
