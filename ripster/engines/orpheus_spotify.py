"""OrpheusDL-Spotify engine — downloads Spotify URLs via OrpheusDL + PKCE OAuth.

Authentication: browser-based PKCE OAuth (supports Apple ID, Google, etc.)
Credentials stored in: orpheus/config/credentials.json (auto-refreshed)
Quality: best available for account (Premium → ~320 kbps OGG Vorbis)
"""
from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path

from .base import EngineBase, EngineResult, Event, EventKind, LineLevel, _strip_ansi
from .errors import classify_download_error
from .registry import register

# ── paths ─────────────────────────────────────────────────────────────────────
def _base_dir() -> Path:
    return Path(sys.argv[0]).resolve().parent if sys.argv else Path(".").resolve()

def _orpheus_dir() -> Path:
    return _base_dir() / "orpheus"


def _orpheus_python() -> str:
    """Interpreter for OrpheusDL. Prefer the ISOLATED venv (tools/orpheusvenv):
    OrpheusDL pins protobuf==3.15.8, which — if installed into the shared bundled
    python — breaks AMD (Apple) and pywidevine (both need protobuf>=6.33). Keeping
    it in its own venv is the fix. Falls back to the current interpreter.
    See the ripster-dependency-versions skill."""
    base = _base_dir()
    for sub in (("Scripts", "python.exe"), ("bin", "python")):
        cand = base / "tools" / "orpheusvenv" / sub[0] / sub[1]
        if cand.is_file():
            return str(cand)
    return sys.executable

def _creds_path() -> Path:
    return _orpheus_dir() / "config" / "credentials.json"

def _settings_path() -> Path:
    return _orpheus_dir() / "config" / "settings.json"

# ── patterns ──────────────────────────────────────────────────────────────────
_RE_DOWNLOADING  = re.compile(r'=== Downloading\s+(track|album|playlist|artist)\s+(.+?)\s+\(', re.I)
_RE_TRACK_FILE   = re.compile(r'Downloading track file', re.I)
_RE_DONE_TRACK   = re.compile(r'=== Done', re.I)
_RE_ERROR        = re.compile(r'\berror\b|\bfailed\b|\bexception\b|\bTraceback', re.I)
_RE_SKIP         = re.compile(r'skip|already exist|ignore', re.I)
# Only the real track counter "Track N/M" — NOT any "N/M" (e.g. librespot's
# "session attempt 1/4 failed" would otherwise hijack the count → "1/4" on a
# 14-track album, the cause of the wrong total in the bot card).
_RE_PROGRESS     = re.compile(r'\bTrack\s+(\d+)\s*/\s*(\d+)', re.I)
_RE_NEED_LOGIN    = re.compile(r'Logging into|authorization|ORPHEUS_AUTH_URL', re.I)
_RE_PREMIUM       = re.compile(r'premium|subscription', re.I)
_RE_NEW_SETTINGS  = re.compile(r'New settings detected|configuration has been reset', re.I)
# OrpheusDL prints this once, right after "=== Downloading playlist … ===" and
# BEFORE the first track → lets us refuse an oversized native playlist up-front.
_RE_PLAYLIST_COUNT = re.compile(r'Number of tracks:\s*(\d+)', re.I)


def _playlist_cap() -> int:
    """Max tracks for a NATIVE Spotify playlist before we refuse it (a 1000-track
    playlist = hours of downloads + a real ban risk, and OrpheusDL's playlist path
    is fragile at scale). Env-overridable (self-update); 0 disables the cap."""
    try:
        return max(0, int(os.environ.get("SPOTIFY_PLAYLIST_MAX") or 100))
    except (TypeError, ValueError):
        return 100


# ── self-healing vendor patch: OrpheusDL Spotify playlist crash ──────────────
# A playlist puts fully-parsed TrackInfo OBJECTS in PlaylistInfo.tracks (albums
# use id strings), so OrpheusDL's core passes a TrackInfo as `track_id` into
# get_track_info, where `track_id in self._track_info_cache` raises
# `TypeError: unhashable type: 'TrackInfo'` and kills EVERY native Spotify
# playlist on track 1. OrpheusDL is installed at runtime (not bundled), so a
# fresh install re-introduces the bug → patch the vendor file idempotently before
# each run, mirroring the deezer-py guard (issue #23).
_GTI_ANCHOR = ('        """Fetches track information and parses it into a TrackInfo '
               'object. Also handles episode IDs via fallback."""\n')
