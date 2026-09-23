"""
Deezer engine via deemix CLI.

Install:  pip install deemix
Config:   put ARL token in Settings -> Deezer.

NOTE: deemix CLI has no --arl flag. We write the ARL to deemix's
config directory (``%APPDATA%\\deemix\\.arl`` on Windows,
``~/.config/deemix/.arl`` on Linux/macOS) before each run.
"""
from __future__ import annotations
import os
import re
import sys
import platform
from pathlib import Path
from .base import EngineBase, EngineResult
from .registry import register
from ripster import http_client as _HTTP
from ripster.py_runtime import app_python

_QUALITIES = [
    {"id": "flac",    "label": "FLAC",    "sub": "Lossless CD quality", "badge": "LOSSLESS", "color": "#3ecfaa", "bitrate": "1411 kbps", "ext": "flac", "req": "premium"},
    {"id": "mp3_320", "label": "MP3 320", "sub": "High quality lossy",  "badge": "LOSSY",    "color": "#EF9F27", "bitrate": "320 kbps",  "ext": "mp3",  "req": "premium"},
    {"id": "mp3_128", "label": "MP3 128", "sub": "Standard quality",    "badge": "LOSSY",    "color": "#EF9F27", "bitrate": "128 kbps",  "ext": "mp3",  "req": "free"},
]

# deemix --bitrate integers: 1=128 kbps, 3=320 kbps, 9=FLAC
_BITRATE = {"flac": "9", "mp3_320": "3", "mp3_128": "1"}

_RE_DONE       = re.compile(r"\bDone\b|\bFinished\b|\bsaved\b|\bCompleted\b", re.I)
_RE_ERR        = re.compile(r"\berror\b|\bfailed\b|cannot|invalid", re.I)
_RE_TRACK      = re.compile(r"(\d+)\s*/\s*(\d+)")
_RE_ARL        = re.compile(r"\b(arl|login|credentials|unauthori[sz]ed|not\s+logged)\b", re.I)
# Per-track download percentage: "[album_123_9] Download at 86%"
_RE_PERCENT    = re.compile(r"Download\s+at\s+(\d+)\s*%", re.I)
# Per-track success marker: "Completed download of \01 - Artist - Title.flac"
_RE_TRACK_DONE = re.compile(r"Completed\s+download\s+of", re.I)
# Короткие ссылки «Поделиться» из приложения Deezer.
_RE_SHORT_LINK = re.compile(r"^https?://(link\.deezer\.com|deezer\.page\.link|dzr\.page\.link)/", re.I)
_RE_DZ_CANON   = re.compile(r"https?://(?:www\.)?deezer\.com/(?:[a-z]{2}/)?(track|album|playlist|artist)/\d+", re.I)


def _expand_short_link(url: str) -> str:
    """14.09.2026: deemix не знает `link.deezer.com/s/…` — отвечает «Link is not
    recognized», пишет «All done!» и ноль файлов; сводка винила ARL и качество,
    авто-повтор прогонял то же самое трижды. Разворачиваем в канонический
    `www.deezer.com/<type>/<id>`: редирект кладёт его в параметр `dest`/`awf`."""
    from urllib.parse import unquote
    if not _RE_SHORT_LINK.match(url or ""):
        return url
    try:
        with _HTTP.shared() as c:
            r = c.get(url, follow_redirects=False, timeout=10)
        loc = unquote(r.headers.get("location", ""))
        if not loc:
            r = _HTTP.client().get(url, timeout=10)
            loc = unquote(str(r.url))
        m = _RE_DZ_CANON.search(loc)
        if m:
            return m.group(0)
    except Exception as e:
        print(f"[deezer] short link not expanded: {e}", flush=True)
    return url


def _deemix_config_dir(override: str = "") -> Path:
    """Return the deemix config folder for the current OS — or, for a
    multi-account pool slot, an isolated per-slot directory (see
    ripster/deezer_pool.py) so two ARLs don't clobber the same .arl file when
    downloading concurrently."""
    if override:
        return Path(override) / "deemix"
    system = platform.system()
    if system == "Windows":
        base = os.environ.get("APPDATA") or str(Path.home() / "AppData" / "Roaming")
        return Path(base) / "deemix"
    if system == "Darwin":
        return Path.home() / "Library" / "Application Support" / "deemix"
    # Linux / other Unix
    xdg = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(xdg) / "deemix"


def _write_arl(arl: str, override: str = "") -> Path:
    """Persist the ARL token where deemix expects to read it."""
    cfg = _deemix_config_dir(override)
    cfg.mkdir(parents=True, exist_ok=True)
    arl_file = cfg / ".arl"
    arl_file.write_text(arl.strip(), encoding="utf-8")
    return arl_file


