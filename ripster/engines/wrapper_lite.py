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
import struct as _struct
import subprocess
from pathlib import Path

from .base import EngineResult
from .registry import register
from .zhaarey import ZhaereyEngine
from ripster import lite, lite_shim


# ── локальная расшифровка скачанного без Go-загрузчика ──────────────────────
# Temari за один вызов кладёт кусок ЗАЩИЩЕННЫХ байт ОДНОГО под-сэмпла:
# whole-сегмент в неё суют только после нарезки по moof/trun/senc (cbcsStripeDecrypt
# в runv2.go). Сырые байты fMP4, отданные Temari целиком, превращаются в мусор
# той же длины — на этом живая проверка 25.09.2026 и споткнулась. Поэтому ниже
# два пути: mp4decrypt (Bento4) — когда ключ наружу голый, и per-sample
# Temari-оракул (decrypt_fmp4_temari) — когда contentKey это Apple-форма CKC
# и единственный способ расшифровать — шаблон ctx/state, как в продукте.

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


# ── per-sample CBCS без Go: тот же раскрой, что делает runv2.go ──────────────
# Парсер читает init (sinf/schm/tenc) и каждый moof (tfhd/trun/senc), режет
# сэмплы на под-сэмплы и зовёт оракул ОДНИМ вызовом на каждый защищённый
# диапазон — ровно как cbcsDecryptSample→cbcsStripeDecrypt в Go: сначала
# собираются паттерн-блоки (crypt:skip, у Apple Audio — 1:9), они уходят
# сплошным куском, обратный поток раскладывается на те же места. Порядок
# вызовов важен: состояние IV живёт в шаблоне Temari, а не в аргументе.
# На выходе — честный clear fMP4: sinf/senc/saiz/saio/seig-группы вырезаны,
# trun.data_offset исправлен на размер выброшенного (как DecryptFragment).

_CONTAINERS = {b"moov": 0, b"trak": 0, b"mdia": 0, b"minf": 0, b"stbl": 0,
               b"dinf": 0, b"edts": 0, b"mvex": 0, b"udta": 0,
               b"moof": 0, b"mfra": 0, b"traf": 0, b"sinf": 0, b"schi": 0,
               b"stsd": 8, b"mdri": 0}
_STRIP_INIT = {b"sinf", b"pssh"}
_STRIP_MOOF = {b"senc", b"saiz", b"saio", b"pssh", b"psshs"}
_PIFF_SENC = bytes.fromhex("9a04e0709aa34a2e94439dd2bc2ee50e")


class _Box:
    __slots__ = ("typ", "lo", "hi", "plo")

    def __init__(self, typ, lo, hi, plo):
        self.typ, self.lo, self.hi, self.plo = typ, lo, hi, plo


def _iter_boxes(data, lo, hi):
    """Боксы верхнего уровня диапазона (молча останавливается на мусоре)."""
    out = []
    while hi - lo >= 8:
        size = int.from_bytes(data[lo:lo + 4], "big")
        typ = bytes(data[lo + 4:lo + 8])
        hdr = 8
        if size == 1:
            if hi - lo < 16:
                return out
            size = int.from_bytes(data[lo + 8:lo + 16], "big")
            hdr = 16
        elif size == 0:
            size = hi - lo
        if size < hdr or lo + size > hi:
            return out
        out.append(_Box(typ, lo, lo + size, lo + hdr))
        lo += size
    return out


def _sample_entry_prefix(data, bx):
    """SampleEntry (дети stsd): после заголовка идёт служебный префикс —
    28 байт у аудио (SampleEntry 8 + AudioSampleEntry 20), 86 у видео.
    Берём тот, при котором дети ровно покрывают бокс."""
    for pre in (28, 86):
        kids = _iter_boxes(data, bx.plo + pre, bx.hi)
        if kids and kids[-1].hi == bx.hi:
            return pre
        if not kids and data[bx.plo + 4:bx.plo + 8] in (b"mp4a", b"alac",
                                                        b".mp3", b"ac-3"):
            return pre  # пустой энтри без детей
    return None


