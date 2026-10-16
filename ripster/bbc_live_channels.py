"""Каталог живых BBC-эфиров (worldwide HLS) — единый источник для движка,
планировщика и UI.

Формат ссылки — публичный адрес Akamai, которым BBC раздаёт собственное радио
по всему миру:

    https://as-hls-ww-live.akamaized.net/<pool>/live/ww/<ch>/<ch>.isml/
        <ch>-audio%3d320000.norewind.m3u8

`<pool>` привязан к каналу: с чужим пулом Akamai отвечает 404, с устаревшим —
410 (так в 2023-м умер всеобщий `pool_904`). Пулы собраны из открытых списков
BBC-стримов и проверены ответом 200 20.09.2026.
"""
from __future__ import annotations

# 320 кбит/с AAC-LC — единственный по-настоящему полный звук, который BBC
# отдаёт наружу (on-demand версия того же эфира — 96..102 кбит/с HE-AAC).
BITRATE = 320000

CHANNELS: list[dict] = [
    {"id": "bbc_radio_one",       "label": "BBC Radio 1"},
    {"id": "bbc_radio_two",       "label": "BBC Radio 2"},
    {"id": "bbc_radio_three",     "label": "BBC Radio 3"},
    {"id": "bbc_radio_fourfm",    "label": "BBC Radio 4"},
    {"id": "bbc_radio_five_live", "label": "BBC Radio 5 Live"},
    {"id": "bbc_6music",          "label": "BBC Radio 6 Music"},
    {"id": "bbc_1xtra",           "label": "BBC Radio 1Xtra"},
    {"id": "bbc_radio_four_extra","label": "BBC Radio 4 Extra"},
    {"id": "bbc_world_service",   "label": "BBC World Service"},
    {"id": "bbc_asian_network",   "label": "BBC Asian Network"},
]

_CHANNELS = {c["id"]: c for c in CHANNELS}

# канал → akamai-пул (см. docstring модуля)
_POOLS = {
    "bbc_radio_one":        "pool_01505109",
    "bbc_radio_two":        "pool_74208725",
    "bbc_radio_three":      "pool_23461179",
    "bbc_radio_fourfm":     "pool_55057080",
    "bbc_radio_five_live":  "pool_89021708",
    "bbc_6music":           "pool_81827798",
    "bbc_1xtra":            "pool_92079267",
    "bbc_radio_four_extra": "pool_26173715",
    "bbc_world_service":    "pool_87948813",
    "bbc_asian_network":    "pool_22108647",
}


def known(channel: str) -> bool:
    return channel in _CHANNELS


def label(channel: str) -> str:
    return (_CHANNELS.get(channel) or {}).get("label") or channel


def stream_url(channel: str, bitrate: int = BITRATE) -> str:
    """HLS-плейлист эфира. пустая строка — канал неизвестен (движок превратит
    это в честную ошибку, а не в исключение)."""
    pool = _POOLS.get(channel)
    if not pool:
        return ""
    return (f"https://as-hls-ww-live.akamaized.net/{pool}/live/ww/{channel}/"
            f"{channel}.isml/{channel}-audio%3d{bitrate}.norewind.m3u8")