def _write_deemix_config(lyrics: bool | None = None, synced_lyrics: bool | None = None,
                         override: str = "") -> None:
    """Pin deemix's EMBEDDED (in-audio) cover to 1000 px — uniform tag artwork
    across all services by request. deemix defaults to embeddedArtworkSize 800.
    The SAVED external cover stays large (localArtworkSize 1400) so the on-disk
    original is unaffected. Merges into the existing config.json so no other
    deemix setting is clobbered; deemix creates the file on first run otherwise.

    ``lyrics``/``synced_lyrics`` map to deemix's own ``lyrics`` (embed) and
    ``syncedLyrics`` (save .lrc) settings — None leaves whatever's already in
    deemix's config.json untouched (deemix ships both False by default)."""
    import json
    cfg_dir = _deemix_config_dir(override)
    cfg_dir.mkdir(parents=True, exist_ok=True)
    cfg_file = cfg_dir / "config.json"
    data: dict = {}
    if cfg_file.exists():
        try:
            data = json.loads(cfg_file.read_text(encoding="utf-8"))
        except Exception:
            data = {}
    data["embeddedArtworkSize"] = 1000
    data["saveArtwork"]         = True
    # Раскладка «артист / релиз / файл» — общая для ВСЕХ сервисов (требование
    # владельца 09.08.2026). У deemix по умолчанию `createSingleFolder` и
    # `createArtistFolder` выключены, и одиночный трек ложился ПЛОСКО прямо в
    # `deezer/<качество>/`. Последствие было не косметическим: `_get_task_dir`
    # ищет папку релиза, плоский файл ей не является — в лог уходило
    # «dir unresolved at completion», манифест не писался вовсе, а бот по
    # пустому манифесту честно сообщал «нет файлов» и ставил задачу заново.
    # 06-07.09.2026 так утонули четыре гостевые выдачи подряд, 12.09 случай
    # воспроизведён заново: задача `done` за 10 секунд, манифеста нет и через
    # две минуты, файл при этом лежал на диске.
    # Отдавать резолверу саму папку качества нельзя — в ней лежат треки ДРУГИХ
    # задач, и гость получил бы чужое. Поэтому чиним раскладку; резолвер же с
    # 24.09.2026 умеет сузить выдачу до файлов задачи (routes/download.py
    # `_task_delivery_files`) — это подстраховка для синглов, которые легли
    # плоско ДО этого фикса, а не замена раскладке.
    data["createArtistFolder"] = True
    data["createAlbumFolder"]  = True
    data["createSingleFolder"] = True
    # Keep the saved cover.jpg high-res (only set a default if unset, so a
    # user-customised value survives).
    data.setdefault("localArtworkSize", 1400)
    if lyrics is not None:
        data["lyrics"] = bool(lyrics)
    if synced_lyrics is not None:
        data["syncedLyrics"] = bool(synced_lyrics)
    try:
        cfg_file.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    except Exception as e:
        print(f"[deezer] cannot write deemix config: {e}", flush=True)


_MEDIA_BAD = "result['preview'] = track['MEDIA'][0]['HREF']"
_MEDIA_GOOD = "result['preview'] = track['MEDIA'][0]['HREF'] if track.get('MEDIA') else ''"


def _patch_media_src(src: str) -> str:
    """Add the empty-MEDIA guard to deezer-py's map_track source. Idempotent:
    returns src unchanged if already guarded (``_MEDIA_GOOD`` present) or if the
    target line isn't there (upstream changed)."""
    if _MEDIA_BAD in src and _MEDIA_GOOD not in src:
        return src.replace(_MEDIA_BAD, _MEDIA_GOOD)
    return src


def _ensure_media_patch() -> None:
    """Guard deezer-py's ``map_track`` against a track with an empty ``MEDIA`` list.

    Upstream does ``track['MEDIA'][0]['HREF']`` unguarded, so an album that contains
    ONE unavailable / no-preview track (empty ``MEDIA``) raises ``IndexError`` during
    metadata generation and kills the ENTIRE album download (issue #23). deemix runs
    as a SUBPROCESS, so an in-process monkeypatch wouldn't reach it — instead patch
    the installed vendor file on disk, idempotently. Self-healing: re-applied before
    every run, survives a ``pip install --upgrade`` of deezer-py.
    """
    try:
        import deezer.utils as _du
        p = Path(_du.__file__)
        src = p.read_text(encoding="utf-8")
        patched = _patch_media_src(src)
        if patched != src:
            p.write_text(patched, encoding="utf-8")
            print("[deezer] patched deezer-py map_track (empty-MEDIA guard, issue #23)", flush=True)
    except Exception as e:
        print(f"[deezer] media-guard patch skipped: {e}", flush=True)


