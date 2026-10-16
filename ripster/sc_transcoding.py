"""SoundCloud-транскоды: одна таблица пресетов и один выбор лучшего.

Повод (25.09.2026): часовик «Anjunadeep — The Anjunadeep Edition 612» уехал в
папку «AAC 256», а ffprobe по файлу показал aac 160000 бит/с. Транскод
``aac_256k`` (quality=hq) у трека ЕСТЬ — его отдаёт Go+ учётка; бесплатная
учётка (primary-токен конфига) listing'ом до него не видит вовсе. DRM-движок
брал ПЕРВЫЙ попавшийся ``*-encrypted-hls`` без всякого порядка по пресету, а
токен ему подсовывался primary — то есть потолок срезали дважды и молча.

Здесь чистые функции, общие для мест, которые раньше выбирали каждая по-своему:
``sc_widevine_runner.py`` (загрузка DRM и не-DRM), ``routes/soundcloud.py``
(плеер и панель «что отдаст этот релиз») и ``runner.py`` (метка папки по факту).

Числа подтверждены замером, а не догадкой: ``aac_256k``/``abr_hq`` сняты
25.09.2026 с Go+ токена и перепроверены ffprobe по скачанному файлу;
``aac_hq`` — из замеров 22.09.2026 (см. ripster/quality_tiers.py). Пресета,
битрейт которого мы не мерили, в таблице нет: неизвестный пресет в гонке не
участвует (см. `pick`) и уезжает в ``unknown`` в панели.
"""
from __future__ import annotations

PRESETS: dict[str, tuple[str, int]] = {
    "aac_hq":   ("AAC", 256),
    "aac_256k": ("AAC", 256),
    "abr_hq":   ("AAC", 256),
    "aac_160k": ("AAC", 160),
    "abr_sq":   ("AAC", 128),
    "aac_96k":  ("AAC", 96),
    "mp3_1_0":  ("MP3", 128),
    "mp3_0_0":  ("MP3", 128),
}

#: Потолок, который покупает Go+; выше него сервис в этом тарифе не ходит.
GO_PLUS_KBPS = 256
#: Что отдаёт бесплатный слой (замер движка: «иначе AAC 160 или MP3 128»).
FREE_KBPS = 160


def preset_info(preset: str) -> "tuple[str, int] | None":
    """(кодек, кбит/с) для имени пресета; None — пресет не измерен нами."""
    return PRESETS.get(str(preset or ""))


def kbps_of(t: dict) -> int:
    """Замеренный битрейт транскода в кбит/с; 0 — пресет неизвестен."""
    info = preset_info((t or {}).get("preset"))
    return info[1] if info else 0


def is_snipped(t: dict) -> bool:
    """30-секундный отрывок вместо всего трека (SC помечает и полем, и quality)."""
    t = t or {}
    return bool(t.get("snipped")) or str(t.get("quality") or "") == "preview"


def is_drm(t: dict) -> bool:
    return "encrypted-hls" in str(((t or {}).get("format") or {}).get("protocol") or "")


def protocol(t: dict) -> str:
    return str(((t or {}).get("format") or {}).get("protocol") or "")


def _rank(t: dict):
    """Порядок: битрейт → не-DRM → явный aac_ вместо адаптивного abr_ →
    progressive вместо hls. Неизвестный пресет (0 кбит/с) проигрывает всем."""
    preset = str(t.get("preset") or "")
    return (
        kbps_of(t),
        0 if is_drm(t) else 1,
        0 if preset.startswith("abr_") else 1,
        1 if protocol(t) == "progressive" else 0,
    )


def pick(trans: list, go_plus: "bool | None" = None) -> "dict | None":
    """Лучший из ПРЕДОСТАВЛЕННЫХ-this-account транскодов, или None.

    ``go_plus`` — потолок учётки: False отсекает класс hq (сервис такой поток
    бесплатной учётке не отдаёт, и обмен на URL вернул бы 403), None — «тариф
    не измеряли», берём лучшее из видимого. SoundCloud сам фильтрует listing по
    учётке, поэтому обычно отсева не происходит; отсев здесь — защита от кэша,
    снятого другим токеном.
    """
    cands = [t for t in (trans or []) if isinstance(t, dict) and t.get("url")]
    if go_plus is False:
        cands = [t for t in cands if kbps_of(t) <= FREE_KBPS or kbps_of(t) == 0]
    if not cands:
        return None
    full = [t for t in cands if not is_snipped(t)]
    pool = full or cands
    return max(pool, key=_rank)


def folder_quality(codec: str, bit_rate: "int | str | None") -> str:
    """Идентификатор качества для ПАПКИ — по замеру файла, не по запрошенному
    пресету. "" — измерить нечем (нет ffprobe, пустой битрейт, незнакомый
    кодек): угадывать папку не будем, остаётся имя от движка."""
    try:
        kbps = int(float(bit_rate or 0)) // 1000
    except (TypeError, ValueError):
        return ""
    c = str(codec or "").lower()
    if kbps <= 0:
        return ""
    if c == "aac":
        if kbps >= 240: return "aac_256"
        if kbps >= 140: return "aac_160"
        if kbps >= 80:  return "aac_96"
        return ""
    if c == "mp3":
        return "mp3" if kbps >= 96 else ""
    return ""


def label(codec: str, kbps: int) -> str:
    """Человеческая метка замера для карточки и истории: «AAC 160»."""
    c = str(codec or "").upper()
    return f"{c} {int(kbps)}" if c and kbps else (c or "")
