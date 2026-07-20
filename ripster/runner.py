"""
Task runner — executes individual download tasks and manages the queue loop.

Public API:
    install(ctx: AppContext)
    run_task(task)
    process_queue()
"""
from __future__ import annotations

import asyncio
import os
import re as _re
import time
from datetime import datetime
from pathlib import Path

# Errors that are not worth retrying (auth, subscription, missing binary, etc.)
_RE_NO_RETRY = _re.compile(
    r'auth.*fail|invalid.*token|token.*invalid|invalid.*arl|arl.*invalid|'
    r'unauthorized|not\s+logged|login\s+failed|AuthenticationError|'
    r'бесплатный\s+аккаунт|free\s+account|IneligibleError|subscription|'
    r'Не\s+найден\s+исполняемый|binary\s+not\s+found|No\s+such\s+file|'
    r'токен\s+недействителен|wrapper\s+not\s+responding|ORPHEUS_NOT_AUTHED|'
    r'-1002|KeyError.*AUDIO-SESSION|codec\s+not\s+found|cookies|'
    r'\bForbidden\b|запрос\s+отклонён|токен\s+истёк|'
    # Qobuz "0 tracks" is a permanent no-account / bad-link condition (downloads
    # need the user's own paid Qobuz login) — retrying it just spins the task for
    # ~28 min and the tile looks stuck. Fail fast with the clear message instead.
    r'Qobuz:\s*0\s+треков|'
    # Qobuz precise diagnostics (geo/licensing lock, wrong custom app_id/secret,
    # missing creds) are ALL permanent until the user changes something — retrying
    # just spins the tile. Match their distinctive wording from engines/qobuz.py.
    r'гео-?/?лицензионное\s+ограничение|app_id/secret\s+не\s+подошли|'
    r'не\s+заданы\s+данные\s+входа|'
    # Deezer ARL missing/expired (engines/deezer.py) — permanent until ARL updated.
    r'ARL\s+не\s+задан|'
    # SoundCloud engine not installed / no Node (engines/soundcloud.py) — permanent.
    r'движок\s+не\s+установлен|'
    # Beatport territory restriction is permanent for this account/region.
    r'Territory\s+Restricted|недоступен в регионе|region\s+locked',
    _re.I,
)
_MAX_AUTO_RETRIES = 3
_RETRY_BACKOFF    = [15, 45, 120]   # seconds before each retry attempt

# Transient public-wrapper conditions (wm.wol.moe overloaded / not-ready / no
# device for region / unreachable / 0 tracks). These are NOT real failures —
# the wrapper is just busy — so they get many more patient retries (capped at
# 120s spacing → no hammering), instead of giving up after 3. This is the fix
# for "downloads silently fail during a wrapper overload window" (and the guest
# 'directory error' that followed an empty download). A genuine no-lossless
# asset is matched by _RE_NO_RETRY-style messages and is NOT patient-retried.
_RE_PATIENT = _re.compile(
    # NOTE: do NOT match a bare "0 треков" here — that also matches Qobuz's
    # no-account failure, which is permanent and must NOT be patient-retried (it
    # spun the tile for ~28 min). The AMD/wrapper "0 треков" cases still match via
    # wm.wol.moe / "не вернул device" / ready=false below.
    r'wm\.wol\.moe|wrapper-manager unreachable|ready=false|не\s+ready|'
    r'не\s+вернул device|wrapper\s+не\s+ready|no\s+device',
    _re.I,
)
_MAX_PATIENT_RETRIES = 15   # ~28 min of patient retrying through an overload blip

# Decrypt gRPC CORE down (decrypt_init AioRpcError / StatusCode.UNAVAILABLE /
# "DECRYPT STREAM DOWN"). This is NOT the same as "wrapper busy" — the public
# wrapper's decrypt core itself is overloaded/dead and does NOT recover by
# hammering it for half an hour (observed: 15×120s = 35 min, all UNAVAILABLE).
# Give it a FEW honest tries, then stop with clear guidance (raise a LOCAL
# docker wrapper / try later / non-Hi-Res). Matched BEFORE _RE_PATIENT (the
# decrypt message also contains "wm.wol.moe", which would otherwise grant 15).
_RE_DECRYPT_DOWN = _re.compile(
    r'DECRYPT\s+STREAM\s+DOWN|AMD_WRAPPER_DECRYPT_ERROR|ошибку\s+декрипт|'
    r'decrypt_init|AioRpcError|decrypt.*UNAVAILABLE|внутренн.*ошибку\s+декрипт',
    _re.I,
)
_MAX_DECRYPT_RETRIES = 2   # ~3 min, then stop — decrypt core won't self-heal now

# Public wrapper is DEAD/UNREACHABLE (not merely busy): wm.wol.moe not resolving/
# refusing/timing out its manager API. Retrying a server that is DOWN is pure waste
# — it just makes the tile "hang" for many minutes while the user keeps re-queuing
# the same release. Fail FAST (one honest retry) with a clear message that names the
# real cause (public server down → use local ALAC / try later), so the owner does
# NOT keep pulling the same Hi-Res release. Checked BEFORE _RE_DECRYPT_DOWN /
# _RE_PATIENT (all three can mention wm.wol.moe).
_RE_WRAPPER_DEAD = _re.compile(
    r'wrapper-manager unreachable|Wrapper-manager\s+недоступен|Deadline\s+Exceeded|'
    r'getaddrinfo|Name or service not known|Connection refused|'
    r'connection refused|Max retries exceeded|Failed to establish a new connection|'
    r'ConnectError|ConnectTimeout|11001',
    _re.I,
)
_MAX_DEAD_RETRIES = 1   # one quick re-check, then stop with clear guidance


# ── Partial-download reason classifier (issue #5) ───────────────────────────────
# When a release comes back short, name WHY in one canonical token so the bot
# card / web history can show it ("3/5 — region-locked") instead of a vague
# "some tracks missing". Ordered most-specific → generic; first match wins.
_PARTIAL_REASON_PATTERNS = [
    ("decryption", _re.compile(r"Decryption is not available|AAC.*wrapper|"
                               r"-1002|AUDIO-SESSION-KEY", _re.I)),
    ("region",     _re.compile(r"not available in your country|region|"
                               r"unavailable in|geo", _re.I)),
    ("no-flac",    _re.compile(r"desired bitrate|no\s+FLAC|not.*available.*bitrate", _re.I)),
    ("removed",    _re.compile(r"Resource not found|no longer available|"
                               r"removed|gone|404", _re.I)),
    ("unavailable",_re.compile(r"Unavailable|Failed to dl", _re.I)),
]


def _classify_partial_reason(log_text: str, permanent: bool) -> str:
    """Return a short canonical reason token for a partial download.

    Tokens: decryption | region | no-flac | removed | unavailable | postprocess.
    `permanent` is the runner's existing region/decryption signal — when nothing
    more specific matches we fall back to it so a known-permanent shortfall never
    reads as a transient post-processing glitch."""
    txt = log_text or ""
    for token, pat in _PARTIAL_REASON_PATTERNS:
        if pat.search(txt):
            return token
    return "region" if permanent else "postprocess"


# ── Per-track failure extractor (issue #5b, phase 1) ────────────────────────────
# EngineResult only carries COUNTS (tracks_ok/tracks_err), so to tell the user
# WHICH tracks fell short — not just how many — we scan the engine log for the
# per-track failure lines that the engines which emit them print. Best-effort and
# defensive: an engine with no known format simply yields nothing and the card
# falls back to the aggregate reason. Formats handled now:
#   • deemix (Deezer):      "[track_<id>_<br>] <Artist - Title> :: <reason>"
#   • SoundCloud runner:    "[N/M] Failed: <Title> - <reason>"
# (Apple zhaarey/amd/gamdl + streamrip to be added as their formats are sampled.)
_FAILED_TRACK_RE = [
    _re.compile(r"\[track_\d+_\d+\]\s*(?P<track>.+?)\s*::\s*(?P<reason>[^\r\n]*"
                r"(?:not found|desired bitrate|no alternative|unavailable|"
                r"removed|region|error|failed)[^\r\n]*)", _re.I),
    _re.compile(r"\[\d+\s*/\s*\d+\]\s*Failed:\s*(?P<track>[^\r\n]+?)\s*-\s*"
                r"(?P<reason>[^\r\n]+)", _re.I),
]
_MAX_FAILED_TRACKS = 25


def _extract_failed_tracks(log_text: str) -> list:
    """Best-effort [{track, reason}] for the tracks that failed, parsed from the
    engine log. `reason` is a canonical token (via _classify_partial_reason) so
    the bot/web can render it localised. Deduped by track name, capped. Returns
    [] when the engine has no recognised per-track format — caller then shows the
    aggregate reason instead."""
    out: list = []
    seen: set = set()
    for pat in _FAILED_TRACK_RE:
        for m in pat.finditer(log_text or ""):
            track = (m.group("track") or "").strip()
            if not track:
                continue
            key = track.lower()
            if key in seen:
                continue
            seen.add(key)
            reason_txt = (m.groupdict().get("reason") or "").strip()
            out.append({"track": track,
                        "reason": _classify_partial_reason(reason_txt, False)})
            if len(out) >= _MAX_FAILED_TRACKS:
                return out
    return out


async def _resolve_shortfall_detail(task: dict, reason: str):
    """#5b Phase 2: AUTHORITATIVE per-track shortfall report.

    Resolves the SOURCE release's canonical tracklist (from its own album API) and
    diffs it against the delivered files, then routes each missing track via the
    probe-gate (``release_diff.classify_missing``): cross-service top-up when the
    source can't give it in the requested form, same-service retry when the source
    HAS it and the run merely failed. Unlike ``_extract_failed_tracks`` (log-parse,
    empty for many engines) this works from the API regardless of log format.

    Best-effort: returns None on any failure so it never blocks finalize. Deezer
    first; Qobuz/Tidal/Apple resolvers are a follow-up (same shape).
    """
    from ripster import release_diff as _rd
    url = task.get("url", "") or ""
    svc = (task.get("service") or "").lower()
    canonical = None
    if "deezer.com" in url or svc == "deezer":
        canonical = await _rd.fetch_deezer_tracklist(url)
    if not canonical:
        return None
    missing = _rd.diff_tracklist(canonical, task.get("_files") or [])
    if not missing:
        return None
    cross, retry = _rd.classify_missing(missing, reason)
    return {
        "missing": [{"num": m.get("num"), "title": m.get("title")} for m in missing],
        "cross_service": [m.get("title") for m in cross],
        "retry_same": [m.get("title") for m in retry],
    }

# ── Path redactor ─────────────────────────────────────────────────────────────
_WIN_PATH_RE = _re.compile(r'[A-Za-z]:\\(?:[^\s"\'<>|*?\r\n\\]+\\)*[^\s"\'<>|*?\r\n\\]*')
_UNX_PATH_RE = _re.compile(r'/(?:home|root|Users)/[^\s"\'<>|*?\r\n]+')


def _strip_paths(msg: str) -> str:
    """Replace absolute paths with just the last filename component."""
    def _last(m: _re.Match) -> str:
        parts = m.group(0).rstrip('/\\').replace('/', '\\').split('\\')
        return next((p for p in reversed(parts) if p), '[path]')
    msg = _WIN_PATH_RE.sub(_last, msg)
    msg = _UNX_PATH_RE.sub(_last, msg)
    return msg

from ripster.task_state import (
    TaskStatus,
    advance     as _advance_task,
    try_advance as _try_advance_task,
)
from ripster.engines import get_engine
from ripster import i18n as _i18n
import ripster.amd as _amd_mod

# ── Injected by install() ────────────────────────────────────────────────────
_config: dict           = {}
_broadcast              = None
_queue: list            = []
_qs = None   # QueueManager — set by install()
_queue_snapshot         = None
_download_history: list = []
_save_history           = None
_detect_service         = None
_IS_WINDOWS: bool       = False
_BASE_DIR: Path         = Path(".")


def install(ctx) -> None:
    global _config, _broadcast, _queue, _qs, _queue_snapshot
    global _download_history, _save_history, _detect_service
    global _IS_WINDOWS, _BASE_DIR
    _config           = ctx.config
    _raw_bc           = ctx.broadcast
    async def _safe_bc(msg: dict) -> None:
        if msg.get("type") == "log":
            if "msg" in msg:
                msg = {**msg, "msg": _strip_paths(msg["msg"])}
            # i18n logs carry params the client interpolates into msg_key — strip
            # absolute paths from those too, else a full path leaks to the client.
            if isinstance(msg.get("params"), dict):
                msg = {**msg, "params": {
                    k: (_strip_paths(v) if isinstance(v, str) else v)
                    for k, v in msg["params"].items()}}
        await _raw_bc(msg)
    _broadcast        = _safe_bc
    _queue            = ctx.queue
    _qs               = ctx.queue_manager
    _queue_snapshot   = ctx.queue_snapshot
    _download_history = ctx.download_history
    _save_history     = ctx.save_history
    _detect_service   = ctx.detect_service
    _IS_WINDOWS       = ctx.is_windows
    _BASE_DIR         = ctx.base_dir
    # Point the download manifest at the app dir — the single source of truth
    # for "where did this task's files go" (read back by the serving endpoints).
    try:
        from ripster import download_manifest as _dm
        _dm.init(_BASE_DIR)
    except Exception:
        pass


async def _log(text: str, level: str = "info", task_id: str = "") -> None:
    ts = datetime.now().strftime("%H:%M:%S")
    msg: dict = {"type": "log", "text": f"[{ts}] {text}", "level": level}
    if task_id:
        msg["task_id"] = task_id
    await _broadcast(msg)


class _NeedAMDFallback(Exception):
    """Raised by _run_engine_task when gamdl/zhaarey hits -1002 and AMD is available."""


class _NeedZhaareyFallback(Exception):
    """AMD failed to get lossless content (codec unavailable) → retry with zhaarey AAC."""
    def __init__(self, quality: str = "aac"):
        self.quality = quality


class _NeedRetry(Exception):
    """Raised to signal a one-shot automatic retry (e.g. OrpheusDL new-settings)."""