def _arl_plan(arl: str = "") -> tuple[str, bool, bool]:
    """Тариф, живость и права на lossless у ARL, которым шёл ЭТОТ прогон:
    («Deezer Free», True, False).

    `arl` пустой — спросим основной из конфига (одиночная учётка, прежнее
    поведение). При включённом пуле сюда обязан прийти ARL слота: 04.09.2026
    отказал слот 3, а диагноз ставили слоту 0 — и на живую бесплатную учётку
    выдали «ARL протух, переклей токен». Диагноз не про того, кто упал, хуже
    отсутствующего: он уводит от настоящей причины.

    Живёт отдельной функцией, потому что зовут её из СИНХРОННОГО `is_finished`,
    а проверка асинхронная: `asyncio.run` при уже работающем цикле раннера
    бросил бы RuntimeError, и разбор молча свалился бы в старое «ARL протух» —
    ровно ту ошибку, ради которой всё и затевалось. Поэтому отдельный поток со
    своим циклом.

    Ошибка здесь не должна ничего ломать: не смогли спросить — вернём пусто, и
    вызывающий покажет прежнее сообщение.
    """
    out: dict = {}

    def _run() -> None:
        try:
            import asyncio
            from ripster import deezer_accounts as _da
            from ripster.credential_health import _load_raw_config
            arl_use = (arl or "").strip()
            if not arl_use:
                cfg = _load_raw_config() or {}
                arl_use = str(cfg.get("deezer-arl") or "").strip()
            if not arl_use:
                return
            loop = asyncio.new_event_loop()
            try:
                out.update(loop.run_until_complete(_da.arl_info(arl_use, fresh=True)) or {})
            finally:
                loop.close()
        except Exception:                                       # noqa: BLE001
            pass

    import threading
    t = threading.Thread(target=_run, daemon=True)
    t.start()
    t.join(timeout=12)
    return (str(out.get("plan") or ""), bool(out.get("alive")),
            bool(out.get("lossless")))


_RE_DZ_ITEM = re.compile(r"deezer\.com/(?:[a-z]{2}(?:-[a-z]{2})?/)?(track|album)/(\d+)", re.I)


def _deezer_region_lock(url: str, arl: str = "") -> dict | None:
    """Закрыт ли релиз для СТРАНЫ аккаунта: {"country","locked","total"} или None.

    Зачем: deemix при «нет в выбранном качестве» и при гео-блоке часто пишет
    одно и то же («not found at desired bitrate and no alternative found») —
    у гео-заблокированного трека в витрине аккаунта просто нет ни одного
    файла, и отдельную ошибку страны (код 2002) deemix до неё не доходит.
    Совет «выбери качество пониже» при гео-блоке не поможет никогда.

    Различающий признак даёт сам Deezer: публичный `api.deezer.com/track/{id}`
    отдаёт `available_countries` — список стран, где трек разрешён. Он НЕ
    зависит от IP сервера (в отличие от `readable`), поэтому сверяем его со
    страной ARL (`arl_info` → `country`). Только метаданные, без загрузки.

    Ничего не ломает: не смогли спросить — None, и вызывающий покажет прежнее
    сообщение про качество.
    """
    m = _RE_DZ_ITEM.search(url or "")
    if not m:
        return None
    kind, item_id = m.group(1).lower(), m.group(2)
    out: dict = {}

    def _run() -> None:
        try:
            import asyncio
            import httpx
            from ripster import deezer_accounts as _da
            from ripster.credential_health import _load_raw_config
            arl_use = (arl or "").strip()
            if not arl_use:
                arl_use = str((_load_raw_config() or {}).get("deezer-arl") or "").strip()
            if not arl_use:
                return
            loop = asyncio.new_event_loop()
            try:
                info = loop.run_until_complete(_da.arl_info(arl_use)) or {}
            finally:
                loop.close()
            country = str(info.get("country") or "").strip().upper()
            if not country:
                return
            with httpx.Client(timeout=6) as c:
                if kind == "track":
                    ids = [item_id]
                else:
                    r = c.get(f"https://api.deezer.com/album/{item_id}/tracks",
                              params={"limit": 200})
                    ids = [str(x.get("id")) for x in (r.json().get("data") or [])
                           if x.get("id")][:60]
                locked = total = 0
                for tid in ids:
                    d = c.get(f"https://api.deezer.com/track/{tid}").json()
                    ac = d.get("available_countries")
                    if not isinstance(ac, list) or not ac:
                        continue            # нет данных о странах — не считаем
                    total += 1
                    if country not in {str(x).upper() for x in ac}:
                        locked += 1
            out.update(country=country, locked=locked, total=total)
        except Exception:                                       # noqa: BLE001
            pass

    import threading
    t = threading.Thread(target=_run, daemon=True)
    t.start()
    t.join(timeout=15)
    return dict(out) if out.get("total") else None


def _region_message(country: str, locked: int, total: int) -> str:
    """Вердикт «гео-блок» тем же текстом, что и общий классификатор."""
    from .errors import classify_download_error
    hit = classify_download_error("region locked")
    region = hit[1] if hit else "недоступно в регионе твоего аккаунта (гео-блок)."
    cc = f" ({country})" if country else ""
    if total and 0 < locked < total:
        return (f"Deezer: {locked} из {total} треков закрыты для страны аккаунта{cc} — "
                f"{region} Остальные недоступны в выбранном качестве — выбери MP3 320/128.")
    return f"Deezer: релиз закрыт для страны аккаунта{cc} — {region}"


@register

