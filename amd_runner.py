"""
AppleMusicDecrypt v2 — Headless Runner with Full Diagnostics
Usage: python amd_runner.py <url> <codec> <language>
"""
import sys, os, asyncio, traceback, time, threading
# Force UTF-8 output on Windows (avoids cp1251 UnicodeEncodeError)
if sys.stdout.encoding and sys.stdout.encoding.lower() not in ("utf-8","utf8"):
    import io
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace", line_buffering=True)
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace", line_buffering=True)

url   = sys.argv[1]
codec = sys.argv[2] if len(sys.argv) > 2 else "alac"
lang  = sys.argv[3] if len(sys.argv) > 3 else "en-US"

# ── Diagnostic printer ────────────────────────────────────────────────────
_t0 = time.time()
def diag(stage: str, msg: str, level: str = "INFO"):
    elapsed = time.time() - _t0
    prefix = {"INFO": "i", "OK": "OK", "WARN": "!", "ERROR": "X", "STEP": ">"}.get(level, ".")
    print(f"[AMD][{elapsed:6.1f}s][{level}] {prefix} {stage}: {msg}", flush=True)

# Module-level "last activity" stamp, refreshed by BOTH per-sample decrypt
# callbacks AND every post-decrypt blocking stage (run_sync: encapsulate /
# ffmpeg fix_encapsulate / write_metadata / save). The region stall-watchdog
# reads max(this, _prog["last"]) so it does NOT falsely cancel a fully-decrypted
# big track (a 60-min continuous mix takes minutes to encapsulate/mux/save its
# ~635MB with no decrypt events — that gap used to trip the 120s watchdog and
# kill the track right before it landed).
_LAST_ACTIVITY = [time.time()]

diag("INIT", f"url={url} codec={codec} lang={lang}", "STEP")
diag("INIT", f"Python {sys.version.split()[0]} | pid={os.getpid()}", "INFO")
diag("INIT", f"cwd={os.getcwd()}", "INFO")

# ── Ensure Bento4 CLI tools are on PATH ─────────────────────────────────────
# src/mp4.py invokes mp4extract / mp4decrypt as BARE command names. They ship
# in the AMD dir and tools/ but not on the system PATH, and AMD runs them from
# inside temp dirs (so CWD search misses them). Prepend the absolute dirs with
# os.pathsep (Windows PATH is ';'-separated — NOT ':') so CreateProcess finds
# them. Without this, ALAC/AAC extraction dies with "[WinError 2] file not found".
_root = os.path.dirname(os.path.abspath(__file__))
_bin_dirs = [os.path.join(_root, "AppleMusicDecrypt"), os.path.join(_root, "tools"), os.getcwd()]
_extra = os.pathsep.join(d for d in _bin_dirs if os.path.isdir(d))
if _extra:
    os.environ["PATH"] = _extra + os.pathsep + os.environ.get("PATH", "")
    diag("INIT", f"PATH +Bento4: {_extra}", "INFO")

# ── Ensure ffmpeg is on PATH ────────────────────────────────────────────────
# src/mp4.py `fix_encapsulate`/`fix_esds_box` call `ffmpeg` as a BARE name and
# read its output file WITHOUT checking the return code, so if ffmpeg is missing
# the track DECRYPTS fine but then dies with "[WinError 2]" when the (never
# created) output is opened — the classic "have N but no file on disk" + a
# misleading partial. amd_runner must not depend on the spawner's PATH (the app
# may be launched with a stripped env); locate ffmpeg ourselves: PATH first,
# then the winget package dir and common install spots.
import shutil as _shutil, glob as _glob
if not _shutil.which("ffmpeg"):
    _ff_cands = []
    _la = os.environ.get("LOCALAPPDATA", "")
    if _la:
        _ff_cands += _glob.glob(os.path.join(_la, "Microsoft", "WinGet", "Packages",
                                              "Gyan.FFmpeg*", "**", "ffmpeg.exe"), recursive=True)
    _ff_cands += [r"C:\112\deps\ffmpeg.exe", r"C:\ffmpeg\bin\ffmpeg.exe"]
    for _ff in _ff_cands:
        if os.path.isfile(_ff):
            os.environ["PATH"] = os.path.dirname(_ff) + os.pathsep + os.environ.get("PATH", "")
            diag("INIT", f"PATH +ffmpeg: {os.path.dirname(_ff)}", "INFO")
            break
    else:
        diag("INIT", "ffmpeg NOT found on PATH or known dirs — fix_encapsulate will fail "
                     "(tracks decrypt but never save)", "WARN")

# ── Preflight: Bento4 CLI MUST be reachable ─────────────────────────────────
# src/mp4.py extract_song shells out to BOTH `mp4decrypt` and `mp4extract`. If
# either is missing every track dies at the decrypt step with a cryptic
# `[WinError 2]`, and the region-rotation loop then retries the SAME doomed step
# across ~17 storefronts (~10 min wasted) before giving up. Detect it ONCE here
# and abort fast with an actionable message instead.
_missing_b4 = [t for t in ("mp4decrypt", "mp4extract") if not _shutil.which(t)]
if _missing_b4:
    diag("PREFLIGHT", f"Bento4 не найден: {', '.join(t + '.exe' for t in _missing_b4)}. "
                      f"ALAC/AAC-декрипт невозможен — установи Bento4 в Setup → "
                      f"«Bento4 (mp4decrypt)». Искал в: {_extra}", "ERROR")
    print("AMD_FATAL: Bento4 (mp4extract/mp4decrypt) не установлен — открой Setup и поставь "
          "компонент «Bento4 (mp4decrypt)», затем повтори.", flush=True)
    sys.exit(3)

# ── Make AppleMusicDecrypt importable ───────────────────────────────────────
# `import src.logger` / `src.utils` / `src.api` resolve against the AMD package
# dir, NOT the project root where this runner lives. When Python launches a
# script by path, sys.path[0] is the SCRIPT's dir (project root) — the cwd is
# NOT added automatically — so `import src` would fail with ModuleNotFoundError.
# Prepend the AMD dir (and cwd as a fallback) so the package is found regardless
# of where the process was spawned from.
for _amd_dir in (os.path.join(_root, "AppleMusicDecrypt"), os.getcwd()):
    if os.path.isdir(os.path.join(_amd_dir, "src")) and _amd_dir not in sys.path:
        sys.path.insert(0, _amd_dir)
        diag("INIT", f"sys.path +AMD: {_amd_dir}", "INFO")

