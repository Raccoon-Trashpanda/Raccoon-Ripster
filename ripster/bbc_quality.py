"""BBC: что поток отдаёт НА САМОМ ДЕЛЕ — и до записи, и после неё.

Один источник качества для трёх мест: карточка будущего эфира (прежде чем
человек нажмёт «записать»), движок (не обещать то, чего поток не даёт) и
планировщик (сверить готовый файл с обещанием).

Почему это отдельный модуль, а догадка по шильдику: BBC называет качеством НЕ то,
что отдаёт. Промер 21.09.2026 на живых объектах:

* MediaSelector для on-demand выпуска Essential Mix (vpid m00315gp) отвечает
  ``"bitrate": "320"``, а в его же HLS-мастере лежат ровно два варианта — 51 и
  102 кбит/с, оба ``mp4a.40.5``, то есть HE-AAC. «320» в этом ответе — имя тира,
  а не битрейт.
* RMS в ``download.quality_variants`` того же выпуска рисует 96/128/320 — и у
  всех трёх ``file_url: null``.
* Тот же эфир с live-потока: мастер канала честно перечисляет 48/96 кбит/с
  HE-AAC и 128/320 кбит/с AAC-LC, а ``ffmpeg -c copy`` с варианта 320000 даёт
  файл, у которого ffprobe меряет 319 937…320 046 бит/с, профиль LC, 48 кГц.
  Проверено на Radio 1 и 6 Music — единственный честный «320» у BBC.

Отсюда правило модуля: качество — это либо список вариантов из плейлиста, либо
числа из ffprobe по готовому файлу. Всё остальное — реклама.
"""
from __future__ import annotations

import json
import re
import subprocess
from datetime import datetime, timezone
from pathlib import Path

_CNW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

# Тот же профиль — двумя словами: в HLS он пишется как OTI, ffprobe отвечает
# именем. mp4a.40.2 = LC (полоса до ~20 кГц), mp4a.40.5 = HE-AAC со SBR: при той
# же полосе пропускания сверху ~15 кГц, поэтому 102k HE-AAC ≠ 102k AAC-LC.
_PROFILES = {
    "mp4a.40.2":  "AAC-LC",
    "mp4a.40.5":  "HE-AAC",
    "mp4a.40.29": "HE-AAC v2",
    "he-aac v2":  "HE-AAC v2",
    "he-aac":     "HE-AAC",
    "lc":         "AAC-LC",
    "main":       "AAC-Main",
}

# Измеренный битрейт считается дотянувшим до обещанного, если он не ниже этой
# доли: ±10% — обычный разброс CBR-потока и контейнерных обёрток, меньше — уже
# другой поток (именно так 97 кбит/с не должны притворяться 320).
_OK_RATIO = 0.9


def codec_label(token: str) -> str:
    """mp4a.40.5 → HE-AAC. Неизвестное отдаём как есть: выдумывать «AAC-LC» на
    глазок хуже, чем показать непрочитанный код."""
    tok = (token or "").strip()
    return _PROFILES.get(tok.lower(), tok)


def _int(v, default=0) -> int:
    try:
        return int(float(str(v).strip()))
    except Exception:
        return default


def utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ── Плейлисты: что поток предлагает прямо сейчас ─────────────────────────────

_ATTR = re.compile(r'([A-Z-]+)=("[^"]*"|[^,]*)')


def parse_variants(text: str) -> list[dict]:
    """#EXT-X-STREAM-INF → [{kbps, bandwidth, codec, codec_id, lc, uri}].

    `kbps` — номинал из имени варианта (``…-audio=320000.m3u8``), иначе
    AVERAGE-BANDWIDTH; `bandwidth` — пиковый BANDWIDTH, по нему и выбирают.
    """
    out: list[dict] = []
    lines = (text or "").splitlines()
    for i, ln in enumerate(lines):
        if not ln.startswith("#EXT-X-STREAM-INF"):
            continue
        attrs = {k: v.strip('"') for k, v in _ATTR.findall(ln.split(":", 1)[1] if ":" in ln else "")}
        bw, abw = _int(attrs.get("BANDWIDTH")), _int(attrs.get("AVERAGE-BANDWIDTH"))
        codec_id = (attrs.get("CODECS") or "").split(",")[0].strip()
        uri = ""
        for nxt in lines[i + 1:]:
            s = nxt.strip()
            if s and not s.startswith("#"):
                uri = s
                break
        mnm = re.search(r"audio[=_](\d+)", uri)
        nominal = _int(mnm.group(1)) if mnm else (abw or bw)
        out.append(_variant(kbps=round(nominal / 1000), bandwidth=bw, codec_id=codec_id, uri=uri))
    return out


def _variant(kbps: int, bandwidth: int, codec_id: str, uri: str = "") -> dict:
    return {"kbps": int(kbps or 0), "bandwidth": int(bandwidth or 0),
            "codec_id": codec_id or "", "codec": codec_label(codec_id),
            "lc": (codec_id or "").lower() == "mp4a.40.2", "uri": uri or ""}


def best_variant(variants: list[dict]) -> dict | None:
    return max(variants, key=lambda v: v.get("bandwidth") or v.get("kbps") or 0) \
        if variants else None