# AMD per-track codec failure patterns (trigger zhaarey fallback)
_RE_AMD_CODEC_ERR = _re.compile(
    r'Lossless audio does not exist'
    r'|lossless.*unavailable'
    r'|no.*lossless.*stream',
    _re.I,
)
# AMD wrapper-quality IDs — only these trigger zhaarey fallback (AAC won't)
_AMD_WRAPPER_QUALS = frozenset({"alac", "atmos", "ac3", "aac-binaural", "aac-downmix"})


# ── History ──────────────────────────────────────────────────────────────────

# Audio files eligible for post-download transcode (any of these → the target).
_TRANSCODE_SRC_EXTS = {".flac", ".m4a", ".alac", ".wav", ".aac", ".aiff",
                       ".mp3", ".ogg", ".opus"}

# target → (output extension, ffmpeg audio-codec name, encode args)
_TRANSCODE_TARGETS = {
    "mp3":  (".mp3",  "mp3",  ["-map_metadata", "0", "-id3v2_version", "3",
                               "-codec:a", "libmp3lame", "-b:a", "320k"]),
    "flac": (".flac", "flac", ["-map", "0:a", "-map", "0:v?", "-map_metadata", "0",
                               "-c:v", "copy", "-codec:a", "flac",
                               "-compression_level", "8"]),
    "alac": (".m4a",  "alac", ["-map", "0:a", "-map", "0:v?", "-map_metadata", "0",
                               "-c:v", "copy", "-codec:a", "alac"]),
}


def _ffprobe_for(ffmpeg_path: str) -> str:
    """Derive the ffprobe path that sits next to the configured ffmpeg binary."""
    import re as _re
    return _re.sub(r"ffmpeg(\.exe)?$", lambda m: "ffprobe" + (m.group(1) or ""),
                   ffmpeg_path) if ffmpeg_path else "ffprobe"


def _probe_acodec(ffprobe: str, f) -> str:
    """Return the first audio stream's codec_name (lower), or '' on any failure."""
    import subprocess as _sp
    try:
        cp = _sp.run([ffprobe, "-v", "error", "-select_streams", "a:0",
                      "-show_entries", "stream=codec_name", "-of",
                      "default=nw=1:nk=1", str(f)],
                     capture_output=True, timeout=30,
                     creationflags=getattr(_sp, "CREATE_NO_WINDOW", 0))
        return (cp.stdout or b"").decode("utf-8", "ignore").strip().lower()
    except Exception:
        return ""


def _transcode_dir(save_dir: str, target: str = "mp3") -> int:
    """Optional, service-agnostic: re-encode every downloaded audio file in
    `save_dir` to a single uniform `target` format ("mp3" 320 / "flac" / "alac")
    via ffmpeg (metadata + cover carried over). Controlled by Settings key
    `transcode-format`. Files already in the target codec are skipped. Returns the
    count converted. Blocking — call via asyncio.to_thread. Originals removed
    unless `transcode-keep-original`."""
    import subprocess as _sp
    import os as _os
    from pathlib import Path as _P
    target = (target or "mp3").lower()
    if target not in _TRANSCODE_TARGETS:
        return 0
    out_ext, codec, enc = _TRANSCODE_TARGETS[target]
    ff = (_config.get("gamdl-ffmpeg-path") or "ffmpeg").strip() or "ffmpeg"
    fp = _ffprobe_for(ff)
    keep = bool(_config.get("transcode-keep-original"))
    n = 0
    try:
        for f in _P(save_dir).rglob("*"):
            if not f.is_file() or f.suffix.lower() not in _TRANSCODE_SRC_EXTS:
                continue
            # Already in the target codec? Extension is decisive except for .m4a,
            # which may hold either AAC or ALAC — probe to tell them apart.
            if f.suffix.lower() == out_ext:
                if out_ext != ".m4a" or _probe_acodec(fp, f) == codec:
                    continue
            out_final = f.with_suffix(out_ext)
            # Encode to a temp file first, then atomically move into place. This
            # also handles the ALAC-from-AAC case where source and output share
            # the .m4a extension (out_final == f) by overwriting in one step.
            tmp = f.with_name(f".__tc__{f.stem}{out_ext}")
            try:
                cp = _sp.run([ff, "-y", "-i", str(f), *enc, str(tmp)],
                             capture_output=True, timeout=600,
                             creationflags=getattr(_sp, "CREATE_NO_WINDOW", 0))
                if cp.returncode == 0 and tmp.exists() and tmp.stat().st_size > 0:
                    _os.replace(str(tmp), str(out_final))   # atomic; may overwrite f
                    n += 1
                    if out_final != f and not keep:
                        try: f.unlink()
                        except Exception: pass
                else:
                    try: tmp.unlink()
                    except Exception: pass
                    print(f"[transcode] ffmpeg failed for {f.name}: "
                          f"{(cp.stderr or b'')[-200:]!r}", flush=True)
            except Exception as e:
                try: tmp.unlink()
                except Exception: pass
                print(f"[transcode] {f.name}: {type(e).__name__}: {e}", flush=True)
    except Exception as e:
        print(f"[transcode] scan error: {e}", flush=True)
    return n


def _transcode_dir_to_mp3(save_dir: str, bitrate: str = "320k") -> int:
    """Backward-compat shim — see `_transcode_dir`."""
    return _transcode_dir(save_dir, "mp3")


# ── Multi-disc → per-disc subfolders (universal, all services) ────────────────
_DISC_AUDIO_EXTS = {".m4a", ".mp3", ".flac", ".ogg", ".opus", ".aac", ".wav", ".aiff", ".alac"}
_RE_DISC_PREFIX  = _re.compile(r"^(\d+)-\d+")   # AMD names tracks "1-01 Title"


def _disc_number(f: "Path") -> int:
    """Best-effort disc number for a track: tag first (mutagen, format-agnostic
    via easy=True → 'discnumber' like '1' / '1/2'), then the AMD filename prefix
    'N-NN', else disc 1."""
    try:
        from mutagen import File as _MF
        mf = _MF(str(f), easy=True)
        if mf is not None:
            dn = mf.get("discnumber") or mf.get("disc")
            if dn:
                s = str(dn[0] if isinstance(dn, (list, tuple)) else dn)
                m = _re.match(r"\s*(\d+)", s)
                if m:
                    return int(m.group(1))
    except Exception:
        pass
    m = _RE_DISC_PREFIX.match(f.name)
    return int(m.group(1)) if m else 1


def _organize_discs(save_dir: str) -> int:
    """If a release spans multiple discs, move each track into a `CD N/` subfolder
    — universally, for every service. Reads the disc number from tags (or the AMD
    'N-NN' filename prefix). Operates ONLY on audio sitting directly in the album
    root: if an engine (deemix/streamrip) already split into CD/Disc subfolders,
    the root has no loose audio → this is a no-op (no double-nesting). A matching
    `.lrc` follows its track; cover/.cue stay at the album root. Returns moved
    count. Blocking — call via asyncio.to_thread."""
    try:
        root = Path(save_dir)
        if not root.is_dir():
            return 0
        files = [f for f in root.iterdir()
                 if f.is_file() and f.suffix.lower() in _DISC_AUDIO_EXTS]
        if len(files) < 2:
            return 0
        discs = {f: _disc_number(f) for f in files}
        distinct = sorted(set(discs.values()))
        # Genuinely multi-disc only: ≥2 distinct disc numbers present.
        if len(distinct) < 2:
            return 0
        moved = 0
        for f, dn in discs.items():
            sub = root / f"CD {dn}"
            try:
                sub.mkdir(parents=True, exist_ok=True)
                target = sub / f.name
                if target.resolve() == f.resolve():
                    continue
                if target.exists():
                    # A CD-folder copy is already there (left by an earlier run /
                    # the engine's own disc split). Don't strand a duplicate in the
                    # album root: drop the loose root copy when it's identical
                    # (same size); otherwise leave it untouched to be safe.
                    try:
                        if target.stat().st_size == f.stat().st_size:
                            f.unlink()
                            moved += 1
                    except Exception:
                        pass
                    continue
                f.rename(target)
                moved += 1
                # carry the sidecar lyrics file with its track
                lrc = f.with_suffix(".lrc")
                if lrc.is_file():
                    try:
                        lrc.rename(sub / lrc.name)
                    except Exception:
                        pass
            except Exception:
                pass
        return moved
    except Exception as e:
        print(f"[discs] organize error: {e}", flush=True)
        return 0


async def _maybe_auto_mix(task: dict, tid: str) -> None:
    """If enabled (`coder-auto`) and the release is a "(DJ Mix)", merge its tracks
    into one continuous file + CUE via Ripster Coder — but only from a LOSSLESS
    source (gapless requires it). Output goes to the `mixed/` folder."""
    if not _config.get("coder-auto"):
        return
    import re as _re
    meta  = task.get("meta") or {}
    album = meta.get("album") or meta.get("title") or ""
    # Continuous-mix markers in the album title: DJ Mix / Mixed / Continuous /
    # DJ Set / Megamix / Mixed-Selected-Compiled by … / Live from|at|in ….
    if not _re.search(
        r"\b(?:dj[ \-]?mix|continuous(?:\s+mix)?|mixed(?:\s+by)?|dj\s*set|megamix|"
        r"(?:selected|compiled|presented)\s+by|live\s+(?:from|at|in)\b)",
        album, _re.I):
        return
    d = task.get("_save_dir")
    if not d:
        return
    from pathlib import Path as _P
    from ripster.routes.download import _find_audio_files as _faf
    files = _faf(_P(d))
    if len(files) < 2:
        return
    from ripster.mixcue import (build_mixes, clean_mix_name, clean_mix_title,
                                _probe, _ffprobe_for, _LOSSLESS)
    ff = (_config.get("gamdl-ffmpeg-path") or "ffmpeg").strip() or "ffmpeg"
    if _probe(_ffprobe_for(ff), files[0]).get("codec", "") not in _LOSSLESS:
        await _broadcast(_i18n.log_event("console.automix_skipped_lossy", level="warn", task_id=tid))
        return
    artist  = meta.get("artist") or ""
    fmt     = (_config.get("coder-auto-format") or "source").strip().lower()
    out_dir = (_config.get("coder-out-dir") or "").strip() or str(_P(_config.get("save-path", "downloads")) / "mixed")
    name    = clean_mix_name(album, artist)
    await _broadcast(_i18n.log_event("console.automix_start", level="info", task_id=tid, name=name))
    res = await asyncio.to_thread(build_mixes, [str(f) for f in files], out_dir, name,
                                  fmt, ff, clean_mix_title(album, artist), artist)
    made = [m for m in res.get("mixes", []) if m.get("ok")]
    if made:
        names = ", ".join(_P(m["file"]).name for m in made)
        if res.get("multi"):
            await _broadcast(_i18n.log_event("console.automix_done_discs", level="success",
                                             task_id=tid, discs=res["discs"], names=names, out_dir=out_dir))
        else:
            await _broadcast(_i18n.log_event("console.automix_done", level="success",
                                             task_id=tid, names=names, out_dir=out_dir))
    else:
        err = next((m.get("error") for m in res.get("mixes", []) if m.get("error")), res.get("error", ""))
        await _broadcast(_i18n.log_event("console.automix_error", level="error", task_id=tid, err=err))


def _add_to_history(task: dict) -> None:
    """Record a finished (or failed) task in history.json and notify clients."""
    meta   = task.get("meta") or {}
    status = task.get("status", "done")
    if status not in ("done", "error", "cancelled"):
        return
    entry = {
        "id":        task.get("id", ""),
        "url":       task.get("url", ""),
        "quality":   task.get("quality", ""),
        "engine":    task.get("engine", _config.get("engine", "zhaarey")),
        "service":   task.get("service") or (_detect_service(task.get("url", "")) if _detect_service else ""),
        "title":     meta.get("title",      "") or "",
        "artist":    meta.get("artist",     "") or "",
        "album":     meta.get("album",      "") or "",
        "artworkUrl": meta.get("artworkUrl", "") or "",
        "tracks":    meta.get("trackCount") or meta.get("totalTracks") or 0,
        "status":    status,
        "partial":   bool(task.get("_partial")),          # got fewer tracks than expected
        "missing":   int(task.get("_missing") or 0),      # how many tracks didn't land
        "got":       len(task.get("_files") or []) or None,
        "error":     (task.get("error") or "")[:2000],   # captured engine error
        "progress":  task.get("progress", 0),
        "ts":        datetime.now().isoformat(timespec="seconds"),
        # Kept for /api/download-file to find output dir after queue is cleared
        "_base_save_path": task.get("_base_save_path") or _config.get("save-path", "downloads"),
        "_start_time":     task.get("_start_time", 0.0),
        "_done_time":      task.get("_done_time",  0.0),
        "_save_dir":       task.get("_save_dir", ""),
        # Preserved so guests can re-download their own tasks after server restart
        "session_id":      task.get("session_id", ""),
        "_guest_token":    task.get("_guest_token", ""),
        # Download event counters (incremented by download.py on each successful action)
        "_dl_file":        task.get("_dl_file",   0),
        "_dl_zip":         task.get("_dl_zip",    0),
        "_dl_gofile":      task.get("_dl_gofile", 0),
        "_dl_errors":      task.get("_dl_errors", 0),
    }
    _download_history[:] = [h for h in _download_history if h.get("id") != entry["id"]]
    _download_history.insert(0, entry)
    del _download_history[500:]
    _save_history(_download_history)
    print(
        f"[history] recorded {entry['status']:<6} {entry['service']:<8} "
        f"{entry.get('title') or entry['url']}"
        + (f"  ERR: {entry['error'][:120]}" if entry.get("error") else ""),
        flush=True,
    )
    # Native desktop toast on finish (Windows) — only when the owner enabled it in
    # Settings. Lets the user know a download is done while Ripster is minimized/tray;
    # Focus Assist auto-suppresses it over fullscreen games.
    if status in ("done", "error") and _config.get("notify-on-done"):
        try:
            from ripster import notify as _notify
            _notify.toast_download_done(
                entry.get("title") or entry.get("url") or "Ripster",
                status == "done", entry.get("got"))
        except Exception:
            pass
    # Persistent error trail — so a failed download leaves a diagnosable record
    # (full error + tail of the engine output) even after history rotates. Log not
    # just hard "error" status but ALSO partials and any task carrying an error
    # message / partial flag: a CKC-salvaged Apple task records status "done" with
    # partial=True, so the old `status=="error"` gate missed real failures entirely
    # (nothing in errors.log to diagnose — e.g. the "Invalid CKC" wrapper failures).
    # The log tail still carries the engine's failure lines.
    _has_problem = (status in ("error", "partial")
                    or bool(task.get("error")) or bool(task.get("partial")))
    if _has_problem:
        try:
            from pathlib import Path as _P
            logf = _P(__file__).resolve().parent.parent / "errors.log"
            tail = "\n".join(str(x) for x in (task.get("log") or [])[-30:])
            with open(logf, "a", encoding="utf-8") as f:
                f.write(f"\n===== {entry['ts']} | {status.upper()} | {entry['service']} | "
                        f"{entry.get('title') or entry['url']} =====\n")
                f.write(f"url:     {entry['url']}\n")
                f.write(f"engine:  {entry['engine']}   quality: {entry['quality']}\n")
                f.write(f"error:   {task.get('error') or '(none captured — see output below)'}\n")
                f.write(f"--- last engine output ---\n{tail}\n")
        except Exception:
            pass
    try:
        loop = asyncio.get_running_loop()
        loop.create_task(_broadcast({"type": "history_updated"}))
    except RuntimeError:
        pass

    # Stats collector
    try:
        from ripster import stats_collector as _sc
        _sc.record_download(entry)
    except Exception:
        pass

    # Guest activity tracking — record download and consume quota on completion
    try:
        sid = task.get("session_id", "")
        if sid:
            from ripster.guest_manager import get_manager as _get_gm
            gm = _get_gm()
            gm.log_activity(sid, {
                "url":     entry["url"],
                "title":   entry.get("title") or "",
                "service": entry.get("service", ""),
                "status":  entry["status"],
                "quality": entry.get("quality", ""),
            })
            # Idempotent per task object: a task that re-finalizes (retry-in-place,
            # re-delivery) must NOT charge the guest's quota twice. A genuinely new
            # download is a new task object → _quota_consumed unset → charged once.
            if entry["status"] == "done" and not task.get("_quota_consumed"):
                gm.consume_quota(sid)
                task["_quota_consumed"] = True
    except Exception:
        pass