# ── Patch loguru ──────────────────────────────────────────────────────────
diag("PATCH", "Patching loguru for headless output…", "STEP")
try:
    import src.logger as _sl, copy
    from loguru import logger as _base_log

    def _make_logger(prefix=""):
        from loguru import logger as _lg
        # Remove existing handlers and add plain-print sink
        try:
            _lg.remove()
        except Exception:
            pass
        def _sink(msg):
            t = time.time() - _t0
            print(f"[AMD][{t:6.1f}s][LOG] {prefix + ' | ' if prefix else ''}{str(msg).rstrip()}", flush=True)
        _lg.add(_sink, colorize=False, level="DEBUG", format="{level} - {message}")
        return _lg

    def _g_init(self):
        self.logger = _make_logger()
    _sl.GlobalLogger.__init__ = _g_init

    import urllib.parse as _up
    def _r_init(self, _type, item_id):
        self.item_type = _type
        self.item_id   = _up.quote(item_id)
        self.logger    = _make_logger(f"{_type}/{self.item_id[:30]}")
    _sl.RipLogger.__init__ = _r_init
    diag("PATCH", "loguru -> plain print OK", "OK")
except Exception as e:
    diag("PATCH", f"loguru patch failed: {e}", "ERROR")

# ── Patch safely_create_task ──────────────────────────────────────────────
diag("PATCH", "Patching task scheduler…", "STEP")
try:
    import src.utils as _u

    _task_log: dict[str, dict] = {}  # task_id -> {name, start, state}
    # Set when the GLOBAL decrypt stream (wm.decrypt_init) dies — e.g. the public
    # wrapper's Core throws a gRPC INTERNAL/UNAVAILABLE mid-decrypt. That stream is
    # established once for the whole run, so once it's dead NO region can decrypt:
    # rotating storefronts is pointless. The main loop polls this and aborts fast so
    # the queue can patient-retry with a FRESH runner (and a fresh decrypt stream).
    _decrypt_dead: dict = {"v": False, "why": ""}

    def _safe_task(coro, _name=None):
        loop = asyncio.get_event_loop()
        task = loop.create_task(coro)
        tid  = id(task)
        name = _name or getattr(coro, '__qualname__', str(coro)[:40])
        _task_log[tid] = {"name": name, "start": time.time(), "state": "running"}
        _u.background_tasks.add(task)
        # Per-sample decrypt callbacks (on_decrypt_success) fire once PER AUDIO
        # SAMPLE — a 60-min continuous mix has ~150k of them. Logging "created/
        # DONE" for each floods stdout with ~300k lines, which fills the parent
        # app's read pipe and DEADLOCKS the whole download (child blocks on write,
        # parent never catches up). The app filters these lines on read anyway, so
        # stay silent for them here; only the rare CANCELLED/FAILED is surfaced.
        _quiet = "on_decrypt_success" in name
        if not _quiet:
            diag("TASK", f"created [{tid}] {name}", "INFO")

        def _done(t):
            _u.background_tasks.discard(t)
            elapsed = time.time() - _task_log.get(tid, {}).get("start", time.time())
            if t.cancelled():
                diag("TASK", f"CANCELLED [{tid}] {name} after {elapsed:.1f}s", "WARN")
                _task_log[tid]["state"] = "cancelled"
            elif t.exception():
                exc = t.exception()
                _es = str(exc)
                # Benign, expected case: this storefront 404s the album catalog
                # (Apple returns {'errors':[...'40400'...]} → AMD's AlbumMeta fails
                # to validate). The region fallback just tries the next storefront,
                # so log it concisely instead of a scary pydantic traceback.
                if "AlbumMeta" in _es and ("40400" in _es or "'404'" in _es):
                    diag("TASK", f"SKIP [{tid}] {name}: album not in this storefront (404) — next region", "WARN")
                elif name == "decrypt_init" or "decrypt_keepalive" in name:
                    # The global decrypt stream died. Don't dump the scary gRPC
                    # traceback — flag it so the main loop aborts fast and the queue
                    # patient-retries with a fresh wrapper connection.
                    _is_grpc = "RpcError" in type(exc).__name__ or "StatusCode" in _es
                    _decrypt_dead["v"]   = True
                    _decrypt_dead["why"] = f"{type(exc).__name__}: {_es[:160]}"
                    diag("TASK", f"DECRYPT STREAM DOWN [{name}] after {elapsed:.1f}s: "
                                 f"{type(exc).__name__} — публичный wrapper вернул внутреннюю ошибку "
                                 f"декрипта{' (gRPC)' if _is_grpc else ''}. Прерываю и повторю с новым подключением.",
                                 "ERROR")
                    print("AMD_WRAPPER_DECRYPT_ERROR: публичный Apple-wrapper (декрипт) вернул "
                          "внутреннюю ошибку — соединение разорвано. Это перегрузка/сбой бесплатного "
                          "сервера, не твой токен. Повтори позже, выбери обычный ALAC (не Hi-Res) или "
                          "смени инстанс в Настройках.", flush=True)
                else:
                    diag("TASK", f"FAILED [{tid}] {name} after {elapsed:.1f}s: {type(exc).__name__}: {exc}", "ERROR")
                    diag("TASK", f"  Traceback: {''.join(traceback.format_tb(exc.__traceback__)).strip()}", "ERROR")
                _task_log[tid]["state"] = "failed"
            else:
                if not _quiet:
                    diag("TASK", f"DONE [{tid}] {name} in {elapsed:.1f}s", "OK")
                _task_log[tid]["state"] = "done"

        task.add_done_callback(_done)
        return task

    _u.safely_create_task = _safe_task
    diag("PATCH", "safely_create_task -> diagnostic version OK", "OK")
except Exception as e:
    diag("PATCH", f"task patch failed: {e}", "ERROR")

