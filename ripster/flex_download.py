"""Загрузка трека через «Премиум Ripster» на ПК — то же, что FlexDownload в мобилке.

Сервер владельца отдаёт ссылку (getUrl): прямую на CDN сервиса, список сегментов DASH (Tidal) или
свой потоковый адрес (proxy). Дальше всё делает этот ПК: докачка при обрыве, сборка DASH, расшифровка
Deezer (Blowfish) и Яндекса (AES-CTR), распаковка «FLAC в MP4» в нативный .flac, теги и обложка.

Прямой путь не вышел (ссылка привязана к чужому адресу, обрыв) → серверу уходит отчёт, и трек
берётся через proxy. Всё остальное (имя файла по шаблону, ReplayGain, манифест) делает штатный
конвейер раннера после движка.

Секретов в журнале нет: ссылки на CDN подписаны и в сообщения не попадают.
"""
from __future__ import annotations

import asyncio
import re
import shutil
import subprocess
import urllib.parse
from pathlib import Path
from typing import Callable

import httpx

from ripster import flex_ciphers, flex_client

_UA = flex_ciphers.UA
MAX_RESUMES = 6
SEGMENT_PARALLEL = 4
SEGMENT_TRIES = 4
_QOBUZ_APP_ID = "312369995"
_CHUNK = 64 * 1024

Log = Callable[[str], None]
Progress = Callable[[int, "int | None"], None]


class DownloadError(Exception):
    """Короткая причина без секретов; code — для отчёта серверу."""

    def __init__(self, msg: str, code: str = "other"):
        super().__init__(msg)
        self.code = code


# ── разбор ссылки и качества ─────────────────────────────────────────────────

_URL_RULES = (
    ("deezer", re.compile(r"deezer\.com/(?:[a-z]{2}(?:-[a-z]{2})?/)?track/(\d+)")),
    ("qobuz", re.compile(r"qobuz\.com/(?:.*?/)?track/(\d+)")),
    ("qobuz", re.compile(r"qobuz\.com/.*?[?&]trackId=(\d+)")),
    ("tidal", re.compile(r"tidal\.com/(?:browse/)?track/(\d+)")),
    ("yandex", re.compile(r"music\.yandex\.[a-z]+/(?:album/\d+/)?track/(\d+)")),
)


def parse_track_url(url: str):
    """Ссылка на трек → (сервис, id) или None. Альбомы и плейлисты раскрывает resolver раньше."""
    u = urllib.parse.unquote(url or "")
    for svc, rx in _URL_RULES:
        m = rx.search(u)
        if m:
            return svc, m.group(1)
    return None


def flex_quality(q: str) -> str:
    """Качество Ripster → тир сервера (lossless | high | medium | low)."""
    s = (q or "").strip().lower()
    if s in ("27", "7", "6"):                      # Qobuz format_id: 24/16-bit FLAC
        return "lossless"
    if s == "5":                                   # Qobuz MP3 320
        return "high"
    if any(k in s for k in ("flac", "lossless", "hires", "hi-res", "alac", "hifi", "master", "max")):
        return "lossless"
    if any(k in s for k in ("320", "high", "aac_192", "192")):
        return "high"
    if any(k in s for k in ("128", "medium")):
        return "medium"
    if any(k in s for k in ("aac_64", "64", "low")):
        return "low"
    return "lossless"


# ── метаданные ───────────────────────────────────────────────────────────────

def _year(d: str) -> str:
    return (d or "")[:4] if re.match(r"^\d{4}", d or "") else ""


async def fetch_meta(service: str, track_id: str) -> dict:
    """Теги трека из публичного/выданного источника. Пустой словарь — не получилось (качаем без тегов)."""
    try:
        if service == "deezer":
            return await _meta_deezer(track_id)
        if service == "qobuz":
            return await _meta_qobuz(track_id)
        if service == "tidal":
            return await _meta_tidal(track_id)
        if service == "yandex":
            return await _meta_yandex(track_id)
    except Exception:
        return {}
    return {}