# ── AMD runner ───────────────────────────────────────────────────────────────



# ── Tag → Filename post-processor ───────────────────────────────────────────

_RENAME_TEMPLATE = "{tracknumber:02d}. {artist} - {title}"


async def _sc_drm_fallback(orig_task: dict) -> None:
    """SC track/playlist failed because Lucida can't decrypt FairPlay HLS.
    Pull title+artist (+duration) for each source track and look it up on
    Deezer → Qobuz → Apple by name+duration. If a match within ±3 s lands,
    queue it as a fresh task on the matched service. Best-effort; logs only.

    Triggered by runner when an SC task fails with a DRM-shaped error.
    """
    import re as _r
    import httpx as _httpx
    tid = orig_task.get("id", "")
    src_url = orig_task.get("url", "").strip()
    if not src_url:
        return
    await _log("🔁 SC: пробую найти трек(и) на других сервисах…", "info", tid)

    # Resolve SC URL → list of {title, artist, duration_s}
    async def _resolve_sc_tracks() -> list[dict]:
        try:
            from ripster.routes.soundcloud import _get_client_id, _API, _UA
        except Exception:
            return []
        cid = await _get_client_id()
        if not cid:
            return []
        oauth = (_config.get("soundcloud-oauth-token") or "").strip()
        headers = {"User-Agent": _UA}
        if oauth:
            headers["Authorization"] = (oauth if oauth.lower().startswith("oauth ")
                                        else f"OAuth {oauth}")
        # Extract numeric ID (best-effort) or hit the /resolve endpoint
        async with _httpx.AsyncClient(timeout=_httpx.Timeout(connect=4, read=6, write=6, pool=6),
                                      headers=headers) as c:
            r = await c.get(f"{_API}/resolve",
                            params={"url": src_url, "client_id": cid})
            if r.status_code != 200:
                return []
            data = r.json() or {}
            kind = data.get("kind")
            if kind == "track":
                return [{
                    "title":    data.get("title", ""),
                    "artist":   (data.get("user") or {}).get("username", ""),
                    "duration": round((data.get("duration") or 0) / 1000),
                }]
            if kind in ("playlist", "system-playlist"):
                out: list[dict] = []
                for t in (data.get("tracks") or []):
                    out.append({
                        "title":    t.get("title", "") or "",
                        "artist":   (t.get("user") or {}).get("username", ""),
                        "duration": round((t.get("duration") or 0) / 1000),
                    })
                return [t for t in out if t["title"]]
        return []

    # Search a service by title + artist, return the best match URL or "".
    async def _find_on(service: str, title: str, artist: str, dur_s: int) -> dict | None:
        try:
            from ripster.engines import get_engine
            eng = get_engine({"qobuz":"qobuz","deezer":"deezer","apple":"zhaarey",
                              "tidal":"tidal"}[service])
            q = f"{artist} {title}".strip()
            hits = await eng.search(q, "track", 5, _config) or []
        except Exception:
            return None
        if not hits:
            return None
        # Normalise title — drop "(Remix)" / "[Mix]" suffixes for matching
        def _norm(s: str) -> str:
            s = (s or "").lower()
            s = _r.sub(r'[\(\[].*?[\)\]]', '', s)
            s = _r.sub(r'[^a-z0-9 ]+', ' ', s)
            return _r.sub(r'\s+', ' ', s).strip()
        want = _norm(title)
        for h in hits:
            if _norm(h.get("title","")) != want:
                continue
            # duration check when both sides know it
            hd = h.get("duration") or 0
            if dur_s and hd and abs(hd - dur_s) > 3:
                continue
            return h
        # No strict match — return the top hit if title overlaps strongly
        for h in hits:
            if want and want.split()[0] in _norm(h.get("title","")):
                return h
        return hits[0]   # best-effort

    tracks = await _resolve_sc_tracks()
    if not tracks:
        await _log("🔁 SC: не удалось получить список треков для fallback", "warn", tid)
        return
    await _log(f"🔁 SC: ищу {len(tracks)} трек(ов) на Deezer/Qobuz/Apple…", "info", tid)

    # For each source track, search across services and queue best match.
    # Priority: Deezer (no auth headache) → Qobuz (HiRes if HiRes works) → Apple.
    queued = 0
    for src in tracks:
        for svc in ("deezer", "qobuz", "apple"):
            try:
                match = await _find_on(svc, src["title"], src["artist"], src["duration"])
            except Exception:
                match = None
            if not match:
                continue
            url = match.get("url") or ""
            if not url:
                continue
            try:
                from ripster.routes.queue import _make_task as _mk, _queue, _queue_snapshot
                from ripster.service_config import get_save_path as _gsp
                qual = {"deezer":"flac","qobuz":"7","apple":"alac"}[svc]
                engine = {"deezer":"deezer","qobuz":"qobuz","apple":"amd"}[svc]
                t = _mk(url, qual, engine, svc, "sc_fallback",
                        session_id=orig_task.get("session_id", ""))
                t["meta"] = {"title": match.get("title") or src["title"],
                             "artist": match.get("artist") or src["artist"],
                             "artworkUrl": match.get("cover") or "",
                             "_sc_fallback_origin": tid}
                t["_base_save_path"] = _gsp(_config, svc, qual)
                t["_sc_fallback_origin"] = tid
                _queue.append(t)
                # Tag the source task so the UI / download endpoint can show
                # "redirected to NEW_TASK_ID" instead of a confusing error.
                orig_meta = orig_task.setdefault("meta", {})
                fb_list = orig_meta.setdefault("fallback_targets", [])
                fb_list.append(t["id"])
                orig_meta["fallback_target"] = t["id"]   # last one wins (singular)
                orig_task["_fallback_target"] = t["id"]
                queued += 1
                await _broadcast({"type": "queue_update", "queue": _queue_snapshot()})
                await _broadcast({
                    "type": "sc_fallback_added",
                    "origin_task_id": tid,
                    "new_task_id":    t["id"],
                    "service":        svc,
                    "title":          t["meta"]["title"],
                    "artist":         t["meta"]["artist"],
                })
                await _log(f"  ✓ {src['title']} → {svc.upper()} ({match.get('title','')})",
                           "success", tid)
                break
            except Exception as e:
                await _log(f"  fallback queue error ({svc}): {e}", "warn", tid)
                continue
        else:
            await _log(f"  ✗ {src['title']} — не найдено", "warn", tid)
    if queued:
        await _log(f"🔁 SC fallback: {queued}/{len(tracks)} поставлено в очередь",
                   "success", tid)
        # Make sure the queue worker is running so the new tasks actually start.
        if not _qs.is_running:
            _qs.start()
            asyncio.create_task(process_queue())


def _apply_rename(task: dict, tid: str) -> None:
    """Rename audio files in the task's output directory so the filename matches
    the (corrected) tags — keeps names clean/ASCII across every engine."""
    from ripster.routes.download import _get_task_dir
    from ripster.tagger import rename_from_tags
    try:
        d = _get_task_dir(task)
        if not d:
            return
        renamed = rename_from_tags(d, _RENAME_TEMPLATE)
        if renamed:
            print(f"[tagger] renamed {len(renamed)} file(s) in {d}", flush=True)
    except Exception as e:
        print(f"[tagger] rename failed for task {tid}: {e}", flush=True)


async def _apply_apple_placeholder_fix(task: dict, tid: str) -> None:
    """Apple-only: a DJ mix / pre-release pulled by the LOCAL wrapper in the
    wrong storefront comes out as '00. AppleMusic' placeholders (empty tags).
    Fetch the real tracklist from amp-api by the URL's storefront and write
    proper tags BEFORE rename, so files don't get locked in as 'AppleMusic 01'.
    No-op for healthy releases and non-Apple sources."""
    url = task.get("url", "")
    if "music.apple.com" not in url:
        return
    from ripster.routes.download import _get_task_dir
    from ripster.tagger import retag_apple_placeholders
    try:
        d = _get_task_dir(task)
        if not d:
            return
        fixed = await retag_apple_placeholders(
            Path(d), url, _config, log=lambda m: print(f"[apple-retag] {m}", flush=True))
        if fixed:
            print(f"[apple-retag] task {tid}: fixed {fixed} placeholder file(s)", flush=True)
            await _broadcast(_i18n.log_event("console.mix_tags_fixed", level="success",
                                             task_id=tid, n=fixed))
    except Exception as e:
        print(f"[apple-retag] failed for task {tid}: {e}", flush=True)


async def _apply_retag(task: dict, tid: str) -> None:
    """Re-tag CJK-localised metadata via ISRC lookup (Deezer → Apple Music)."""
    from ripster.routes.download import _get_task_dir
    from ripster.tagger import retag_directory
    try:
        d = _get_task_dir(task)
        if not d:
            return
        summary = await retag_directory(
            d, _config, log=lambda m: print(f"[retag] {m}", flush=True),
        )
        if summary.get("retagged"):
            print(f"[retag] task {tid}: {summary}", flush=True)
            await _broadcast(_i18n.log_event("console.tags_fixed_isrc", level="success",
                                             task_id=tid, n=summary['retagged']))
    except Exception as e:
        print(f"[retag] failed for task {tid}: {e}", flush=True)


def _apply_fix_artists(task: dict, tid: str) -> None:
    """Fix slash-joined artist tags ('A / B') in all downloaded files."""
    from ripster.routes.download import _get_task_dir
    from ripster.tagger import fix_artist_tags
    try:
        d = _get_task_dir(task)
        if not d:
            return
        fixed = fix_artist_tags(d)
        if fixed:
            print(f"[tagger] fixed artist tags in {len(fixed)} file(s) in {d}", flush=True)
    except Exception as e:
        print(f"[tagger] fix_artists failed for task {tid}: {e}", flush=True)


def _apply_cover_to_folder(task: dict, tid: str) -> None:
    """Ensure a uniform folder cover.jpg across ALL services. Most engines embed
    art; some (Deezer/SC/etc.) don't drop a sidecar image. If the release dir has
    no cover.* yet, extract the embedded art from the first track and write a
    1200px cover.jpg — so every release looks the same regardless of source."""
    if not _config.get("save-cover-to-folder", True):
        return
    from ripster.routes.download import _get_task_dir, _find_audio_files
    try:
        d = _get_task_dir(task)
        if not d:
            # Fallback for engines whose dir the generic resolver misses (e.g.
            # orpheus_beatport sets no _save_dir/manifest entry): locate the release
            # folder from the newest audio file under the task's base save path.
            bsp = task.get("_base_save_path")
            try:
                if bsp and Path(bsp).is_dir():
                    auds = [p for p in Path(bsp).rglob("*")
                            if p.suffix.lower() in (".flac", ".m4a", ".mp3", ".alac", ".aac")]
                    if auds:
                        d = max(auds, key=lambda p: p.stat().st_mtime).parent
            except Exception:
                d = None
        if not d:
            print(f"[cover] no task dir for {tid} — skip", flush=True)
            return
        if any((d / n).exists() for n in ("cover.jpg", "cover.jpeg", "cover.png")):
            return   # engine already saved one — leave it
        from ripster.tagger import extract_embedded_cover
        raw = None
        for f in _find_audio_files(d):
            raw = extract_embedded_cover(f)
            if raw:
                break
        if not raw and (d / "folder.jpg").exists():
            raw = (d / "folder.jpg").read_bytes()
        if not raw:
            return
        out = d / "cover.jpg"
        try:
            from io import BytesIO
            from PIL import Image
            im = Image.open(BytesIO(raw)).convert("RGB")
            sz = int(str(_config.get("cover-size", "1200")).split("x")[0] or 1200)
            if max(im.size) > sz:
                im.thumbnail((sz, sz))
            im.save(str(out), "JPEG", quality=90)
        except Exception:
            out.write_bytes(raw)   # PIL missing/odd format → save the raw art as-is
        print(f"[cover] wrote folder cover.jpg in {d}", flush=True)
    except Exception as e:
        print(f"[cover] failed for task {tid}: {e}", flush=True)