# ── Register creart creators EARLY ────────────────────────────────────────
# The per-song / save / mp4 / gRPC diagnostic patches below import src.rip,
# src.mp4, src.save and src.grpc.manager — and those modules resolve creart's
# Config at import time. If the creators aren't registered yet the imports fail
# with "current environment does not contain support for src.config:Config",
# which silently disables per-song progress and SAVE_DIR logging. Register the
# creators here (idempotently) so the patches below can import cleanly; BOOT
# skips re-registration via _CREART_REGISTERED.
_CREART_REGISTERED = False
diag("CREART", "Registering creart creators…", "STEP")
try:
    from creart import add_creator, it
    from src.logger       import LoggerCreator;   add_creator(LoggerCreator)
    from src.config       import ConfigCreator;   add_creator(ConfigCreator)
    from src.api          import APICreator;      add_creator(APICreator)
    from src.grpc.manager import WMCreator;       add_creator(WMCreator)
    from src.measurer     import MeasurerCreator; add_creator(MeasurerCreator)
    _CREART_REGISTERED = True
    diag("CREART", "creators registered", "OK")
except Exception as e:
    diag("CREART", f"early registration failed (BOOT will retry): {e}", "WARN")

# ── Patch Ripper methods for per-song diagnostics ─────────────────────────
diag("PATCH", "Patching Ripper for per-song tracking…", "STEP")
try:
    from src.rip import Ripper as _Ripper

    # Use *args/**kw so the wrapper is robust to the real rip_song/rip_album
    # signature (it takes more positional args than just url/codec/flags — a
    # hardcoded signature breaks the call with a TypeError).
    _orig_rip_song = _Ripper.rip_song
    async def _diag_rip_song(self, *args, **kw):
        song_id = getattr(args[0], 'id', '?') if args else '?'
        codec   = args[1] if len(args) > 1 else kw.get('codec', '?')
        diag("SONG", f"START id={song_id} codec={codec}", "STEP")
        t = time.time()
        try:
            result = await _orig_rip_song(self, *args, **kw)
            diag("SONG", f"SAVED id={song_id} in {time.time()-t:.1f}s", "OK")
            return result
        except Exception as e:
            diag("SONG", f"FAILED id={song_id}: {type(e).__name__}: {e}", "ERROR")
            raise
    _Ripper.rip_song = _diag_rip_song

    _orig_rip_album = _Ripper.rip_album
    async def _diag_rip_album(self, *args, **kw):
        album_id = getattr(args[0], 'id', '?') if args else '?'
        codec    = args[1] if len(args) > 1 else kw.get('codec', '?')
        diag("ALBUM", f"START id={album_id} codec={codec}", "STEP")
        t = time.time()
        try:
            result = await _orig_rip_album(self, *args, **kw)
            diag("ALBUM", f"DISPATCHED id={album_id} in {time.time()-t:.1f}s", "OK")
            return result
        except Exception as e:
            diag("ALBUM", f"FAILED id={album_id}: {type(e).__name__}: {e}", "ERROR")
            raise
    _Ripper.rip_album = _diag_rip_album

    diag("PATCH", "Ripper methods wrapped OK", "OK")
except Exception as e:
    diag("PATCH", f"Ripper patch failed: {e}", "WARN")

# ── Patch write_metadata for diagnostics ─────────────────────────────────
diag("PATCH", "Patching write_metadata…", "STEP")
try:
    import src.mp4 as _mp4_mod
    _orig_write_meta = _mp4_mod.write_metadata

    def _diag_write_meta(*args, **kw):
        t = time.time()
        metadata = args[1] if len(args) > 1 else kw.get('metadata')
        try:
            result = _orig_write_meta(*args, **kw)
            title = getattr(metadata, 'title', '?')
            artist = getattr(metadata, 'artist', '?')
            diag("META", f"written OK in {time.time()-t:.2f}s title={title!r} artist={artist!r}", "OK")
            return result
        except Exception as e:
            import traceback as _tb
            diag("META", f"FAILED: {type(e).__name__}: {e}", "ERROR")
            diag("META", f"  {_tb.format_exc().strip()}", "ERROR")
            raise

    _mp4_mod.write_metadata = _diag_write_meta
    diag("PATCH", "write_metadata wrapped OK", "OK")
except Exception as e:
    diag("PATCH", f"write_metadata patch failed: {e}", "WARN")

# ── Disk-truth helpers (fix have/missing under-count + duplicate spawning) ──
# The album's on-disk folder is the source of truth for "do we already have this
# track". AMD's own check_song_exists() compares by EXACT filename built from the
# current song-file-format, so a track saved earlier under a DIFFERENT template
# (e.g. "1-01 Title.m4a" vs "01. Artist - Title.m4a") is invisible → the track is
# re-counted as missing, re-dispatched, and re-saved (a duplicate). We instead
# learn the album dir from the first save and match expected tracks to files by a
# NORMALISED title substring — tolerant of any naming scheme.
_album_dir = {"path": None}   # set by the save hook below; read by _disk_have()

import re as _re
def _norm_title(s: str) -> str:
    """Lowercase, drop parenthetical suffixes (feat./remaster/etc) and all
    non-alphanumerics so the same song matches across filename templates."""
    s = str(s or "").lower()
    s = _re.sub(r"\([^)]*\)", "", s)          # strip "(feat. …)" / "(remaster)"
    s = _re.sub(r"[^0-9a-zа-яё]+", "", s)      # keep only alphanumerics (incl. cyrillic)
    return s

# ── Patch save for directory tracking ─────────────────────────────────────
# src.rip does `from src.save import save` at import time, so we must patch
# both src.save.save AND src.rip.save (the already-captured local binding).
diag("PATCH", "Patching save for directory tracking…", "STEP")
try:
    import src.save as _save_mod
    import src.rip  as _rip_mod
    _orig_save = _save_mod.save

    def _diag_save(*args, **kw):
        result = _orig_save(*args, **kw)
        try:
            _album_dir["path"] = str(result.parent)   # learn album folder for disk-truth scan
            diag("SAVE_DIR", f"dir={str(result.parent)}", "OK")
        except Exception:
            pass
        return result

    _save_mod.save = _diag_save
    _rip_mod.save  = _diag_save   # patch the already-imported binding in rip.py
    diag("PATCH", "save -> directory logger OK", "OK")
except Exception as e:
    diag("PATCH", f"save patch failed: {e}", "WARN")

