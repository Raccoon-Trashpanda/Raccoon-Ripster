"""
ConfigService — typed, schema-aware wrapper over the config dict.

Wraps the mutable config dict by reference so mutations are visible
everywhere immediately.  Implements the full MutableMapping protocol so
every existing .get() / [] / in / update() call continues to work unchanged.

New code can use typed @property accessors instead of magic strings:
    cfg.qobuz_auth_token   → str   (no scattered default="")
    cfg.amd_parallel       → int
    cfg.embed_cover        → bool

save_config in app.py must dump cfg._data (not cfg itself) to avoid
YAML tagging the wrapper object:
    yaml.dump(cfg._data, f, ...)
"""
from __future__ import annotations

from typing import Any, Iterator

_UNSET = object()


# ── Property factories ────────────────────────────────────────────────────────

def _s(key: str, default: str = "") -> property:
    def _get(self) -> str:
        v = self._data.get(key, default)
        return str(v) if v is not None else default
    return property(_get)


def _i(key: str, default: int = 0) -> property:
    def _get(self) -> int:
        try:
            return int(self._data.get(key, default))
        except (ValueError, TypeError):
            return default
    return property(_get)


def _b(key: str, default: bool = False) -> property:
    def _get(self) -> bool:
        v = self._data.get(key, default)
        if isinstance(v, bool):
            return v
        if isinstance(v, int):
            return bool(v)
        return str(v).lower() in ("true", "1", "yes", "on")
    return property(_get)


# ── ConfigService ─────────────────────────────────────────────────────────────