def offers(variants: list[dict], kbps: int = 320) -> bool:
    """Есть ли в лестнице вариант нужной полосы И именно AAC-LC.

    Профиль обязателен: 320 кбит/с HE-AAC — это не те 320, ради которых эфир и
    пишут (у HE-AAC полоса сверху пустая, сколько бы бит ни шло).
    """
    return any((v.get("kbps") or 0) >= kbps and (v.get("lc") or codec_label(v.get("codec_id") or v.get("codec") or "") == "AAC-LC")
               for v in variants or [])


def ladder_summary(variants: list[dict], checked_utc: str = "", error: str = "") -> dict:
    """Один ответ на вопрос «что этот поток отдаёт» — числами, без текста: ни
    одного слова, которое человек прочтёт как текст, в API быть не должно."""
    best = best_variant(variants) or {}
    return {"ok": bool(variants) and not error,
            "checked_utc": checked_utc or "",
            "best_kbps": best.get("kbps", 0),
            "best_codec": best.get("codec", ""),
            "lc": bool(best.get("lc")),
            "has_320_lc": offers(variants, 320),
            "variants": [{"kbps": v.get("kbps", 0), "codec": v.get("codec", ""),
                          "lc": bool(v.get("lc"))} for v in variants or []],
            "error": error}


async def probe_url(url: str, client=None) -> dict:
    """Снять плейлист по адресу и вернуть лестницу. Пустой адрес или сбой сети —
    ok=False с причиной: «неизвестно» честнее выдуманного 320."""
    if not url:
        return ladder_summary([], error="no_url")
    try:
        if client is not None:
            text = await _get_via(client, url)
        else:
            from ripster import http_client as _HTTP
            async with _HTTP.ashared() as c:
                text = await _get_via(c, url)
    except Exception as e:
        return ladder_summary([], checked_utc=utc(), error=type(e).__name__)
    if text is None:
        return ladder_summary([], checked_utc=utc(), error="http")
    return ladder_summary(parse_variants(text), checked_utc=utc())


async def _get_via(client, url: str):
    r = await client.get(url, headers={"Accept": "application/vnd.apple.mpegurl, text/plain"})
    return r.text if r.status_code == 200 else None


def ladder_sync(url: str) -> dict:
    """Та же лестница, что probe_url, но для синхронного `build_cmd` движка:
    движок не владеет event loop и не имеет права его блокировать await-ом."""
    if not url:
        return ladder_summary([], error="no_url")
    try:
        from ripster import http_client as _HTTP
        with _HTTP.shared() as c:
            r = c.get(url, headers={"Accept": "application/vnd.apple.mpegurl, text/plain"})
            if r.status_code != 200:
                return ladder_summary([], error=f"http_{r.status_code}")
            return ladder_summary(parse_variants(r.text), checked_utc=utc())
    except Exception as e:
        return ladder_summary([], error=type(e).__name__)


async def live_ladder(channel: str, client=None) -> dict:
    """Лестница live-потока канала — ровно того адреса, с которого пишет bbc_live.

    Адрес мастера выводим из рабочего адреса движка (убрав имя варианта), чтобы
    пулы и формат продолжала знать одна сущность — ripster.bbc_live_channels.
    """
    from ripster import bbc_live_channels as _CH
    master = live_master_url(channel)
    if not master:
        return ladder_summary([], error="unknown_channel")
    res = await probe_url(master, client)
    res["channel"] = channel
    res["url"] = _CH.stream_url(channel)
    return res


def live_master_url(channel: str) -> str:
    """Мастер-плейлист live-канала (пусто — канала нет в каталоге)."""
    from ripster import bbc_live_channels as _CH
    one = _CH.stream_url(channel)
    return one.rsplit("/", 1)[0] + "/master.m3u8" if one else ""


def best_live_variant(channel: str, want_kbps: int = 320) -> dict:
    """Вариант live-потока, который канал отдаёт ПРЯМО СЕЙЧАС, — адрес и числа.

    Нужен в синхронном ``build_cmd`` движка: мастер живёт минуты, а вариант 320
    в лестнице — обещание, а не гарантия (21.09.2026 все десять каналов его
    держали; через год — как повезёт). Если нужной ступени нет, берём лучшую
    доступную и называем её числом: записать эфир на 96 кбит/с и честно сказать
    «96» лучше, чем снять окно эфира ошибкой «320 нет».
    """
    from ripster import bbc_live_channels as _CH
    master = live_master_url(channel)
    if not master:
        return {"url": "", "error": "unknown_channel"}
    try:
        from ripster import http_client as _HTTP
        with _HTTP.shared() as c:
            r = c.get(master, headers={"Accept": "application/vnd.apple.mpegurl, text/plain"})
            if r.status_code != 200:
                return {"url": _CH.stream_url(channel), "error": f"http_{r.status_code}"}
            variants = parse_variants(r.text)
    except Exception as e:
        return {"url": _CH.stream_url(channel), "error": type(e).__name__}
    base = master.rsplit("/", 1)[0]
    picked = next((v for v in sorted(variants, key=lambda v: -v["kbps"])
                   if v["kbps"] >= want_kbps and v["lc"]), None) \
        or best_variant([v for v in variants if v["lc"]]) or best_variant(variants)
    if not picked:
        return {"url": _CH.stream_url(channel), "error": "empty_ladder"}
    uri = picked.get("uri") or ""
    url = uri if uri.startswith("http") else f"{base}/{uri}" if uri else _CH.stream_url(channel)
    return {"url": url, "kbps": picked["kbps"], "codec": picked["codec"],
            "lc": bool(picked["lc"]), "wanted_kbps": int(want_kbps),
            "ladder": ladder_summary(variants, checked_utc=utc())}