# ── Feed the stall-watchdog from post-decrypt processing ──────────────────────
# rip.py runs every heavy post-decrypt step (extract_song / encapsulate /
# fix_encapsulate / write_metadata / check_integrity / save) through run_sync.
# Wrapping it to stamp _LAST_ACTIVITY before AND after each call keeps the region
# watchdog alive across the minutes a big track spends muxing/saving with zero
# decrypt events — so it lands instead of being cancelled at the finish line.
diag("PATCH", "Patching run_sync to feed the stall watchdog…", "STEP")
try:
    from src.rip import run_sync as _orig_run_sync
    async def _act_run_sync(task, *args):
        _LAST_ACTIVITY[0] = time.time()
        try:
            return await _orig_run_sync(task, *args)
        finally:
            _LAST_ACTIVITY[0] = time.time()
    _rip_mod.run_sync = _act_run_sync   # patch the binding rip.py actually calls
    diag("PATCH", "run_sync -> watchdog-feeding version OK", "OK")
except Exception as e:
    diag("PATCH", f"run_sync patch failed: {e}", "WARN")

# ── Patch WrapperManager for gRPC call diagnostics ────────────────────────
diag("PATCH", "Patching WrapperManager for gRPC diagnostics…", "STEP")
try:
    from src.grpc import manager as _mgr_mod
    _WM = _mgr_mod.WrapperManager

    for _method_name in ("m3u8", "decrypt", "key"):
        _orig = getattr(_WM, _method_name, None)
        if _orig is None:
            continue
        def _make_wrapper(name, orig):
            async def _wrapped(self, *args, **kw):
                t = time.time()
                try:
                    result = await orig(self, *args, **kw)
                    # `decrypt` is called once PER SAMPLE (~150k times for a 60-min
                    # mix) — logging each OK floods stdout and deadlocks the app's
                    # read pipe. Only the low-frequency m3u8/key OKs are useful.
                    if name != "decrypt":
                        diag("gRPC", f"{name} OK in {time.time()-t:.2f}s", "OK")
                    return result
                except Exception as e:
                    diag("gRPC", f"{name} FAILED in {time.time()-t:.2f}s: {type(e).__name__}: {str(e)[:120]}", "ERROR")
                    raise
            return _wrapped
        setattr(_WM, _method_name, _make_wrapper(_method_name, _orig))

    diag("PATCH", "WrapperManager gRPC methods wrapped OK", "OK")
except Exception as e:
    diag("PATCH", f"WrapperManager patch failed: {e}", "WARN")

# ── Progress reporter thread ───────────────────────────────────────────────
_stop_reporter = threading.Event()
def _reporter():
    last_count = -1
    while not _stop_reporter.is_set():
        time.sleep(10)
        try:
            n = len(_u.background_tasks)
            states = {}
            for tid, info in list(_task_log.items()):
                s = info["state"]
                states[s] = states.get(s, 0) + 1
            if n != last_count:
                diag("PROGRESS", f"background_tasks={n} | states={states}", "INFO")
                last_count = n
        except Exception:
            pass
_reporter_thread = threading.Thread(target=_reporter, daemon=True)

# ── Boot creart ───────────────────────────────────────────────────────────
diag("BOOT", "Initialising creart modules…", "STEP")
loop = asyncio.new_event_loop()
asyncio.set_event_loop(loop)

from creart import add_creator, it
if not _CREART_REGISTERED:
    # Early registration above failed — register here as a fallback.
    from src.logger       import LoggerCreator;   add_creator(LoggerCreator)
    from src.config       import ConfigCreator;   add_creator(ConfigCreator)
    from src.api          import APICreator;      add_creator(APICreator)
    from src.grpc.manager import WMCreator;       add_creator(WMCreator)
    from src.measurer     import MeasurerCreator; add_creator(MeasurerCreator)

from src.api          import WebAPI
from src.config       import Config
from src.grpc.manager import WrapperManager
from src.rip          import Ripper
from src.url          import AppleMusicURL, URLType
from src.utils        import run_sync, background_tasks
from src.flags        import Flags

# ── Harden get_song_info against transient amp-api 404s ───────────────────────
# AMD's WebAPI._request returns the response WITHOUT raise_for_status, so its
# tenacity @retry (which only catches httpx.HTTPError) never fires on a 4xx.
# A flaky 404 from amp-api.music.apple.com then reaches SongData.model_validate
# and explodes with a pydantic ValidationError ("data Field required"), which
# permanently drops an otherwise-available track from the run (verified: the
# track existed in jp/us/gb at the same moment). Retry the call a few times so a
# transient blip self-heals; a genuine permanent 404 still raises after retries.
try:
    _GSI_RETRIES   = int(os.environ.get("AMD_SONGINFO_RETRIES", "4"))
    _orig_get_song_info = WebAPI.get_song_info
    async def _retry_get_song_info(self, song_id, storefront, lang, *a, **kw):
        last = None
        for attempt in range(max(1, _GSI_RETRIES)):
            try:
                return await _orig_get_song_info(self, song_id, storefront, lang, *a, **kw)
            except Exception as e:               # ValidationError on 404 error-json, or httpx errors
                last = e
                if attempt + 1 < _GSI_RETRIES:
                    diag("SONGINFO", f"{song_id}@{storefront} attempt {attempt+1}/{_GSI_RETRIES} "
                                     f"failed ({type(e).__name__}); retrying", "WARN")
                    await asyncio.sleep(1.5 * (attempt + 1))
        diag("SONGINFO", f"{song_id}@{storefront} still failing after {_GSI_RETRIES} tries: "
                         f"{type(last).__name__}: {last}", "ERROR")
        raise last
    WebAPI.get_song_info = _retry_get_song_info
    diag("PATCH", f"get_song_info wrapped with {_GSI_RETRIES}x transient-404 retry", "OK")
except Exception as e:
    diag("PATCH", f"get_song_info retry patch failed: {e}", "WARN")

diag("BOOT", "creart modules loaded", "OK")