class ConfigService:
    """Typed view over the mutable config dict.

    Usage:
        config = ConfigService(load_config())   # wrap existing dict
        config.qobuz_auth_token                 # → str, no default needed
        config["qobuz-auth-token"]              # still works
        config.get("qobuz-auth-token", "")      # still works
    """

    __slots__ = ("_data",)

    def __init__(self, data: dict) -> None:
        object.__setattr__(self, "_data", data)

    # ── MutableMapping protocol ───────────────────────────────────────────────

    def reload(self, config_file: "_Path", tokens_dir: "_Path") -> int:
        """Перечитать конфиг с диска В ТОТ ЖЕ словарь. Возвращает число ключей.

        Зачем: приложение держит конфиг в памяти и сохраняет его ЦЕЛИКОМ, а
        внешние правки (сторож снял мёртвую учётку, автозамена поставила
        основной другую) идут прямо в файл. Без перечитывания первая же запись
        из памяти вернула бы всё обратно — правка выглядела бы сделанной и
        молча откатывалась (12.09.2026, автозамена учётки Tidal).

        Обновляем НА МЕСТЕ, а не подменяем объект: ссылку на этот словарь уже
        держат маршруты и движки, и подмена оставила бы их со старой копией.
        """
        fresh = load_config(config_file, tokens_dir)
        if not isinstance(fresh, dict) or not fresh:
            return 0
        d = self._data
        # Ключи, начинающиеся с подчёркивания, ставит само приложение в
        # рантайме (`_release_version` и подобные) — их в файле нет, и терять
        # их нельзя.
        runtime = {k: v for k, v in d.items() if str(k).startswith("_")}
        d.clear()
        d.update(fresh)
        d.update(runtime)
        return len(d)

    def get(self, key: str, default: Any = _UNSET) -> Any:
        if default is _UNSET:
            return self._data.get(key)
        return self._data.get(key, default)

    def __getitem__(self, key: str) -> Any:
        return self._data[key]

    def __setitem__(self, key: str, value: Any) -> None:
        self._data[key] = value

    def __delitem__(self, key: str) -> None:
        del self._data[key]

    def __contains__(self, key: object) -> bool:
        return key in self._data

    def __iter__(self) -> Iterator[str]:
        return iter(self._data)

    def __len__(self) -> int:
        return len(self._data)

    def keys(self):
        return self._data.keys()

    def values(self):
        return self._data.values()

    def items(self):
        return self._data.items()

    def update(self, other=(), **kw) -> None:
        self._data.update(other, **kw)

    def setdefault(self, key: str, default: Any = None) -> Any:
        return self._data.setdefault(key, default)

    def pop(self, key: str, *args) -> Any:
        return self._data.pop(key, *args)

    def copy(self) -> dict:
        return self._data.copy()

    def __repr__(self) -> str:
        return f"ConfigService({len(self._data)} keys)"

    # ── Core / Apple Music ────────────────────────────────────────────────────

    media_user_token    = _s("media-user-token")
    authorization_token = _s("authorization-token")
    engine              = _s("engine",  "zhaarey")
    quality             = _s("quality", "alac")
    storefront          = _s("storefront", "us")
    language            = _s("language",   "en-US")
    save_path           = _s("save-path",  "")
    wrapper_mode        = _s("wrapper-mode",  "docker-remote")
    decrypt_port        = _s("decrypt-port",  "127.0.0.1:10020")
    m3u8_port           = _s("m3u8-port",     "127.0.0.1:20020")
    atmos_max           = _i("atmos-max",  2448)
    max_memory          = _i("max-memory", 256)
    use_go_run          = _b("use-go-run", True)

    # ── AMD ───────────────────────────────────────────────────────────────────

    amd_dir             = _s("amd-dir",           "")
    amd_instance_url    = _s("amd-instance-url",  "wm.wol.moe")
    amd_instance_secure = _b("amd-instance-secure", True)
    amd_parallel        = _i("amd-parallel",      8)
    amd_save_lyrics     = _b("amd-save-lyrics",   True)
    amd_lyrics_format   = _s("amd-lyrics-format", "lrc")
    amd_codec_alt       = _b("amd-codec-alt",     True)

    # ── gamdl ─────────────────────────────────────────────────────────────────

    gamdl_cookies_path         = _s("gamdl-cookies-path",        "")
    gamdl_use_wrapper          = _b("gamdl-use-wrapper",         False)
    gamdl_wrapper_account_url  = _s("gamdl-wrapper-account-url", "http://127.0.0.1:30020")
    gamdl_download_mode        = _s("gamdl-download-mode",       "ytdlp")
    gamdl_song_codec           = _s("gamdl-song-codec",          "alac")
    gamdl_overwrite            = _b("gamdl-overwrite",           False)
    gamdl_save_playlist        = _b("gamdl-save-playlist",       False)
    gamdl_synced_lyrics_format = _s("gamdl-synced-lyrics-format","lrc")
    gamdl_no_synced_lyrics     = _b("gamdl-no-synced-lyrics",    False)
    gamdl_lyrics_only          = _b("gamdl-lyrics-only",         False)
    gamdl_use_album_date       = _b("gamdl-use-album-date",      False)
    gamdl_fetch_extra_tags     = _b("gamdl-fetch-extra-tags",    False)
    gamdl_artist_auto_select   = _b("gamdl-artist-auto-select",  False)
    gamdl_mv_remux_mode        = _s("gamdl-mv-remux-mode",       "ffmpeg")
    gamdl_mv_resolution        = _s("gamdl-mv-resolution",       "1080p")
    gamdl_cover_size           = _i("gamdl-cover-size",          1200)
    gamdl_cover_format         = _s("gamdl-cover-format",        "jpg")
    gamdl_album_template       = _s("gamdl-album-template",      "{album_artist}/{album}")
    gamdl_file_template        = _s("gamdl-file-template",       "{track:02d} {title}")
    gamdl_exclude_tags         = _s("gamdl-exclude-tags",        "")
    gamdl_truncate             = _i("gamdl-truncate",            100)

    # ── Cover / embed ─────────────────────────────────────────────────────────

    embed_cover          = _b("embed-cover",          True)
    cover_size           = _s("cover-size",           "3000x3000")
    cover_format         = _s("cover-format",         "jpg")
    save_cover_to_folder = _b("save-cover-to-folder", True)
    embed_lrc            = _b("embed-lrc",            True)
    save_lrc_file        = _b("save-lrc-file",        False)
    lrc_type             = _s("lrc-type",             "lyrics")
    lrc_format           = _s("lrc-format",           "lrc")

    # ── Qobuz ─────────────────────────────────────────────────────────────────

    qobuz_user_id    = _s("qobuz-user-id",    "")
    qobuz_auth_token = _s("qobuz-auth-token", "")
    qobuz_app_id     = _s("qobuz-app-id",     "")
    qobuz_secrets    = _s("qobuz-secrets",    "")
    qobuz_email      = _s("qobuz-email",      "")
    qobuz_password   = _s("qobuz-password",   "")
    qobuz_quality    = _s("qobuz-quality",    "27")
    qobuz_save_path  = _s("qobuz-save-path",  "")

    # ── Deezer ────────────────────────────────────────────────────────────────

    deezer_arl       = _s("deezer-arl",       "")
    deezer_quality   = _s("deezer-quality",   "flac")
    deezer_save_path = _s("deezer-save-path", "")

    # ── Tidal ─────────────────────────────────────────────────────────────────

    tidal_token        = _s("tidal-token",        "")
    tidal_refresh      = _s("tidal-refresh",      "")
    tidal_user_id      = _s("tidal-user-id",      "")
    tidal_country      = _s("tidal-country",      "RU")
    tidal_token_expiry = _s("tidal-token-expiry", "")
    tidal_quality      = _s("tidal-quality",      "lossless")
    tidal_save_path    = _s("tidal-save-path",    "")

    # ── Spotify ───────────────────────────────────────────────────────────────

    spotify_client_id      = _s("spotify-client-id",      "")
    spotify_client_secret  = _s("spotify-client-secret",  "")
    spotify_sp_dc          = _s("spotify-sp-dc",          "")
    spotify_engine         = _s("spotify-engine",         "convert")
    orpheus_quality        = _s("orpheus-quality",        "hifi")
    orpheus_save_path      = _s("orpheus-save-path",      "")

    # ── Beatport ──────────────────────────────────────────────────────────────

    beatport_username  = _s("beatport-username",  "")
    beatport_password  = _s("beatport-password",  "")
    beatport_quality   = _s("beatport-quality",   "hifi")
    beatport_save_path = _s("beatport-save-path", "")

    # ── SoundCloud ────────────────────────────────────────────────────────────

    soundcloud_oauth_token = _s("soundcloud-oauth-token", "")
    soundcloud_hq          = _b("soundcloud-hq",          False)
    soundcloud_save_path   = _s("soundcloud-save-path",   "")

    # ── Remote / queue ────────────────────────────────────────────────────────

    public_url      = _s("public-url",      "")
    remote_enabled  = _b("remote-enabled",  False)
    queue_autostart = _b("queue-autostart", True)
    ngrok_auto      = _b("ngrok-auto",      True)