def _child_payload(data, bx, parent_typ):
    """С какого байта дети бокса (None — бокс не контейнер)."""
    if parent_typ == b"stsd":
        pre = _sample_entry_prefix(data, bx)
        return None if pre is None else bx.plo + pre
    pre = _CONTAINERS.get(bx.typ)
    return None if pre is None else bx.plo + pre


def _find_box(data, lo, hi, typ):
    stack = [(lo, hi, None)]
    while stack:
        a, b, parent = stack.pop()
        for bx in _iter_boxes(data, a, b):
            if bx.typ == typ:
                return bx
            c = _child_payload(data, bx, parent)
            if c is not None:
                stack.append((c, bx.hi, bx.typ))
    return None


def _parse_tenc(data, bx):
    """tenc (ISO/IEC 14496-15): verflags, reserved(2), is_enc(1), iv_size(1),
    KID(16), хвост. Хвост бывает трёх видов: два байта crypt/skip (v1 по
    спеке), либо — и в живом Apple-потоке, и в фикстуре Bento4 — length-
    prefixed константный IV («10» + 16 байт) при iv_size=0. IV движку не
    нужен (он в шаблоне Temari), но спутать его с паттерном нельзя:
    «16:210» вместо сплошного шифрования расшифровывает мусор. (0,0) =
    сплошное шифрование — тот же переход, что cbcsDecryptRaw по
    skipBlockLen==0 в Go."""
    ver = data[bx.plo]
    p = bx.plo + 4
    is_enc = data[p + 2]
    iv_size = data[p + 3]
    kid = bytes(data[p + 4:p + 20])
    rest = bytes(data[p + 20:bx.hi])
    const_iv = b""
    crypt = skip = 0
    if iv_size and is_enc and len(rest) >= iv_size:
        const_iv, rest = rest[:iv_size], rest[iv_size:]
    if len(rest) >= 1 and rest[0] in (8, 16) and len(rest) == 1 + rest[0]:
        const_iv = rest[1:]
    elif len(rest) >= 2 and (ver >= 1 or any(rest[:2])):
        crypt, skip = rest[0], rest[1]
    return {"is_enc": is_enc, "iv_size": iv_size, "kid": kid.hex(),
            "const_iv": const_iv, "crypt": crypt, "skip": skip, "ver": ver}


def _init_tracks(data):
    """track_ID → tenc дорожки из init (sinf в stbl, KID из первого tenc)."""
    tracks = {}
    moov = _find_box(data, 0, len(data), b"moov")
    if moov is None:
        return tracks
    for top in _iter_boxes(data, moov.plo, moov.hi):
        if top.typ != b"trak":
            continue
        tkhd = _find_box(data, top.lo, top.hi, b"tkhd")
        if tkhd is None:
            continue
        ver = data[tkhd.plo]
        off = tkhd.plo + 4 + (8 if ver else 4) + (8 if ver else 4)
        track_id = int.from_bytes(data[off:off + 4], "big")
        sinf = _find_box(data, top.lo, top.hi, b"sinf")
        if sinf is None:
            continue
        tenc = _find_box(data, sinf.lo, sinf.hi, b"tenc")
        if tenc is not None:
            tracks[track_id] = _parse_tenc(data, tenc)
    return tracks


def _parse_senc(data, bx, tenc, n_samples):
    """senc → [[(clear, prot), ...]|None, ...] на сэмпл. Живой Apple-поток
    устроен хитро: tenc объявляет per-sample IV = 0, а флаг под-сэмплного
    шифрования — 0x2 (как его читает mp4ff в продукте), пара = clear(u16) +
    prot(u32). ISO-вариант (0x1, prot u24) и Piff (u16+u16, count из саmples)
    тоже встречаются — поэтому Layout НЕ берётся с веры: перебираем
    (наличие под-сэмплов, длина IV, ширина prot) и принимается только тот,
    что разбирает ровно весь бокс. Placeholder senc с count=0 (фикстура
    Bento4) — законное «записей нет, сэмплы целиком»."""
    flags = int.from_bytes(data[bx.plo + 1:bx.plo + 4], "big")
    uuid_senc = bx.typ == b"uuid"
    if uuid_senc and bytes(data[bx.plo:bx.plo + 16]) != _PIFF_SENC:
        return None
    p = bx.plo + (16 if uuid_senc else 4)
    if uuid_senc:
        count = n_samples
    else:
        count = int.from_bytes(data[p:p + 4], "big")
        p += 4
    if count < 0 or (n_samples and count != n_samples and not uuid_senc):
        return None
    if count == 0:
        return []
    rem = bx.hi - p
    if rem < 0 or (rem % count):
        pass  # не делится нацело — под-сэмплы неровные, решатель сам подберёт
    if flags & 3:
        for pw in (4, 3, 2):
            for iv in {tenc["iv_size"], 0, 8, 16}:
                got = _senc_layout(data, p, count, iv, True, bx.hi, pw)
                if got is not None:
                    return got
    for iv in {tenc["iv_size"], 8, 16, 0}:
        got = _senc_layout(data, p, count, iv, False, bx.hi, 0)
        if got is not None:
            return got
    return None