async def _meta_deezer(tid: str) -> dict:
    hdr = {"Accept-Language": "en", "User-Agent": _UA}      # жанр всегда по-английски
    async with httpx.AsyncClient(timeout=12, headers=hdr) as c:
        t = (await c.get(f"https://api.deezer.com/track/{tid}")).json()
        if not t.get("id"):
            return {}
        alb = t.get("album") or {}
        a = {}
        if alb.get("id"):
            a = (await c.get(f"https://api.deezer.com/album/{alb['id']}")).json()
    genres = ((a.get("genres") or {}).get("data") or [])
    artists = [x.get("name") for x in (t.get("contributors") or []) if x.get("name")] or [(t.get("artist") or {}).get("name")]
    return {
        "title": t.get("title") or "", "artist": ", ".join(x for x in artists if x),
        "albumartist": (a.get("artist") or {}).get("name") or (t.get("artist") or {}).get("name") or "",
        "album": alb.get("title") or "", "track": t.get("track_position") or "",
        "tracktotal": a.get("nb_tracks") or "", "disc": t.get("disk_number") or "",
        "year": _year(t.get("release_date") or a.get("release_date") or ""),
        "genre": (genres[0].get("name") if genres else "") or "", "label": a.get("label") or "",
        "isrc": t.get("isrc") or "", "cover": alb.get("cover_xl") or alb.get("cover_big") or "",
    }


async def _meta_qobuz(tid: str) -> dict:
    from ripster import premium_search as ps
    token, app_id = await ps.qobuz_creds()
    async with httpx.AsyncClient(timeout=12, headers={"User-Agent": _UA}) as c:
        r = await c.get("https://www.qobuz.com/api.json/0.2/track/get",
                        params={"track_id": tid, "app_id": app_id or _QOBUZ_APP_ID},
                        headers={"X-User-Auth-Token": token} if token else {})
        t = r.json()
    if not t.get("id"):
        return {}
    alb = t.get("album") or {}
    title = t.get("title") or ""
    ver = (t.get("version") or "").strip()
    if ver and ver.lower() not in title.lower():
        title = f"{title} ({ver})"
    img = alb.get("image") or {}
    return {
        "title": title, "artist": (t.get("performer") or {}).get("name") or (alb.get("artist") or {}).get("name") or "",
        "albumartist": (alb.get("artist") or {}).get("name") or "", "album": alb.get("title") or "",
        "track": t.get("track_number") or "", "tracktotal": alb.get("tracks_count") or "",
        "disc": t.get("media_number") or "",
        "year": _year(alb.get("release_date_original") or t.get("release_date_original") or ""),
        "genre": (alb.get("genre") or {}).get("name") or "", "label": (alb.get("label") or {}).get("name") or "",
        "isrc": t.get("isrc") or "", "cover": img.get("large") or img.get("small") or "",
    }


async def _meta_tidal(tid: str) -> dict:
    from ripster import premium_search as ps
    async with httpx.AsyncClient(timeout=12, headers={"User-Agent": _UA}) as c:
        r, cc = await ps.tidal_get(c, f"https://api.tidal.com/v1/tracks/{tid}", {})
        if r is None or r.status_code != 200:
            return {}
        t = r.json()
        alb = t.get("album") or {}
        a = {}
        if alb.get("id"):
            ra, _ = await ps.tidal_get(c, f"https://api.tidal.com/v1/albums/{alb['id']}", {})
            if ra is not None and ra.status_code == 200:
                a = ra.json()
    title = t.get("title") or ""
    ver = (t.get("version") or "").strip()
    if ver and ver.lower() not in title.lower():
        title = f"{title} ({ver})"
    cover = alb.get("cover") or ""
    return {
        "title": title, "artist": ", ".join(x.get("name") for x in (t.get("artists") or []) if x.get("name"))
        or (t.get("artist") or {}).get("name") or "",
        "albumartist": (a.get("artist") or {}).get("name") or "", "album": alb.get("title") or "",
        "track": t.get("trackNumber") or "", "tracktotal": a.get("numberOfTracks") or "",
        "disc": t.get("volumeNumber") or "", "year": _year(a.get("releaseDate") or ""),
        "genre": "", "label": "", "isrc": t.get("isrc") or "",
        "cover": f"https://resources.tidal.com/images/{cover.replace('-', '/')}/1280x1280.jpg" if cover else "",
    }


async def _meta_yandex(tid: str) -> dict:
    async with httpx.AsyncClient(timeout=12, headers={"User-Agent": _UA}) as c:
        r = await c.get(f"https://api.music.yandex.net/tracks/{tid}")
        res = (r.json().get("result") or [None])[0] if r.status_code == 200 else None
    if not res:
        return {}
    alb = (res.get("albums") or [{}])[0]
    cu = res.get("coverUri") or alb.get("coverUri") or ""
    return {
        "title": res.get("title") or "", "artist": ", ".join(a.get("name") for a in (res.get("artists") or []) if a.get("name")),
        "album": alb.get("title") or "", "track": (alb.get("trackPosition") or {}).get("index") or "",
        "tracktotal": alb.get("trackCount") or "", "year": str(alb.get("year") or ""),
        "genre": alb.get("genre") or "", "cover": ("https://" + cu.replace("%%", "1000x1000")) if cu else "",
    }