# ── Default config ────────────────────────────────────────────────────────────

DEFAULT_CONFIG: dict = {
    "media-user-token": "",
    "authorization-token": "",
    "storefront": "us",
    "language": "en-US",
    "engine": "amd",
    "quality": "alac",
    # GitHub repo for in-app self-update (Setup tab → check/apply update).
    "ripster-repo": "Raccoon-Trashpanda/Raccoon-Ripster",
    # Optional PAT — only needed to self-update from a PRIVATE repo (public works
    # token-less). Ships empty in the distributable; owner sets it locally.
    "ripster-repo-token": "",
    # ── AMD v2 (AppleMusicDecrypt) ───────────────────────────────────────────
    "amd-dir":           "",
    "amd-instance-url":  "wm.wol.moe",
    # wm.wol.moe с 10.09.2026 требует API-ключ (@wm_auth_bot → /newkey). Секрет:
    # пишется штатным конфиг-райтером и полем Настроек, гостям маскируется
    # (ripster/routes/core.py::_SECRET_KEYS), в логи/тесты/отчёты не попадает.
    "amd-wm-api-key":    "",
    # Публичный пул обслуживает столько витрин, сколько стран у волонтёров
    # (06.09.2026 — тринадцать, нашей `us` среди них нет, `nz` есть). Ссылку
    # переводим в обслуживаемую витрину; выключается этим ключом, порядок
    # витрин задаётся следующим.
    "amd-region-rewrite": True,
    "amd-region-preference": ["nz", "jp", "it", "tw", "kr", "sg", "my", "th"],
    "amd-instance-secure": True,
    "amd-parallel":      8,
    "amd-save-lyrics":   True,
    "amd-lyrics-format": "lrc",
    "amd-codec-alt":     True,
    # zhaarey (local wrapper) — parallel track downloads within one album
    "apple-parallel-tracks": False,
    "apple-parallel-count":  4,
    # Солёмка для уникального `-I` device-info каждой учётки wrapper'а
    # (ripster/wrapper_device_info). Пишется штатным конфиг-райтером, наружу не
    # печатается. Пустая — отпечатки всё равно уникальны по id аккаунта.
    "apple-device-salt":     "",
    # ── Пейсинг против бана (ripster/pacing.py) ───────────────────────────────
    # Мягкие потолки НАШИХ запросов к сервису: частота — то, за что отвечают
    # 429/403, суточный объём — то, за что банят учётку. Ноль = потолок снят.
    "apple-requests-per-hour":  1500,
    "apple-requests-per-day":   15000,
    "qobuz-playlist-per-hour":  120,
    "qobuz-playlist-per-day":   1200,
    # ── Qobuz ───────────────────────────────────────────────────────────────
    "qobuz-user-id":    "",
    "qobuz-auth-token": "",
    "qobuz-app-id":     "",
    "qobuz-secrets":    "",
    "qobuz-email":      "",
    "qobuz-password":   "",
    "qobuz-quality":    "27",
    "qobuz-save-path":  "",
    # ── Deezer ──────────────────────────────────────────────────────────────
    "deezer-arl":       "",
    "deezer-quality":   "flac",
    "deezer-save-path": "",
    # ── Tidal ───────────────────────────────────────────────────────────────
    "tidal-token":        "",
    "tidal-refresh":      "",
    "tidal-user-id":      "",
    "tidal-country":      "RU",
    "tidal-token-expiry": "",
    "tidal-quality":      "lossless",
    "tidal-save-path":    "",
    # ── Spotify ─────────────────────────────────────────────────────────────
    "spotify-client-id":       "",
    "spotify-client-secret":   "",
    "spotify-sp-dc":           "",
    # ── Remote access ───────────────────────────────────────────────────────
    "public-url": "",
    "remote-enabled": False,
    # ── Queue ────────────────────────────────────────────────────────────────
    "queue-autostart": True,
    # ── Cover art ────────────────────────────────────────────────────────────
    "cover-max-px":     3000,
    "cover-no-upscale": True,
    # ── gamdl ────────────────────────────────────────────────────────────────
    "gamdl-cookies-path":         "",
    "gamdl-use-wrapper":          False,
    "gamdl-wrapper-account-url":  "http://127.0.0.1:30020",
    "gamdl-download-mode":        "ytdlp",
    "gamdl-nm3u8dlre-path":       "N_m3u8DL-RE",
    "gamdl-ffmpeg-path":          "ffmpeg",
    "gamdl-song-codec":           "alac",
    "gamdl-overwrite":            False,
    "gamdl-save-playlist":        False,
    "gamdl-synced-lyrics-format": "lrc",
    "gamdl-no-synced-lyrics":     False,
    "gamdl-lyrics-only":          False,
    "gamdl-use-album-date":       False,
    "gamdl-fetch-extra-tags":     False,
    "gamdl-artist-auto-select":   False,
    "gamdl-mv-remux-mode":        "ffmpeg",
    "gamdl-mv-resolution":        "1080p",
    "gamdl-cover-size":           1200,
    "gamdl-cover-format":         "jpg",
    "gamdl-album-template":       "{album_artist}/{album}",
    "gamdl-file-template":        "{track:02d} {title}",
    "gamdl-exclude-tags":         "",
    "gamdl-truncate":             100,
    "embed-cover": True,
    "cover-size": "3000x3000",
    "cover-format": "jpg",
    "save-cover-to-folder": True,
    "embed-lrc": True,
    "save-lrc-file": False,
    "lrc-type": "lyrics",
    "lrc-format": "lrc",
    "save-path": "downloads",
    "atmos-path": "downloads/Atmos",
    "aac-path": "downloads/AAC",
    "wrapper-mode": "docker-remote",
    "decrypt-port": "127.0.0.1:10020",
    "m3u8-port": "127.0.0.1:20020",
    # ── Wrapper Lite (второй локальный бэкенд; apple-wrapper=lite включает) ──
    # key-сервер стоит в контейнере ripster-wrapper-lite, публикуется СТРОГО на
    # петлю (API без авторизации — аудит docs/WRAPPER_LITE_AUDIT_2026-09-24.md).
    # Порты ниже — наш TCP-оракул для Go-загрузчика (ripster/lite_shim.py),
    # тоже только 127.0.0.1. По умолчанию движок ВЫКЛЮЧЕН: Lite выбирается
    # владельцем вручную, как и публичный пул.
    "apple-lite-url": "http://127.0.0.1:12340",
    "apple-lite-decrypt-port": "127.0.0.1:12345",
    "apple-lite-m3u8-port": "127.0.0.1:12346",
    "max-memory": 256,
    "downloader-path": "apple-music-downloader",
    "use-go-run": True,
    "main-go-path": "main.go",
    "atmos-max": 2448,
    # ── Spotify download engine ──────────────────────────────────────────────
    "spotify-engine":    "convert",
    "orpheus-quality":   "hifi",
    "orpheus-save-path": "",
    # ── Beatport ────────────────────────────────────────────────────────────
    "beatport-username": "",
    "beatport-password": "",
    "beatport-quality":  "hifi",
    "beatport-save-path": "",
    # ── JioSaavn (OrpheusDL module, no login) ────────────────────────────────
    "jiosaavn-quality":   "high",
    "jiosaavn-save-path": "",
    # Сколько треков одного релиза качается одновременно (1 = последовательно,
    # как было до 22.09.2026). Пишется в corridor settings.json перед прогоном,
    # читает модуль; потолок 6 держит движок (orpheus_jiosaavn._jiosaavn_parallel).
    "jiosaavn-parallel-count": 3,
}