def _senc_layout(data, p, count, iv_size, subs, end, prot_width):
    out = []
    for _ in range(count):
        p += iv_size
        if not subs:
            out.append(None)
            continue
        if p + 2 > end:
            return None
        n = int.from_bytes(data[p:p + 2], "big")
        p += 2
        pairs = []
        for _k in range(n):
            if p + 2 + prot_width > end:
                return None
            clear = int.from_bytes(data[p:p + 2], "big")
            prot = int.from_bytes(data[p + 2:p + 2 + prot_width], "big")
            p += 2 + prot_width
            pairs.append((clear, prot))
        out.append(pairs)
    return out if p == end else None


def _parse_trun_fields(data, bx):
    """Позиция поля data_offset бокса trun (для патча); None — флага 0x001 нет."""
    flags = int.from_bytes(data[bx.plo + 1:bx.plo + 4], "big")
    if flags & 0x001:
        return bx.plo + 8           # verflags(4) + count(4)
    return None


def _read_tfhd(data, bx):
    """tfhd → (track_id, base_offset|None, default_size|None). base_offset
    flag 0x01 — АБСОЛЮТНЫЙ смещён в файле; без него отсчёт — начало moof."""
    flags = int.from_bytes(data[bx.plo + 1:bx.plo + 4], "big")
    p = bx.plo + 4
    track_id = int.from_bytes(data[p:p + 4], "big")
    p += 4
    base = None
    if flags & 0x01:
        base = int.from_bytes(data[p:p + 8], "big")
        p += 8
    if flags & 0x02:
        p += 4
    if flags & 0x08:
        p += 4
    def_size = None
    if flags & 0x10:
        def_size = int.from_bytes(data[p:p + 4], "big")
    return track_id, base, def_size


def _read_trun(data, bx):
    """trun → (count, data_offset|None, sizes[sample_size|None])."""
    flags = int.from_bytes(data[bx.plo + 1:bx.plo + 4], "big")
    p = bx.plo + 4
    count = int.from_bytes(data[p:p + 4], "big")
    p += 4
    doff = None
    if flags & 0x001:
        doff = int.from_bytes(data[p:p + 4], "big", signed=True)
        p += 4
    if flags & 0x004:
        p += 4
    sizes = []
    for _ in range(count):
        if flags & 0x100:
            p += 4
        if flags & 0x200:
            sizes.append(int.from_bytes(data[p:p + 4], "big"))
            p += 4
        else:
            sizes.append(None)
        if flags & 0x400:
            p += 4
        if flags & 0x800:
            p += 4
    return count, doff, sizes


