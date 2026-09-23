"""OrpheusDL-JioSaavn engine — downloads JioSaavn URLs via OrpheusDL + orpheusdl-jiosaavn.

No login: the module talks to JioSaavn's public web API (www.jiosaavn.com/api.php)
and needs no credentials (module_information: no session settings).

Quality tiers (module README — all AAC-LC in an MP4 container, JioSaavn has no
lossless tier at all):
  high    → AAC 320 kbps
  medium  → AAC 160 kbps
  low     → AAC 96 kbps

GEO: JioSaavn licenses its catalogue for India (and a few markets). Outside of it
the METADATA API still answers 200 (the album, tracklist and cover come through,
the response even carries ``"disabled_text": "Unavailable"``), but the audio CDN
refuses the signed URL:
  web.saavncdn.com   → 403 Akamai "Access Denied" (431-byte HTML page)
  preview.saavncdn.com → 451 Unavailable For Legal Reasons
The module does not check the HTTP status, so OrpheusDL writes that HTML page
to disk as "<track>.m4a" and then crashes in mutagen ("not a MP4 file" / "moov
not found"). Proven from this machine (RU IP) on 19.09.2026. This engine names
that honestly as a geo-block and deletes the fake .m4a files, otherwise the next
run (e.g. via an Indian VPN) would SKIP them as "already downloaded".

Module must be cloned to orpheus/modules/jiosaavn/ before first use.
Repo: https://github.com/bunnykek/orpheusdl-jiosaavn (no extra pip deps — it only
uses OrpheusDL's own utils/requests, so it cannot pull a protobuf/construct pin).
"""
from __future__ import annotations

import html
import json
import re
import time
from pathlib import Path

from .base import EngineBase, EngineResult, Event, EventKind, LineLevel, _strip_ansi
from .registry import register
# One OrpheusDL install, one interpreter choice — shared with Beatport on purpose
# (same base, same isolated venv rules). Настройки при ЭТОМ у каждого движка СВОИ:
# `_corridor_settings` даёт отдельный каталог на движок, потому что прогоны из
# разных полос очереди идут параллельно и общий settings.json перебивал бы
# качество/папку одного прогона другим.
from .orpheus_beatport import (_orpheus_dir, _corridor_config_dir,
                               _corridor_settings, orpheus_cmd)

REPO_URL = "https://github.com/bunnykek/orpheusdl-jiosaavn"


def _module_path() -> Path:
    return _orpheus_dir() / "modules" / "jiosaavn"


def _settings_path() -> Path:
    return _corridor_settings("jiosaavn")


# ── patterns ─────────────────────────────────────────────────────────────────
_RE_DOWNLOADING = re.compile(r'===\s*Downloading\s+(track|album|playlist|artist)\s+(.+?)\s*(?:\(|===)', re.I)
_RE_TRACK_FILE  = re.compile(r'Downloading track file', re.I)
_RE_TRACK_N     = re.compile(r'^\s*Track\s+(\d+)\s*/\s*(\d+)', re.I)
_RE_ERROR       = re.compile(r'\berror\b|\bfailed\b|\bexception\b|\bTraceback', re.I)
_RE_SKIP        = re.compile(r'already exists|skipping', re.I)
# CDN handed back an HTML error page instead of audio. mutagen's wording for it.
_RE_NOT_AUDIO   = re.compile(r"not a MP4 file|b'moov' not found|moov atom not found", re.I)
# Explicit geo / legal refusal wording (Akamai page / HTTP 451 reason phrase).
_RE_GEO         = re.compile(r'Access Denied|Unavailable For Legal Reasons|'
                             r'not available in your (?:country|region)', re.I)
# Transport: network/TLS/DNS/timeout. A retry may help; never a "login" problem
# (JioSaavn has no login at all).
_RE_NET         = re.compile(r'ConnectTimeout|ReadTimeout|ConnectError|ConnectionError|'
                             r'handshake operation timed out|timed\s+out|\btimeout\b|'
                             r'Max retries exceeded|getaddrinfo|SSLError|NameResolution|'
                             r'Temporary failure in name resolution|RemoteDisconnected|'
                             r'Connection (?:aborted|reset|refused)|ProxyError|'
                             r'ChunkedEncodingError|IncompleteRead', re.I)