# ── Config I/O ────────────────────────────────────────────────────────────────

import sys as _sys
import yaml as _yaml
from pathlib import Path as _Path


def _load_token_files(tokens_dir: _Path) -> dict:
    """Read tokens/*.yaml; later files win on key conflicts."""
    result: dict = {}
    if not tokens_dir.is_dir():
        return result
    for tf in sorted(tokens_dir.glob("*.yaml")):
        try:
            data = _yaml.safe_load(tf.read_text(encoding="utf-8")) or {}
            if not data:
                continue
            # A token file must be a `key: value` mapping. If it isn't (e.g. raw
            # lines pasted in by hand), skip it with a clear hint instead of
            # crashing on dict.update() — and never touch the user's file.
            if not isinstance(data, dict):
                print(f"[tokens] skipped {tf.name}: not a key:value mapping "
                      f"(got {type(data).__name__}) - use e.g. 'soundcloud-oauth-token: ...'",
                      file=_sys.stderr, flush=True)
                continue
            result.update(data)
            print(f"[tokens] loaded {tf.name} ({len(data)} keys)", flush=True)
        except Exception as e:
            print(f"[tokens] failed to load {tf.name}: {e}", file=_sys.stderr, flush=True)
    return result


def _atomic_write_yaml(path: _Path, data: dict) -> bool:
    """Write *data* as YAML atomically — temp file in the same directory, then
    os.replace(). A crash mid-write can never truncate or corrupt the real
    file (losing config.yaml = losing every token and setting)."""
    import os, tempfile
    tmp = ""
    try:
        fd, tmp = tempfile.mkstemp(dir=str(path.parent),
                                   prefix="." + path.name + ".", suffix=".tmp")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            _yaml.dump(data, f, allow_unicode=True, default_flow_style=False,
                       sort_keys=False)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)          # atomic rename over the real file
        return True
    except Exception as e:
        print(f"[config] atomic save failed ({path}): {e}", file=_sys.stderr, flush=True)
        if tmp:
            try:
                os.unlink(tmp)
            except OSError:
                pass
        return False