def _stripe_decrypt(buf, lo, size, dec, skip, call):
    """Один вызов оракула на защищённый диапазон — байт-в-байт как
    cbcsFullSubsampleDecrypt/cbcsStripeDecrypt в runv2.go. Возвращает число
    байт, ушедших оракулу (0 — диапазон слишком мал, Go его тоже не шлёт)."""
    if size <= 0:
        return 0
    if skip == 0:
        # сплошное шифрование (Apple Music ALAC): донце короче 16 байт остаётся claro
        tlen = size & ~0xF
        if tlen:
            plain = call(bytes(buf[lo:lo + tlen]))
            if len(plain) != tlen:
                raise lite.LiteError(
                    f"оракул вернул {len(plain)} байт вместо {tlen}")
            buf[lo:lo + tlen] = plain
        return tlen
    if dec <= 0:
        raise lite.LiteError("cbcs: crypt=0 при nonzero skip — паттерн нечитаем")
    pos, blocks = 0, []
    while True:
        if size - pos < dec:
            break
        blocks.append(bytes(buf[lo + pos:lo + pos + dec]))
        pos += dec
        if size - pos < skip:
            break
        pos += skip
    if not blocks:
        return 0
    blob = b"".join(blocks)
    plain = call(blob)
    if len(plain) != len(blob):
        raise lite.LiteError(f"оракул вернул {len(plain)} байт вместо {len(blob)}")
    pos, i = 0, 0
    while True:
        if size - pos < dec:
            break
        buf[lo + pos:lo + pos + dec] = plain[i * dec:(i + 1) * dec]
        i += 1
        pos += dec
        if size - pos < skip:
            break
        pos += skip
    return len(blob)


def _rebuild_strip(data, lo, hi, strip, parent_typ=None):
    """Копия диапазона боксов с вырезанными `strip`. Размеры контейнеров
    пересчитываются, служебные префиксы сохраняются как есть: у stsd —
    verflags+entry_count (число детей правится по факту), у SampleEntry —
    28/86 байт заголовка кодека. Терять их нельзя: ffmpeg видит «encl»
    вместо «mp4a» и выдаёт codec unknown."""
    out = bytearray()
    for bx in _iter_boxes(data, lo, hi):
        if bx.typ in strip:
            continue
        if bx.typ == b"uuid" and bytes(data[bx.plo:bx.plo + 16]) == _PIFF_SENC:
            continue
        c = _child_payload(data, bx, parent_typ)
        if c is None:
            out += data[bx.lo:bx.hi]
            continue
        inner = _rebuild_strip(data, c, bx.hi, strip, bx.typ)
        head = bytearray()
        if bx.typ == b"stsd":
            head += data[bx.plo:bx.plo + 4]
            n = len(_iter_boxes(inner, 0, len(inner)))
            head += n.to_bytes(4, "big")
        elif parent_typ == b"stsd":
            head += data[bx.plo:c]
        hdr_len = bx.plo - bx.lo
        new_len = hdr_len + len(head) + len(inner)
        if hdr_len == 16:
            out += (1).to_bytes(4, "big") + bx.typ + new_len.to_bytes(8, "big")
        else:
            out += new_len.to_bytes(4, "big") + bx.typ
        out += head + inner
    return bytes(out)


def _patch_frag_offsets(moof_bytes, own, preceding=0):
    """trun data_offset — от начала moof: moof похудел на `own`, mdat уехал
    влево на столько же (Go: trun.DataOffset -= bytesRemoved). tfhd
    base_offset — абсолютный адрес в файле, сюда давит и всё, что съедено
    левее фрагмента (`preceding`)."""
    m = bytearray(moof_bytes)
    for top in _iter_boxes(m, 0, len(m)):
        if top.typ != b"moof":
            continue
        for traf in _iter_boxes(m, top.plo, top.hi):
            if traf.typ != b"traf":
                continue
            for ch in _iter_boxes(m, traf.plo, traf.hi):
                if ch.typ == b"tfhd" and \
                        int.from_bytes(m[ch.plo + 1:ch.plo + 4], "big") & 0x01:
                    pos = ch.plo + 8          # verflags(4) + track_ID(4)
                    v = int.from_bytes(m[pos:pos + 8], "big")
                    m[pos:pos + 8] = (v - preceding - own).to_bytes(8, "big")
                elif ch.typ == b"trun":
                    pos = _parse_trun_fields(m, ch)
                    if pos is not None:
                        v = int.from_bytes(m[pos:pos + 4], "big", signed=True)
                        m[pos:pos + 4] = (v - own).to_bytes(4, "big",
                                                            signed=True)
    return bytes(m)