# ── AMD pre-flight ───────────────────────────────────────────────────────────

async def _amd_preflight(task: dict, quality: str) -> bool:
    """Verify AMD install, patch headless mode, write config.toml, check wrapper-manager.
    Returns False if task should abort (status already set to ERROR)."""
    tid = task.get("id", "")
    amd_dir = _amd_mod.get_amd_dir()
    if not (amd_dir / "main.py").exists():
        await _log("✗ AppleMusicDecrypt не установлен. Перейди в Setup → Auto-install", "error", tid)
        _try_advance_task(task, TaskStatus.ERROR)
        return False

    runner_script = _BASE_DIR / "amd_runner.py"
    if not runner_script.exists():
        await _log("✗ amd_runner.py не найден — переустанови AMD через Settings", "error", tid)
        _try_advance_task(task, TaskStatus.ERROR)
        return False

    # ── Bento4 turnkey: ALAC/AAC decrypt shells out to mp4extract + mp4decrypt.
    # A fresh install never has them (the installer ships none) → every track dies
    # at the "Decrypting song…" step with a cryptic [WinError 2], in every region.
    # Auto-install the FULL Bento4 toolset ONCE here so a clean user can rip ALAC
    # without ever knowing about the Setup → Bento4 button. Search the dirs
    # amd_runner adds to PATH (<base>/tools, AMD dir) plus the live PATH.
    import shutil as _shutil
    _b4_dirs = [_BASE_DIR / "tools", amd_dir]
    def _have_bento4() -> bool:
        for _t in ("mp4decrypt", "mp4extract"):
            if any((d / f"{_t}.exe").exists() for d in _b4_dirs) or _shutil.which(_t):
                continue
            return False
        return True
    if not _have_bento4():
        await _log("📦 Bento4 (mp4extract/mp4decrypt) не найден — ставлю автоматически…", "info", tid)
        try:
            from ripster.setup import install_mp4decrypt_windows as _install_b4
            await _install_b4()
        except Exception as _be:
            await _log(f"⚠ Авто-установка Bento4 не удалась: {_be}", "warn", tid)
        if not _have_bento4():
            await _log("✗ Bento4 не установился — ALAC/AAC-декрипт невозможен. "
                       "Открой Setup → «Bento4 (mp4decrypt)» и установи вручную "
                       "(или проверь интернет/файрвол).", "error", tid)
            _try_advance_task(task, TaskStatus.ERROR)
            return False
        await _log("✓ Bento4 установлен — продолжаю.", "success", tid)

    from ripster.engines.amd import _CODEC_MAP as _AMD_CODEC_MAP
    codec = _AMD_CODEC_MAP.get(quality, "alac")
    await _amd_mod.patch_amd_for_headless(amd_dir)
    _amd_mod.write_amd_config(amd_dir, codec=codec)
    await _log(f"📄 config.toml записан в {amd_dir} (codec={codec})", "info", tid)

    instance = _config.get("amd-instance-url", "wm.wol.moe")
    secure   = _config.get("amd-instance-secure", True)
    await _log(f"🌐 Проверяю wrapper-manager: {instance}…", "info", tid)
    wm = await _amd_mod.amd_wrapper_status(instance, secure)
    if wm.get("error"):
        await _log(f"✗ Wrapper-manager недоступен: {wm['error']}", "error", tid)
        _try_advance_task(task, TaskStatus.ERROR)
        return False
    if not wm.get("ready"):
        await _log(
            f"⚠ Wrapper-manager «{instance}» ready=False "
            f"(клиентов: {wm.get('client_count', 0)}, "
            f"регионов: {len(wm.get('regions', []))}) — продолжаю",
            "warn", tid,
        )
    else:
        regions_str = ", ".join(wm.get("regions", []))
        await _log(f"✓ Wrapper-manager готов — регионы: {regions_str}", "success", tid)

    await _log(f"▶ AMD v2 [{codec.upper()}] — {task.get('url', '')}", "info", tid)
    return True


async def _soundcloud_preflight(task: dict) -> bool:
    """Turnkey SoundCloud — auto-provision Node 20 + the built Lucida engine.

    A fresh install ships runner.mjs but NOT the compiled Lucida (lucida-src/build)
    and may have only an old system Node. Node 18's undici breaks every SC fetch
    with 'terminated' (proven live on the tester box; Node 20 fixes it). Rather
    than make the user hunt for a Setup button, provision both ONCE here so a
    clean install rips SoundCloud out of the box. Returns False (task → ERROR)
    only when provisioning genuinely fails."""
    tid = task.get("id", "")
    from ripster.engines import soundcloud as _sc_eng
    from ripster import setup as _setup_mod

    # 1) Node ≥ 20 — install_node_windows() now enforces the minimum and drops a
    #    portable v20 beside a stale system Node, prepending it to PATH so the
    #    runner.mjs child process picks it up.
    node_exe = _setup_mod.tool_path("node")
    if node_exe is None or _setup_mod._node_version(node_exe) < _setup_mod._MIN_NODE_MAJOR:
        await _log("📦 SoundCloud: ставлю Node.js 20 (старый/отсутствующий Node ломает "
                   "Lucida с 'terminated')…", "info", tid)
        try:
            await _setup_mod.install_node_windows()
        except Exception as e:                                       # noqa: BLE001
            await _log(f"⚠ Авто-установка Node не удалась: {e}", "warn", tid)

    # 2) Lucida built? (runner.mjs + lucida-src/build/index.js)
    if not _sc_eng.is_installed():
        await _log("📦 SoundCloud: устанавливаю движок Lucida (один раз, ~1–2 мин)…",
                   "info", tid)
        try:
            from ripster.routes.setup import _install_soundcloud_component
            await _install_soundcloud_component()
        except Exception as e:                                       # noqa: BLE001
            await _log(f"⚠ Авто-установка Lucida не удалась: {e}", "warn", tid)

    if not _sc_eng.is_installed():
        await _log("✗ SoundCloud-движок (Lucida) не установлен. Открой Setup → SoundCloud "
                   "и установи вручную (нужны интернет + git).", "error", tid)
        _try_advance_task(task, TaskStatus.ERROR)
        return False
    if not _sc_eng.node_available():
        await _log("✗ Node.js не найден для SoundCloud — установка не удалась.", "error", tid)
        _try_advance_task(task, TaskStatus.ERROR)
        return False
    return True


_RE_BBC_PID = _re.compile(r'/sounds/play/([a-zA-Z0-9]+)')


async def _bbc_preflight(task: dict, page_url: str) -> "str | None":
    """Resolve a BBC Sounds page URL to a fresh, playable HLS stream URL —
    MediaSelector tokens are short-lived, so this MUST run right before the
    download, not at enqueue time (a task can sit queued behind others for a
    while). Also stashes title/artist/pid onto `task` so build_cmd (which only
    gets url/quality/config) can name the output file — same pattern as the
    `_sc_cover_override` stash used for SoundCloud's cover picker.

    Returns the HLS URL on success, or None (task → ERROR, already logged)."""
    tid = task.get("id", "")
    m = _RE_BBC_PID.search(page_url or "")
    if not m:
        await _log(f"✗ BBC: не удалось разобрать pid из ссылки: {page_url}", "error", tid)
        _try_advance_task(task, TaskStatus.ERROR)
        return None
    pid = m.group(1)
    import httpx as _httpx
    try:
        async with _httpx.AsyncClient(timeout=20) as c:
            pr = await c.get(f"https://www.bbc.co.uk/programmes/{pid}.json")
            if pr.status_code != 200:
                await _log(f"✗ BBC: programmes API {pr.status_code} для {pid}", "error", tid)
                _try_advance_task(task, TaskStatus.ERROR)
                return None
            prog = (pr.json() or {}).get("programme") or {}
            versions = prog.get("versions") or []
            if not versions:
                await _log(f"✗ BBC: нет доступных версий для {pid}", "error", tid)
                _try_advance_task(task, TaskStatus.ERROR)
                return None
            vpid     = versions[0]["pid"]
            duration = int(versions[0].get("duration") or 0)
            title = prog.get("title") or (prog.get("display_title") or {}).get("title") or pid
            artist = (((prog.get("parent") or {}).get("programme") or {}).get("title")
                      or ((prog.get("ownership") or {}).get("service") or {}).get("title")
                      or "BBC Radio")

            msel = (f"https://open.live.bbc.co.uk/mediaselector/6/select/version/2.0/"
                    f"mediaset/iptv-all/vpid/{vpid}/format/json")
            mr = await c.get(msel)
            if mr.status_code != 200:
                await _log(f"✗ BBC: MediaSelector {mr.status_code} для {vpid}", "error", tid)
                _try_advance_task(task, TaskStatus.ERROR)
                return None
            best_cf = best_ak = None
            for media in (mr.json() or {}).get("media", []):
                for conn in media.get("connection", []):
                    href = conn.get("href", "")
                    if conn.get("protocol") != "https" or ".m3u8" not in href:
                        continue
                    sup = conn.get("supplier", "")
                    if "cloudfront" in sup and not best_cf:
                        best_cf = href
                    elif "akamai" in sup and not best_ak:
                        best_ak = href
            hls = best_cf or best_ak
            if not hls:
                await _log(f"✗ BBC: не нашёл HLS-поток для {vpid}", "error", tid)
                _try_advance_task(task, TaskStatus.ERROR)
                return None
    except Exception as e:
        await _log(f"✗ BBC: ошибка резолва потока: {e}", "error", tid)
        _try_advance_task(task, TaskStatus.ERROR)
        return None

    task["_bbc_title"]    = title
    task["_bbc_artist"]   = artist
    task["_bbc_pid"]      = pid
    task["_bbc_duration"] = duration
    return hls


# Quality folders produced ONLY by Apple's Go downloaders (zhaarey/AMD). These are
# unambiguous — no other service emits them — so a bare-root copy can be re-homed
# under apple/ without guessing the source service. (AAC 256 is intentionally NOT
# here: SoundCloud-hq and Tidal-high also map to it.)
_APPLE_EXCLUSIVE_QFOLDERS = {"ALAC (Lossless)", "Atmos", "Binaural", "AAC Downmix", "Downmix"}


def _merge_dir(src: Path, dst: Path) -> None:
    """Move every child of ``src`` into ``dst``, recursing on directory collisions.
    The destination copy is authoritative and never overwritten; a file that
    already exists in ``dst`` is a redundant duplicate of the same release track, so
    the SOURCE copy is dropped (otherwise the emptied bare-root dir could never be
    removed and the very debris we're cleaning would persist). Emptied source dirs
    are removed."""
    import shutil
    dst.mkdir(parents=True, exist_ok=True)
    for child in sorted(src.iterdir()):
        target = dst / child.name
        if not target.exists():
            shutil.move(str(child), str(target))
        elif child.is_dir() and target.is_dir():
            _merge_dir(child, target)
        else:
            # File already present in dest (authoritative) → drop the redundant
            # source copy so the bare-root dir can be fully cleaned up.
            try:
                child.unlink()
            except OSError:
                pass
    try:
        src.rmdir()
    except OSError:
        pass


def _reclaim_bare_apple(config: dict) -> list[str]:
    """Safety net: re-home Apple-exclusive quality folders that landed at the bare
    downloads root into ``<base>/apple/<quality>/<artist>/<album>``.

    zhaarey/AMD write ``<base>/<quality>/…`` and the runner normally relocates each
    task's dir immediately — but when the engine log didn't yield a parseable
    output dir (``extract_save_dir`` → None) the per-task relocate never ran and the
    release was orphaned at the root (on disk but invisible to the manifest/bot =
    "downloaded, delivered nothing"). This sweep runs after every Apple task and
    catches any such stragglers. Apple-exclusive folders only → attribution is
    unambiguous. Returns the canonical dirs it moved."""
    moved: list[str] = []
    try:
        import shutil
        base = Path(config.get("save-path") or "downloads")
        if not base.is_dir():
            return moved
        for qname in _APPLE_EXCLUSIVE_QFOLDERS:
            bare = base / qname
            if not bare.is_dir():
                continue
            canon_q = base / "apple" / qname
            for artist in sorted(p for p in bare.iterdir() if p.is_dir()):
                for album in sorted(p for p in artist.iterdir() if p.is_dir()):
                    dest = canon_q / artist.name / album.name
                    if dest.resolve() == album.resolve():
                        continue
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    if dest.exists():
                        _merge_dir(album, dest)
                    else:
                        shutil.move(str(album), str(dest))
                    moved.append(str(dest))
                    print(f"[reclaim] {album} -> {dest}", flush=True)
            # Tidy now-empty bare <quality>/ (and its emptied artist dirs).
            try:
                for artist in [p for p in bare.iterdir() if p.is_dir()]:
                    try: artist.rmdir()
                    except OSError: pass
                bare.rmdir()
            except OSError:
                pass
    except Exception as e:                            # never let cleanup break a task
        print(f"[reclaim] skipped: {e}", flush=True)
    return moved


def _filter_engine_files(audio: list, engine_files) -> list:
    """Restrict the manifest's file list to the ones THIS task actually saved.

    Issue #19: two parallel Apple tasks can share an output dir, so the manifest
    must not be a blind directory glob — the engine reports the exact files it
    wrote, and we keep only those (matched by stem).

    BUT only when the stem-filter accounts for EVERY engine-reported file. Post-
    download retag/rename (Apple placeholder fix '01.'→'1.', CJK re-tag, the
    {artist} - {title} template) changes the on-disk names AFTER the engine log
    was captured, so a stale stem-filter would wrongly DROP real files (the
    manifest-records-2-of-3 bug). A partial match = names changed → trust the full
    glob instead: by this point the dir is already relocated to the task's own
    per-album folder, so the glob can't leak a parallel task's files anyway.
    """
    if not engine_files:
        return audio
    from pathlib import Path as _P
    stems = {_P(n).stem for n in engine_files if n}
    if not stems:
        return audio
    filtered = [f for f in audio if f.stem in stems]
    # `>= len(stems)` means every engine file was found under its original name →
    # the filter is valid (no rename happened). Fewer → names were rewritten → the
    # filter is stale; keep the full glob so renamed tracks aren't lost.
    if filtered and len(filtered) >= len(stems):
        return filtered
    return audio