def load_config(config_file: _Path, tokens_dir: _Path) -> dict:
    """Load config.yaml, overlay token files, fill defaults."""
    merged = DEFAULT_CONFIG.copy()
    if config_file.exists():
        try:
            data = _yaml.safe_load(config_file.read_text(encoding="utf-8")) or {}
            merged.update(data)
        except Exception as e:
            print(f"[config] load failed ({config_file}): {e}. Using defaults.",
                  file=_sys.stderr, flush=True)
    merged.update(_load_token_files(tokens_dir))

    # ── Normalize save paths → always absolute & user-writable ──────────────────
    # A relative save-path (e.g. the default "downloads") resolves against the
    # process CWD = the install dir. If that's a protected Program Files dir, the
    # app runs un-elevated and Windows UAC *virtualizes* the write into
    # %LocalAppData%\VirtualStore\... — the download "succeeds" but the file is
    # nowhere the user looks. Redirect any relative / Program Files save path to
    # %USERPROFILE%\Music\Ripster. Deliberate absolute paths elsewhere are kept.
    import os as _os
    _home    = _os.environ.get("USERPROFILE") or str(_Path.home())
    _safe_dl = str(_Path(_home) / "Music" / "Ripster")

    def _bad_path(p: str) -> bool:
        p = (p or "").strip()
        return (not p) or (not _os.path.isabs(p)) or ("program files" in p.lower())

    if _bad_path(str(merged.get("save-path", ""))):
        merged["save-path"] = _safe_dl
    for _k in list(merged.keys()):
        if (_k.endswith("-save-path") or _k.endswith("-save-folder")) and _k != "save-path":
            _v = str(merged.get(_k) or "").strip()
            if _v and ((not _os.path.isabs(_v)) or ("program files" in _v.lower())):
                merged[_k] = _safe_dl
    try:
        _Path(merged["save-path"]).mkdir(parents=True, exist_ok=True)
    except Exception:
        pass

    # Учётки, снятые сторожем здоровья, не должны воскресать при старте.
    # Сторож работает отдельным процессом и правит файл; если бы мы просто
    # читали файл, всё бы сходилось — но сохранение пишет config.yaml целиком
    # из памяти, и раз в сутки мёртвый ARL возвращался (см. retired_credentials).
    try:
        from . import retired_credentials as _retired
        for _n in _retired.strip_from_config(merged):
            print(f"[config] {_n}", flush=True)
    except Exception as _e:
        print(f"[config] реестр снятых учёток недоступен: {_e}",
              file=_sys.stderr, flush=True)

    return merged