_GTI_GUARD = (
    "        # AUTO-PATCH (Ripster): a playlist passes a TrackInfo object as track_id;\n"
    "        # return it directly so an unhashable TrackInfo never reaches the cache\n"
    "        # dict test below (TypeError: unhashable type: 'TrackInfo').\n"
    "        if isinstance(track_id, TrackInfo):\n"
    "            return track_id\n"
)


def _patch_gti_src(src: str) -> str:
    """Inject the TrackInfo guard into interface.py's get_track_info source.
    Idempotent: no-op if already guarded or the anchor is gone (upstream change)."""
    if "if isinstance(track_id, TrackInfo):" in src:
        return src
    if _GTI_ANCHOR in src:
        return src.replace(_GTI_ANCHOR, _GTI_ANCHOR + _GTI_GUARD, 1)
    return src


def _ensure_playlist_patch() -> None:
    """Apply `_patch_gti_src` to the installed OrpheusDL Spotify interface, on disk,
    idempotently. Self-healing: survives a fresh OrpheusDL install / reinstall."""
    try:
        p = _orpheus_dir() / "modules" / "spotify" / "interface.py"
        if not p.exists():
            return
        src = p.read_text(encoding="utf-8")
        patched = _patch_gti_src(src)
        if patched != src:
            p.write_text(patched, encoding="utf-8")
            print("[orpheus] patched Spotify interface (playlist TrackInfo guard)", flush=True)
    except Exception as e:
        print(f"[orpheus] playlist-guard patch skipped: {e}", flush=True)

_QUALITIES = [
    {
        "id": "hifi",   "label": "HiFi",   "engine": "orpheus_spotify",
        "sub": "Лучшее доступное (OGG ~320 kbps, Premium)",
        "badge": "HIFI",  "color": "#3ecfaa", "bitrate": "320 kbps",
        "ext": "ogg",  "req": "premium",
    },
    {
        "id": "high",   "label": "High",   "engine": "orpheus_spotify",
        "sub": "OGG ~160 kbps",
        "badge": "HIGH",  "color": "#EF9F27", "bitrate": "160 kbps",
        "ext": "ogg",  "req": "free",
    },
    {
        "id": "normal", "label": "Normal", "engine": "orpheus_spotify",
        "sub": "OGG ~96 kbps",
        "badge": "96k",   "color": "#6a6a8a", "bitrate": "96 kbps",
        "ext": "ogg",  "req": "free",
    },
]

_QUALITY_ORPHEUS = {
    "hifi":   "hifi",      # native OGG ~320 (premium)
    "ogg":    "hifi",
    "320":    "hifi",      # download OGG 320, then transcode → MP3 320
    "high":   "high",
    "normal": "medium",
}


def is_installed() -> bool:
    # Both the entry script AND the inner orpheus/ package must exist — a partial
    # clone with only orpheus.py crashes with "ModuleNotFoundError: orpheus.core".
    return ((_orpheus_dir() / "orpheus.py").exists()
            and (_orpheus_dir() / "orpheus" / "core.py").exists())


def _blob_path() -> Path:
    """Durable desktop/zeroconf reusable-credentials blob (tools/spotify_pair.py)."""
    return _orpheus_dir() / "config" / ".librespot_cache" / "reusable_credentials.json"


def _blob_bak_path() -> Path:
    """Backup the re-login flow (routes/core.py spotify_auth_start) parks the live
    blob into before opening a browser OAuth. An ABANDONED re-login (browser closed
    without finishing) used to strand the durable credential here forever, so
    downloads died with ORPHEUS_NOT_AUTHED even though the account was still valid."""
    return _orpheus_dir() / "config" / ".librespot_cache" / "reusable_credentials.json.bak"


def _heal_blob() -> bool:
    """Self-heal: if the live blob is missing but a backup exists, restore it.
    Makes Spotify auth autonomous — an interrupted re-login can no longer log the
    account out permanently. Returns True if a usable blob exists afterwards."""
    live, bak = _blob_path(), _blob_bak_path()
    if live.exists():
        return True
    if bak.exists():
        try:
            import shutil as _sh
            _sh.copy2(bak, live)   # copy, not move — keep the backup as a safety net
            return True
        except OSError:
            return bak.exists()
    return False