# Module crashed while READING the release (bad/removed id → the API returns an
# empty shell: primary_artists=None, no 'songs', …).
_RE_CONTENT_FAIL = re.compile(
    r'modules[\\/]+jiosaavn[\\/]+interface\.py.*\n.*in\s+get_(?:album|track|playlist|artist)_(?:info|json)',
    re.I)
_RE_PARSE_EXC   = re.compile(r'^(?:TypeError|KeyError|IndexError|AttributeError|'
                             r'json\.decoder\.JSONDecodeError|JSONDecodeError|'
                             r'requests\.exceptions\.JSONDecodeError)\b', re.I | re.M)
_RE_NO_MODULE   = re.compile(r'URL location "[^"]*jiosaavn[^"]*" is not found in modules', re.I)

# Прежняя редакция этого вердикта утверждала, что нужен индийский IP или прокси.
# Это оказалось неправдой: 21.09.2026 выяснилось, что JioSaavn раздаёт один и тот
# же файл с двух хостов. Подписанный `web.saavncdn.com` (его выдаёт ручка
# `song.generateAuthToken`) отбивает нас 403 всегда, а `aac.saavncdn.com`, куда
# указывает расшифрованный `encrypted_media_url`, отдаёт всё без подписи — замер
# 97/161/321 кбит/с AAC-LC. Движок теперь ходит на второй, и VPN не нужен вовсе.
# Поэтому 403 сегодня означает не «страна не та», а «ушли не на тот хост».
_GEO_VERDICT = ("JioSaavn: CDN ответил 403 Access Denied. Индийский IP для этого НЕ нужен — "
                "так отвечает только подписанный хост web.saavncdn.com; файл лежит на "
                "aac.saavncdn.com и отдаётся без подписи. Значит загрузка ушла на старый "
                "хост — проверь getCdnURL в orpheus/modules/jiosaavn/interface.py.")
_NET_VERDICT = ("JioSaavn: сеть недоступна (таймаут/обрыв соединения с jiosaavn.com или "
                "saavncdn.com) — повторю. Это не гео-блок и не проблема аккаунта.")
_CONTENT_VERDICT = ("JioSaavn: релиз не найден по ссылке — сервис вернул пустую карточку "
                    "(удалён, ссылка битая или неверный тип ссылки).")


_QUALITIES = [
    {
        "id": "high",   "label": "AAC 320", "engine": "orpheus_jiosaavn",
        "sub": "AAC 320 kbps",
        "badge": "320k", "color": "#2bc5b4", "bitrate": "320 kbps",
        "ext": "m4a",
    },
    {
        "id": "medium", "label": "AAC 160", "engine": "orpheus_jiosaavn",
        "sub": "AAC 160 kbps",
        "badge": "160k", "color": "#EF9F27", "bitrate": "160 kbps",
        "ext": "m4a",
    },
    {
        "id": "low",    "label": "AAC 96",  "engine": "orpheus_jiosaavn",
        "sub": "AAC 96 kbps",
        "badge": "96k", "color": "#6a6a8a", "bitrate": "96 kbps",
        "ext": "m4a",
    },
]

#: Ripster quality id (and foreign aliases a bot/guest may send) → OrpheusDL tier.
#: The module maps HIFI/LOSSLESS/HIGH all to AAC_320, MEDIUM → 160, LOW/MINIMUM → 96.
_QUALITY_ORPHEUS = {
    "high": "high", "320": "high", "aac": "high", "mp3": "high",
    "hifi": "high", "lossless": "high", "flac": "high",
    "medium": "medium", "160": "medium", "normal": "medium",
    "low": "low", "96": "low", "minimum": "low", "128": "low",
}


def normalize_quality(q: str) -> str:
    """Ripster quality id for JioSaavn ('high'|'medium'|'low'); unknown → 'high'."""
    return {"high": "high", "medium": "medium", "low": "low"}.get(
        _QUALITY_ORPHEUS.get((q or "").lower(), "high"), "high")


def is_installed() -> bool:
    return ((_orpheus_dir() / "orpheus.py").exists()
            and (_orpheus_dir() / "orpheus" / "core.py").exists()
            and (_module_path() / "interface.py").exists())