def decrypt_fmp4_temari(parts, key_data, out_path, decrypt=None,
                        keep_enc: bool = False, stats: dict | None = None):
    """init+сегменты (как их отдаёт CDN) + data-объект /key (ctx/state) →
    clear MP4 per-sample-расшифровкой через Temari-оракул. `decrypt(chunk)` —
    вызов оракула (по умолчанию — боевой lite_shim._crypto с этим шаблоном);
    тесты подставляют поддельный с известным ключом. Возвращает Path."""
    blob = bytearray(b"".join(parts))
    if decrypt is None:
        def decrypt(chunk):
            return lite_shim._crypto.decrypt(key_data, chunk)
    tops = _iter_boxes(blob, 0, len(blob))
    first_moof = next((i for i, bx in enumerate(tops)
                       if bx.typ == b"moof"), None)
    if not first_moof:
        raise lite.LiteError("в потоке нет moof после init — расшифровывать "
                             "нечего или init потерян")
    init_part = bytes(blob[tops[0].lo:tops[first_moof].lo])
    tracks = _init_tracks(init_part)
    enc_tracks = [t for t in tracks.values() if t["is_enc"]]
    if not enc_tracks:
        raise lite.LiteError("в init нет зашифрованных дорожек (sinf/tenc) — "
                             "расшифровать нечем")
    t0 = enc_tracks[0]

    # ── фаза 1: per-sample расшифровка in place (порядок вызовов = Go) ──
    calls = samples = prot_bytes = 0
    for bx in tops[first_moof:]:
        if bx.typ != b"moof":
            continue
        for traf in _iter_boxes(blob, bx.plo, bx.hi):
            if traf.typ != b"traf":
                continue
            kids = _iter_boxes(blob, traf.plo, traf.hi)
            tfhd = next((c for c in kids if c.typ == b"tfhd"), None)
            truns = [c for c in kids if c.typ == b"trun"]
            senc = next((c for c in kids
                         if c.typ == b"senc"
                         or (c.typ == b"uuid" and
                             bytes(blob[c.plo:c.plo + 16]) == _PIFF_SENC)),
                        None)
            if tfhd is None or not truns:
                raise lite.LiteError("в traf нет tfhd/trun — поток не "
                                     "фрагментирован по ISO 14496-12")
            track_id, base, def_size = _read_tfhd(blob, tfhd)
            tenc = tracks.get(track_id, t0)
            n_samples = sum(_read_trun(blob, t)[0] for t in truns)
            entries = None
            if senc is not None and tenc["is_enc"]:
                entries = _parse_senc(blob, senc, tenc, n_samples)
                if entries is None:
                    raise lite.LiteError(
                        f"senc не разложился ({n_samples} сэмплов, iv "
                        f"{tenc['iv_size']}) — непривычная раскладка бокса")
            if not tenc["is_enc"]:
                continue
            dec_len, skip_len = tenc["crypt"] * 16, tenc["skip"] * 16
            pos = (base if base is not None else bx.lo)
            k = 0
            for t in truns:
                count, doff, sizes = _read_trun(blob, t)
                if doff is not None:
                    pos = (base if base is not None else bx.lo) + doff
                for sz0 in sizes:
                    sz = sz0 if sz0 is not None else def_size
                    pairs = entries[k] if entries and k < len(entries) else None
                    k += 1
                    if pairs:
                        total = sum(c + p for c, p in pairs)
                        if sz is None:
                            sz = total
                        elif sz != total:
                            raise lite.LiteError(
                                f"сэмпл {k}: trun обещает {sz} байт, "
                                f"senc — {total}")
                        rng = []
                        off = 0
                        for clear, prot in pairs:
                            off += clear
                            if prot:
                                rng.append((off, prot))
                            off += prot
                    else:
                        # без под-сэмплов: весь сэмпл защищён (путь Go при
                        # len(subSamplePatterns)==0)
                        if sz is None:
                            raise lite.LiteError(
                                f"сэмпл {k}: ни размера в trun, ни под-сэмплов")
                        rng = [(0, sz)]
                    for off, prot in rng:
                        sent = _stripe_decrypt(blob, pos + off, prot,
                                               dec_len, skip_len, decrypt)
                        if sent:
                            calls += 1
                        prot_bytes += sent
                    samples += 1
                    pos += sz
    if not samples:
        raise lite.LiteError("ни одного сэмпла не прошло через оракул — "
                             "расшифровка не подтверждена")

    # ── фаза 2: clear-файл — вырезанные боксы шифрования + честные offset ──
    new_init = _rebuild_strip(init_part, 0, len(init_part), _STRIP_INIT)
    shift = len(init_part) - len(new_init)      # сколько байт уже съедено слева
    out = bytearray(new_init)
    for i, bx in enumerate(tops):
        if i < first_moof:
            continue
        if bx.typ == b"moof":
            new_moof = _rebuild_strip(blob, bx.lo, bx.hi, _STRIP_MOOF)
            own = (bx.hi - bx.lo) - len(new_moof)
            out += _patch_frag_offsets(new_moof, own, shift)
            shift += own
        else:
            out += bytes(blob[bx.lo:bx.hi])
    out_path = Path(out_path)
    if keep_enc:
        out_path.with_name(out_path.name + ".enc").write_bytes(bytes(blob))
    out_path.write_bytes(bytes(out))
    if stats is not None:
        stats.update({"samples": samples, "calls": calls,
                      "encrypted_bytes": prot_bytes,
                      "in_bytes": len(blob), "out_bytes": len(out)})
    return out_path


