# -*- coding: utf-8 -*-
"""Apple Wrapper Lite: тот же Go-загрузник, но ключи с локального lite-сервера.

Движок — опциональный (``apple-wrapper = lite``, по умолчанию выключен) и
устроен как надстройка над zhaarey: команда, разбор лога и качества общие,
различается только источник расшифровки — вместо Docker-враппера с TCP-
протоколом ours ``ripster/lite_shim.py`` (он же шифрованный кэш ключей
``ripster/lite.py``, он же Temari). Публичный wm.wol.moe к Lite не имеет
отношения и остаётся manual-only.
"""
from __future__ import annotations

import base64
import subprocess
from pathlib import Path

from .registry import register
from .zhaarey import ZhaereyEngine
from ripster import lite, lite_shim


# ── локальная расшифровка скачанного без Go-загрузчика ──────────────────────
# Temari расшифровывает ОДИН сэмпл за вызов: whole-сегмент в неё суют только
# после того, как Go сам нарезал его по trun/senc (cbcsStripeDecrypt в
# runv2.go). Сырые байты fMP4, отданные Temari целиком, превращаются в мусор
# той же длины — на этом живая проверка 25.09.2026 и споткнулась. Без Go
# корректный per-sample CBCS/CENC умеет только Bento4 (`mp4decrypt`), он читает
# таблицы moof/senc сам.

def _bento4(name: str) -> str:
    from ripster.setup import tool_path
    p = tool_path(name)
    if not p:
        raise lite.LiteError(f"Bento4 ({name}) не установлен — "
                             "открой Setup → «Bento4 (mp4decrypt)»")
    return p


def kid_from_mp4(data: bytes) -> str:
    """default_KID из первого tenc (ISO/IEC 14496-15: fourcc, ver/flags(4),
    reserved(2), is-encrypted(1), iv-size(1), KID(16)); '' — если бокса нет."""
    i = data.find(b"tenc")
    if i < 0 or len(data) < i + 28:
        return ""
    return data[i + 12:i + 28].hex()


def _key_hex(content_key: str) -> str:
    """contentKey из /key → hex для mp4decrypt.

    32 hex (тестовые/исторические ответы) и base64 на голые 16 байт — просто
    ключ. Живой wm.wol.moe отдаёт «Apple-форму» — persistent key
    SVFootHillPKey.ckc (playback.cpp get_content_key_impl отдаёт его дословно;
    доказано 25.09: base64, первые 4 байт 00000001, длина ≠ 16): 4-байтный
    BE-заголовок версии, затем 16-байтный content key, далее хвост (IV/поля
    формата) — берём окно после заголовка. Хвост при этом проверяется: голый
    ключ в Apple-форме — единственный расклад, который mp4decrypt вообще
    может использовать; если Apple положил перед ключом ещё что-то, proof
    честно упадёт на декоде, а не отдаст мусор.
    """
    k = str(content_key or "").strip()
    if len(k) == 32 and all(c in "0123456789abcdef" for c in k.lower()):
        return k.lower()
    try:
        raw = base64.b64decode(k + "=" * (-len(k) % 4), validate=True)
    except Exception:                                       # noqa: BLE001
        raw = b""
    if len(raw) == 16:
        return raw.hex()
    if len(raw) >= 20 and raw[:4] == b"\x00\x00\x00\x01":
        return raw[4:20].hex()
    raise lite.LiteError(
        "contentKey от lite — ни 32 hex, ни base64 на 16 байт, ни Apple-форма "
        f"(длина строки {len(k)}, декодировано {len(raw)} байт"
        + (f", первые 4: {raw[:4].hex()}" if len(raw) >= 4 else "") + ")")


def decrypt_fmp4(parts, content_key: str, out_path) -> Path:
    """init + сегменты (фрагментированный MP4 в произвольной нарезке чанков) +
    contentKey из /key → обычный MP4/M4A per-sample-расшифровкой mp4decrypt.
    ALAC и AAC — для контейнера путь один. Падает честно: LiteError."""
    blob = b"".join(parts)
    key = _key_hex(content_key)
    kid = kid_from_mp4(blob)
    if not kid:
        raise lite.LiteError("в потоке нет tenc/default_KID — расшифровать нечем")
    out_path = Path(out_path)
    # ANSI-argv Bento4 не переживает кириллические пути (см. process_runner),
    # поэтому временный шифротекст живёт строго рядом с результатом.
    enc = out_path.with_name(out_path.name + ".enc")
    enc.write_bytes(blob)
    try:
        r = subprocess.run(
            [_bento4("mp4decrypt"), "--key", f"{kid}:{key}",
             str(enc), str(out_path)],
            capture_output=True, timeout=600)
        if r.returncode != 0:
            err = (r.stderr or r.stdout or b"").decode("utf-8", "replace")
            raise lite.LiteError(
                f"mp4decrypt вышел с кодом {r.returncode}: {err.strip()[:200]}")
    finally:
        try:
            enc.unlink()
        except OSError:
            pass
    if not out_path.exists() or out_path.stat().st_size == 0:
        raise lite.LiteError("mp4decrypt отдал пустой файл")
    return out_path


@register
class WrapperLiteEngine(ZhaereyEngine):
    name = "lite"

    def build_cmd(self, url: str, quality: str, config: dict) -> list[str]:
        # Поднимаем оракул ДО запуска Go: падение здесь — честная ошибка
        # задачи («Lite-оракул не поднялся: …»), а не тихий уход на враппер.
        lite_shim.ensure(config)
        return super().build_cmd(url, quality, config)

    def working_dir(self) -> str | None:
        # config.yaml с портами оракула пишет ensure(); без него — обычный cwd.
        return lite_shim.cwd_dir()