def _ensure_module_init() -> None:
    """The upstream repo ships no __init__.py; OrpheusDL imports
    modules.<name>.interface, which works as a namespace package, but other
    Ripster code (and is_installed of sibling modules) expects the file. Harmless."""
    try:
        p = _module_path() / "__init__.py"
        if _module_path().is_dir() and not p.exists():
            p.write_text("", encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass


def _update_orpheus_settings(quality: str, save_path: str) -> None:
    """Point this engine's OWN OrpheusDL settings (corridor copy, see
    `_corridor_settings`) at this run's quality + folder. Only the two global
    keys are touched (the module has no settings of its own)."""
    sp = _settings_path()
    if not sp.exists():
        return
    try:
        cfg = json.loads(sp.read_text(encoding="utf-8"))
        gen = cfg.setdefault("global", {}).setdefault("general", {})
        if quality:
            gen["download_quality"] = quality
        if save_path:
            gen["download_path"] = save_path.rstrip("/\\") + "\\"
        sp.write_text(json.dumps(cfg, indent=4, ensure_ascii=False), encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass


def _is_cdn_error_page(p: Path) -> bool:
    """A small ".m4a" that is really the CDN's HTML error page."""
    try:
        if p.stat().st_size > 16 * 1024:
            return False
        head = p.read_bytes()[:512].lstrip().lower()
        return head.startswith(b"<html") or head.startswith(b"<!doctype html")
    except Exception:  # noqa: BLE001
        return False


@register
class OrpheusJioSaavnEngine(EngineBase):
    name = "orpheus_jiosaavn"

    def __init__(self):
        self._save_root: str = ""
        self._t0: float = 0.0
        self._net_seen = False
        self._ffmpeg: str = "ffmpeg"
        #: Fake audio files (CDN error pages) found + removed by is_finished.
        self.junk_removed: list[str] = []

    def qualities(self) -> list[dict]:
        return [dict(q) for q in _QUALITIES]

    def working_dir(self) -> str:
        return str(_orpheus_dir())

    def build_cmd(self, url: str, quality: str, config: dict) -> list[str]:
        if not (_orpheus_dir() / "orpheus.py").exists():
            raise ValueError("OrpheusDL не установлен — Настройки → Установка → OrpheusDL")
        _ensure_module_init()
        if not is_installed():
            raise ValueError(
                "Модуль JioSaavn не установлен — Настройки → Установка → JioSaavn "
                f"(или клонируй {REPO_URL} в orpheus/modules/jiosaavn/)")

        save_path = config.get("jiosaavn-save-path") or config.get("save-path") or ""
        orpheus_quality = _QUALITY_ORPHEUS.get((quality or "").lower(), "high")
        _update_orpheus_settings(orpheus_quality, save_path)
        self._save_root = save_path.rstrip("/\\") if save_path else ""
        self._t0 = time.time()
        # ffprobe ищем рядом с настроенным ffmpeg — так же, как это делает
        # раннер. Полагаться на PATH нельзя: он различается между оболочками,
        # и замер молча превращался бы в «не знаю».
        self._ffmpeg = (config.get("gamdl-ffmpeg-path") or "ffmpeg").strip() or "ffmpeg"

        # У JioSaavn входа нет — сессионный файл ему тоже не нужен общий,
        # пустой loginstorage лежит в его коридоре и к аккаунту Beatport не
        # имеет отношения.
        return orpheus_cmd(_corridor_config_dir("jiosaavn"), url, save_path)

    def iter_events(self, line: str, *, progress: tuple[int, int]):
        clean = _strip_ansi(line).strip()
        if not clean:
            return
        if _RE_NET.search(clean):
            self._net_seen = True
        yield from super().iter_events(clean, progress=progress)

    def classify_line(self, line: str) -> str:
        if _RE_NOT_AUDIO.search(line) or _RE_GEO.search(line):
            return "error"
        if _RE_ERROR.search(line):
            return "error"
        if _RE_SKIP.search(line):
            return "warn"
        if _RE_DOWNLOADING.search(line) or _RE_TRACK_FILE.search(line):
            return "success"
        return "stdout"

    def parse_progress(self, line: str, current: int, total: int) -> tuple[int, int]:
        m = _RE_TRACK_N.search(line)
        if m:
            cur, tot = int(m.group(1)), int(m.group(2))
            return max(0, cur - 1), tot
        return current, total

    # ── verdict ──────────────────────────────────────────────────────────────
    def _sweep_cdn_error_pages(self) -> list[str]:
        """Delete ".m4a" files written during THIS run that are really the CDN's
        HTML refusal. Scoped to our save root and to files newer than build_cmd,
        so nothing downloaded earlier (or by another engine) is ever touched."""
        removed: list[str] = []
        root = Path(self._save_root) if self._save_root else None
        if not root or not root.is_dir():
            return removed
        since = self._t0 - 5 if self._t0 else 0
        try:
            for p in root.rglob("*"):
                if p.suffix.lower() not in (".m4a", ".mp4", ".aac"):
                    continue
                try:
                    if since and p.stat().st_mtime < since:
                        continue
                except OSError:
                    continue
                if _is_cdn_error_page(p):
                    try:
                        p.unlink()
                        removed.append(str(p))
                    except OSError:
                        pass
        except Exception:  # noqa: BLE001
            pass
        self.junk_removed = removed
        return removed

    def _measured_tier(self) -> str:
        """Какую ступень мы получили НА САМОМ ДЕЛЕ, по файлу.

        320 есть не у каждого трека: `getCdnURL` спускается по лестнице
        320→160→96 до первой существующей. Папка при этом уже названа по
        ЗАПРОШЕННОМУ качеству и молча врёт — ровно та же ловушка, что была у
        Яндекса 28.07.2026. Возвращаем измеренное в штатном поле, раннер
        перепишет им качество задачи, и карточка, история и бот скажут правду.
        Пустая строка означает «не смог измерить» — это не то же самое, что
        «получил запрошенное», и выдумывать вместо неё ничего нельзя.
        """
        root = Path(self._save_root) if self._save_root else None
        if not root or not root.is_dir():
            return ""
        since = self._t0 - 5 if self._t0 else 0
        newest, newest_mt = None, since
        try:
            for p in root.rglob("*"):
                if p.suffix.lower() not in (".m4a", ".mp4", ".aac"):
                    continue
                try:
                    mt = p.stat().st_mtime
                except OSError:
                    continue
                if mt >= newest_mt:
                    newest, newest_mt = p, mt
        except Exception:  # noqa: BLE001
            return ""
        if newest is None:
            return ""

        import re as _re
        import subprocess as _sp
        ffprobe = _re.sub(r"ffmpeg(\.exe)?$",
                          lambda m: "ffprobe" + (m.group(1) or ""),
                          self._ffmpeg) if self._ffmpeg else "ffprobe"
        try:
            cp = _sp.run([ffprobe, "-v", "error", "-show_entries",
                          "format=bit_rate", "-of", "default=nw=1:nk=1",
                          str(newest)], capture_output=True, timeout=30,
                         creationflags=getattr(_sp, "CREATE_NO_WINDOW", 0))
            kbps = int((cp.stdout or b"").decode("utf-8", "ignore").strip() or 0) // 1000
        except Exception:  # noqa: BLE001
            return ""
        if kbps <= 0:
            return ""
        # Пороги стоят посередине между ступенями: замеры дают 97 / 161 / 321,
        # так что запас в обе стороны большой и округление контейнера не вредит.
        if kbps >= 240:
            return "high"
        if kbps >= 130:
            return "medium"
        return "low"

    def is_finished(self, log_text: str, rc: int = -1) -> EngineResult:
        log_text = log_text or ""
        junk = self._sweep_cdn_error_pages()

        if _RE_NO_MODULE.search(log_text):
            return EngineResult(False, error=(
                "JioSaavn: OrpheusDL не видит модуль jiosaavn — модуль не установлен "
                "или не импортируется (Настройки → Установка → JioSaavn)."))

        attempts = len(_RE_TRACK_FILE.findall(log_text))
        not_audio = bool(_RE_NOT_AUDIO.search(log_text))

        # 1) GEO. Strongest proof: the "audio" on disk is the CDN's HTML refusal.
        #    Second: mutagen choked on the file right after a download attempt, or
        #    the log carries the refusal wording itself — and NO network error.
        if junk or (attempts and not_audio and not _RE_NET.search(log_text)) \
                or (_RE_GEO.search(log_text) and not _RE_NET.search(log_text)):
            return EngineResult(False, error=_GEO_VERDICT)

        # 2) NETWORK — transport failed before/while fetching. Retryable.
        net = bool(_RE_NET.search(log_text)) or self._net_seen
        tb = "Traceback" in log_text
        if net and (tb or rc not in (0, -1) or attempts == 0):
            return EngineResult(False, error=_NET_VERDICT)

        # 3) CONTENT — the module crashed parsing the release (empty API shell).
        if tb and attempts == 0 and (_RE_CONTENT_FAIL.search(log_text)
                                     or _RE_PARSE_EXC.search(log_text)):
            return EngineResult(False, error=_CONTENT_VERDICT)

        # 4) Success: OrpheusDL aborts the WHOLE run on the first exception, so a
        #    traceback anywhere means the release is incomplete.
        if attempts and not tb and rc in (0, -1):
            return EngineResult(success=True, tracks_ok=attempts)
        if rc == 0 and not tb and _RE_SKIP.search(log_text):
            return EngineResult(success=True, tracks_ok=0)
        if attempts and tb:
            done = max(0, attempts - 1)
            return EngineResult(False, tracks_ok=done, tracks_err=1, error=(
                f"JioSaavn: загрузка оборвалась на треке {attempts} "
                f"(сохранено {done}) — {self._last_exc(log_text) or 'см. лог'}"))

        exc = self._last_exc(log_text)
        if exc:
            return EngineResult(False, error=f"OrpheusDL JioSaavn: {exc}")
        if rc == 0 and not log_text.strip():
            return EngineResult(False, error="OrpheusDL JioSaavn: нет вывода")
        return EngineResult(False, error=f"OrpheusDL JioSaavn: завершился с кодом {rc}")

    # ── search (public web API, no login; works outside India — only the audio
    #    CDN is geo-locked, verified 19.09.2026 from a RU IP) ────────────────────
    async def search(self, query: str, search_type: str, limit: int, config: dict) -> list[dict]:
        return (await search_jiosaavn(query, search_type, limit)).get("results") or []

    async def get_album(self, album_id: str, config: dict):
        """Card for /api/album/jiosaavn/<id> — the Yandex shape {album, tracks}."""
        d = await _webapi_get(_saavn_token(album_id), "album")
        err = d.pop("error", None)
        if err:
            return {"error": err}
        # A wrong or removed id is NOT a 404: JioSaavn answers 200 with a filler
        # card (albumid "0", one fake "sample trailer" song). Only the fields the
        # card is built from say whether the release is real.
        if not (d.get("title") or d.get("name")) or str(d.get("albumid") or "0") in ("", "0"):
            return {"error_key": "err.album_not_found", "error": "Альбом не найден"}
        enc = _saavn_token(d.get("perma_url")) or str(d.get("albumid") or "")
        songs = [s for s in (d.get("songs") or []) if _un(s.get("song"))]
        tracks = [{
            "id":      str(s.get("id", "")),
            "title":   _un(s.get("song")),
            "artist":  _un(s.get("primary_artists") or s.get("singers") or d.get("primary_artists")),
            "duration": _int_or(s.get("duration")),
            "track_no": n,
            "url":     s.get("perma_url", ""),
            "cover":   _big_cover(s.get("image", "")),
        } for n, s in enumerate(songs, 1)]
        return {"album": {
            "id":      enc,
            "title":   _un(d.get("title") or d.get("name")),
            "artist":  _un(d.get("primary_artists")),
            "cover":   _big_cover(d.get("image", "")),
            "year":    str(d.get("year") or ""),
            "date":    str(d.get("release_date") or ""),
            "label":   _un((songs[0] if songs else {}).get("label")),
            "tracks":  len(tracks),
            "service": "jiosaavn",
            "url":     d.get("perma_url") or f"https://www.jiosaavn.com/album/{enc}",
        }, "tracks": tracks}

    async def get_artist(self, artist_id: str, types: str, config: dict):
        """Card for /api/artist/jiosaavn/<id> — the Yandex shape {artist, releases}."""
        tok = _saavn_token(artist_id)
        # webapi.get answers 500 (empty body) for an artist without the flags the
        # web page itself sends — the module's own docs call this out.
        d = await _webapi_get(tok, "artist", {
            "sub_type": "all", "isGallery": "true", "isMusic": "true", "isOverview": "true",
            "album_page_size": "100", "artist_page_size": "100"})
        err = d.pop("error", None)
        if err:
            return {"error": err, "releases": []}
        if not d.get("name") or d.get("artistId") in (None, ""):
            return {"error": "JioSaavn: артист не найден", "releases": []}
        want = {t.strip().lower() for t in (types or "").split(",") if t.strip()}
        releases, seen = [], set()
        # topAlbums сервиса отдают пустым; релизы артиста лежат в двух других
        # модулях — берём все три, какой ответит.
        for key, kind in (("topAlbums", "album"),
                         ("latest_release", ""),      # album|single по числу треков
                         ("singles", "single")):
            for it in (d.get(key) or []):
                enc = _saavn_token(it.get("url") or "")
                n = _int_or(it.get("numSongs")) or 1
                typ = kind or ("album" if n > 1 else "single")
                if not enc or enc in seen or (want and typ not in want):
                    continue
                seen.add(enc)
                releases.append({
                    "id":      enc,
                    "title":   _un(it.get("album") or it.get("title")),
                    "artist":  _un(it.get("primaryArtists") or d.get("name")),
                    "cover":   _big_cover((it.get("imageUrl") or [""])[0]
                                          if isinstance(it.get("imageUrl"), list)
                                          else it.get("imageUrl", "")),
                    "year":    str(it.get("year") or ""),
                    "date":    str(it.get("release_date") or ""),
                    "tracks":  n,
                    "type":    typ,
                    "url":     it.get("url", ""),
                    "service": "jiosaavn",
                })
        releases.sort(key=lambda r: r.get("date") or r.get("year") or "", reverse=True)
        urls = d.get("urls") or {}
        return {"artist": {
            "id":      tok,
            "name":    _un(d.get("name")),
            "picture": _big_cover(d.get("image", "")),
            "url":     urls.get("songs") or urls.get("overview")
                       or f"https://www.jiosaavn.com/artist/{tok}",
            "service": "jiosaavn",
        }, "releases": releases}

    @staticmethod
    def _last_exc(log_text: str) -> str:
        for ln in reversed(log_text.splitlines()):
            s = ln.strip()
            if re.match(r'^[A-Za-z_][\w.]*(Error|Exception):\s', s):
                return s[:200]
        return ""


# ── Search helper (shared by the engine and /api/search) ─────────────────────
_SEARCH_CALL = {
    "album":    "search.getAlbumResults",
    "track":    "search.getResults",
    "artist":   "search.getArtistResults",
    "playlist": "search.getPlaylistResults",
}

_API_URL = "https://www.jiosaavn.com/api.php"


def _un(s) -> str:
    """JioSaavn escapes its text fields (&amp;, &#38;, …) even in JSON."""
    return html.unescape(str(s if s is not None else "")).strip()


def _int_or(v) -> int:
    try:
        return int(str(v).strip())
    except (TypeError, ValueError):
        return 0


def _saavn_token(v) -> str:
    """JioSaavn знает два вида id: числовой (38682222) и кодированный
    ('wSM2AOubajk_' — хвост perma_url). webapi.get понимает ТОЛЬКО второй:
    на числовой он молча отвечает пустой карточкой-заглушкой. Поэтому карточка
    поиска несёт в `id` кодированный id, а сюда прилетает уже он; заодно
    принимаем и полную ссылку."""
    s = str(v or "").strip()
    if "/" in s:
        s = s.split("?", 1)[0].split("#", 1)[0].rstrip("/")
        s = s.rsplit("/", 1)[-1]
    return s


async def _webapi_get(token: str, typ: str, extra: dict | None = None) -> dict:
    """Один вызов webapi.get → разобранный ответ либо {'error': ...}.

    Ошибку считаем честной и различимой: сеть — это сеть, 500 — это битый id,
    а «не найдено» сервис ответит 200 с заглушкой, это решают наверху."""
    import httpx
    params = {"__call": "webapi.get", "token": token, "type": typ,
              "_format": "json", "_marker": "0", "ctx": "web6dot0"}
    params.update(extra or {})
    if not token:
        return {"error": "JioSaavn: пустой id"}
    try:
        async with httpx.AsyncClient(timeout=12, follow_redirects=True,
                                     headers={"User-Agent": "Mozilla/5.0"}) as c:
            r = await c.get(_API_URL, params=params)
    except (httpx.TransportError, OSError) as e:
        return {"error": f"JioSaavn: сеть недоступна ({type(e).__name__})"}
    except Exception as e:  # noqa: BLE001
        return {"error": f"JioSaavn: {type(e).__name__}: {e}"}
    if r.status_code != 200:
        return {"error": f"JioSaavn: карточка не открылась (HTTP {r.status_code}) — "
                         f"сервис не принял id {token!r}"}
    try:
        data = r.json() or {}
    except Exception:  # noqa: BLE001
        return {"error": "JioSaavn: сервис ответил не JSON"}
    if not isinstance(data, dict):
        return {"error": "JioSaavn: неожиданный ответ сервиса"}
    err = data.get("error")
    if isinstance(err, dict):
        return {"error": f"JioSaavn: {err.get('msg') or err.get('code') or 'ошибка сервиса'}"}
    return data


def _big_cover(url: str) -> str:
    """JioSaavn image URLs carry the size (…-150x150.jpg / _50x50.jpg); 500 is the
    largest the CDN serves."""
    return re.sub(r'([_-])(?:50x50|150x150)(\.\w+)$', r'\g<1>500x500\2', url or "")


async def search_jiosaavn(q: str, ent: str, limit: int) -> dict:
    """Search JioSaavn's public web API → the shared result-card shape
    ({id,title,artist,type,url,cover,year,service}). On failure returns an
    honest error instead of an empty list that reads as 'nothing found'."""
    import httpx
    ent = ent if ent in _SEARCH_CALL else "album"
    params = {"__call": _SEARCH_CALL[ent], "q": q, "p": "1",
              "n": str(max(1, min(int(limit or 20), 50))),
              "_format": "json", "_marker": "0", "api_version": "4", "ctx": "web6dot0"}
    try:
        async with httpx.AsyncClient(timeout=12, follow_redirects=True,
                                     headers={"User-Agent": "Mozilla/5.0"}) as c:
            r = await c.get(_API_URL, params=params)
        if r.status_code != 200:
            return {"results": [], "error": f"JioSaavn: поиск ответил HTTP {r.status_code}"}
        data = r.json() or {}
    except (httpx.TransportError, OSError) as e:
        return {"results": [], "error": f"JioSaavn: сеть недоступна ({type(e).__name__})"}
    except Exception as e:  # noqa: BLE001
        return {"results": [], "error": f"JioSaavn: {type(e).__name__}: {e}"}

    un = _un
    out: list[dict] = []
    for it in (data.get("results") or []):
        mi = it.get("more_info") or {}
        prim = [a.get("name", "") for a in ((mi.get("artistMap") or {}).get("primary_artists") or [])]
        if ent == "artist":
            name = un(it.get("name") or it.get("title"))
            artist = name
        elif ent == "playlist":
            name = un(it.get("title"))
            artist = un(mi.get("firstname") or it.get("subtitle"))
        else:
            name = un(it.get("title"))
            artist = un(", ".join(p for p in prim if p) or mi.get("music") or it.get("subtitle"))
        row = {
            "id":      _saavn_token(it.get("perma_url")) or str(it.get("id", "")),
            "title":   name,

            "artist":  artist,
            "type":    ent,
            "url":     it.get("perma_url", ""),
            "cover":   _big_cover(it.get("image", "")),
            "year":    str(it.get("year") or ""),
            "explicit": str(it.get("explicit_content", "0")) == "1",
            "service": "jiosaavn",
        }
        if ent == "track":
            row["album"] = un(mi.get("album"))
            try:
                row["duration"] = int(mi.get("duration") or 0)
            except (TypeError, ValueError):
                pass
        if row["url"]:
            out.append(row)
    return {"results": out}