def _relocate_to_service_folder(save_dir: str, service: str, quality: str, config: dict) -> str:
    """Keep the downloads tree consistent: every release should live under
    ``<base>/<service>/<quality>/…``. Most engines already do (the runner shadows
    their save-path keys), but Apple's Go downloaders (zhaarey/AMD) write
    ``<base>/<quality>/<artist>/<album>`` directly, skipping the ``<service>/``
    segment — which leaves the downloads folder a mix of ``<base>/<service>/<quality>``
    and bare ``<base>/<quality>`` folders. When (and ONLY when) the output landed at
    ``<base>/<canonical-quality>/…`` we re-home it under the service folder. Same-drive
    move = a fast rename. Conservative: any other shape is left untouched. Returns the
    (possibly new) directory."""
    try:
        import shutil
        from ripster.service_config import get_save_path
        sd = Path(save_dir)
        if not sd.is_dir():
            return save_dir
        canon = Path(get_save_path(config, service, quality))     # <base>/<service>/<quality>
        sd_r, canon_r = sd.resolve(), canon.resolve()
        if sd_r == canon_r or canon_r in sd_r.parents:
            return save_dir                                       # already under canon
        base = Path(config.get("save-path") or "downloads").resolve()
        try:
            rel = sd_r.relative_to(base)                          # <quality>/<artist>/<album>
        except ValueError:
            return save_dir                                       # outside base — leave it
        parts = rel.parts
        # Only the "missing <service> segment" shape: first part == the canonical
        # quality folder. Anything else we leave untouched (no guessing).
        if not parts or parts[0] != canon.name:
            return save_dir
        sub  = Path(*parts[1:]) if len(parts) > 1 else None
        dest = (canon / sub) if sub else canon
        if dest.resolve() == sd_r:
            return save_dir
        dest.parent.mkdir(parents=True, exist_ok=True)
        if dest.exists():
            # The release is already under the service folder (re-download or a
            # parallel task) — MERGE the bare-root copy in rather than orphaning it
            # at the downloads root (the old code bailed here, which is exactly how
            # bare <base>/<quality>/ debris accumulated). Never overwrites files.
            _merge_dir(sd, dest)
        else:
            shutil.move(str(sd), str(dest))
        print(f"[relocate] {sd} -> {dest}", flush=True)
        # Tidy now-empty bare parents (artist/, then <quality>/) up to base. If a
        # parent still holds STRAY FILES — zhaarey writes an artist-level folder.jpg
        # next to the album dir — fold those into the canonical sibling so nothing
        # is orphaned at the bare root (this is the leftover `Artist/folder.jpg`
        # debris). Subdirs (a concurrent task's album) are left untouched: we stop
        # climbing the moment one remains.
        try:
            p  = sd.parent
            dp = dest.parent
            while p.resolve() != base and base in p.resolve().parents:
                try:
                    p.rmdir()
                except OSError:
                    try:
                        dp.mkdir(parents=True, exist_ok=True)
                        for f in list(p.iterdir()):
                            if f.is_file():
                                tgt = dp / f.name
                                if tgt.exists():
                                    f.unlink()
                                else:
                                    shutil.move(str(f), str(tgt))
                    except OSError:
                        pass
                    try:
                        p.rmdir()
                    except OSError:
                        break          # subdirs remain (concurrent album) → stop
                p  = p.parent
                dp = dp.parent
        except OSError:
            pass
        return str(dest)
    except Exception as _e:
        print(f"[relocate] skipped: {_e}", flush=True)
        return save_dir


# ── Engine-based runner ──────────────────────────────────────────────────────