def is_authenticated() -> bool:
    # Durable desktop/zeroconf blob is a full, long-lived credential — prefer it.
    # Self-heal from the .bak an abandoned re-login may have left behind.
    if _heal_blob():
        return True
    p = _creds_path()
    if not p.exists():
        return False
    try:
        d = json.loads(p.read_text(encoding="utf-8"))
        return bool(d.get("access_token"))
    except Exception:
        return False


def delete_creds() -> None:
    p = _creds_path()
    if p.exists():
        try:
            p.unlink()
        except OSError:
            pass


def _update_orpheus_settings(quality: str, save_path: str, config: dict,
                             convert_mp3: bool | None = None) -> None:
    """Patch orpheus/config/settings.json at runtime."""
    sp = _settings_path()
    if not sp.exists():
        return
    try:
        _orig = sp.read_text(encoding="utf-8")
        cfg = json.loads(_orig)
        gen = cfg.setdefault("global", {}).setdefault("general", {})
        if quality:
            gen["download_quality"] = quality
        if save_path:
            gen["download_path"] = save_path.rstrip("/\\") + "\\"

        # Folder structure: always create Artist/Album/ hierarchy
        fmt = cfg["global"].setdefault("formatting", {})
        fmt.setdefault("album_format",             "{name}{explicit}")
        fmt.setdefault("playlist_format",          "{name}{explicit}")
        fmt.setdefault("track_filename_format",    "{track_number}. {name}")
        fmt["single_full_path_format"] = "{artist}/{name}"  # track-only URLs get artist subfolder
        fmt.setdefault("enable_zfill",             True)

        # Cover: embed a uniform 1000×1000 in-audio cover (per request, all
        # services). Spotify's source art maxes at 640, so it simply delivers its
        # native max here — the 1000 cap only matters for larger sources. The
        # external cover.jpg stays highest-res (original on disk unaffected).
        covers = cfg["global"].setdefault("covers", {})
        covers["embed_cover"]          = True
        covers["main_resolution"]      = 1000
        covers["save_external"]        = True
        covers["external_format"]      = "jpg"
        covers["external_resolution"]  = 3000
        covers["external_compression"] = "low"

        # MP3 conversion (lossy→lossy, requires enable_undesirable_conversions).
        # Default OFF — keep the NATIVE Spotify OGG Vorbis. Transcoding OGG→MP3 is
        # lossy→lossy (quality loss for zero gain) and was silently degrading every
        # download; the user opts in via the Settings toggle if they really want MP3.
        advanced = cfg["global"].setdefault("advanced", {})
        _want_mp3 = (config.get("orpheus-convert-mp3", False)
                     if convert_mp3 is None else bool(convert_mp3))
        if _want_mp3:
            advanced["codec_conversions"]           = {"vorbis": "mp3"}
            advanced["enable_undesirable_conversions"] = True
            advanced.setdefault("conversion_flags", {})["mp3"] = {"b:a": "320k"}
            advanced["conversion_keep_original"]    = False
        else:
            conv = advanced.get("codec_conversions", {})
            conv.pop("vorbis", None)
            if not conv:
                advanced.pop("codec_conversions", None)
            advanced["enable_undesirable_conversions"] = False

        sp_mod = cfg.setdefault("modules", {}).setdefault("spotify", {})
        # Always sync username from credentials.json so it stays consistent
        # even after re-auth or credentials reset.
        creds_p = _creds_path()
        if creds_p.exists():
            try:
                uname = json.loads(creds_p.read_text(encoding="utf-8")).get("spotify_username", "")
                if uname:
                    sp_mod["username"] = uname
            except Exception:
                pass
        # OrpheusDL requires a non-empty username to start PKCE flow;
        # use a stable placeholder when credentials.json is absent (first run / after reset).
        if not sp_mod.get("username"):
            sp_mod["username"] = "orpheus_pkce_user"
        # Always reset client_id/secret to "" so OrpheusDL falls back to its built-in PKCE
        # client (65b708073f...). A non-empty custom client_id triggers a stored-vs-current
        # mismatch check that wipes credentials.json.
        sp_mod["client_id"] = ""
        sp_mod["client_secret"] = ""

        # Idempotent write: only touch settings.json when something actually
        # changed. OrpheusDL prints "New settings detected" and exits whenever the
        # file changes, so re-writing identical content on every run (incl. the
        # auto-retry) would loop it forever. Writing only on real change lets the
        # retry run see a stable file and proceed.
        _new = json.dumps(cfg, indent=4, ensure_ascii=False)
        if _new != _orig:
            sp.write_text(_new, encoding="utf-8")
    except Exception:
        pass