# ── Main ──────────────────────────────────────────────────────────────────
async def main():
    # Web API init
    diag("API", "Initialising WebAPI…", "STEP")
    try:
        await run_sync(it(WebAPI).init)
        diag("API", "WebAPI init OK", "OK")
    except Exception as e:
        diag("API", f"WebAPI init FAILED: {e}", "ERROR")
        return 1

    # Wrapper-manager connection
    wm  = it(WrapperManager)
    cfg = it(Config)
    diag("WM", f"Connecting to {cfg.instance.url} (secure={cfg.instance.secure})…", "STEP")
    try:
        await wm.init(cfg.instance.url, cfg.instance.secure)
        diag("WM", "gRPC channel opened", "OK")
    except Exception as e:
        diag("WM", f"Connection FAILED: {e}", "ERROR")
        return 1

    # Check status
    try:
        status = await wm.status()
        inst   = getattr(status, 'instanceCount', '?')
        regions = getattr(status, 'regions', [])
        diag("WM", f"ready={status.ready} instances={inst} regions={regions}", "INFO")
        if not status.ready:
            diag("WM", "ready=False but regions available — proceeding anyway", "WARN")
    except Exception as e:
        diag("WM", f"Status check FAILED: {e}", "ERROR")
        return 1

    # Patch WrapperManager.lyrics to skip on 404 instead of retrying 8× (~50s/track)
    try:
        _orig_lyrics = wm.__class__.lyrics
        _raw_lyrics  = getattr(_orig_lyrics, '__wrapped__', _orig_lyrics)
        async def _lyrics_no_retry(self, *a, **kw):
            try:
                return await _raw_lyrics(self, *a, **kw)
            except Exception as e:
                diag("LYRICS", f"skipped — {str(e)[:80]}", "WARN")
                return ""
        wm.__class__.lyrics = _lyrics_no_retry
        diag("LYRICS", "retry-on-failure disabled (404 skipped silently)", "OK")
    except Exception as e:
        diag("LYRICS", f"patch failed: {e}", "WARN")

    # Decrypt init
    diag("WM", "Starting decrypt stream…", "STEP")
    ripper = Ripper()
    # ── Progress tracker (drives the region-rotation watchdog below) ──────────
    # Every decrypted track stamps _prog["last"] and records its adam_id, so we
    # can tell a working region (tracks arriving) from a dry / queue-full one
    # (no progress) and rotate without waiting forever.
    _prog = {"last": time.time(), "ids": set()}
    _orig_on_success = ripper.on_decrypt_success
    async def _tracked_on_success(adam_id, key, sample, sample_index):
        try:
            _prog["last"] = time.time()
            _LAST_ACTIVITY[0] = time.time()
            _prog["ids"].add(str(adam_id))
        except Exception:
            pass
        return await _orig_on_success(adam_id, key, sample, sample_index)
    ripper.on_decrypt_success = _tracked_on_success
    # Count tracks AMD skips because the file is ALREADY on disk (deemix-style
    # "track in folder → skip"). This lets a fresh runner recognise an already-
    # complete album (or resume across regions) WITHOUT re-decrypting — so a done
    # release exits immediately instead of cycling regions into the 120s stall.
    _prog["existing"] = set()
    try:
        import urllib.parse as _upq
        if hasattr(_sl.RipLogger, "already_exist"):
            _orig_already = _sl.RipLogger.already_exist
            def _tracked_already(self, *a, **k):
                try:
                    _prog["existing"].add(_upq.unquote(str(getattr(self, "item_id", ""))))
                    _prog["last"] = time.time()
                except Exception:
                    pass
                return _orig_already(self, *a, **k)
            _sl.RipLogger.already_exist = _tracked_already
            diag("PATCH", "already-exist tracking enabled (file on disk = have)", "OK")
    except Exception as _e:
        diag("PATCH", f"already-exist hook failed: {_e}", "WARN")
    # Count DOWNLOAD bytes as progress too. AMD downloads the WHOLE encrypted
    # file (src/api.py _download_song_internal) into memory BEFORE a single
    # sample decrypts, so a big track — e.g. a ~1h continuous DJ mix (600MB-1GB)
    # — spends minutes downloading with ZERO decrypt events. A decrypt-only
    # stall watchdog then falsely flags "no progress → cancel" and rotates
    # forever (the track never finishes downloading before it's killed). Stamp
    # _prog on every downloaded chunk so a healthy slow download stays alive.
    try:
        from src.measurer import Measurer as _Measurer
        _orig_record_dl = _Measurer.record_download
        def _tracked_record_dl(self, content_length, *a, **k):
            try:
                if content_length:
                    _prog["last"] = time.time()
            except Exception:
                pass
            return _orig_record_dl(self, content_length, *a, **k)
        _Measurer.record_download = _tracked_record_dl
        diag("PATCH", "download bytes feed the stall watchdog (big-file/continuous-mix fix)", "OK")
    except Exception as _e:
        diag("PATCH", f"download-progress hook failed: {_e}", "WARN")
    _safe_task(wm.decrypt_init(
        on_success=ripper.on_decrypt_success,
        on_failure=ripper.on_decrypt_failed,
    ), "decrypt_init")
    await asyncio.sleep(1)
    diag("WM", "decrypt stream started", "OK")

    # Parse URL
    diag("URL", f"Parsing {url}…", "STEP")
    url_obj = AppleMusicURL.parse_url(url)
    if url_obj is None:
        diag("URL", f"Cannot parse URL: {url}", "ERROR")
        return 1
    diag("URL", f"type={url_obj.type} id={url_obj.id} storefront={url_obj.storefront}", "OK")

    flags = Flags(force_save=False, language=lang)

    # Ignore permanent background tasks (decrypt stream + keepalive) that never finish.
    _PERMANENT_TASKS = {"decrypt_init", "_decrypt_keepalive", "WrapperManager._decrypt_keepalive"}

    def _work_done() -> bool:
        running = [
            info for info in _task_log.values()
            if info["state"] == "running" and info["name"] not in _PERMANENT_TASKS
            and not any(p in info["name"] for p in _PERMANENT_TASKS)
        ]
        return len(running) == 0

    def _dispatch(u):
        match u.type:
            case URLType.Song:
                return _safe_task(ripper.rip_song(u, codec, flags), f"rip_song/{u.id}")
            case URLType.Album:
                return _safe_task(ripper.rip_album(u, codec, flags), f"rip_album/{u.id}")
            case URLType.Playlist:
                return _safe_task(ripper.rip_playlist(u, codec, flags), f"rip_playlist/{u.id}")
        return None

    def _dispatch_missing(region: str, ids):
        """Rip ONLY the still-missing track ids as individual songs (instead of
        re-running the whole album). AMD's rip_album itself just fans out a
        rip_song per track under the same task_lock semaphore, so this is the
        identical concurrency model minus the already-saved tracks — they're not
        re-fetched or re-validated at all. Returns a gather Future (or None)."""
        try:
            from src.url import Song as _SongURL
        except Exception as _e:
            diag("DISPATCH", f"per-track import failed: {_e}", "WARN")
            return None
        tasks = []
        for tid in ids:
            try:
                _su = _SongURL(url=f"https://music.apple.com/{region}/song/{tid}",
                               storefront=region, id=str(tid), type=URLType.Song)
                _tk = _safe_task(ripper.rip_song(_su, codec, flags), f"rip_song/{tid}")
                if _tk is not None:
                    tasks.append(_tk)
            except Exception as _e:
                diag("DISPATCH", f"song {tid}: queue failed {type(_e).__name__}", "WARN")
        if not tasks:
            return None
        return asyncio.gather(*tasks, return_exceptions=True)

    # ── Region pool (auto-fallback across the public wrapper devices) ─────────
    # wm.wol.moe only has devices for some regions at any moment. Apple catalog
    # IDs are shared across storefronts, so rotate the storefront across the live
    # regions until the album rips. Same folder each attempt → AMD skips already-
    # downloaded tracks (resume), so no new dir and no duplicate download. When
    # every region has been tried, go round again (PASSES). A no-progress watchdog
    # rotates off a region that hangs instead of waiting forever.
    # Region order = EASTERNMOST FIRST (where the new calendar day — and a fresh
    # release — has already landed), then westward: NZ → AU → JP → … → EU → UK →
    # Americas. New releases go live at local date rollover, so the most-ahead
    # timezone has them first; trying it first grabs new releases ASAP.
    _TZ = {  # storefront → approx UTC offset (DST ignored; for ordering only)
        "nz": 13, "fj": 12, "nc": 11, "pg": 10, "gu": 10, "au": 10,
        "jp": 9, "kr": 9, "kp": 9,
        "cn": 8, "hk": 8, "tw": 8, "sg": 8, "my": 8, "ph": 8, "mo": 8, "bn": 8,
        "th": 7, "vn": 7, "id": 7, "kh": 7, "la": 7, "mm": 6.5,
        "bd": 6, "kz": 6, "np": 5.75, "in": 5.5, "lk": 5.5, "pk": 5, "uz": 5,
        "ae": 4, "om": 4, "az": 4, "ge": 4, "am": 4, "mu": 4,
        "ru": 3, "sa": 3, "qa": 3, "kw": 3, "bh": 3, "iq": 3, "ke": 3, "tz": 3, "tr": 3,
        "il": 2, "gr": 2, "fi": 2, "eg": 2, "za": 2, "ro": 2, "bg": 2, "ua": 2,
        "lt": 2, "lv": 2, "ee": 2, "cy": 2, "jo": 2, "lb": 2,
        "de": 1, "fr": 1, "nl": 1, "it": 1, "es": 1, "se": 1, "no": 1, "dk": 1,
        "pl": 1, "at": 1, "ch": 1, "be": 1, "cz": 1, "hu": 1, "sk": 1, "si": 1,
        "hr": 1, "rs": 1, "ng": 1, "dz": 1, "tn": 1, "ma": 1, "lu": 1,
        "gb": 0, "ie": 0, "pt": 0, "is": 0, "gh": 0, "sn": 0,
        "br": -3, "ar": -3, "cl": -3, "uy": -3, "py": -4, "bo": -4, "ve": -4, "do": -4,
        "us": -5, "ca": -5, "co": -5, "pe": -5, "ec": -5, "pa": -5, "jm": -5,
        "mx": -6, "gt": -6, "cr": -6, "sv": -6, "hn": -6, "ni": -6,
    }
    def _tzrank(code):
        return _TZ.get(code, -50)   # unknown storefronts → near the end
    try:
        _avail = [str(r).lower() for r in (regions or []) if r]
    except Exception:
        _avail = []
    _orig_sf = (url_obj.storefront or "us").lower()
    if _avail:
        # Live regions, easternmost (latest date) first; alpha tiebreak = stable.
        _pool = sorted(set(_avail), key=lambda c: (-_tzrank(c), c))
    else:
        # Wrapper reported no regions — fall back to a fixed east→west sweep.
        _pool = ["nz", "au", "jp", "sg", "in", "ru", "de", "gb", "br", "us"]
    # The URL's own storefront is the account most likely to actually stream/
    # license the track (region-specific catalog, continuous DJ mixes, region-
    # locked content). Try it FIRST — otherwise a /in/ link wastes minutes
    # stalling through nz→jp→… before reaching IN at position 9 (the "Apple
    # hangs" symptom). The rest stay easternmost-first for new-release grabbing.
    if _orig_sf in _pool:
        _pool = [_orig_sf] + [c for c in _pool if c != _orig_sf]
    diag("REGION", f"order(url-sf first → east→west)={_pool} (url storefront '{_orig_sf}', online={_avail or '?'})", "INFO")

    STALL_SEC = int(os.environ.get("AMD_STALL_SEC", "120"))     # no decrypt progress → rotate
    PASSES    = int(os.environ.get("AMD_REGION_PASSES", "2"))   # round-trips over the pool

    _reporter_thread.start()
    diag("WAIT", "Region-rotation download starting…", "STEP")

    # Learn the album's FULL track set so we can complete it across regions: one
    # region's wrapper sometimes drops a few tracks (CKC), so a single attempt is
    # only partial. The catalog API needs no device, so query east→west until a
    # region returns the tracklist (a fresh release is listed first where the date
    # already rolled over). For songs/playlists we keep the simpler "first clean
    # delivery wins" rule.
    expected_ids: set[str] = set()
    expected_titles: dict[str, str] = {}   # adam_id -> normalised title (for disk-truth match)
    if url_obj.type == URLType.Album:
        for _rg in _pool:
            try:
                _ai = await it(WebAPI).get_album_info(url_obj.id, _rg, lang)
                _tr = _ai.data[0].relationships.tracks.data
                expected_ids = {str(getattr(t, "id", "")) for t in _tr if getattr(t, "id", "")}
                expected_titles = {
                    str(getattr(t, "id", "")): _norm_title(getattr(getattr(t, "attributes", None), "name", ""))
                    for t in _tr if getattr(t, "id", "")
                }
                if expected_ids:
                    _tl_region = _rg            # region whose catalog gave the tracklist
                    diag("REGION", f"album tracklist = {len(expected_ids)} tracks (via {_rg})", "OK")
                    break
            except Exception:
                continue
    _total_expected = len(expected_ids)

    # Resolve the album's on-disk folder UP-FRONT (before any download) using AMD's
    # own path builder, so a partially-downloaded album is recognised from region 1.
    # Without this the first full rip_album re-downloads every track whose on-disk
    # name doesn't match the current template (spawning duplicates) before
    # _disk_have() can engage. Falls back to the save-hook-learned dir on any error.
    if url_obj.type == URLType.Album and expected_ids and _album_dir["path"] is None:
        try:
            from src.metadata import SongMetadata as _SM
            from src.utils import get_song_name_and_dir_path as _name_dir
            _first_id = next(iter(expected_ids))
            _raw = await it(WebAPI).get_song_info(_first_id, _tl_region, lang)
            _meta = _SM.parse_from_song_data(_raw)
            _meta.parse_from_album_data(_ai)
            _dirp = _name_dir(codec, _meta)[1]
            if _dirp:
                _album_dir["path"] = str(_dirp)
                diag("REGION", f"album folder (upfront) = {_album_dir['path']}", "OK")
        except Exception as _e:
            diag("REGION", f"upfront album-dir resolve skipped: {type(_e).__name__} (save-hook fallback)", "INFO")

    _disk_cache = {"dir": None, "files": None}
    def _disk_have() -> set:
        # Disk truth: which expected tracks already have a file in the album folder,
        # matched by NORMALISED title substring (tolerant of any naming template).
        # This is what AMD's exact-filename check_song_exists() misses, so without
        # it a track saved under an older template is re-counted as missing and
        # re-downloaded as a duplicate. Returns the set of expected adam_ids present.
        d = _album_dir["path"]
        if not d or not expected_titles:
            return set()
        try:
            # Cache the dir listing; invalidate when the folder path changes.
            if _disk_cache["dir"] != d:
                _disk_cache["dir"] = d
                _disk_cache["files"] = [_norm_title(os.path.splitext(n)[0])
                                        for n in os.listdir(d)
                                        if n.lower().endswith((".m4a", ".flac", ".mp3", ".aac"))]
            stems = _disk_cache["files"] or []
            found = set()
            for tid, ntitle in expected_titles.items():
                if ntitle and any(ntitle in stem for stem in stems):
                    found.add(tid)
            return found
        except Exception:
            return set()

    def _have() -> set:
        # A track counts as "have" if we decrypted it now, AMD reported it already
        # on disk, OR a matching file is physically present (disk truth) — so
        # resume / complete-album works even across filename-template changes.
        return _prog["ids"] | _prog["existing"] | _disk_have()
    def _missing() -> set:
        return (expected_ids - _have()) if expected_ids else set()

    completed = False
    # Early-abort guard: once we've started getting tracks, if several regions in
    # a row add NOTHING, the still-missing tracks aren't regionally available
    # (e.g. the component tracks of a continuous DJ-mix, or region-locked) — no
    # extra region will fill them. Stop instead of grinding the whole pool × all
    # passes (the user saw a 3/42 mix "spin the same thing" through 20+ regions).
    MAX_ZERO_STREAK = int(os.environ.get("AMD_REGION_ZERO_STREAK", "4"))
    _zero_streak = 0
    _abort_zero  = False
    # Storefronts that returned a catalog 404 (album not listed there) — once seen,
    # never re-contacted (saves a full dispatch attempt on every later pass).
    _dead_regions: set[str] = set()
    for _pass in range(PASSES):
        if completed or _abort_zero:
            break
        _pass_start = len(_prog["ids"])
        for region in _pool:
            if completed:
                break
            if expected_ids and not _missing():   # already have every track
                completed = True
                break
            if region in _dead_regions:            # catalog 404 here earlier → skip
                continue
            url_obj.storefront = region
            before = len(_prog["ids"])
            _prog["last"] = time.time()
            _have_str = f"{len(_have())}/{_total_expected}" if expected_ids else f"{len(_prog['ids'])}"
            # Whenever we know the tracklist, fetch the still-missing tracks as
            # INDIVIDUAL songs — including the very first region. A full rip_album
            # fans its per-track rips out as fire-and-forget background tasks that
            # the region loop never awaits, so a track can be counted via its
            # decrypt callback while its save is still in flight (killed on exit →
            # "have N" but no file), and a hung child stays registered in AMD's
            # adam_id_task_mapping forever, blocking re-dispatch of that id in every
            # later region. Per-track dispatch awaits each rip_song to completion
            # (decrypt AND save) and lets the stall watchdog cancel+unregister a
            # hung one, so nothing orphans. Already-saved tracks aren't re-attempted.
            _miss_now = _missing()
            _use_pt   = bool(expected_ids and _miss_now)
            if _use_pt:
                # Cheap catalog probe (no device): if this storefront 404s the
                # album, mark it dead and skip — don't fire N per-song requests
                # that would all fail identically.
                try:
                    await it(WebAPI).get_album_info(url_obj.id, region, lang)
                except Exception as _pe:
                    _pes = str(_pe)
                    if "AlbumMeta" in _pes and ("40400" in _pes or "'404'" in _pes):
                        diag("REGION", f"region={region}: album not in this storefront (404) — won't retry", "WARN")
                        _dead_regions.add(region)
                        continue
                    # other probe errors → try the rip anyway
                diag("DISPATCH", f"pass {_pass+1}/{PASSES} region={region} have={_have_str} → "
                                 f"{len(_miss_now)} missing track(s) individually", "STEP")
                rip_task = _dispatch_missing(region, _miss_now)
                if rip_task is None:
                    continue
            else:
                diag("DISPATCH", f"pass {_pass+1}/{PASSES} region={region} have={_have_str} type={url_obj.type}…", "STEP")
                rip_task = _dispatch(url_obj)
                if rip_task is None:
                    diag("DISPATCH", f"Unsupported type: {url_obj.type}", "ERROR")
                    return 1
            # Wait for this attempt to finish or stall. AMD skips tracks already
            # on disk (resume) so only the still-missing ones decrypt this round.
            while not rip_task.done():
                await asyncio.sleep(1)
                # Global decrypt stream died → rotating regions can't help (the
                # stream is established once for the whole run). Abort fast and let
                # the queue patient-retry with a fresh runner/connection.
                if _decrypt_dead["v"]:
                    diag("REGION", f"region={region}: decrypt stream down ({_decrypt_dead['why']}) "
                                   f"— aborting (rotation can't recover a dead global stream)", "ERROR")
                    rip_task.cancel()
                    try:
                        await asyncio.wait_for(asyncio.shield(rip_task), timeout=8)
                    except (asyncio.CancelledError, Exception):
                        pass
                    return 4   # distinct exit code: wrapper decrypt failure → retryable
                _last_act = max(_prog["last"], _LAST_ACTIVITY[0])
                if (time.time() - _last_act) > STALL_SEC:
                    diag("REGION", f"region={region}: no progress for {STALL_SEC}s — cancel + rotate", "WARN")
                    rip_task.cancel()
                    try:
                        await asyncio.wait_for(asyncio.shield(rip_task), timeout=10)
                    except (asyncio.CancelledError, Exception):
                        # Cancelling the stalled rip makes the shielded await raise
                        # CancelledError — which is a BaseException, NOT Exception, so a
                        # bare `except Exception` lets it crash the whole runner instead
                        # of rotating to the next region. Catch it explicitly so a hung
                        # region just rotates (the missing track may live elsewhere).
                        pass
                    break
            # Let any save / mp4 sub-tasks for this attempt drain briefly.
            _drain = 0
            while not _work_done() and _drain < 20:
                await asyncio.sleep(1); _drain += 1

            new   = len(_prog["ids"]) - before
            clean = rip_task.done() and not rip_task.cancelled()
            if clean:
                _exc = None
                if _use_pt:
                    # gather(return_exceptions=True): exceptions land in the results
                    # list, not raised. Pick the first one (a catalog-404 makes ALL
                    # per-song get_album_info calls fail identically).
                    try:
                        _results = rip_task.result()
                    except (asyncio.CancelledError, Exception) as _ge:
                        # A stall-cancelled gather re-raises CancelledError here —
                        # a BaseException that a bare `except Exception` misses,
                        # crashing the runner instead of just rotating regions.
                        _results = [_ge]
                    _exc = next((r for r in (_results or []) if isinstance(r, BaseException)), None)
                else:
                    try:
                        _exc = rip_task.exception()
                    except (asyncio.CancelledError, Exception):
                        _exc = None
                if _exc is not None:
                    clean = False
                    _es = str(_exc)
                    if "AlbumMeta" in _es and ("40400" in _es or "'404'" in _es):
                        diag("REGION", f"region={region}: album not in this storefront (404) — won't retry", "WARN")
                        _dead_regions.add(region)
                    else:
                        diag("REGION", f"region={region}: rip raised {type(_exc).__name__}: {_es.splitlines()[0] if _es else ''}", "WARN")

            if expected_ids:
                miss = _missing()
                if not miss:
                    diag("REGION", f"region={region}: +{new} → {len(_have())}/{_total_expected} — COMPLETE", "OK")
                    completed = True
                    break
                # Don't stop on a partial region — keep filling the rest elsewhere,
                # accumulating into the same folder (no reset, no new task).
                diag("REGION", f"region={region}: +{new} → {len(_have())}/{_total_expected}, "
                               f"{len(miss)} track(s) still missing — next region fills the rest", "WARN")
            else:
                # Song / playlist (no tracklist): first clean delivery wins.
                if new > 0 and clean:
                    diag("REGION", f"region={region}: +{new} track(s) — done", "OK")
                    completed = True
                    break
                diag("REGION", f"region={region}: +{new} — rotating to next region", "WARN")

            # Consecutive zero-gain regions → remaining tracks are unavailable.
            _zero_streak = _zero_streak + 1 if new == 0 else 0
            if expected_ids and _zero_streak >= MAX_ZERO_STREAK and len(_have()) > 0:
                diag("REGION", f"{_zero_streak} regions in a row added nothing — remaining "
                               f"{len(_missing())} track(s) unavailable in any live region, "
                               f"stopping (have {len(_have())}/{_total_expected})", "WARN")
                _abort_zero = True
                break
        if _abort_zero:
            break
        # A whole pass that added nothing = no live region can fill the gap now;
        # stop instead of spinning forever.
        if not completed and len(_prog["ids"]) == _pass_start:
            diag("REGION", f"pass {_pass+1}: no region added a track — stopping", "WARN")
            break

    # Final flush: wait for any in-flight decrypt→save subtasks to finish writing
    # to disk before summarising/exiting. A track counted via its decrypt callback
    # can still be mid-save; without this wait, loop teardown cancels it and the
    # file is never written ("have N" but nothing on disk). Bounded so it can't hang.
    _flush = 0
    while not _work_done() and _flush < STALL_SEC:
        await asyncio.sleep(1); _flush += 1

    _stop_reporter.set()

    # Summary — success if the album is fully present, we decrypted anything, or
    # the run finished with no failed tasks (already-complete album = all skipped).
    have_n = len(_have())
    done   = sum(1 for v in _task_log.values() if v["state"] == "done")
    failed = sum(1 for v in _task_log.values() if v["state"] == "failed")
    canc   = sum(1 for v in _task_log.values() if v["state"] == "cancelled")
    elapsed_total = time.time() - _t0
    _have_disp = f"{have_n}/{_total_expected}" if expected_ids else f"{have_n}"
    _total_tasks = len(_task_log)
    # Keep the summary in the EXACT shape engines/amd.py is_finished() greps for
    # ("tasks: N total, X OK, Y failed, Z cancelled") so the server counts an
    # already-complete album (every track skipped) as success instead of retrying.
    diag("DONE", f"Finished in {elapsed_total:.1f}s — have {_have_disp} "
                 f"(decrypted {len(_prog['ids'])}, already-on-disk {len(_prog['existing'])}); "
                 f"tasks: {_total_tasks} total, {done} OK, {failed} failed, {canc} cancelled", "OK")
    return 0 if (completed or len(_prog["ids"]) > 0 or failed == 0) else 1

try:
    rc = loop.run_until_complete(main())
    sys.exit(rc or 0)
except KeyboardInterrupt:
    diag("EXIT", "Interrupted by user", "WARN")
    sys.exit(0)
except Exception as e:
    diag("EXIT", f"FATAL: {type(e).__name__}: {e}", "ERROR")
    traceback.print_exc()
    sys.exit(1)
finally:
    _stop_reporter.set()
    loop.close()