async def _run_engine_task(task: dict, engine_name: str, url: str, quality: str) -> None:
    """Run task via a registered engine plugin (ProcessRunner)."""
    tid = task.get("id", "")
    try:
        eng = get_engine(engine_name)
    except KeyError:
        _try_advance_task(task, TaskStatus.ERROR)
        task["log"].append(f"Unknown engine: {engine_name}")
        _add_to_history(task)
        return

    if engine_name == "amd":
        # AMD writes a config.toml from the global _cfg in ripster.amd — that
        # path needs to point at the per-quality subfolder. Temporarily shadow
        # the three Apple sub-keys so write_amd_config sees the right base, then
        # restore them. (We can't pass a dict view through _amd_preflight without
        # changing its signature, so this monkey-poke is the minimal patch.)
        _base = task.get("_base_save_path") or ""
        _q = (quality or "").lower()
        _amd_overrides: dict[str, str] = {}
        if _base:
            if _q in ("atmos", "binaural", "downmix"):
                _amd_overrides = {"atmos-save-folder": _base, "atmos-path": _base}
            elif _q.startswith("aac"):
                _amd_overrides = {"aac-save-folder": _base, "aac-path": _base}
            else:
                _amd_overrides = {"alac-save-folder": _base}
        _saved = {k: _config.get(k) for k in _amd_overrides}
        _config.update(_amd_overrides)
        try:
            ok = await _amd_preflight(task, quality)
        finally:
            for k, v in _saved.items():
                if v is None: _config.pop(k, None)
                else:         _config[k] = v
        if not ok:
            _add_to_history(task)
            return

    if engine_name == "soundcloud":
        if not await _soundcloud_preflight(task):
            _add_to_history(task)
            return

    if engine_name == "bbc":
        resolved = await _bbc_preflight(task, url)
        if not resolved:
            _add_to_history(task)
            return
        url = resolved   # HLS stream URL — build_cmd never sees the page URL

    # ── Wrapper pool: Apple-local parallelism. Acquired below for zhaarey,
    # released in the finally. ANY failure here → default single-wrapper cwd, so
    # a download is NEVER broken by the pool. Defined out here so finally sees them.
    _pool = None
    _pool_slot = None
    _task_cwd = None

    try:
        # Build a per-task config view so the quality-subfolder is visible to
        # every engine without each one re-deriving it. _base_save_path already
        # includes the quality suffix (see run_task → get_save_path), so we
        # just shadow the global save-path AND the per-service path key.
        from ripster.service_config import _SVC_PATH_KEYS as _SVC_KEYS
        _cfg_view = dict(_config)
        _base = task.get("_base_save_path")
        if _base:
            _cfg_view["save-path"] = _base
            _svc_for_path = task.get("service") or ""
            _svc_key = _SVC_KEYS.get(_svc_for_path)
            if _svc_key:
                _cfg_view[_svc_key] = _base
            # Apple uses three sibling keys depending on the codec; rewrite the
            # one matching the chosen quality so engines pick it up.
            if _svc_for_path in ("apple", ""):
                _q = (quality or "").lower()
                if _q in ("atmos", "binaural", "downmix"):
                    _cfg_view["atmos-save-folder"] = _base
                    _cfg_view["atmos-path"]       = _base
                elif _q.startswith("aac"):
                    _cfg_view["aac-save-folder"]  = _base
                    _cfg_view["aac-path"]         = _base
                else:
                    _cfg_view["alac-save-folder"] = _base
        # Cover-source override picked in the SC mix drawer → hand it to the engine.
        _cov_override = (task.get("meta") or {}).get("coverUrl")
        if _cov_override and task.get("service") == "soundcloud":
            _cfg_view["_sc_cover_override"] = _cov_override
        # BBC: title/artist/pid resolved in _bbc_preflight (build_cmd needs them
        # to name the output file — it only gets url/quality/config, not task).
        if task.get("service") == "bbc":
            _cfg_view["_bbc_title"]    = task.get("_bbc_title", "")
            _cfg_view["_bbc_artist"]   = task.get("_bbc_artist", "")
            _cfg_view["_bbc_pid"]      = task.get("_bbc_pid", "")
            _cfg_view["_bbc_duration"] = task.get("_bbc_duration", 0)
        cmd = eng.build_cmd(url, quality, _cfg_view)
        task["log"].append(f"▶ {' '.join(cmd)}")
        await _broadcast(_i18n.log_event("console.cmd_start", level="info", task_id=tid, cmd=' '.join(cmd[:3])))

        from ripster.process_runner import ProcessRunner
        from ripster.engines.base import EventKind
        # SC and big-mix providers can pipe-stream + ffmpeg-transcode silently
        # for many minutes; default 300 s would kill long mixes. AMD/Qobuz often
        # have their own silent windows during tagging. SC handles its own
        # heartbeats in runner.mjs but we still leave headroom for slow CDNs.
        if engine_name == "amd":
            line_timeout = 600.0    # long silent mp4decrypt/tagging windows
        elif engine_name == "qobuz":
            # streamrip renders a rich/tqdm progress bar via \r (NO \n) for the whole
            # duration of a track transfer — same as OrpheusDL below. readline() blocks
            # on a newline, so the runner sees ZERO output while a big file downloads.
            # A long Hi-Res album (e.g. Yes — "Tales From Topographic Oceans", 4 tracks
            # ~20 min each, 24/192 ≈ 700 MB/track, all downloading concurrently) stays
            # newline-silent well past 300 s → the watchdog killed a LIVE download.
            # Give it the same 20-min headroom as Tidal/SC; each finished track prints
            # a newline that resets the watchdog, so a genuine hang still fails eventually.
            line_timeout = 1200.0
        elif engine_name == "soundcloud":
            line_timeout = 1200.0   # 20 min — heartbeats every 30 s anyway
        elif engine_name in ("tidal", "orpheus_spotify", "orpheus_beatport"):
            # OrpheusDL streams a tqdm progress bar via \r (NO \n) for the whole
            # duration of a track's DASH/segment download. readline() blocks on a
            # newline, so the runner sees zero output for that entire window — and
            # on a slow VPN one Tidal track can take minutes, blowing past the
            # default 300 s. The watchdog then kills OrpheusDL mid-download and
            # only cover.jpg lands. Give it SoundCloud-style headroom; each
            # finished track prints a newline that resets the watchdog.
            line_timeout = 1200.0
        elif engine_name == "bbc":
            # Same class of bug: yt-dlp's `--downloader ffmpeg` reports progress
            # via ffmpeg's own `-stats` line, which updates in place with \r (no
            # \n) for the ENTIRE download — a 2-hour Essential Mix can run silent
            # (by readline()'s definition) well past 300s. Give it the same
            # headroom as Tidal/SC; a real hang still fails eventually.
            line_timeout = 1200.0
        else:
            line_timeout = 300.0
        if engine_name == "amd":
            # AMD shells out to `mp4extract` (and friends) via `subprocess.run`
            # without a full path — those Bento4 utilities live next to the AMD
            # main.py. Prepend the AMD dir to PATH so CreateProcess can find them.
            _amd_dir_str = str(Path(_amd_mod.get_amd_dir()).resolve())
            extra_env = {
                "PYTHONPATH":               _amd_dir_str,
                "PYTHONIOENCODING":         "utf-8",
                "PYTHONLEGACYWINDOWSSTDIO": "0",
                "PATH":                     _amd_dir_str + os.pathsep + os.environ.get("PATH", ""),
            }
        elif engine_name == "orpheus_spotify":
            extra_env = {"PROTOCOL_BUFFERS_PYTHON_IMPLEMENTATION": "python", "PYTHONIOENCODING": "utf-8"}
        else:
            extra_env = {"PYTHONIOENCODING": "utf-8"}
        # Apple-local pool: give this zhaarey task its OWN wrapper container so
        # several Apple releases download in parallel. Slot 0 reuses the always-on
        # amd-wrapper, so a single download spins up nothing new and behaves
        # exactly as before. acquire() is blocking docker → off-thread. On ANY
        # failure we leave _task_cwd None → default cwd → the global wrapper.
        if engine_name == "zhaarey":
            try:
                from ripster import wrapper_pool as _wp
                _pool = _wp.get_pool(_config)
                if _pool is not None:
                    _acq = await asyncio.to_thread(_pool.acquire)
                    if _acq:
                        _slot, _dec_p, _m3u_p = _acq
                        _pool_slot = _slot
                        _base_dir = str(Path(__file__).resolve().parent.parent)
                        # apple-parallel-tracks: hand this album the WHOLE pool so
                        # the Go tool fans its tracks across every container (each
                        # concurrent track gets its own CKC session). zh_cap is
                        # forced to 1 below, so no other album competes for slots.
                        _dports = ""
                        if _config.get("apple-parallel-tracks"):
                            try:
                                _plist = await asyncio.to_thread(
                                    _wp.ensure_all_decrypt_ports, _config)
                                _dports = ",".join(_plist)
                                if _dports:
                                    task["log"].append(
                                        f"🦝 parallel tracks across pool: {_dports}")
                            except Exception:
                                _dports = ""
                        _task_cwd = await asyncio.to_thread(
                            _wp.slot_cwd, _slot, _dec_p, _m3u_p, _base_dir, _dports)
                        task["log"].append(f"🦝 wrapper-pool: slot {_slot} (decrypt {_dec_p}, m3u8 {_m3u_p})")
                        try:
                            await _broadcast({"type": "pool_update", "pool": _wp.live_status()})
                        except Exception:
                            pass
            except Exception as _pe:
                _pool = None
                _pool_slot = None
                _task_cwd = None
                print(f"[pool] acquire failed → single-wrapper fallback: {_pe}", flush=True)
        runner = ProcessRunner(
            cmd=cmd, engine=eng, line_timeout=line_timeout,
            env=extra_env, cwd=(_task_cwd if _task_cwd is not None else eng.working_dir()),
            # AMD's child (amd_runner.py) is itself an asyncio+gRPC app and exits
            # early when spawned via asyncio.create_subprocess_exec on Windows.
            # Run it through a blocking Popen + reader thread instead.
            use_thread=(engine_name == "amd"),
        )
        _qs.register_runner(tid, runner)
        _qs.proc = None

        if engine_name == "qobuz":
            msg = "⏳ Qobuz: скачиваю… (может занять до нескольких минут)"
            task["log"].append(msg)
            await _broadcast({"type": "log", "msg": msg, "level": "info", "task_id": tid})

        fatal_hit = False
        _fatal_amd_hint = False   # zhaarey FATAL said "переключись на AMD"
        async for ev in runner.run():
            if ev.kind is EventKind.FATAL:
                task["log"].append(ev.message)
                await _broadcast({"type": "log", "msg": f"✗ {ev.message}", "level": "error", "task_id": tid})
                if ev.message.startswith("ORPHEUS_NOT_AUTHED"):
                    await _broadcast({"type": "orpheus_not_authed"})
                elif engine_name == "zhaarey" and "AMD" in ev.message:
                    _fatal_amd_hint = True
                fatal_hit = True
                await runner.cancel()
                break

            if ev.kind is EventKind.PROGRESS:
                if ev.total and ev.total > 0:
                    task["progress"] = int((ev.current or 0) / ev.total * 100)
                    # Store engine's progress total separately — don't overwrite
                    # meta.trackCount which comes from the API and is the real
                    # track count (Go tool may report steps/segments, not tracks).
                    task["_prog_total"] = ev.total
                    await _broadcast({"type": "progress", "id": tid,
                                      "progress": task["progress"],
                                      "current":  ev.current or 0,
                                      "total":    ev.total})
                elif ev.total == 0 and ev.current:
                    # Track completion counter (StreamripMixin).
                    # Estimate % from meta.trackCount so the progress bar actually moves.
                    meta = task.get("meta") or {}
                    total_tracks = meta.get("trackCount") or meta.get("totalTracks") or 0
                    if total_tracks > 1:
                        task["progress"] = min(int(ev.current / total_tracks * 100), 99)
                    await _broadcast({"type": "progress", "id": tid,
                                      "progress": task.get("progress", 0),
                                      "current":  ev.current,
                                      "total":    0})
                continue

            if ev.message:
                task["log"].append(ev.message)
                _logd = {"type": "log", "msg": ev.message, "level": ev.level.value, "task_id": tid}
                # Engines may attach an i18n key+params via Event.extra so the
                # client can translate the line (msg stays the RU fallback).
                _mk = ev.extra.get("msg_key") if ev.extra else None
                if _mk:
                    _logd["msg_key"] = _mk
                    _logd["params"]  = ev.extra.get("params", {})
                await _broadcast(_logd)

        log_text = "\n".join(task.get("log", []))

        if fatal_hit:
            if _fatal_amd_hint and (_amd_mod.get_amd_dir() / "main.py").exists():
                raise _NeedAMDFallback()
            _try_advance_task(task, TaskStatus.ERROR)
            return

        if runner._cancelled:
            _try_advance_task(task, TaskStatus.CANCELLED)
            return

        rc        = runner.result.exit_code if runner.result else -1
        timed_out = runner.result.timed_out  if runner.result else False

        if timed_out and rc != 0:
            _try_advance_task(task, TaskStatus.ERROR)
            await _broadcast(_i18n.log_event("console.timeout", level="error", task_id=tid))
            return

        result = eng.is_finished(log_text, rc=rc)
        if result.quality_actual:
            task["quality"] = result.quality_actual

        # Cross-service error clarity (EVERY engine, one place): when a download
        # FAILED and the engine only gave a GENERIC message, scan the log for a
        # region-lock / phantom-removed-link cause and surface the REAL reason so
        # users understand WHY. Engine-specific reasons (auth/token/Premium/CKC/
        # wrapper) are NOT touched — only generic/empty errors get upgraded.
        if not result.success:
            _err = result.error or ""
            _GENERIC = ("завершился с кодом", "нет маркера", "не докачался",
                        "неожиданное", "unexpected", "0 треков", "ни один трек",
                        "нет вывода", "Exit code", "проверь")
            if (not _err) or any(g in _err for g in _GENERIC):
                from ripster.engines.errors import classify_download_error
                _cls = classify_download_error(log_text)
                if _cls:
                    _svc = (task.get("service") or "").capitalize()
                    result.error = f"{_svc}: {_cls[1]}" if _svc else _cls[1]
                else:
                    # Disk-truth salvage: an engine can exit non-zero AFTER its files
                    # already landed (e.g. OrpheusDL-Spotify crashes on a trailing
                    # metadata fetch while the .ogg tracks are on disk → bar at 66%
                    # then "Couldn't download the release"). With a GENERIC error and
                    # no real failure reason classified, dropping the release =
                    # "downloaded but delivered nothing". If FRESH audio files (mtime
                    # ≥ this run's start) sit in the output dir, flip to success so the
                    # normal path + silent-partial guard delivers what landed. Auth /
                    # region / premium failures save zero files, so they never trip
                    # this (and the mtime gate ignores any stale same-named folder).
                    try:
                        from ripster.routes.download import (_get_task_dir as _gtd_chk,
                                                             _find_audio_files as _faf_chk)
                        _dd = _gtd_chk(task)
                        if _dd:
                            _st = float(task.get("_start_time") or 0.0) - 5.0
                            _fresh = [f for f in _faf_chk(_dd) if f.stat().st_mtime >= _st]
                            if _fresh:
                                task.setdefault("log", []).append(
                                    f"[salvage] engine exited rc={rc} but {len(_fresh)} fresh "
                                    f"file(s) on disk → delivering as partial")
                                result.success    = True
                                result.tracks_ok  = len(_fresh)
                                result.tracks_err = 0
                                result.error      = ""
                    except Exception:
                        pass

        # AMD lossless-unavailable → auto-fallback to zhaarey AAC.
        # Trigger when: AMD was used, wrapper quality requested, codec errors present
        # in the log, not a connection failure, and we haven't already fallen back.
        if (
            engine_name == "amd"
            and quality in _AMD_WRAPPER_QUALS
            and not task.get("_amd_fallback")
            and not (result.error or "").startswith("wrapper-manager unreachable")
            and _RE_AMD_CODEC_ERR.search(log_text)
        ):
            raise _NeedZhaareyFallback("aac")

        if result.success:
            _try_advance_task(task, TaskStatus.DONE)
            task["progress"]   = 100
            task["_done_time"] = time.time()
            # Capture the exact output directory from engine JSON/log output
            # so /api/download-file can find the files without guessing.
            save_dir = eng.extract_save_dir(log_text)
            # Also capture the EXACT files this task saved (issue #19): two parallel
            # Apple tasks can share an output dir, so the manifest must not be built
            # by a blind directory glob. getattr → engines without it just fall back.
            try:
                _ef = getattr(eng, "extract_save_files", None)
                task["_engine_files"] = _ef(log_text) if _ef else None
            except Exception:
                task["_engine_files"] = None
            # Apple Go downloaders (zhaarey/AMD) write to a bare <base>/<quality>/
            # root; sweep any stragglers under apple/ so they're never orphaned at
            # the downloads root (orphaned = invisible to the manifest = "downloaded
            # but delivered nothing"). If the engine log gave no dir but the sweep
            # moved exactly ONE release, that's almost certainly THIS task's output
            # → adopt it so the marker/manifest still get written and delivery
            # happens. More than one moved → too ambiguous to attribute, leave it
            # (files are at least correctly placed now).
            # Only sweep the bare root as a FALLBACK when the engine gave no dir.
            # On the common path save_dir is known and `_relocate_to_service_folder`
            # below moves THIS task's own dir — safe under parallel zhaarey. A blind
            # sweep on every success could grab a concurrent task's in-progress dir.
            if (not save_dir) and (task.get("service") in ("apple", "")) and engine_name in ("zhaarey", "amd"):
                _reclaimed = _reclaim_bare_apple(_config)
                if len(_reclaimed) == 1:
                    save_dir = _reclaimed[0]
                    task["log"].append(f"[reclaim] adopted orphaned output: {save_dir}")
            if save_dir:
                # Engine-agnostic layout fix: re-home output that landed at a bare
                # <base>/<quality>/… (Apple's Go downloaders) under the canonical
                # <base>/<service>/<quality>/… so the downloads folder stays uniform.
                save_dir = _relocate_to_service_folder(save_dir, task.get("service") or "",
                                                       quality, _config)
                task["_save_dir"] = save_dir
                # Write marker file so download.py can find this dir by task ID
                try:
                    from ripster.task_marker import write_marker as _write_marker
                    _write_marker(Path(save_dir), tid, task)
                except Exception:
                    pass
                # Optional post-download transcode → one uniform output format
                # (Settings `transcode-format`, all services). The selector is
                # AUTHORITATIVE once present — an explicit "" means native/no-convert
                # and must override the legacy booleans (otherwise a stale
                # transcode-mp3=true would silently re-enable conversion). Legacy
                # booleans are only consulted when the new key was never set.
                if "transcode-format" in _config:
                    _tc_target = (_config.get("transcode-format") or "").strip().lower()
                else:
                    _tc_target = ("flac" if _config.get("transcode-flac")
                                  else "mp3" if _config.get("transcode-mp3") else "")
                if _tc_target in _TRANSCODE_TARGETS:
                    _tc_label = {"mp3": "MP3 320", "flac": "FLAC", "alac": "ALAC"}[_tc_target]
                    await _broadcast(_i18n.log_event("console.transcode_start", level="info",
                                                     task_id=tid, label=_tc_label))
                    n = await asyncio.to_thread(_transcode_dir, save_dir, _tc_target)
                    await _broadcast(_i18n.log_event("console.transcode_done", level="success",
                                                     task_id=tid, label=_tc_label, n=n))
            # Post-process: fix slash-joined artist tags (Deezer writes "A / B / C")
            _apply_fix_artists(task, tid)
            # Apple wrong-storefront placeholder fixer — MUST run before rename so
            # 'AppleMusic'-junk files get real catalog tags first (else rename locks
            # them in as 'AppleMusic 01'). No-op unless the files are actually broken.
            await _apply_apple_placeholder_fix(task, tid)
            # Post-process: re-tag CJK-localised metadata via ISRC lookup, then
            # rename every file so the filename matches the corrected tags.
            if _config.get("auto-retag", True):
                await _apply_retag(task, tid)
            # orpheus_spotify keeps OrpheusDL's OWN filenames: it skips already-
            # downloaded tracks by its own naming on auto-retry. Renaming them to
            # the {artist} - {title} template would hide those files from that
            # skip-check → re-download → byte-identical _2/_3 duplicates. So don't
            # rename native-Spotify output (the bot delivers by embedded tags
            # anyway, so the on-disk filename doesn't matter for quality/labels).
            if engine_name != "orpheus_spotify":
                _apply_rename(task, tid)
            # Uniform folder cover.jpg for every service (extract embedded art if
            # the engine didn't drop a sidecar). Before disc-split so it lands at
            # the album root, above any CD 1/CD 2 subfolders.
            _apply_cover_to_folder(task, tid)
            # Multi-disc → per-disc folders (CD 1/, CD 2/…), universal across all
            # services. No-op for single-disc releases and for engines that already
            # split into disc subfolders. Done before the manifest so it records
            # the final (moved) paths.
            if _config.get("disc-subfolders", True):
                try:
                    _sd = task.get("_save_dir")
                    if _sd:
                        _nd = await asyncio.to_thread(_organize_discs, _sd)
                        if _nd:
                            await _broadcast(_i18n.log_event("console.discs_organized", level="info",
                                                             task_id=tid, n=_nd))
                except Exception as _e:
                    print(f"[discs] {_e}", flush=True)
            # ── Record the download manifest (THE durable map: task → dir + files).
            # Done here, after all post-processing, so the file list is final. The
            # serving endpoints read this first and never have to guess the folder.
            try:
                from ripster import download_manifest as _dm
                from ripster.routes.download import _get_task_dir as _gtd, _find_audio_files as _faf
                final_dir = task.get("_save_dir")
                d = Path(final_dir) if final_dir else None
                if not (d and d.is_dir()):
                    d = _gtd(task)          # robust resolver at the freshest moment
                if d and d.is_dir():
                    task["_save_dir"] = str(d)
                    # Qobuz: streamrip tags only the primary `performer` → collabs
                    # land with ONE artist and no composer. Re-derive the FULL credits
                    # from the Qobuz API (per-track credits) and rewrite ARTIST +
                    # COMPOSER. Runs HERE, on the robustly-resolved delivered folder
                    # `d` — the earlier `if save_dir:` gate never fired for streamrip
                    # (its extract_save_dir returns empty), so the retag was silently
                    # skipped for every Qobuz download.
                    if task.get("service") == "qobuz":
                        try:
                            import re as _re_q
                            _u = task.get("url") or url or ""
                            _am = _re_q.search(r'/album/(?:[^/]+/)?([A-Za-z0-9]{6,})', _u)
                            if _am:
                                from ripster.engines.qobuz_retag import retag_qobuz_album
                                _rn = await retag_qobuz_album(_am.group(1), _cfg_view, str(d))
                                print(f"[qobuz-retag] {_am.group(1)} → {_rn} file(s) retagged", flush=True)
                                if _rn:
                                    task.setdefault("log", []).append(
                                        f"[qobuz] артисты/композиторы в тегах исправлены: {_rn} файл(ов)")
                            else:
                                print(f"[qobuz-retag] no album id in url {_u!r}", flush=True)
                        except Exception as _re_e:
                            import traceback as _tb
                            print(f"[qobuz-retag] FAILED: {_re_e}\n{_tb.format_exc()[:500]}", flush=True)
                    audio = _faf(d)
                    # Issue #19: if the engine reported the EXACT files it saved,
                    # keep only those so a shared output dir can't leak a parallel
                    # task's files. Rename-aware: a stale post-retag filter that
                    # would drop real files falls back to the full glob (see helper).
                    audio = _filter_engine_files(audio, task.get("_engine_files"))
                    task["_files"] = [f.name for f in audio]
                    _dm.record(tid, str(d), audio, task)
                    print(f"[manifest] {_dm.short_id(tid)} → {d.name} ({len(audio)} files)", flush=True)
                else:
                    print(f"[manifest] {_dm.short_id(tid)} — dir unresolved at completion", flush=True)
            except Exception as _e:
                print(f"[manifest] record skipped: {_e}", flush=True)
            # Silent-partial guard: the engine may report success while some tracks
            # never landed (e.g. Apple wrapper "Invalid CKC" on a few tracks but the
            # summary still says OK). Compare ACTUAL audio files on disk against the
            # release's expected track count and surface a shortfall honestly —
            # retry once in-place (skip-existing recovers transient failures), then
            # report a clear PARTIAL instead of a misleading green "done".
            _meta = task.get("meta") or {}
            try:
                _expected = int(_meta.get("trackCount") or _meta.get("totalTracks")
                                or _meta.get("tracks") or 0)
            except Exception:
                _expected = 0
            # Apple's catalog metadata trackCount can be INFLATED — e.g. a phantom
            # pre-order / region-locked / removed track that's still counted (the
            # Mujhse soundtrack shows "8 songs" in OG metadata but only 7 are in
            # the actual catalog tracklist). The engine prints an authoritative
            # "have N/M" where M is the real per-storefront tracklist it walked, so
            # trust that over the metadata to avoid a permanent false "7/8 partial".
            _m_have = _re.search(r"\bhave\s+(\d+)\s*/\s*(\d+)\b", log_text)
            if _m_have:
                _expected = int(_m_have.group(2))
            _got = len(task.get("_files") or [])
            _shortfall = _expected > 1 and 0 < _got < _expected
            # Some missing tracks are PERMANENTLY unavailable via this engine
            # (e.g. gamdl AAC can't decrypt certain tracks → "Decryption is not
            # available for media ID"; region-locked / removed tracks). Re-running
            # never recovers them — worse, the retry can hit a transient network
            # error on a healthy 4/6 and flip the whole task to "error". So when
            # the shortfall is permanent, skip the retry and report PARTIAL with
            # what we actually got (the user can grab the rest via the wrapper).
            _permanent_miss = bool(_re.search(
                r"Decryption is not available|not available in your country|"
                r"Resource not found|no longer available|region", log_text, _re.I))
            # Auto-complete a partial release ON ITS OWN — keep re-running (engines
            # skip already-downloaded tracks) until it's whole or we stop making
            # progress, so the user never has to press "повторить" 4 times. Bail
            # out early when a pass adds NO new tracks (the rest is permanently
            # missing) or the shortfall is a known-permanent one.
            _MAX_PARTIAL_RETRY = 4
            _pr = int(task.get("_partial_retry", 0))
            _no_progress = _got == int(task.get("_last_got", -1))
            if (_shortfall and not _permanent_miss
                    and _pr < _MAX_PARTIAL_RETRY and not _no_progress):
                _miss = _expected - _got
                task["_partial_retry"] = _pr + 1
                task["_last_got"]      = _got
                await _broadcast(_i18n.log_event("console.topup_missing", level="warn", task_id=tid,
                                                 got=_got, expected=_expected, miss=_miss,
                                                 attempt=_pr + 1, max=_MAX_PARTIAL_RETRY))
                await asyncio.sleep(3)
                task["status"]      = "queued"
                task["progress"]    = 0
                task["log"]         = []
                task["_auto_retry"] = True   # gate the error/tracks_err retry paths
                for _k in ("_start_time", "_done_time", "_save_dir",
                           "_prog_total", "_prog_current"):
                    task.pop(_k, None)
                task["_in_retry"] = True
                await _broadcast({"type": "queue_update", "queue": _queue_snapshot()})
            elif _shortfall:
                _miss = _expected - _got
                task["_partial"]  = True
                task["_missing"]  = _miss
                # Issue #5: carry a canonical, human reason for the shortfall so the
                # delivery card / web history can say WHY N of M arrived — not just
                # that some are missing. Classified from the engine log; runner
                # already knows `_permanent_miss`, this just names it.
                _reason = _classify_partial_reason(log_text, _permanent_miss)
                task["_partial_reason"] = _reason
                task.setdefault("meta", {})["_partial_reason"] = _reason
                # Issue #5b: which tracks fell short (best-effort, engines that
                # emit per-track failure lines). Empty for engines without a known
                # format → card shows the aggregate reason above.
                _failed = _extract_failed_tracks(log_text)
                if _failed:
                    task["_failed_tracks"] = _failed
                    task.setdefault("meta", {})["_failed_tracks"] = _failed
                # Issue #5b Phase 2: authoritative per-track shortfall + cross-
                # service/retry routing from the source tracklist (works even when
                # the engine log has no per-track lines). Bounded, best-effort.
                try:
                    _detail = await asyncio.wait_for(
                        _resolve_shortfall_detail(task, _reason), timeout=8)
                    if _detail:
                        task["_shortfall_detail"] = _detail
                        task.setdefault("meta", {})["_shortfall_detail"] = _detail
                except Exception:
                    pass
                try:
                    from ripster import download_manifest as _dm_sp
                    _dm_sp.set_partial(tid, _got, _expected, _miss, _reason, _failed)
                except Exception:
                    pass
                _pkey = "console.partial_permanent" if _permanent_miss else "console.partial_region"
                await _broadcast(_i18n.log_event(_pkey, level="warn", task_id=tid,
                                                 got=_got, expected=_expected, miss=_miss))
            else:
                await _broadcast(_i18n.log_event("console.done_tracks", level="success",
                                                 task_id=tid, n=result.tracks_ok))
            # Optional auto-merge of "(DJ Mix)" releases → one file + CUE.
            try:
                await _maybe_auto_mix(task, tid)
            except Exception as _e:
                print(f"[coder] auto-mix skipped: {_e}", flush=True)
        elif (result.tracks_err > 0
              and not task.get("_auto_retry")
              and not (engine_name == "soundcloud"
                       and "FairPlay" in (result.error or "")
                       and result.tracks_ok == 0)):
            # Partial failure — reset in-place (same tile, same ID).
            # AMD/streamrip skip already-downloaded tracks, so a second pass
            # picks up the failed ones without re-downloading the successful ones.
            n_ok  = result.tracks_ok
            n_err = result.tracks_err
            await _broadcast(_i18n.log_event("console.partial_retry", level="warn",
                                             task_id=tid, n_ok=n_ok, n_err=n_err))
            await asyncio.sleep(3)
            task["status"]      = "queued"
            task["progress"]    = 0
            task["log"]         = []
            task["_auto_retry"] = True
            for _k in ("_start_time", "_done_time", "_save_dir",
                       "_prog_total", "_prog_current"):
                task.pop(_k, None)
            task["_in_retry"] = True   # tell finally to skip _add_to_history
            await _broadcast({"type": "queue_update", "queue": _queue_snapshot()})
            await _broadcast(_i18n.log_event("console.autoretry_partial", level="info"))
        else:
            msg = result.error or f"Exit code {rc}"
            if msg.startswith("ORPHEUS_NOT_AUTHED"):
                await _broadcast({"type": "orpheus_not_authed"})
            elif "-1002" in msg and engine_name in ("gamdl", "zhaarey"):
                if (_amd_mod.get_amd_dir() / "main.py").exists():
                    raise _NeedAMDFallback()
            elif "New settings detected" in msg or "обнаружены новые настройки" in msg:
                raise _NeedRetry()

            # SoundCloud DRM-only fallback — Lucida can't decrypt FairPlay HLS,
            # so try to match the track on Deezer/Qobuz/Apple by title+artist+
            # duration and queue THAT as a fresh task. Opt-in via config so
            # users who want SC-only results don't get auto-redirected.
            if (
                engine_name == "soundcloud"
                and ("FairPlay" in msg or "ffmpeg" in msg.lower())
                and not task.get("_sc_fallback_tried")
                and bool(_config.get("sc-isrc-fallback", False))
            ):
                task["_sc_fallback_tried"] = True
                asyncio.create_task(_sc_drm_fallback(task))

            retry_n = task.get("_retry_count", 0)
            # Transient wrapper overload → patient (many) retries; otherwise the
            # normal small cap. Either way capped at 120s spacing (no hammering).
            # Decrypt-core-down first (fewest retries — won't self-heal), then the
            # patient wrapper-busy class, else the normal small cap.
            if _RE_WRAPPER_DEAD.search(msg):
                max_r = _MAX_DEAD_RETRIES
            elif _RE_DECRYPT_DOWN.search(msg):
                max_r = _MAX_DECRYPT_RETRIES
            elif _RE_PATIENT.search(msg):
                max_r = _MAX_PATIENT_RETRIES
            else:
                max_r = _MAX_AUTO_RETRIES
            can_retry = (
                retry_n < max_r
                and not _RE_NO_RETRY.search(msg)
                and not task.get("_auto_retry")   # don't chain onto partial-fail retry
            )
            if can_retry:
                delay = _RETRY_BACKOFF[min(retry_n, len(_RETRY_BACKOFF) - 1)]
                # Trim long errors at a WORD boundary (not mid-word like the old
                # msg[:120] → "…Повтори позже, выб") so the retry line reads clean.
                _emsg = msg if len(msg) <= 160 else (msg[:160].rsplit(" ", 1)[0] + "…")
                await _broadcast(_i18n.log_event("console.error_retry", level="warn", task_id=tid,
                                                 msg=_emsg, n=retry_n + 1, max=max_r, delay=delay))
                await asyncio.sleep(delay)
                # Reset in-place: same tile, same ID, no new queue entry.
                task["status"]       = "queued"
                task["progress"]     = 0
                task["log"]          = []
                task["_retry_count"] = retry_n + 1
                for _k in ("_start_time", "_done_time", "_save_dir",
                           "_prog_total", "_prog_current", "_amd_fallback"):
                    task.pop(_k, None)
                task["_in_retry"] = True   # tell finally to skip _add_to_history
                await _broadcast({"type": "queue_update", "queue": _queue_snapshot()})
                await _broadcast(_i18n.log_event("console.autoretry_n", level="info",
                                                 n=retry_n + 1, max=max_r))
            else:
                # ── Salvage-from-disk ────────────────────────────────────────
                # The process failed/hung (e.g. the Apple Go tool downloads a
                # track, then stalls before printing "Completed" and the watchdog
                # kills it). If REAL audio files actually landed on disk, deliver
                # them as a partial result instead of throwing the download away.
                # Strictly guarded: only salvages when audio files genuinely exist.
                salvaged = False
                try:
                    from ripster.routes.download import (
                        _get_task_dir as _gtd, _find_audio_files as _faf)
                    from ripster import download_manifest as _dm
                    _d = _gtd(task)
                    # Apple Go output lands at a bare <base>/<quality>/ root. Re-home
                    # it under <service>/ BEFORE recording — otherwise a CKC/partial
                    # salvage leaves a bare orphan folder at the downloads root (the
                    # success path relocates; the salvage path used to skip it).
                    if _d and _d.is_dir() and _faf(_d):
                        _d = Path(_relocate_to_service_folder(
                            str(_d), task.get("service") or "", quality, _config))
                    _audio = _faf(_d) if (_d and _d.is_dir()) else []
                    if _audio:
                        task["_save_dir"] = str(_d)
                        task["_files"]    = [f.name for f in _audio]
                        _dm.record(tid, str(_d), _audio, task)
                        try:
                            from ripster.task_marker import write_marker as _wm
                            _wm(_d, tid, task)
                        except Exception:
                            pass
                        _try_advance_task(task, TaskStatus.DONE)
                        task["progress"]   = 100
                        task["_done_time"] = time.time()
                        task["partial"]    = True   # bot shows ↺ + "N из M"
                        await _broadcast(_i18n.log_event("console.salvaged_disk", level="warn",
                                                         task_id=tid, msg=msg[:80], n=len(_audio)))
                        await _broadcast({"type": "queue_update",
                                          "queue": _queue_snapshot()})
                        salvaged = True
                        print(f"[salvage] {_dm.short_id(tid)} → {_d.name} "
                              f"({len(_audio)} files)", flush=True)
                except Exception as _e:
                    print(f"[salvage] failed: {_e}", flush=True)

                if not salvaged:
                    task["error"] = msg
                    _try_advance_task(task, TaskStatus.ERROR)
                    await _broadcast({"type": "log", "msg": f"✗ {msg}", "level": "error", "task_id": tid})

    except asyncio.CancelledError:
        r = _qs.get_runner(tid)
        if r is not None:
            await r.cancel()
        if task.pop("_pause_requested", False):
            # Admin paused a RUNNING task: kill the engine but keep the task held
            # (status 'paused' → runner skips it). Resume re-runs the engine, which
            # skips already-downloaded tracks (skip-existing), so it finishes fast.
            task["status"] = "paused"
            try:
                await _broadcast({"type": "queue_update", "queue": _queue_snapshot()})
            except Exception:
                pass
        else:
            _try_advance_task(task, TaskStatus.CANCELLED)
        raise
    except _NeedAMDFallback:
        raise
    except _NeedZhaareyFallback:
        raise
    except _NeedRetry:
        raise
    except Exception as e:
        import traceback
        msg_e = str(e)
        task["error"] = f"Engine error: {msg_e}"
        _try_advance_task(task, TaskStatus.ERROR)
        task["log"].append(f"Engine error: {msg_e}")
        await _broadcast({"type": "log", "msg": f"✗ Engine error: {msg_e}", "level": "error", "task_id": tid})
        if msg_e.startswith("ORPHEUS_NOT_AUTHED"):
            await _broadcast({"type": "orpheus_not_authed"})
        traceback.print_exc()
    finally:
        # Release the pool wrapper slot back for the next Apple task (every exit
        # path passes here, including auto-retry re-queue and fallbacks).
        if _pool is not None and _pool_slot is not None:
            try:
                _pool.release(_pool_slot)
            except Exception:
                pass
            try:
                from ripster import wrapper_pool as _wp_rel
                await _broadcast({"type": "pool_update", "pool": _wp_rel.live_status()})
            except Exception:
                pass
        # Skip history when the task was reset in-place for auto-retry —
        # it's still queued and will be re-run by process_queue().
        if not task.pop("_in_retry", False):
            _add_to_history(task)
        _qs.unregister_runner(tid)
        _qs.proc = None


