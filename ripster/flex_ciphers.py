"""Общие константы расшифровки Flex: их знают и сервер (отдаёт поток), и ПК-клиент (расшифровывает файл).

Вынесены в отдельный модуль, чтобы клиентской сборке (публичной) не нужен был серверный `flex_sources`
с его выпуском ссылок через рабочие учётки.
"""
from __future__ import annotations

import hashlib

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")

BLOCK = 2048                                   # Deezer: шифруется каждый третий блок по 2048 байт
BF_SECRET = b"g4el58wc0zvf9na1"
BF_IV = b"\x00\x01\x02\x03\x04\x05\x06\x07"


def deezer_bf_key(track_id: str) -> bytes:
    md5 = hashlib.md5(str(track_id).encode()).hexdigest().encode()
    return bytes(md5[i] ^ md5[i + 16] ^ BF_SECRET[i] for i in range(16))