@register
class OrpheusSpotifyEngine(EngineBase):
    name = "orpheus_spotify"

    def qualities(self) -> list[dict]:
        return list(_QUALITIES)

    def working_dir(self) -> str:
        return str(_orpheus_dir())

    def build_cmd(self, url: str, quality: str, config: dict) -> list[str]:
        if not is_installed():
            raise ValueError("OrpheusDL не установлен — перейди в Settings → Spotify")
        if not is_authenticated():
            raise ValueError(
                "ORPHEUS_NOT_AUTHED: нет сохранённых учётных данных Spotify — "
                "войди через Settings → Spotify"
            )

        # Heal OrpheusDL's playlist crash before each run (self-healing, survives a
        # fresh OrpheusDL install — native Spotify playlists used to die on track 1).
        _ensure_playlist_patch()

        save_path = (
            config.get("orpheus-save-path") or
            config.get("save-path") or
            ""
        )
        orpheus_quality = _QUALITY_ORPHEUS.get(quality, "hifi")
        # Per-download format from the quality code: "320" → transcode OGG→MP3 320;
        # "hifi"/"ogg" → keep NATIVE OGG. Other codes fall back to the global toggle.
        if quality in ("320", "mp3"):
            _conv = True
        elif quality in ("hifi", "ogg", "native"):
            _conv = False
        else:
            _conv = None
        _update_orpheus_settings(orpheus_quality, save_path, config, convert_mp3=_conv)

        # Bundled embeddable Python runs ISOLATED (sys.flags.isolated==1 via ._pth) so
        # it does NOT add the script dir to sys.path and ignores PYTHONPATH → a plain
        # `python orpheus.py` fails with "ModuleNotFoundError: orpheus.core". Bootstrap
        # via -c to put the OrpheusDL dir on sys.path before running orpheus.py.
        orph_dir   = str(_orpheus_dir())
        orpheus_py = str(_orpheus_dir() / "orpheus.py")
        _boot = (
            "import sys, runpy; "
            f"sys.path.insert(0, {orph_dir!r}); "
            f"sys.argv = [{orpheus_py!r}] + sys.argv[1:]; "
            f"runpy.run_path({orpheus_py!r}, run_name='__main__')"
        )
        cmd = [_orpheus_python(), "-c", _boot]
        if save_path:
            cmd += ["-o", save_path.rstrip("/\\")]
        cmd.append(url)
        return cmd

    def iter_events(self, line: str, *, progress: tuple[int, int]):
        clean = _strip_ansi(line).strip()
        if not clean:
            return

        if "ORPHEUS_NOT_AUTHED" in clean or _RE_NEED_LOGIN.search(clean):
            yield Event(
                kind=EventKind.FATAL,
                message="ORPHEUS_NOT_AUTHED: нет сохранённых данных Spotify — войди через Settings → Spotify",
                level=LineLevel.ERROR,
            )
            return

        # Refuse an oversized native playlist UP-FRONT — OrpheusDL prints the count
        # before track 1, so a FATAL here makes the runner cancel before any
        # download (no 1000-track grind / ban risk).
        _m_cnt = _RE_PLAYLIST_COUNT.search(clean)
        if _m_cnt:
            _cap = _playlist_cap()
            _n = int(_m_cnt.group(1))
            if _cap and _n > _cap:
                yield Event(
                    kind=EventKind.FATAL,
                    message=(f"Spotify-плейлист слишком большой для нативной загрузки: {_n} "
                             f"треков (лимит {_cap}). Возьми частями или используй convert "
                             f"(по трекам в lossless). Лимит меняется env SPOTIFY_PLAYLIST_MAX."),
                    level=LineLevel.ERROR,
                )
                return

        yield from super().iter_events(clean, progress=progress)

    def classify_line(self, line: str) -> str:
        if _RE_ERROR.search(line):        return "error"
        if _RE_PREMIUM.search(line):      return "warn"
        if _RE_SKIP.search(line):         return "warn"
        if _RE_NEW_SETTINGS.search(line): return "warn"
        if _RE_DOWNLOADING.search(line) or _RE_TRACK_FILE.search(line):
            return "success"
        return "stdout"

    def parse_progress(self, line: str, current: int, total: int) -> tuple[int, int]:
        m = _RE_PROGRESS.search(line)
        if m:
            cur, tot = int(m.group(1)), int(m.group(2))
            # "Track N/M" fires at the START of track N — N-1 tracks are completed
            return max(0, cur - 1), tot
        return current, total

    def is_finished(self, log_text: str, rc: int = -1) -> EngineResult:
        if "ORPHEUS_NOT_AUTHED" in log_text or _RE_NEED_LOGIN.search(log_text):
            return EngineResult(False, error="ORPHEUS_NOT_AUTHED: войди в Spotify через Settings")

        if _RE_NEW_SETTINGS.search(log_text):
            return EngineResult(
                False,
                error="OrpheusDL: обнаружены новые настройки — файл settings.json был создан/сброшен. "
                      "Повтори загрузку (обычно исправляется автоматически).",
            )

        # "=== Track ... failed ===" is the definitive failure marker per track.
        track_failed = len(re.findall(r'=== Track .+ failed ===', log_text, re.I))
        downloads = len(re.findall(r'Downloading track file', log_text, re.I))
        # Librespot auth failure means no audio was actually saved.
        librespot_fail = bool(re.search(r'Librespot.{0,40}fail|BadCredentials|credentials.*missing', log_text, re.I))
        ok = max(0, downloads - track_failed)
        if librespot_fail and ok == 0:
            return EngineResult(
                False,
                error="ORPHEUS_NOT_AUTHED: Librespot не смог войти — токен истёк. "
                      "Открой Settings → Spotify, нажми «Войти снова».",
            )
        # Token expired / auth wall: metadata 401 → nothing downloaded. Must NOT be
        # reported as success (otherwise the bot shows "готово" with zero files).
        if downloads == 0 and re.search(
                r'unauthorized\s*\(401\)|Auth error\s*\(401\)|processing failed|'
                r'Could not extract access token|getAlbum unauthorized|getTrack unauthorized',
                log_text, re.I):
            return EngineResult(
                False,
                error="ORPHEUS_NOT_AUTHED: Spotify-токен истёк (401). Обнови: бот → "
                      "/sptoken, или Рипстер → Настройки → Spotify.")

        if downloads > 0:
            # Partial success: at least one track was fetched. Do NOT fail the whole
            # release when some tracks fail — `ok = downloads - track_failed` can hit
            # 0 even though a track landed on disk (a track that fails BEFORE its
            # "Downloading track file" line still counts in track_failed). Deliver
            # what we got; the runner's silent-partial guard recounts actual files on
            # disk and marks the release partial, so the bot sends them + offers ↺.
            return EngineResult(success=True, tracks_ok=max(ok, 0), tracks_err=track_failed)

        if rc == 0 and log_text.strip():
            skips = len(re.findall(r'skip|already exist', log_text, re.I))
            if skips:
                return EngineResult(success=True, tracks_ok=0, tracks_err=0)
            return EngineResult(success=True)

        if rc == 0 and not log_text.strip():
            return EngineResult(False, error="OrpheusDL: нет вывода — возможно ошибка аутентификации")

        if _RE_PREMIUM.search(log_text):
            return EngineResult(False, error="OrpheusDL: требуется Spotify Premium для этого качества")

        # Phantom/removed track (Spotify often shows a link to something deleted)
        # or a geo-locked release → tell the user the real reason.
        _cls = classify_download_error(log_text)
        if _cls:
            return EngineResult(False, error=f"Spotify: {_cls[1]}")

        return EngineResult(False, error=f"OrpheusDL: завершился с кодом {rc} — проверь логин")