# ── Task dispatcher ──────────────────────────────────────────────────────────



async def run_task(task: dict) -> None:
    """Dispatch a single task to the correct engine runner."""
    svc    = task.get("service", "apple")
    engine = task.get("engine", _config.get("engine", "zhaarey"))
    url    = task.get("url", "")
    qid    = task.get("quality", _config.get("quality", "alac"))

    _advance_task(task, TaskStatus.RUNNING)
    task["progress"]    = 0
    task["_start_time"] = time.time()
    svc = task.get("service", "apple")
    from ripster.service_config import get_save_path
    task["_base_save_path"] = get_save_path(_config, svc, qid or "")
    await _broadcast({"type": "queue_update", "queue": _queue_snapshot()})

    # Normalize empty engine string to configured default
    engine = engine or _config.get("engine", "zhaarey")

    # Owner forced the LOCAL premium wrapper (Settings → Apple → Wrapper = local):
    # honour it absolutely — NEVER silently auto-switch an Apple task to the public
    # wm.wol.moe wrapper, even on a wrapper-down / DRM-CKC failure. The local pool
    # starts its own container on acquire(), so "wrapper not running" is not a
    # reason to leave the premium account. (Matches apple_router's local-mode rule.)
    _apple_local_only = (str(_config.get("apple-wrapper") or "auto").strip().lower() == "local")

    try:
        # zhaarey without wrapper → auto-switch to AMD (skipped in local-only mode)
        if engine == "zhaarey" and svc in ("apple", "") and not _apple_local_only:
            wrapper_quals = {"alac", "atmos", "binaural", "downmix"}
            if qid in wrapper_quals and not await _amd_mod.check_wrapper_running():
                amd_dir = _amd_mod.get_amd_dir()
                if (amd_dir / "main.py").exists():
                    await _log(
                        f"⚠ Wrapper не запущен — переключаюсь на AMD (wm.wol.moe) для {qid.upper()}",
                        "warn",
                    )
                    engine = "amd"
                else:
                    dec_port = _config.get("decrypt-port", "127.0.0.1:10020")
                    msg = (
                        f"⚠ Wrapper не запущен (порт {dec_port}) — ALAC/Atmos требует Docker-враппер.\n"
                        f"  Перейди в Setup → Запустить враппер  или выбери качество AAC."
                    )
                    task["log"].append(msg)
                    await _broadcast({"type": "log", "msg": msg, "level": "warn"})
                    await _broadcast({"type": "wrapper_status", "running": False})

        try:
            await _run_engine_task(task, engine, url, qid)
        except _NeedAMDFallback:
            if _apple_local_only:
                # Local-only: do NOT salvage via the public wrapper. Surface the
                # local-wrapper DRM/CKC failure as a real error so the owner can
                # re-login the premium wrapper instead of silently going public.
                await _broadcast(_i18n.log_event("console.wrapper_local_drm_fail", level="error",
                                                 task_id=task.get("id", "")))
                task["log"].append("─── local-only: AMD-фолбэк подавлен ───")
                _try_advance_task(task, TaskStatus.ERROR)
            else:
                await _broadcast(_i18n.log_event("console.drm_retry_amd", level="warn"))
                task["log"].append("─── AMD auto-fallback ───")
                await _run_engine_task(task, "amd", url, qid)
        except _NeedZhaareyFallback as _fb:
            _fbq = _fb.quality
            await _broadcast(_i18n.log_event("console.amd_alac_fallback", level="warn",
                                             quality=_fbq.upper()))
            task["log"].append(f"─── zhaarey {_fbq} auto-fallback ───")
            task["_amd_fallback"] = True
            _advance_task(task, TaskStatus.RUNNING)
            task["progress"] = 0
            await _broadcast({"type": "queue_update", "queue": _queue_snapshot()})
            await _run_engine_task(task, "zhaarey", url, _fbq)
        except _NeedRetry:
            await _broadcast(_i18n.log_event("console.orpheus_retry", level="warn",
                                             task_id=task.get("id", "")))
            task["log"].append("─── auto-retry (new settings) ───")
            _advance_task(task, TaskStatus.RUNNING)
            task["progress"] = 0
            await _run_engine_task(task, engine, url, qid)
    except Exception as e:
        import traceback
        _try_advance_task(task, TaskStatus.ERROR)
        task["log"] = task.get("log", []) + [f"run_task error: {e}"]
        await _broadcast({"type": "log", "msg": f"✗ {e}", "level": "error"})
        traceback.print_exc()
    finally:
        await _broadcast({"type": "queue_update", "queue": _queue_snapshot()})