def dump_cbcs_structure(data: bytes) -> list[str]:
    """Дерево боксов и параметры шифрования (tenc/trun/senc) — для офлайн-
    разбора живого шифротекста. Ключевых байт нет: IV — только длина, KID —
    публичный идентификатор."""
    lines: list[str] = []
    tops = _iter_boxes(data, 0, len(data))
    first_moof = next((i for i, bx in enumerate(tops)
                       if bx.typ == b"moof"), None)
    if first_moof is None:
        return [f"моофа нет, верхний уровень: "
                f"{[b.typ.decode('latin1') for b in tops]}"]
    init = bytes(data[tops[0].lo:tops[first_moof].lo])
    lines.append("init: " + ", ".join(
        f"{b.typ.decode('latin1')}({b.hi - b.lo})" for b in tops[:first_moof]))
    tracks = _init_tracks(init)      # 01.10.2026: ниже `tracks` использовался, но не был определён (NameError при senc)
    for tid, t in sorted(tracks.items()):
        schm = _find_box(init, 0, len(init), b"schm")
        scheme = (bytes(init[schm.plo + 4:schm.plo + 8]).decode("latin1")
                  if schm else "—")
        lines.append(f"track {tid}: scheme {scheme}, tenc v{t['ver']} "
                     f"is_enc {t['is_enc']}, iv_size {t['iv_size']}, "
                     f"const_iv {len(t['const_iv'])} Б, kid {t['kid']}, "
                     f"pattern {t['crypt']}:{t['skip']}")
    for n, bx in enumerate(tops[first_moof:]):
        if bx.typ != b"moof":
            continue
        lines.append(f"фрагмент {n}: moof({bx.hi - bx.lo})")
        for traf in _iter_boxes(data, bx.plo, bx.hi):
            if traf.typ != b"traf":
                continue
            kids = _iter_boxes(data, traf.plo, traf.hi)
            tf = next((c for c in kids if c.typ == b"tfhd"), None)
            tid, base, def_size = _read_tfhd(data, tf) if tf else (0, None, None)
            tl = ", ".join(f"{c.typ.decode('latin1')}({c.hi - c.lo})"
                           for c in kids)
            lines.append(f"  traf track {tid}: {tl}")
            nsamp = sum(_read_trun(data, t)[0] for t in kids if t.typ == b"trun")
            sc = next((c for c in kids if c.typ in (b"senc", b"uuid")), None)
            if sc is not None:
                tf = next((c for c in kids if c.typ == b"tfhd"), None)
                tid2 = _read_tfhd(data, tf)[0] if tf else 0
                en = _parse_senc(data, sc, tracks.get(tid2, {}), nsamp) or []
                got = [e for e in en if e]
                lines.append(
                    f"  senc: {len(en)} записей, с под-сэмплами {len(got)}; "
                    f"первые: {got[:2]}")
            for t in [c for c in kids if c.typ == b"trun"]:
                count, doff, sizes = _read_trun(data, t)
                lines.append(f"  trun: {count} сэмплов, data_offset={doff}, "
                             f"первые размеры: {sizes[:3]}")
    return lines


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