# ── скачивание ───────────────────────────────────────────────────────────────

async def _stream_resumable(url: str, dest: Path, headers: dict, progress: Progress, log: Log,
                            size_hint: "int | None" = None) -> int:
    """GET в файл с докачкой Range после обрыва. 4xx не повторяем (ссылка недействительна)."""
    dest.unlink(missing_ok=True)
    written, total, resumes = 0, size_hint, 0
    while True:
        hdr = {"User-Agent": _UA, **headers}
        if written:
            hdr["Range"] = f"bytes={written}-"
        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(60.0, connect=15.0), follow_redirects=True) as c:
                async with c.stream("GET", url, headers=hdr) as resp:
                    if 400 <= resp.status_code < 500:
                        raise DownloadError(f"HTTP {resp.status_code}", "ip_denied" if resp.status_code in (401, 403) else "upstream")
                    if resp.status_code >= 500:
                        raise httpx.TransportError(f"HTTP {resp.status_code}")
                    append = bool(written) and resp.status_code == 206
                    if written and not append:
                        written = 0          # сервер не понял Range — начинаем сначала, а не клеим кривой файл
                    if not written:
                        cl = resp.headers.get("content-length")
                        total = int(cl) if cl and cl.isdigit() else size_hint
                    with open(dest, "ab" if append else "wb") as f:
                        async for chunk in resp.aiter_bytes(_CHUNK):
                            f.write(chunk)
                            written += len(chunk)
                            progress(written, total)
            return written
        except DownloadError:
            raise
        except (httpx.TransportError, httpx.TimeoutException) as e:
            written = dest.stat().st_size if dest.exists() else 0
            resumes += 1
            log(f"resume #{resumes} at {written}: {type(e).__name__}")
            if resumes > MAX_RESUMES:
                raise DownloadError("stream interrupted", "timeout")
            await asyncio.sleep(1.2 * resumes)


async def _fetch_segment(c: httpx.AsyncClient, url: str) -> bytes:
    for attempt in range(1, SEGMENT_TRIES + 1):
        try:
            r = await c.get(url)
            if 400 <= r.status_code < 500:
                raise DownloadError(f"segment HTTP {r.status_code}", "ip_denied" if r.status_code in (401, 403) else "upstream")
            r.raise_for_status()
            return r.content
        except DownloadError:
            raise
        except (httpx.HTTPError, httpx.TimeoutException):
            if attempt == SEGMENT_TRIES:
                raise DownloadError("segment failed", "upstream")
            await asyncio.sleep(0.8 * attempt)
    raise DownloadError("segment failed", "upstream")


async def fetch_segments(urls: list, dest: Path, progress: Progress) -> int:
    """DASH: первый URL — init; сегменты качаем пачками параллельно, пишем строго по порядку."""
    written = 0
    dest.unlink(missing_ok=True)
    async with httpx.AsyncClient(timeout=httpx.Timeout(45.0, connect=15.0), headers={"User-Agent": _UA},
                                 follow_redirects=True) as c:
        with open(dest, "wb") as f:
            for i in range(0, len(urls), SEGMENT_PARALLEL):
                parts = await asyncio.gather(*(_fetch_segment(c, u) for u in urls[i:i + SEGMENT_PARALLEL]))
                for b in parts:
                    f.write(b)
                    written += len(b)
                progress(written, None)
    return written


def decrypt_deezer(src: Path, dst: Path, track_id: str) -> None:
    """Blowfish-CBC: каждый третий блок по 2048 байт зашифрован."""
    try:
        from Crypto.Cipher import Blowfish
    except ImportError:
        from Cryptodome.Cipher import Blowfish
    key = flex_ciphers.deezer_bf_key(track_id)
    bs = flex_ciphers.BLOCK
    with open(src, "rb") as fi, open(dst, "wb") as fo:
        i = 0
        while True:
            block = fi.read(bs)
            if not block:
                break
            if i % 3 == 0 and len(block) == bs:
                block = Blowfish.new(key, Blowfish.MODE_CBC, flex_ciphers.BF_IV).decrypt(block)
            fo.write(block)
            i += 1