_BACKUP_KEEP = 30

# Ключи, которые пишутся сами по расписанию и ничего не говорят о настройках.
_VOLATILE_SUFFIXES = ("-checked-at",)


def _meaningful(path: _Path) -> Any:
    """Содержимое конфига без служебных отметок времени — для сравнения копий."""
    data = _yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if isinstance(data, dict):
        return {k: v for k, v in data.items()
                if not str(k).endswith(_VOLATILE_SUFFIXES)}
    return data


def _backup_config(config_file: _Path) -> None:
    """Копия config.yaml в backups/ ПЕРЕД перезаписью; хвост подрезается.

    Зачем: сохранение переписывает файл ЦЕЛИКОМ, и любая логика, обнулившая
    поля (снятие учётки реестром, сохранение вкладки с пустыми полями, чистка
    токена), уносит их без следа — откатиться было не на что. Держим последние
    `_BACKUP_KEEP` копий: хватает отмотать несколько шагов, и папка не растёт.
    """
    if not config_file.is_file():
        return
    import shutil as _sh
    from datetime import datetime as _dt
    dst_dir = config_file.parent / "backups"
    dst_dir.mkdir(parents=True, exist_ok=True)
    dst = dst_dir / f"config_{_dt.now():%Y%m%d_%H%M%S}.yaml"
    if dst.exists():
        return                      # та же секунда — копия уже сделана
    # Копируем ТОЛЬКО при реальном изменении. Приложение сохраняет конфиг часто
    # (пробы сервисов, обновление токенов), и без этой проверки 30 слотов
    # истории вымывались за минуты — страховка стала бы бесполезной ровно тогда,
    # когда нужна (инцидент замечают через часы). Сверяем с САМОЙ СВЕЖЕЙ копией.
    made = sorted(dst_dir.glob("config_20*.yaml"))
    if made:
        try:
            if made[-1].read_bytes() == config_file.read_bytes():
                return              # ничего не изменилось — копия не нужна
            # Отметки «когда проверяли учётку» меняются каждые 5 минут у
            # каждого сервиса. 23.09.2026 из-за них 30 копий сменялись за
            # ~12 минут: откатиться дальше четверти часа было нельзя, то есть
            # страховка снова не страховала. Такая разница — не изменение.
            if _meaningful(made[-1]) == _meaningful(config_file):
                return
        except Exception:           # noqa: BLE001
            pass
    _sh.copy2(config_file, dst)
    made = sorted(dst_dir.glob("config_20*.yaml"))
    for p in made[:-_BACKUP_KEEP]:
        try:
            p.unlink()
        except Exception:           # noqa: BLE001
            pass