# ── Queue worker ─────────────────────────────────────────────────────────────

def _lock_key(task: dict) -> str | None:
    """Contended-resource key for queue concurrency. Tasks sharing a key run ONE
    at a time; tasks with key None run fully parallel (up to max-parallel).

    The public AMD wrapper (`amd`) is a remote gRPC client — no local port, each
    process independent — so multiple Apple ALAC/Atmos releases can download at
    once. Serialized paths: `zhaarey` (local docker wrapper binds fixed ports
    10020/20020), `deemix`/Deezer (shared %APPDATA%\\deemix\\.arl) and streamrip
    (shared config.toml) — plus their per-account rate-limits."""
    eng = (task.get("engine") or "").lower()
    svc = (task.get("service") or "").lower()
    if eng == "amd":
        return None                      # public wrapper → parallel-safe
    if eng == "zhaarey":
        return "apple-local"             # local wrapper → fixed ports, one at a time
    return svc or eng or "default"       # deemix/streamrip/etc → serialize per service


# ── Fair per-requester scheduling (issue #11) ────────────────────────────────────
# When several people share the box (owner + guest links), a single requester's
# big playlist (say 40 queued tracks) would, under plain FIFO, sit at the head of
# a shared service lane and make everyone else wait their whole batch out. We
# round-robin the *pending* list across requesters BEFORE the lane-assignment loop
# so each active requester gets a turn — one user's bulk job can no longer starve
# another's single track in the same lane. This only reorders which queued task is
# *considered* first; it never touches the lane caps, the disk brake, or running
# tasks. Requester identity = the guest session token; the owner (and every
# bot-submitted task, which enters as the owner) shares the empty-string bucket.
# A single requester — the common case — leaves the order byte-for-byte FIFO.
# Kill-switch: RIPSTER_FAIR_SCHED=0 restores strict FIFO (self-update principle).
def _fair_sched_on() -> bool:
    return (os.environ.get("RIPSTER_FAIR_SCHED", "1") or "1").strip() not in ("0", "false", "no")


def _fair_order(pending: list[dict]) -> list[dict]:
    """Round-robin *pending* across requesters (guest token; owner = ""). Stable
    within a requester. Identity-preserving for 0/1 requester → unchanged FIFO."""
    if len(pending) < 2 or not _fair_sched_on():
        return pending
    from collections import OrderedDict
    from itertools import chain, zip_longest
    buckets: "OrderedDict[str, list]" = OrderedDict()
    for t in pending:
        buckets.setdefault(t.get("_guest_token") or "", []).append(t)
    if len(buckets) < 2:
        return pending                   # one requester → strict FIFO, no reorder
    _SENT = object()
    return [t for t in chain.from_iterable(zip_longest(*buckets.values(), fillvalue=_SENT))
            if t is not _SENT]


# ── Disk-space safety brake ─────────────────────────────────────────────────────
# Protect the HOST (a powerful home box OR a small VPS the project is uploaded to)
# from filling its disk: when free space on the download volume drops below the
# floor, process_queue stops STARTING new downloads — running ones finish and
# auto_cleanup frees space, then it resumes on its own. Hardware-agnostic on
# purpose: an absolute GB floor (works on any disk size) plus an optional % floor,
# BOTH env-overridable so retuning never needs a rebuild (self-update principle).
# Defaults keep a 5 GB headroom on every machine. Set RIPSTER_MIN_FREE_GB=0 to off.
def _env_float(name: str, default: float) -> float:
    try:
        v = float(os.environ.get(name, "") or default)
        return v if v >= 0 else default
    except (TypeError, ValueError):
        return default

_DISK_MIN_FREE_GB  = _env_float("RIPSTER_MIN_FREE_GB", 5.0)
_DISK_MIN_FREE_PCT = _env_float("RIPSTER_MIN_FREE_PCT", 0.0)   # 0 = % check off
_disk_warn_ts = 0.0


def _disk_free(path: str) -> "tuple[float, float]":
    """(free_GB, free_%) for the filesystem holding `path`. Fails OPEN: any error
    (or an unreadable path) returns (inf, 100) so a broken probe NEVER blocks
    downloads — the brake only ever engages on a CONFIRMED low-space reading."""
    import shutil
    try:
        pp = Path(path or ".")
        while not pp.exists() and pp.parent != pp:   # dir may not exist yet
            pp = pp.parent
        u = shutil.disk_usage(str(pp))
        return u.free / (1024 ** 3), (u.free / u.total * 100 if u.total else 100.0)
    except Exception:
        return float("inf"), 100.0


def _disk_guard_ok() -> "tuple[bool, float, float]":
    """(ok, free_GB, free_%) — ok is False when free space is below the floor and
    new downloads should be held. Returns the numbers so the caller can warn."""
    if _DISK_MIN_FREE_GB <= 0 and _DISK_MIN_FREE_PCT <= 0:
        return True, float("inf"), 100.0
    free_gb, free_pct = _disk_free(_config.get("save-path", "downloads"))
    ok = (free_gb >= _DISK_MIN_FREE_GB
          and (_DISK_MIN_FREE_PCT <= 0 or free_pct >= _DISK_MIN_FREE_PCT))
    return ok, free_gb, free_pct


async def process_queue() -> None:
    """Main queue worker — supports 1..N parallel downloads via max-parallel config."""
    global _disk_warn_ts
    if not _qs.is_running:
        return
    active: dict[str, asyncio.Task] = {}
    try:
        while _qs.is_running:
            if _qs.is_paused:
                await asyncio.sleep(0.3)
                continue

            # Remove finished asyncio tasks
            done_ids = [tid for tid, t in active.items() if t.done()]
            for tid in done_ids:
                del active[tid]
            _qs.active_tasks.clear()
            _qs.active_tasks.update(active)

            # Smart per-lane parallelism: every contended resource (lock-key)
            # is its OWN lane — Deezer never waits on Apple and vice-versa.
            #   • each keyed lane (deemix/streamrip/zhaarey…) runs ONE at a time
            #     (shared ARL / config.toml / fixed ports / per-account limits);
            #   • the public AMD wrapper (key=None) is a remote stateless gRPC
            #     client, so it gets a wide lane of `max(3, max-parallel)`.
            # Different lanes run fully in parallel — there is no single global
            # pool that one service can starve. Floor of 3 per the user's rule.
            amd_cap = max(3, int(_config.get("max-parallel", 1)))
            # Apple-local (zhaarey) was a single lane (one local wrapper, fixed
            # ports). The wrapper pool now gives it `pool_size` concurrent slots —
            # each task acquires its own container. Pool off ⇒ 1 ⇒ original serial.
            zh_cap = 1
            if any((t.get("engine") or "").lower() == "zhaarey" for t in _queue):
                # apple-parallel-tracks dedicates the WHOLE pool to ONE album (the
                # Go tool fans tracks across every container), so albums must run
                # one-at-a-time — otherwise two albums fight over the same slots.
                if _config.get("apple-parallel-tracks"):
                    zh_cap = 1
                else:
                    try:
                        from ripster.wrapper_pool import pool_size as _pool_size
                        # Honour max-parallel as a REAL ceiling for the local wrapper:
                        # the pool may expose N container slots, but if the user lowered
                        # parallelism (e.g. to ease the wrapper's CKC load), running more
                        # than that overloads the local Apple session → "Invalid CKC".
                        # zh_cap = min(pool slots, max-parallel).
                        _mp = max(1, int(_config.get("max-parallel", 1)))
                        zh_cap = max(1, min(_pool_size(_config), _mp))
                    except Exception:
                        zh_cap = 1
            by_id = {t["id"]: t for t in _queue}
            busy_keys = set()
            amd_active = 0
            zh_active  = 0
            for atid in active:
                if atid in by_id:
                    k = _lock_key(by_id[atid])
                    if k is None:
                        amd_active += 1
                    elif k == "apple-local":
                        zh_active += 1
                    else:
                        busy_keys.add(k)
            pending = [
                t for t in _queue
                if t["status"] == "queued" and t["id"] not in active
            ]
            # Disk safety brake: below the free-space floor, don't START new
            # downloads this round — running ones keep going (auto_cleanup then
            # frees space) and the queue resumes by itself. Already-running tasks
            # are never interrupted. Warning is throttled to once a minute.
            if pending:
                _ok, _free_gb, _free_pct = _disk_guard_ok()
                if not _ok:
                    if time.time() - _disk_warn_ts > 60:
                        _disk_warn_ts = time.time()
                        await _broadcast({
                            "type": "log", "level": "warn",
                            "message": (f"⚠ Low disk / Мало места: {_free_gb:.1f} GB free "
                                        f"(floor {_DISK_MIN_FREE_GB:.0f} GB) — new downloads "
                                        f"paused, will resume when space frees / "
                                        f"новые загрузки на паузе до освобождения места.")})
                    pending = []
            # Fair-share: round-robin queued tasks across requesters so one user's
            # bulk batch can't monopolise a shared lane (issue #11). No-op for a
            # single requester; lane caps below are unaffected.
            for task in _fair_order(pending):
                k = _lock_key(task)
                if k is None:
                    if amd_active >= amd_cap:
                        continue          # AMD lane full
                    amd_active += 1
                elif k == "apple-local":
                    if zh_active >= zh_cap:
                        continue          # Apple-local pool full (all wrappers busy)
                    zh_active += 1
                elif k in busy_keys:
                    continue              # this service's lane already busy
                else:
                    busy_keys.add(k)      # claim this service's lane
                at = asyncio.create_task(run_task(task))
                active[task["id"]] = at
            _qs.active_tasks.clear()
            _qs.active_tasks.update(active)

            if not active and not [t for t in _queue if t["status"] == "queued"]:
                break

            await asyncio.sleep(0.2)
    finally:
        _qs.stop()
        _qs.proc = None
        _qs.active_tasks.clear()
        await _broadcast({"type": "queue_done"})
        await _broadcast(_i18n.log_event("console.queue_finished", level="success"))