def decrypt_aes_ctr(src: Path, dst: Path, key_hex: str) -> None:
    """Яндекс lossless: весь файл — AES-128-CTR, nonce 12 нулей, счётчик с нуля."""
    try:
        from Crypto.Cipher import AES
    except ImportError:
        from Cryptodome.Cipher import AES
    aes = AES.new(bytes.fromhex(key_hex), AES.MODE_CTR, nonce=bytes(12))
    with open(src, "rb") as fi, open(dst, "wb") as fo:
        while True:
            chunk = fi.read(_CHUNK)
            if not chunk:
                break
            fo.write(aes.decrypt(chunk))


def _u32(b: bytes, o: int) -> int:
    return int.from_bytes(b[o:o + 4], "big")


def _scan_mp4(f, size: int):
    """Верхний уровень MP4: (dfLa-блок из moov | None, [(начало, длина) тел mdat])."""
    pos, dfla, mdat = 0, None, []
    while pos + 8 <= size:
        f.seek(pos)
        h = f.read(8)
        bsize, typ, hdr = _u32(h, 0), h[4:8], 8
        if bsize == 1:
            bsize, hdr = int.from_bytes(f.read(8), "big"), 16
        elif bsize == 0:
            bsize = size - pos
        if bsize < hdr or pos + bsize > size:
            break                                   # обрезанный хвост: берём целые фрагменты
        if typ == b"moov" and dfla is None and bsize - hdr <= 32 * 1024 * 1024:
            f.seek(pos + hdr)
            body = f.read(bsize - hdr)
            k = body.find(b"fLaC")
            d = body.find(b"dfLa", k) if k >= 0 else -1
            if d >= 4:
                n = _u32(body, d - 4)
                start, end = d + 8, d - 4 + n          # после типа и версии/флагов
                if n >= 12 and end <= len(body) and start <= end:
                    dfla = body[start:end]
        elif typ == b"mdat":
            mdat.append((pos + hdr, bsize - hdr))
        pos += bsize
    return dfla, mdat


def _flac_blocks(d: bytes):
    out, i = [], 0
    while i + 4 <= len(d):
        b = d[i]
        n = int.from_bytes(d[i + 1:i + 4], "big")
        i += 4
        if i + n > len(d):
            return None
        out.append((b & 0x7F, d[i:i + n]))
        i += n
        if b & 0x80:
            break
    if not out or out[0][0] != 0 or len(out[0][1]) != 34:      # STREAMINFO обязателен, первым, ровно 34 байта
        return None
    return out


def remux_flac_in_mp4_py(src: Path, dst: Path) -> bool:
    """«FLAC во фрагментированном MP4» (Tidal DASH) → нативный .flac чистым Python: ни ffmpeg, ни перекодирования.

    Порт `Fmp4Flac.kt` мобильного приложения: метаданные берём из `dfLa`, кадры — тела всех `mdat` подряд.
    False — это не MP4 с FLAC или файл повреждён (dst убран, src не тронут)."""
    try:
        size = src.stat().st_size
        with open(src, "rb") as f:
            dfla, mdat = _scan_mp4(f, size)
            blocks = _flac_blocks(dfla) if dfla else None
            if not blocks or not mdat:
                return False
            with open(dst, "wb") as out:
                out.write(b"fLaC")
                for i, (typ, data) in enumerate(blocks):
                    last = 0x80 if i == len(blocks) - 1 else 0
                    out.write(bytes([typ | last]) + len(data).to_bytes(3, "big") + data)
                for start, length in mdat:
                    f.seek(start)
                    left = length
                    while left > 0:
                        chunk = f.read(min(1 << 20, left))
                        if not chunk:
                            raise OSError("mdat truncated")
                        out.write(chunk)
                        left -= len(chunk)
        return True
    except Exception:
        dst.unlink(missing_ok=True)
        return False


def remux_flac_in_mp4(src: Path, dst: Path) -> bool:
    """Распаковать FLAC из MP4 в нативный .flac. Сначала чистый Python (внешних программ не нужно), ffmpeg — запасной."""
    if remux_flac_in_mp4_py(src, dst):
        return True
    ff = shutil.which("ffmpeg")
    if not ff:
        return False
    try:
        r = subprocess.run([ff, "-v", "error", "-y", "-i", str(src), "-vn", "-c:a", "copy", str(dst)],
                           capture_output=True, timeout=300)
    except Exception:
        return False
    if r.returncode != 0 or not dst.exists() or dst.stat().st_size == 0:
        dst.unlink(missing_ok=True)
        return False
    return True


# ── один трек целиком ────────────────────────────────────────────────────────

def _safe(s: str) -> str:
    return re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", str(s or "")).strip(" .") or "_"