# ── Готовый файл: что реально записалось ──────────────────────────────────────

def measure_file(path) -> dict:
    """ffprobe по файлу: кодек, профиль, измеренный битрейт. Ни имя папки, ни
    расширение, ни обещание движка здесь участия не принимают."""
    p = Path(path)
    if not p.is_file():
        return {}
    cmd = ["ffprobe", "-v", "error", "-select_streams", "a:0",
           "-show_entries", "stream=codec_name,profile,bit_rate,sample_rate,channels",
           "-show_entries", "format=duration,bit_rate,size",
           "-of", "json", str(p)]
    try:
        r = subprocess.run(cmd, timeout=30, capture_output=True, creationflags=_CNW)
        d = json.loads((r.stdout or b"").decode(errors="replace") or "{}")
    except Exception:
        return {}
    st = (d.get("streams") or [{}])[0]
    fm = d.get("format") or {}
    if not st.get("codec_name"):
        return {}
    size, dur = _int(fm.get("size")), _float(fm.get("duration"))
    # Средний по файлу — самая упрямая цифра: она не зависит от того, что
    # написал в заголовке кодировщик.
    avg = round(8 * size / dur) if (size and dur > 0.5) else _int(fm.get("bit_rate"))
    return {"codec": (st.get("codec_name") or "").lower(),
            "profile": codec_label(st.get("profile") or ""),
            "sample_rate": _int(st.get("sample_rate")),
            "channels": _int(st.get("channels")),
            "duration": round(dur, 1), "size": size,
            "kbps": round((_int(st.get("bit_rate")) or avg) / 1000),
            "avg_kbps": round(avg / 1000)}


def _float(v, default=0.0) -> float:
    try:
        return float(str(v).strip())
    except Exception:
        return default


def verdict(path, promised_kbps: int = 320) -> dict:
    """Приговор записанному файлу: as_promised | below | unmeasured.

    Профиль решает наравне с битрейтом: 320 кбит/с HE-AAC человек, ждущий
    полный эфир, получил бы за «320», а полоса у него сверху пустая.
    """
    m = measure_file(path)
    promised = int(promised_kbps or 0)
    if not m:
        return {"state": "unmeasured", "promised_kbps": promised, "measured": {},
                "measured_utc": utc()}
    got = m.get("avg_kbps") or m.get("kbps") or 0
    lc_ok = (promised < 192) or (m.get("profile") == "AAC-LC") or (m.get("codec") != "aac")
    ok = got >= int(promised * _OK_RATIO) and lc_ok
    return {"state": "as_promised" if ok else "below",
            "promised_kbps": promised, "measured": m,
            "kbps": got, "codec": m.get("profile") or m.get("codec"),
            "ratio": round(got / promised, 2) if promised else 0,
            "measured_utc": utc()}


def is_upconvert(source_kbps: int, target_kbps: int) -> bool:
    """Кодировать lossy-источник ВВЕРХ по полосе — второе поколение потерь без
    единого лишнего герца информации (ripster-audio-integrity)."""
    return bool(source_kbps) and bool(target_kbps) and target_kbps > source_kbps


# Стандартные ступени CBR у LAME: выбирать надо из них, а не из фантазии.
_CBR_STEPS = (32, 40, 48, 56, 64, 80, 96, 112, 128, 160, 192, 224, 256, 320)

# Потолок on-demand Sounds по всем промерам 21.09.2026 — 102 кбит/с HE-AAC.
# Если лестницу прочитать не удалось, целимся именно сюда, а не в 320: хуже
# ошибиться в большую сторону один раз, чем снова называть 320 то, чем оно не
# является, на всех файлах сразу.
ONDEMAND_CEILING = 102


def mp3_target_kbps(source_kbps: int = 0) -> int:
    """Битрейт MP3-цели, который НЕ прикидывается более полным звуком, чем
    источник: ближайшая ступень CBR не выше измеренного источника.

    Ни одной ступени выше источника — иначе это апконверт: полоса шире, а
    герцев больше не стало (только второе поколение потерь и лишние байты).
    Потеря пары процентов полосы у 102→96 измеряема, а вот «112 из 102» уже
    противоречит тому, что написано на карточке качества, поэтому выбираем
    молчаливое «не больше», а не громкое «как бы не отрезать».
    """
    src = int(source_kbps or 0) or ONDEMAND_CEILING
    under = [s for s in _CBR_STEPS if s <= src]
    return under[-1] if under else _CBR_STEPS[0]