def save_config(cfg: Any, config_file: _Path, tokens_dir: _Path) -> None:
    """Persist config to config.yaml and sync any token files."""
    raw = cfg._data if isinstance(cfg, ConfigService) else cfg
    # Перед записью сверяемся с реестром снятых: сохранение переписывает файл
    # ЦЕЛИКОМ, и без этой сверки оно возвращало бы в конфиг учётку, которую
    # сторож уже снял (ровно так ARL ...947f15 пережил собственное удаление
    # 03.09.2026). Правим `raw` на месте — это и есть живой словарь приложения,
    # так что поле освобождается сразу, а не только в файле.
    try:
        from . import retired_credentials as _retired
        for _n in _retired.strip_from_config(raw):
            print(f"[config] {_n}", flush=True)
    except Exception as _e:
        print(f"[config] реестр снятых учёток недоступен: {_e}",
              file=_sys.stderr, flush=True)
    # Страховка от массового затирания: перед КАЖДОЙ перезаписью кладём копию.
    # 18.09.2026 владелец нажал «удалить токен Qobuz» и недосчитался почти всех
    # токенов, а вернуть смог ровно один — потому что отката не существовало.
    # Конфиг ~12 КБ, копия стоит копейки и превращает потерю данных в «откатил
    # файл». Ошибка бэкапа НЕ должна мешать сохранению — только пишем в лог.
    try:
        _backup_config(config_file)
    except Exception as _e:                                   # noqa: BLE001
        print(f"[config] бэкап перед записью не удался: {_e}",
              file=_sys.stderr, flush=True)
    if not _atomic_write_yaml(config_file, raw):
        return
    if tokens_dir.is_dir():
        for tf in sorted(tokens_dir.glob("*.yaml")):
            try:
                tdata = _yaml.safe_load(tf.read_text(encoding="utf-8")) or {}
                changed = False
                for k in list(tdata):
                    if k in cfg and str(cfg[k]) != str(tdata[k]):
                        tdata[k] = cfg[k]
                        changed = True
                if changed and _atomic_write_yaml(tf, tdata):
                    print(f"[config] synced token file {tf.name}", flush=True)
            except Exception as e:
                print(f"[config] failed to sync token file {tf.name}: {e}",
                      file=_sys.stderr, flush=True)


def write_downloader_config(config: Any, work_dir: str) -> bool:
    """Write config.yaml into the Go downloader's working directory."""
    import yaml as _y
    cfg_path = _Path(work_dir) / "config.yaml"
    cover_size = config.get("cover-size", "3000x3000")
    cover_px = 0 if cover_size == "original" else cover_size

    def port_only(addr: str) -> str:
        return addr.split(":")[-1] if ":" in addr else addr

    dl_cfg = {
        "media-user-token":   config.get("media-user-token", ""),
        "storefront":         config.get("storefront", "us"),
        "language":           config.get("language", ""),
        "alac-save-folder":   config.get("save-path", "downloads"),
        "atmos-save-folder":  config.get("save-path", "downloads") + "/Atmos",
        "aac-save-folder":    config.get("save-path", "downloads") + "/AAC",
        "embed-cover":        config.get("embed-cover", True),
        "cover-size":         cover_px,
        "cover-format":       config.get("cover-format", "jpg"),
        "save-artist-cover":  config.get("save-cover-to-folder", True),
        "embed-lrc":          config.get("embed-lrc", True),
        "save-lrc-file":      config.get("save-lrc-file", False),
        "lrc-type":           config.get("lrc-type", "lyrics"),
        "lrc-format":         config.get("lrc-format", "lrc"),
        "decrypt-m3u8-port":  config.get("decrypt-port", "127.0.0.1:10020"),
        "get-m3u8-port":      config.get("m3u8-port",    "127.0.0.1:20020"),
        "max-memory-limit":   config.get("max-memory", 256),
        "atmos-max":          config.get("atmos-max", 2448),
        # Клипы. Без этих двух ключей Go-загрузчик получал НОЛЬ в `mv-max`
        # (yaml без ключа → нулевое значение int), а `extractVideo` берёт первый
        # вариант с высотой ≤ потолка — то есть ни одного, и падал с «no suitable
        # video stream found». Дефект скрытый: сегодня маршрутизатор шлёт всё
        # видео в gamdl, поэтому ветка не выполнялась ни разу.
        #
        # 2160 и atmos — то, что стоит в конфиге самого загрузчика: он выбирает
        # вариант с наибольшим битрейтом в пределах потолка, так что для клипа
        # без 4K это просто «бери лучшее из имеющегося».
        "mv-max":             config.get("mv-max", 2160),
        "mv-audio-type":      config.get("mv-audio-type", "atmos"),
    }
    try:
        with open(cfg_path, "w", encoding="utf-8") as f:
            _y.dump(dl_cfg, f, allow_unicode=True, default_flow_style=False, sort_keys=False)
        return True
    except Exception as e:
        print(f"[config] Failed to write downloader config: {e}", flush=True)
        return False