async def _obtain(res: dict, tmp: Path, progress: Progress, log: Log) -> Path:
    """Выполнить один ответ getUrl → готовый файл рядом с tmp (расширение подбирается по содержимому)."""
    mode = res.get("mode")
    ext = str(res.get("extension") or "bin").lstrip(".")
    if mode == "direct" and res.get("segments"):
        raw = tmp.with_suffix(".m4a")
        log(f"DASH direct: {len(res['segments'])} segments")
        await fetch_segments(list(res["segments"]), raw, progress)
        if str(res.get("qualityTier") or "") == "lossless" or res.get("container") in ("flac", "mp4"):
            out = tmp.with_suffix(".flac")
            if remux_flac_in_mp4(raw, out):
                raw.unlink(missing_ok=True)
                return out
        return raw
    raw = tmp.with_suffix("." + ext)
    if mode == "direct":
        dec = res.get("decryptor") or {}
        enc = tmp.with_suffix(".enc") if dec else raw
        await _stream_resumable(res["url"], enc, {}, progress, log, res.get("size"))
        if dec.get("id") == "deezer_bf":
            decrypt_deezer(enc, raw, str(dec.get("trackId") or ""))
            enc.unlink(missing_ok=True)
        elif dec.get("id") == "yandex_aes_ctr":
            decrypt_aes_ctr(enc, raw, str(dec.get("key") or ""))
            enc.unlink(missing_ok=True)
        elif dec:
            raise DownloadError("unknown cipher", "format")
        return raw
    await _stream_resumable(flex_client.resolve_url(res["url"]), raw, {}, progress, log, res.get("size"))
    return raw


async def download_track(url: str, quality: str, out_dir: Path, log: Log, progress: Progress) -> Path:
    """Скачать трек по ссылке сервиса → файл с тегами в out_dir/<Альбом>/. Бросает DownloadError."""
    parsed = parse_track_url(url)
    if not parsed:
        raise DownloadError("link is not a single track", "other")
    svc, tid = parsed
    if not flex_client.is_active():
        raise DownloadError("Premium is not activated on this PC", "no_token")
    meta = await fetch_meta(svc, tid)
    q = flex_quality(quality)
    artists = [a.strip() for a in str(meta.get("artist") or "").split(",") if a.strip()]
    ask = dict(service=svc, track_id=tid, title=meta.get("title") or "", artists=artists,
               album=meta.get("album") or "", release_date=meta.get("year") or "", quality=q,
               isrc=meta.get("isrc") or "")
    folder = Path(out_dir) / _safe(meta.get("album") or f"{svc} {tid}")
    folder.mkdir(parents=True, exist_ok=True)
    tmp = folder / f".flex_{svc}_{_safe(tid)}"
    try:
        try:
            res = await flex_client.get_url(**ask)
        except flex_client.FlexClientError as e:
            raise DownloadError(f"server: {e.code}", e.code)
        log(f"Flex: {res.get('mode')} {res.get('qualityTier')}")
        try:
            path = await _obtain(res, tmp, progress, log)
            if res.get("mode") == "direct":
                await flex_client.report(res.get("reportId"), True)
        except DownloadError as e:
            if res.get("mode") != "direct":
                raise
            # Прямой путь не вышел: серверу — причину (он научится), трек — через proxy.
            await flex_client.report(res.get("reportId"), False, e.code)
            log(f"direct failed ({e.code}), falling back to server")
            res = await flex_client.get_url(**ask, prefer="proxy", dash=False)
            path = await _obtain(res, tmp, progress, log)
        final = folder / (_safe(meta.get("title") or tid) + path.suffix)
        if final.exists():
            final = folder / (_safe(meta.get("title") or tid) + f" [{svc}]" + path.suffix)
        path.replace(final)
    finally:
        for p in folder.glob(f".flex_{svc}_{_safe(tid)}*"):
            p.unlink(missing_ok=True)
    if meta:
        await asyncio.to_thread(_tag, final, meta, log)
    return final


def _tag(path: Path, meta: dict, log: Log) -> None:
    from ripster import tagger
    fields = {k: meta.get(k) for k in ("title", "artist", "albumartist", "album", "track", "tracktotal",
                                       "disc", "year", "genre", "label") if meta.get(k)}
    if not tagger.write_tags(path, fields):
        log("tags: not written")
    cover = meta.get("cover")
    if cover:
        try:
            r = httpx.get(cover, timeout=15, headers={"User-Agent": _UA}, follow_redirects=True)
            if r.status_code == 200 and r.content:
                tagger.embed_cover(path, r.content)
        except Exception:
            log("cover: not embedded")