class DeezerEngine(EngineBase):
    name = "deezer"
    # ARL, которым реально запустили ЭТОТ прогон (слот пула или основной).
    # `is_finished` конфига не получает, а объяснять отказ надо про упавшую
    # учётку, а не про первую в списке. Экземпляр движка свой на задачу
    # (`get_engine` → `cls()`), так что параллельные загрузки не мешают.
    _last_arl = ""
    # Ссылка этого прогона — чтобы при отказе спросить у Deezer, в каких
    # странах релиз разрешён (гео-блок vs «нет в качестве»).
    _last_url = ""

    def qualities(self) -> list[dict]:
        return [{**q, "engine": self.name} for q in _QUALITIES]

    def build_cmd(self, url: str, quality: str, config: dict) -> list[str]:
        # `deezer-arl` and `_deezer_cfg_dir` may be overridden per-task by the
        # multi-account pool dispatch (ripster/runner.py, ripster/deezer_pool.py)
        # to route this download to a specific account's isolated config dir —
        # a plain single-account setup never sets `_deezer_cfg_dir`, so this is
        # a no-op (behaves exactly as before the pool existed).
        arl      = (config.get("deezer-arl") or "").strip()
        self._last_arl = arl
        self._last_url = url
        cfg_override = config.get("_deezer_cfg_dir") or ""
        out_path = config.get("deezer-save-path") or config.get("save-path", "downloads")
        bitrate  = _BITRATE.get(quality, "3")
        # ALWAYS run deemix as a module on the SAME interpreter — never the
        # deemix.exe console-script shim and never shutil.which("deemix"). The
        # setuptools .exe shim does NOT execute under the isolated embeddable
        # Python (exits 1 with ZERO output) → the engine sees no output and lies
        # "нет 'All done!'". which() is a trap: app.py prepends <python>\Scripts to
        # PATH (so AMD can find ffmpeg), so which("deemix") SUCCEEDS in-server and
        # returns that broken shim — it's None only in a bare shell, which is why
        # standalone tests passed. deemix ships __main__, so `-m deemix` is correct.
        deemix_cmd = [app_python(), "-m", "deemix"]

        # Ensure output folder exists so deemix doesn't bail on a missing path
        try:
            Path(out_path).mkdir(parents=True, exist_ok=True)
        except Exception:
            pass

        # Pin embedded cover to 1000 px (uniform across services). Per-release
        # lyrics checkbox override (None = leave deemix's own config alone —
        # both settings stay whatever they already were, i.e. off by default).
        lyrics_ov = config.get("_lyrics_override")
        _write_deemix_config(
            lyrics=lyrics_ov, synced_lyrics=lyrics_ov, override=cfg_override,
        )
        # Heal the deemix subprocess against the empty-MEDIA IndexError (issue #23).
        _ensure_media_patch()

        # Write ARL to the location deemix reads from. deemix CLI has NO --arl flag.
        if arl:
            try:
                _write_arl(arl, override=cfg_override)
            except Exception as e:
                # Fall through — deemix will fail with a clear "login required" message
                # and is_finished() will map that to a user-visible error.
                print(f"[deezer] cannot write ARL file: {e}", flush=True)

        full_url = _expand_short_link(url)
        self._last_url = full_url or url
        return [*deemix_cmd, "--bitrate", bitrate, "--path", str(out_path),
                full_url]

    def classify_line(self, line: str) -> str:
        low = line.lower()
        # "Finished downloading" and "All done!" are endgame signals, not errors
        if ("error" in low or "failed" in low or "invalid arl" in low
                or "paste here your arl" in low or "aborted!" in low):
            return "error"
        if "completed download" in low or "finished downloading" in low or "all done" in low:
            return "success"
        return "stdout"

    def parse_progress(self, line: str, current: int, total: int) -> tuple[int, int]:
        # deemix prints "Download at 86%" per track. Treat current = percent, total = 100.
        m_pct = _RE_PERCENT.search(line)
        if m_pct:
            return int(m_pct.group(1)), 100
        # Fallback: some older deemix versions print "n/N" counters
        m_frac = _RE_TRACK.search(line)
        if m_frac:
            return int(m_frac.group(1)), int(m_frac.group(2))
        return current, total

    def is_finished(self, log_text: str, rc: int = -1) -> EngineResult:
        low = log_text.lower()
        tracks_ok = len(_RE_TRACK_DONE.findall(log_text))
        # ARL missing OR invalid/expired: deemix VALIDATES the .arl file and, if it's
        # absent or rejected, falls back to an interactive prompt ("Paste here your
        # arl:") which immediately aborts under our stdin=DEVNULL subprocess →
        # "Aborted!" with 0 tracks. Both wordings are the SAME root cause and the old
        # catch-all hid it behind "no 'All done'". Surface the real fix instead.
        if tracks_ok == 0 and ("paste here your arl" in low or "aborted!" in low):
            # СПРАШИВАЕМ САМ DEEZER, прежде чем обвинять токен.
            #
            # Та же строка deemix («Paste here your arl» / «Aborted!») выходит и
            # когда ARL мёртв, и когда он ЖИВОЙ, но на аккаунте нет подписки —
            # а качество запрошено платное. Мы говорили «протух» в обоих случаях,
            # и 04.09.2026 гость получил совет переклеивать токен, который был
            # исправен: обе учётки владельца оказались Deezer Free. Совет был не
            # просто бесполезен — он уводил от настоящей причины.
            plan, alive, lossless = _arl_plan(self._last_arl)
            if alive and lossless:
                # Deezer говорит «учётка платная, lossless разрешён», а deemix
                # всё равно спросил ARL. Значит дело не в подписке и не в самом
                # токене: до deemix он не доехал (изолированный конфиг слота,
                # файл `.arl`) либо был отвергнут при входе. Совет «переклей
                # токен» здесь заведомо ложный — 04.09.2026 его получили три
                # раза подряд на живых учётках.
                return EngineResult(
                    False,
                    error=f"Deezer: учётка живая и с подпиской ({plan or 'тариф неизвестен'}), "
                          f"но deemix её не принял — токен либо не доехал до его конфига "
                          f"(слот пула, файл .arl), либо отвергнут при входе. Менять ARL "
                          f"незачем: повтори загрузку; если повторяется — проверь слоты "
                          f"Deezer в настройках.",
                )
            # Тариф читаем ТОЛЬКО как право на lossless (`web_lossless` от самого
            # Deezer). Проверка на слова «premium»/«hifi» в названии врала:
            # Deezer Family — платный тариф с lossless, и его назвали бы
            # «учётка без подписки».
            if alive and plan:
                return EngineResult(
                    False,
                    error=f"Deezer: у этой учётки нет прав на платные качества ({plan}) — "
                          f"FLAC/320 ей недоступны. ARL живой, менять его незачем: "
                          f"нужен ARL аккаунта с платной подпиской, либо выбери качество, "
                          f"доступное бесплатному аккаунту.",
                )
            return EngineResult(
                False,
                error="Deezer: ARL не задан или протух. Открой deezer.com в браузере → "
                      "DevTools → Application → Cookies → скопируй значение cookie `arl` и "
                      "вставь в Настройки → Deezer. Для FLAC/320 нужен ARL от Premium-аккаунта.",
            )
        # Bitrate gate: deemix prints "All done!" with NO audio file when the track
        # isn't available in the requested bitrate. Two distinct wordings:
        #   • "can't stream the track at the desired bitrate"  → free/expired ARL
        #   • "track not found at desired bitrate and no alternative found" → the
        #     release simply has no FLAC/320 master (common on niche/regional
        #     catalogues). Both end in 0 saved tracks. NOTE: the second wording
        #     contains "not found", which the shared GONE classifier would wrongly
        #     read as a dead/phantom link — so we return a SPECIFIC message here
        #     (the runner only re-classifies generic errors), and the wording below
        #     deliberately avoids "not found"/"removed".
        #   • "can't stream the track from your current country" → deemix САМ
        #     опознал гео-блок (media API, код 2002). Раньше эта строка
        #     проваливалась в ветку качества ниже — она тоже кончается на
        #     "no alternative found", — и человеку советовали понизить качество.
        if tracks_ok == 0 and "from your current country" in low:
            v = _deezer_region_lock(self._last_url, self._last_arl) or {}
            return EngineResult(False, error=_region_message(
                v.get("country", ""), v.get("locked", 0), v.get("total", 0)))
        bitrate_blocked = (
            "can't stream the track at the desired bitrate" in low
            or "not found at desired bitrate" in low
            or "no alternative found" in low
        )
        if bitrate_blocked and tracks_ok == 0:
            # У гео-заблокированного трека в витрине аккаунта нет ни одного
            # файла, и deemix пишет ровно ту же фразу про качество. Различает
            # только сам Deezer — список стран трека против страны ARL.
            # «can't stream at the desired bitrate» — это тариф, не регион.
            if "can't stream the track at the desired bitrate" not in low:
                v = _deezer_region_lock(self._last_url, self._last_arl)
                if v and v.get("locked"):
                    return EngineResult(False, error=_region_message(
                        v["country"], v["locked"], v["total"]))
            return EngineResult(
                False,
                error="Deezer: трек недоступен в выбранном качестве "
                      "(нет FLAC/320 на источнике) — выбери MP3 320/128 или поищи "
                      "тот же релиз на другом сервисе. Если ARL стал free — обнови "
                      "его в Settings → Deezer.",
            )
        # Success first: "All done!" is deemix's end-of-run marker.
        if "all done" in low or _RE_DONE.search(low):
            if tracks_ok == 0:
                # "All done" but nothing was actually saved — report failure
                return EngineResult(False, error="Deezer: ни один трек не скачался (проверь ARL и качество)")
            return EngineResult(True, tracks_ok=tracks_ok)
        # ARL / login failures get a dedicated message
        if _RE_ARL.search(low):
            return EngineResult(False, error="Неверный ARL — проверь Settings → Deezer")
        # Any other error — check for a real Python traceback first (an
        # unhandled exception in deemix/deezer-py) so we surface the actual
        # "ExceptionType: message" line rather than whatever a plain
        # substring search happens to land on first while scanning backwards
        # (see ripster/engines/amazon.py for the concrete bug this class of
        # check fixes — confirmed live on a guest's failed download).
        from .errors import extract_traceback_summary
        tb = extract_traceback_summary(log_text)
        if tb:
            return EngineResult(False, error=f"Deezer: {tb}")
        last_err = ""
        for line in reversed(log_text.splitlines()):
            if "error" in line.lower() or "failed" in line.lower():
                last_err = line.strip()[:200]
                break
        if last_err:
            return EngineResult(False, error=last_err)
        # Unexpected exit with no recognised marker. Attach the real last lines +
        # exit code so telemetry shows WHAT happened instead of a bare "no All done"
        # (e.g. a network ConnectError, a killed/terminated process, or a deemix crash).
        _noise = re.compile(r'^\s*$|Logging in|Found|deemix', re.I)
        _tail = [l.strip() for l in log_text.splitlines()
                 if l.strip() and not _noise.search(l)][-3:]
        _tail_s = (" | deemix: " + " ⏎ ".join(_tail)) if _tail else ""
        _rc_s = f" [rc={rc}]" if rc not in (-1, 0) else ""
        return EngineResult(
            False,
            error="Deezer: неожиданное завершение (нет 'All done!' в логе) — обычно сетевой "
                  "обрыв или протухший ARL, повтори; если повторяется — обнови ARL."
                  + _rc_s + _tail_s,
        )

    # ── приватный поиск по КАТАЛОГУ АККАУНТА (gw-light) ────────────────────
    # `api.deezer.com/search` геолоцирует по IP СЕРВЕРА, а не по стране ARL —
    # поэтому «через рипстер с турецким ARL не находит релиз, а Мурглар с
    # британским находит». `deezer.pageSearch` (gw-light, под ARL) отдаёт
    # выдачу в витрине аккаунта. Порт логики мобильного `DeezerGw.kt`.
    _GW_URL = "https://www.deezer.com/ajax/gw-light.php"

    async def _gw_search(self, query: str, ep: str, limit: int, arl: str) -> list[dict]:
        import httpx as _httpx
        base = {"api_version": "1.0", "input": "3", "api_token": ""}
        try:
            async with _httpx.AsyncClient(timeout=15, follow_redirects=True) as c:
                ud = await c.post(self._GW_URL, params={**base, "method": "deezer.getUserData"},
                                  cookies={"arl": arl}, json={})
                if ud.status_code != 200:
                    return []
                res = (ud.json() or {}).get("results") or {}
                tok = res.get("checkForm") or ""
                if not tok or str((res.get("USER") or {}).get("USER_ID") or "0") == "0":
                    return []           # ARL мёртв / гостевая сессия — молча в фолбэк
                sid = ud.cookies.get("sid") or c.cookies.get("sid") or ""
                key = {"track": "TRACK", "album": "ALBUM", "artist": "ARTIST"}.get(ep, "ALBUM")
                body = {"query": query, "start": 0, "nb": max(limit, 20),
                        "filter": "ALL", "output": "ALL", "top_tracks": True}
                sr = await c.post(self._GW_URL,
                                  params={**base, "api_token": tok, "method": "deezer.pageSearch"},
                                  cookies={"arl": arl, **({"sid": sid} if sid else {})}, json=body)
                if sr.status_code != 200:
                    return []
                block = ((sr.json() or {}).get("results") or {}).get(key) or {}
                rows = block.get("data") or []
        except Exception:
            return []

        def _cover(md5: str, kind: str) -> str:
            return (f"https://e-cdns-images.dzcdn.net/images/{kind}/{md5}/500x500-000000-80-0-0.jpg"
                    if md5 else "")

        out = []
        for it in rows[:max(limit, 20)]:
            if key == "TRACK":
                d = str(it.get("DIGITAL_RELEASE_DATE") or it.get("PHYSICAL_RELEASE_DATE") or "")
                out.append({
                    "id": str(it.get("SNG_ID", "")), "title": it.get("SNG_TITLE", ""),
                    "artist": it.get("ART_NAME", ""), "artist_id": str(it.get("ART_ID", "")),
                    "type": "track", "url": f"https://www.deezer.com/track/{it.get('SNG_ID','')}",
                    "cover": _cover(it.get("ALB_PICTURE", ""), "cover"),
                    "year": d[:4], "date": d, "service": "deezer",
                })
            elif key == "ALBUM":
                d = str(it.get("ORIGINAL_RELEASE_DATE") or it.get("PHYSICAL_RELEASE_DATE") or "")
                out.append({
                    "id": str(it.get("ALB_ID", "")), "title": it.get("ALB_TITLE", ""),
                    "artist": it.get("ART_NAME", ""), "artist_id": str(it.get("ART_ID", "")),
                    "type": "album", "url": f"https://www.deezer.com/album/{it.get('ALB_ID','')}",
                    "cover": _cover(it.get("ALB_PICTURE", ""), "cover"),
                    "year": d[:4], "date": d, "label": it.get("LABEL_NAME", ""),
                    "tracks": it.get("NUMBER_TRACK"), "service": "deezer",
                })
            else:  # ARTIST
                out.append({
                    "id": str(it.get("ART_ID", "")), "title": it.get("ART_NAME", ""),
                    "artist": it.get("ART_NAME", ""), "type": "artist",
                    "url": f"https://www.deezer.com/artist/{it.get('ART_ID','')}",
                    "cover": _cover(it.get("ART_PICTURE", ""), "artist"), "service": "deezer",
                })
        return out

    async def search(self, query: str, search_type: str, limit: int, config: dict) -> list[dict]:
        import httpx as _httpx
        import asyncio as _aio
        ent_map = {"album": "album", "track": "track", "artist": "artist", "playlist": "playlist"}
        ep = ent_map.get(search_type, "album")
        arl = (config.get("deezer-arl") or "").strip()
        gw_rows: list[dict] = await self._gw_search(query, ep, limit, arl) if arl else []
        try:
            async with _HTTP.ashared() as c:
                r = await c.get(f"https://api.deezer.com/search/{ep}",
                                params={"q": query, "limit": limit})
                data = r.json()
                # /search/album doesn't return release_date — sort by date won't
                # work without it. Hydrate via parallel /album/{id} fetches.
                album_dates: dict[str, str] = {}
                album_labels: dict[str, str] = {}
                # /search/{album,track} don't return release_date, so "sort by
                # newest" can't work without it. Hydrate dates via parallel
                # /album/{id} fetches — for albums by their own id, for tracks by
                # their (de-duplicated) album ids.
                if ep == "album":
                    ids = [str(it.get("id")) for it in (data.get("data") or []) if it.get("id")]
                elif ep == "track":
                    ids = list(dict.fromkeys(
                        str((it.get("album") or {}).get("id"))
                        for it in (data.get("data") or [])
                        if (it.get("album") or {}).get("id")))
                else:
                    ids = []
                if ids:
                    async def _detail(aid: str):
                        try:
                            rr = await c.get(f"https://api.deezer.com/album/{aid}")
                            if rr.status_code == 200:
                                j = rr.json() or {}
                                return aid, j.get("release_date", "") or "", j.get("label", "") or ""
                        except Exception:
                            pass
                        return aid, "", ""
                    for aid, dt, lbl in await _aio.gather(*[_detail(i) for i in ids]):
                        if dt: album_dates[aid] = dt
                        if lbl: album_labels[aid] = lbl
            results = []
            for item in data.get("data") or []:
                if ep == "album":
                    aid = str(item.get("id", ""))
                    date = album_dates.get(aid) or item.get("release_date", "") or ""
                    label = album_labels.get(aid) or item.get("label", "") or ""
                    results.append({
                        "id":      aid,
                        "title":   item.get("title", ""),
                        "artist":  (item.get("artist") or {}).get("name", ""),
                        "artist_id": str((item.get("artist") or {}).get("id", "")),
                        "type":    search_type,
                        "url":     item.get("link", ""),
                        # Small cover for the search grid — faster + far less
                        # traffic than cover_xl (~1000px). The downloader fetches
                        # full-size art separately, so this only affects display.
                        "cover":   (item.get("cover_medium") or item.get("cover_big")
                                    or item.get("cover", "")),
                        "year":    date[:4],
                        "date":    date,
                        "label":   label,
                        "tracks":  item.get("nb_tracks"),
                        "service": "deezer",
                    })
                elif ep == "track":
                    alb = item.get("album") or {}
                    t_date = album_dates.get(str(alb.get("id", "")), "") or ""
                    results.append({
                        "id":      str(item.get("id", "")),
                        "title":   item.get("title", ""),
                        "artist":  (item.get("artist") or {}).get("name", ""),
                        "artist_id": str((item.get("artist") or {}).get("id", "")),
                        "type":    search_type,
                        "url":     item.get("link", ""),
                        "cover":   (alb.get("cover_medium") or alb.get("cover_big")
                                    or alb.get("cover", "")),
                        "year":    t_date[:4],
                        "date":    t_date,
                        "service": "deezer",
                    })
                elif ep == "artist":
                    results.append({
                        "id":      str(item.get("id", "")),
                        "title":   item.get("name", ""),
                        "artist":  item.get("name", ""),
                        "type":    search_type,
                        "url":     item.get("link", ""),
                        "cover":   item.get("picture_medium", ""),
                        "service": "deezer",
                    })
            # Каталог аккаунта (gw) — ВПЕРЁД, публичная выдача добивает хвост
            # тем, чего в аккаунтной не было. Дедуп по id.
            if gw_rows:
                seen = {r["id"] for r in gw_rows if r.get("id")}
                merged = list(gw_rows)
                for r in results:
                    if r.get("id") and r["id"] not in seen:
                        merged.append(r); seen.add(r["id"])
                return merged[:max(limit, 20)]
            return results
        except Exception:
            return gw_rows or []

    async def get_artist(self, artist_id: str, types: str, config: dict) -> dict:
        import httpx as _httpx
        import asyncio as _aio
        wanted = {t.strip() for t in types.split(",") if t.strip()}
        try:
            async with _HTTP.ashared() as c:
                info_r = await c.get(f"https://api.deezer.com/artist/{artist_id}")
                info = info_r.json()
                if info.get("error"):
                    return {"error": info["error"].get("message", "Deezer error"), "releases": []}
                releases = []
                next_url: str | None = f"https://api.deezer.com/artist/{artist_id}/albums?limit=100"
                while next_url and len(releases) < 200:
                    r = await c.get(next_url)
                    data = r.json()
                    for a in data.get("data", []):
                        rec_type = a.get("record_type", "album")
                        if rec_type == "compile":
                            rec_type = "compilation"
                        releases.append({
                            "id":      str(a.get("id", "")),
                            "title":   a.get("title", ""),
                            "cover":   a.get("cover_medium", "") or a.get("cover", ""),
                            "year":    (a.get("release_date", "") or "")[:4],
                            "date":    a.get("release_date", ""),
                            "tracks":  a.get("nb_tracks"),
                            "type":    rec_type,
                            "url":     a.get("link", ""),
                            "explicit":a.get("explicit_lyrics", False),
                            "service": "deezer",
                        })
                    next_url = data.get("next")

                # «Сборник» в дискографии = чужой релиз, куда попал трек артиста.
                # `/artist/{id}/albums` не говорит НИ чей это релиз, НИ какой
                # именно трек — дотягиваем это одним `/album/{id}` на сборник
                # (их обычно единицы), чтобы показать честно: «разные артисты ·
                # трек: …», а не «Артист — Сборник» как будто это его альбом.
                comp_ids = [r["id"] for r in releases if r["type"] == "compilation" and r["id"]]
                if comp_ids:
                    aname = (info.get("name") or "").strip().lower()

                    async def _comp(aid: str):
                        try:
                            rr = await c.get(f"https://api.deezer.com/album/{aid}")
                            if rr.status_code != 200:
                                return aid, "", ""
                            j = rr.json() or {}
                            va = (j.get("artist") or {}).get("name", "") or ""
                            mine = [
                                t.get("title", "")
                                for t in ((j.get("tracks") or {}).get("data") or [])
                                if (t.get("artist") or {}).get("name", "").strip().lower() == aname
                                or str((t.get("artist") or {}).get("id", "")) == str(artist_id)
                            ]
                            return aid, va, "; ".join(dict.fromkeys(t for t in mine if t))
                        except Exception:
                            return aid, "", ""

                    got = {aid: (va, ap) for aid, va, ap in
                           await _aio.gather(*[_comp(i) for i in comp_ids])}
                    for r in releases:
                        if r["id"] in got:
                            va, ap = got[r["id"]]
                            if va:
                                r["album_artist"] = va
                            if ap:
                                r["appears_as"] = ap

            if wanted and wanted != {"all"}:
                releases = [r for r in releases if r["type"] in wanted]
            releases.sort(key=lambda r: r.get("date", ""), reverse=True)
            return {
                "artist": {
                    "id":      str(info.get("id", "")),
                    "name":    info.get("name", ""),
                    "picture": info.get("picture_xl", "") or info.get("picture_big", ""),
                    "fans":    info.get("nb_fan"),
                    "albums_total": info.get("nb_album"),
                    "url":     info.get("link", ""),
                    "service": "deezer",
                },
                "releases": releases,
            }
        except Exception as e:
            return {"error": str(e), "releases": []}

    async def get_album(self, album_id: str, config: dict) -> dict:
        import httpx as _httpx
        try:
            async with _HTTP.ashared() as c:
                r = await c.get(f"https://api.deezer.com/album/{album_id}")
                a = r.json()
                if a.get("error"):
                    return {"error": a["error"].get("message", "Deezer error")}
                # The EMBEDDED tracklist (a.tracks.data) omits track_position +
                # disk_number, so multi-disc albums looked single-disc. The
                # dedicated /album/{id}/tracks endpoint includes both — page
                # through it so disc + per-disc numbering are correct.
                raw, index = [], 0
                while True:
                    rt = await c.get(f"https://api.deezer.com/album/{album_id}/tracks",
                                     params={"limit": 100, "index": index})
                    jt = rt.json()
                    data = jt.get("data", []) or []
                    raw.extend(data)
                    if not data or not jt.get("next") or index > 2000:
                        break
                    index += len(data)
            # Fallback to the embedded list if the tracks endpoint returned nothing.
            if not raw:
                raw = (a.get("tracks") or {}).get("data", [])
            tracks = []
            for t in raw:
                tracks.append({
                    "id":       str(t.get("id", "")),
                    "title":    t.get("title", ""),
                    "artist":   (t.get("artist") or {}).get("name", ""),
                    "duration": t.get("duration"),
                    "track_no": t.get("track_position"),
                    "disc":     t.get("disk_number"),
                    "preview":  t.get("preview", ""),
                    "explicit": t.get("explicit_lyrics", False),
                    "url":      t.get("link", ""),
                })
            dz_id = str(a.get("id", ""))
            return {
                "album": {
                    "id":     dz_id,
                    "title":  a.get("title", ""),
                    "artist": (a.get("artist") or {}).get("name", ""),
                    "cover":  a.get("cover_xl", "") or a.get("cover_big", ""),
                    "year":   (a.get("release_date", "") or "")[:4],
                    "date":   a.get("release_date", ""),
                    "label":  a.get("label", ""),
                    "upc":    a.get("upc", ""),
                    "genre":  ", ".join(
                        g.get("name", "") for g in ((a.get("genres") or {}).get("data", []) or [])
                    ),
                    "tracks": a.get("nb_tracks"),
                    "url":    a.get("link", "") or f"https://www.deezer.com/album/{dz_id}",
                    "service":"deezer",
                },
                "tracks": tracks,
            }
        except Exception as e:
            return {"error": str(e)}
