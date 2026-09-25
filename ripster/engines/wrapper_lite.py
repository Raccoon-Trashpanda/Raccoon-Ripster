# -*- coding: utf-8 -*-
"""Apple Wrapper Lite: тот же Go-загрузник, но ключи с локального lite-сервера.

Движок — опциональный (``apple-wrapper = lite``) и устроен как надстройка над
zhaarey: команда, разбор лога и качества общие, различается только источник
расшифровки — вместо Docker-враппера с TCP-протоколом ours ``ripster/lite_shim.py``
(он же шифрованный кэш ключей ``ripster/lite.py``, он же Temari).

Тем же движком с 02.09.2026 ездят и режимы публичного враппера: wm.wol.moe
перешёл на Wrapper-Lite HTTP API (gRPC-клиент `amd` больше некому звать), и
для таких задач конфиг-вью несёт `_lite_public` — оракул смотрит в публичный
сервер с Bearer-ключом вместо петли.
"""
from __future__ import annotations

import base64
import subprocess
from pathlib import Path

from .base import EngineResult
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
    ключ. Живой wm.wol.moe отдаёт НЕ его: contentKey — persistent key
    SVFootHillPKey.ckc (base64, заголовок 00000001, в измеренном ответе —
    1084 байт), обёрнутый FairPlay-ключом устройства. 25.09.2026 по живому
    шифротексту перебраны ВСЕ 1069 окон по 16 байт через mp4decrypt+ffmpeg:
    чистых — 0 (минимум — 225 строк ошибок декода, мусор). Голых ключей в
    blob'е нет, «окно после заголовка» не расшифровывает — наружу mp4decrypt
    годится только для форматов выше; реальный путь такой формы — шаблон
    ctx/state через оракул Temari (ripster/lite_shim.py), как в продукте."""
    k = str(content_key or "").strip()
    if len(k) == 32 and all(c in "0123456789abcdef" for c in k.lower()):
        return k.lower()
    try:
        raw = base64.b64decode(k + "=" * (-len(k) % 4), validate=True)
    except Exception:                                       # noqa: BLE001
        raw = b""
    if len(raw) == 16:
        return raw.hex()
    if len(raw) >= 4 and raw[:4] == b"\x00\x00\x00\x01":
        raise lite.LiteError(
            "contentKey от lite — Apple-form (persistent key CKC, обёрнут "
            f"ключом устройства; {len(raw)} байт). mp4decrypt его развернуть "
            "не может — расшифровка только через шаблон ctx/state Temari")
    raise lite.LiteError(
        "contentKey от lite — ни 32 hex, ни base64 на 16 байт "
        f"(длина строки {len(k)}, декодировано {len(raw)} байт"
        + (f", первые 4: {raw[:4].hex()}" if len(raw) >= 4 else "") + ")")


def decrypt_fmp4(parts, content_key: str, out_path, keep_enc: bool = False) -> Path:
    """init + сегменты (фрагментированный MP4 в произвольной нарезке чанков) +
    contentKey из /key → обычный MP4/M4A per-sample-расшифровкой mp4decrypt.
    ALAC и AAC — для контейнера путь один. Падает честно: LiteError.

    keep_enc — оставить шифротекст рядом с результатом (.enc): офлайн-разбор
    «какое окно blob'а было ключом» возможен только пока лежат оригинальные
    байты CDN; mp4decrypt их перезаписывает на месте, восстановить их потом
    нечем. Продуктовая загрузка его не просит — файл не плодится сам собой."""
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
        if not out_path.exists() or out_path.stat().st_size == 0:
            raise lite.LiteError("mp4decrypt отдал пустой файл")
    finally:
        if not keep_enc:
            try:
                enc.unlink()
            except OSError:
                pass
    return out_path


@register
class WrapperLiteEngine(ZhaereyEngine):
    name = "lite"

    def build_cmd(self, url: str, quality: str, config: dict) -> list[str]:
        # Поднимаем оракул ДО запуска Go: падение здесь — честная ошибка
        # задачи («Lite-оракул не поднялся: …»), а не тихий уход на враппер.
        shim = lite_shim.ensure(config)
        # Отметка «что сервер сказал ДО этой попытки»: вердикт в is_finished
        # имеет право ссылаться только на свежий отказ.
        self._cfg_view = config
        self._oracle_seq_before = int(
            (getattr(shim.client, "error_state", lambda: {})() or {}).get("seq") or 0)
        return super().build_cmd(url, quality, config)

    def working_dir(self) -> str | None:
        # config.yaml с портами оракула пишет ensure(); без него — обычный cwd.
        return lite_shim.cwd_dir()

    def is_finished(self, log_text: str, rc: int = -1) -> EngineResult:
        res = super().is_finished(log_text, rc=rc)
        if res.success:
            return res
        # Go на пустой ответ оракула ругается на сам релиз («Unavailable»,
        # «Failed to dl»); настоящая причина — HTTP-отказ key-сервера, и
        # знает её только LiteClient.
        try:
            st = lite_shim.oracle_state(None, getattr(self, "_oracle_seq_before", 0))
        except Exception:                                        # noqa: BLE001
            return res
        if int(st.get("status") or 0) == 429:
            res.error = "Публичный wrapper: квота wm.wol.moe исчерпана (429) — " \
                        "повтори позже, частыми попытками она не возвращается"
            lite.note_public_429({}, getattr(lite_shim._shim, "client", None))
        elif int(st.get("status") or 0) in (401, 403):
            res.error = "Публичный wrapper: wm.wol.moe не пускает наш ключ " \
                        "(401) — проверь Настройки → Apple → API-ключ wm.wol.moe"
        elif st.get("msg"):
            res.error = f"Публичный wrapper: {st['msg']}"
        return res
