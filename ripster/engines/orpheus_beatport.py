"""OrpheusDL-Beatport engine — downloads Beatport URLs via OrpheusDL + orpheusdl-beatport.

Authentication: username + password stored in the engine's own OrpheusDL config
corridor (orpheus/config/beatport/settings.json, seeded from the shared
orpheus/config/settings.json); the Beatport SESSION stays shared in
orpheus/config/loginstorage.bin — its refresh token rotates, so a second copy
would spend an already-revoked token.
Quality tiers:
  hifi/lossless  → FLAC 16-bit (requires Beatport Professional subscription)
  high           → AAC 256 kbps (requires Beatport Professional)
  minimum        → AAC 128 kbps (Link subscription)

Module must be cloned to orpheus/modules/beatport/ before first use.
Repo: https://github.com/Dniel97/orpheusdl-beatport
"""
from __future__ import annotations

import json
import re
import shutil
import sys
from pathlib import Path

from .base import EngineBase, EngineResult, Event, EventKind, LineLevel, _strip_ansi
from .registry import register
from ripster.py_runtime import app_python


def _base_dir() -> Path:
    return Path(sys.argv[0]).resolve().parent if sys.argv else Path(".").resolve()

def _orpheus_dir() -> Path:
    return _base_dir() / "orpheus"


def _orpheus_python() -> str:
    """Interpreter for OrpheusDL — prefer the ISOLATED venv (tools/orpheusvenv) so
    OrpheusDL's protobuf==3.15.8 never pollutes the shared bundled python (which
    would break AMD + pywidevine). See the ripster-dependency-versions skill."""
    base = _base_dir()
    for sub in (("Scripts", "python.exe"), ("bin", "python")):
        cand = base / "tools" / "orpheusvenv" / sub[0] / sub[1]
        if cand.is_file():
            return str(cand)
    # tools/orpheusvenv отсутствует в этой установке — и тогда раньше молча брался
    # sys.executable, то есть ЛЮБОЙ интерпретатор, которым подняли app.py. 10-11.08.2026
    # app.py стартовал из C:\Python314 (не из .venv), и OrpheusDL получил его user-site,
    # где лежит пакет `ffmpeg` вместо `ffmpeg-python` → `ImportError: cannot import name
    # 'Error' from 'ffmpeg'` на КАЖДОМ движке Orpheus (beatport/spotify/tidal), 12 прогонов
    # подряд. В логе это видно буквально: все 203 прогона из .venv отработали без этой
    # ошибки, все 12 из C:\Python314 — с ней. Поэтому берём venv проекта ЯВНО, а не
    # «тот, которым запустились»: интерпретатор движка не должен зависеть от того, как
    # именно подняли сервер.
    # Дальше — ОБЩЕЕ правило приложения (ripster/py_runtime.py): venv проекта с
    # проверкой запускаемости, переопределение через RIPSTER_ENGINE_PYTHON, иначе
    # текущий интерпретатор. Здесь лежала третья копия одного и того же перебора,
    # и именно про такие копии предупреждал разбор 16.08: расходятся они молча.
    return app_python()

#: Куда кладём последний ПОДТВЕРЖДЁННЫЙ тариф. Тариф меняется раз в месяц
#: (биллинг), а `introspect` может транзиентно отвалиться (сеть, гонка refresh).
#: Без памяти любой блип ронял качество до 128k AAC при живом Pro+ — ровно так
#: гость и получил .m4a 13.09.2026. Свежий known-good — куда лучшая основа, чем
#: «раз не ответило — значит не Pro».
_TIER_CACHE = Path(__file__).resolve().parent.parent.parent / "tokens" / "beatport_tier.json"
#: Сколько доверяем закэшированному тарифу при сбое живой проверки. Две недели
#: с запасом переживают сетевые перебои, но если подписку реально свернули —
#: кэш протухнет и мы честно вернёмся к «не знаю», а не к вечному Pro.
_TIER_CACHE_TTL = 14 * 24 * 3600.0


def _tier_cache_write(tier: str) -> None:
    """Запомнить подтверждённый тариф. Пустой не пишем — «не знаю» не факт."""
    if not tier:
        return
    try:
        import time
        _TIER_CACHE.parent.mkdir(parents=True, exist_ok=True)
        _TIER_CACHE.write_text(json.dumps({"tier": tier, "ts": time.time()}),
                               encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass


def _tier_cache_read() -> str:
    """Свежий known-good тариф или '' — если кэша нет / он устарел."""
    try:
        import time
        d = json.loads(_TIER_CACHE.read_text(encoding="utf-8"))
        if time.time() - float(d.get("ts", 0)) <= _TIER_CACHE_TTL:
            return str(d.get("tier") or "")
    except Exception:  # noqa: BLE001
        pass
    return ""


def _tier_now() -> str:
    """Тариф аккаунта прямо сейчас: 'bp_link_pro_plus_2' / 'bp_basic' / '' если
    узнать не удалось. Синхронно — вызывается из build_cmd.

    Пустая строка означает ровно «не знаю», а не «нет подписки»: сеть могла
    отвалиться, токен — протухнуть. Отличать эти два случая обязательно, вся
    починка ниже держится именно на этом различии.

    При живом ответе тариф кэшируется; при сбое проверки возвращаем свежий
    known-good из кэша, а не пустоту — иначе сетевой блип роняет Pro+ до 128k
    AAC (см. `_TIER_CACHE`). Пусто отдаём, только когда и кэш пуст/протух.
    """
    import httpx

    # ПОЧЕМУ не узнали — важнее самого «не узнали». 19.09.2026 10:43 TLS-рукопожатие
    # с api.beatport.com висело, тариф стал «неизвестен», проверка подписки
    # выключилась, строки «subscription detected» не было — и первый же голый
    # 401/403 в логе превращался в «неверный логин/пароль» гостю при живом
    # аккаунте. Причину отдаём движку через _TIER_STATE["fail"].
    _TIER_STATE["fail"] = ""

    def _fail(kind: str = "other") -> str:
        _TIER_STATE["fail"] = kind
        return _tier_cache_read()

    sess = _read_bp_session() or {}
    at = sess.get("access_token") or ""
    rt = sess.get("refresh_token") or ""
    for attempt in ("stored", "refreshed"):
        if attempt == "refreshed":
            if not rt:
                return _fail("auth")
            try:
                r = httpx.post("https://api.beatport.com/v4/auth/o/token/",
                               data={"client_id": _BP_CLIENT_ID, "refresh_token": rt,
                                     "grant_type": "refresh_token"}, timeout=10)
                if r.status_code == 200:
                    j = r.json()
                    at = j["access_token"]
                    _persist_refreshed_session(rt, j)
                else:
                    at = ""
            except (httpx.TransportError, OSError):
                return _fail("network")
            except Exception:
                return _fail()
            if not at:
                return _fail("auth")          # refresh отклонён — это про токен, не про сеть
        if not at:
            continue
        try:
            r = httpx.get("https://api.beatport.com/v4/auth/o/introspect/",
                          headers={"Authorization": f"Bearer {at}"}, timeout=10)
        except (httpx.TransportError, OSError):
            return _fail("network")
        except Exception:
            return _fail()
        if r.status_code == 200:
            try:
                tier = (r.json().get("subscription") or "").strip()
                _tier_cache_write(tier)
                return tier
            except Exception:
                return _fail()
        if r.status_code != 401:
            return _fail()
    return _fail("auth")


def _main_config_dir() -> Path:
    """Каталог конфига OrpheusDL по умолчанию. CWD прогона = orpheus/, поэтому без
    ORPHEUS_CONFIG_DIR core.py резолвит именно `orpheus/config`."""
    return _orpheus_dir() / "config"

def _corridor_config_dir(engine: str) -> Path:
    return _main_config_dir() / engine

def _corridor_settings(engine: str) -> Path:
    """settings.json КОНКРЕТНОГО движка Orpheus — копия общего конфига.

    Качество и путь пишутся перед каждым прогоном, а полосы параллельности у
    очереди раздельные (ripster/runner.py::_lock_key: 'beatport' и 'jiosaavn' —
    РАЗНЫЕ ключи, то есть два прогона идут ОДНОВРЕМЕННО). Один общий файл
    означал, что второй прогон перезапишет качество и папку первого — а сам
    первый ещё и перечитает этот файл на старте. Принцип тот же, что у
    коридоров spotify_pool/tidal_pool.
    """
    sp = _corridor_config_dir(engine) / "settings.json"
    base = _main_config_dir() / "settings.json"
    try:
        if base.is_file() and (not sp.is_file()
                               or base.stat().st_mtime > sp.stat().st_mtime):
            sp.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(base, sp)
    except OSError:
        pass
    return sp

def _settings_path() -> Path:
    return _corridor_settings("beatport")

def _module_path() -> Path:
    return _orpheus_dir() / "modules" / "beatport"

def _session_path() -> Path:
    """Сессия Beatport — ОДНА на всех, вне коридора: refresh-токен ротируется,
    и вторая копия означала бы повторный спуск уже погашенного токена (см.
    `_persist_refreshed_session`)."""
    return _main_config_dir() / "loginstorage.bin"


def orpheus_cmd(config_dir: Path, url: str, save_path: str = "",
                session_file: Path | None = None) -> list[str]:
    """Команда запуска OrpheusDL для одного прогона.

    The bundled embeddable Python runs ISOLATED (sys.flags.isolated==1, a side
    effect of the ._pth) → it does NOT add the script's directory to sys.path AND
    ignores PYTHONPATH. So a plain `python orpheus.py` dies with
    "ModuleNotFoundError: No module named 'orpheus.core'" even though the inner
    orpheus/ package sits right next to orpheus.py (proven on a bundled install;
    the dev .venv is non-isolated so it never reproduced). Bootstrap via -c: put
    the OrpheusDL dir on sys.path, restore argv, then run orpheus.py as __main__.

    Каталог конфига коридора сообщается ТЕМ же бутстрапом, а не окружением
    процесса app.py: все движки живут в одном интерпретаторе, и os.environ у них
    общий — выставив его здесь, мы бы заразили параллельный прогон.
    """
    orph_dir   = str(_orpheus_dir())
    orpheus_py = str(_orpheus_dir() / "orpheus.py")
    _env = f"os.environ['ORPHEUS_CONFIG_DIR'] = {str(config_dir)!r}; "
    if session_file is not None:
        _env += f"os.environ['ORPHEUS_SESSION_STORAGE'] = {str(session_file)!r}; "
    _boot = (
        "import os, sys, runpy; "
        + _env +
        f"sys.path.insert(0, {orph_dir!r}); "
        f"sys.argv = [{orpheus_py!r}] + sys.argv[1:]; "
        f"runpy.run_path({orpheus_py!r}, run_name='__main__')"
    )
    cmd = [_orpheus_python(), "-c", _boot]
    if save_path:
        cmd += ["-o", save_path.rstrip("/\\")]
    cmd.append(url)
    return cmd


#: Как резолвит каталог конфига ЧИСТЫЙ upstream OrpheusDL — жёстко относительно CWD.
_ORPH_CORE_CONFIG_SHARED = (
    "        self.data_folder_base = 'config'\n"
    "        self.settings_location = os.path.join(self.data_folder_base, 'settings.json')\n"
    "        self.session_storage_location = os.path.join(self.data_folder_base, 'loginstorage.bin')\n"
    "\n"
    "        os.makedirs('config', exist_ok=True)\n")

#: ... и как его оставляет `orpheus_cmd`, чтобы каждый движок читал свой каталог.
_ORPH_CORE_CONFIG_CORRIDOR = (
    "        self.data_folder_base = (os.environ.get('ORPHEUS_CONFIG_DIR') or '').strip() or 'config'\n"
    "        self.settings_location = os.path.join(self.data_folder_base, 'settings.json')\n"
    "        self.session_storage_location = (os.environ.get('ORPHEUS_SESSION_STORAGE') or '').strip() or os.path.join(self.data_folder_base, 'loginstorage.bin')\n"
    "\n"
    "        os.makedirs(self.data_folder_base, exist_ok=True)\n")


def orpheus_core_corridor_patch(src: str) -> str:
    """Научить вендоренный core.py брать каталог конфига из окружения.

    Нужен именно патч, а не только наша правка в `orpheus/`: публичная сборка
    ставит OrpheusDL СВЕЖИМ клоном с GitHub, и без патча прогон просто не увидел
    бы ORPHEUS_CONFIG_DIR — то есть писал бы качество в файл, который не читает,
    и молча качал бы чужое качество из общего. Идемпотентен; если upstream
    переписал эти строки, возвращает текст без изменений (тогда коридор не
    работает, и установка честно об этом сообщает).
    """
    if "ORPHEUS_CONFIG_DIR" in src:
        return src
    return src.replace(_ORPH_CORE_CONFIG_SHARED, _ORPH_CORE_CONFIG_CORRIDOR, 1)



# ── live access token from OrpheusDL's saved Beatport session ─────────────────
# Beatport has no public metadata API, so queue cards used to come back blank
# ("beatport · <id>", no cover/title). But the AUTHENTICATED catalog API does
# return full metadata, and the download flow already keeps a valid session in
# orpheus/config/loginstorage.bin. Mint a token from it (refreshing via the
# Serato client if expired) so ripster.metadata can enrich Beatport cards the
# same way Tidal does. Cached in-process; re-reads the pickle every ~2 min so it
# picks up tokens OrpheusDL itself refreshed.
_BP_CLIENT_ID = "Zy2K9Wvy6DkUds7g8s1GNMHfk17E5Ch2BWHlyaGY"  # Serato DJ Lite (== beatport_api.py)
_BP_AT_CACHE: dict = {"token": "", "exp": 0.0}

def _read_bp_session() -> dict | None:
    """The beatport module's saved {access_token, refresh_token, expires} dict."""
    from ripster.safe_pickle import safe_loads
    try:
        blob = safe_loads(_session_path().read_bytes())
        return blob["modules"]["beatport"]["sessions"]["default"]["custom_data"]
    except Exception:
        return None


#: Итог последней проверки тарифа (_tier_now): "" — тариф получен живьём;
#: "network" — api.beatport.com не ответил (таймаут/TLS/DNS); "auth" — токен
#: отклонён; "other" — прочее. Движок читает это в build_cmd.
_TIER_STATE: dict = {"fail": ""}

import threading as _threading
_BP_SESSION_LOCK = _threading.Lock()


def _write_bp_session(mutate) -> bool:
    """Перечитать loginstorage.bin, дать `mutate(custom_data)` поправить сессию
    Beatport (вернуть False — отказаться от записи) и атомарно записать обратно:
    временный файл рядом + os.replace, чтобы OrpheusDL никогда не прочёл
    полузаписанный pickle. Формат тот же, что пишет сам OrpheusDL (core.py:
    pickle.dump({'advancedmode', 'modules'})); читаем через safe_pickle."""
    import os
    import pickle
    from ripster.safe_pickle import safe_loads
    path = _session_path()
    with _BP_SESSION_LOCK:
        try:
            blob = safe_loads(path.read_bytes())
            cd = blob["modules"]["beatport"]["sessions"]["default"]["custom_data"]
        except Exception:
            return False
        if mutate(cd) is False:
            return False
        tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
        try:
            tmp.write_bytes(pickle.dumps(blob))
            os.replace(tmp, path)
            return True
        except Exception as e:  # noqa: BLE001
            print(f"[beatport] не удалось сохранить сессию в {path.name}: "
                  f"{type(e).__name__}", flush=True)
            try:
                tmp.unlink()
            except Exception:  # noqa: BLE001
                pass
            return False


def _persist_refreshed_session(used_rt: str, j: dict) -> bool:
    """Сохранить результат НАШЕГО refresh в loginstorage.bin.

    Beatport при refresh РОТИРУЕТ refresh_token. Раньше новый оставался только в
    памяти, а OrpheusDL на следующем запуске шёл со СТАРЫМ из файла → повторное
    использование уже погашенного refresh_token → Beatport отзывает всю цепочку
    (в том числе только что выданный access) → «Authentication credentials were
    not provided» (19.09.2026). Поэтому новую пару пишем туда, откуда её читает
    OrpheusDL. Если в файле уже ДРУГОЙ refresh_token, чем тот, которым рефрешили
    мы, — OrpheusDL успел обновиться сам, его пара новее: не перезаписываем."""
    from datetime import datetime, timedelta
    new_at = (j or {}).get("access_token") or ""
    if not new_at or not used_rt:
        return False

    def _m(cd: dict):
        if (cd.get("refresh_token") or "") != used_rt:
            print("[beatport] refresh_token в сессии уже обновлён OrpheusDL — "
                  "свою пару не записываю", flush=True)
            return False
        cd["access_token"] = new_at
        if j.get("refresh_token"):
            cd["refresh_token"] = j["refresh_token"]
        try:
            ttl = int(j.get("expires_in", 3600))
        except (TypeError, ValueError):
            ttl = 3600
        cd["expires"] = datetime.now() + timedelta(seconds=ttl)
        return True

    return _write_bp_session(_m)


def _invalidate_bp_session() -> bool:
    """Сбросить сохранённую сессию, которую Beatport отклонил («Authentication
    credentials were not provided» — цепочка токенов отозвана). С refresh_token
    = None OrpheusDL на следующем старте сам входит логином и паролем
    (interface.py: `if session["refresh_token"] is None: self.login(...)`), без
    обращения к refresh — то есть без риска ещё раз погасить цепочку."""
    _BP_AT_CACHE["token"], _BP_AT_CACHE["exp"] = "", 0.0

    def _m(cd: dict):
        if not cd.get("refresh_token") and not cd.get("access_token"):
            return False                     # уже сброшена
        cd["access_token"] = None
        cd["refresh_token"] = None
        return True

    return _write_bp_session(_m)


async def _beatport_access_token() -> str:
    """Return a valid Beatport access_token, or '' if no session. Refreshes via
    the Serato client + saved refresh_token when the stored token has expired."""
    import time
    from datetime import datetime
    now = time.time()
    if _BP_AT_CACHE["token"] and now < _BP_AT_CACHE["exp"]:
        return _BP_AT_CACHE["token"]
    sess = _read_bp_session()
    if not sess:
        return ""
    at = sess.get("access_token") or ""
    exp_dt = sess.get("expires")
    try:
        if at and exp_dt and exp_dt > datetime.now():
            _BP_AT_CACHE["token"] = at
            _BP_AT_CACHE["exp"]   = now + 120   # re-check the pickle periodically
            return at
    except Exception:
        pass
    # Сюда попадаем, когда срок токена ИСТЁК. Дальше либо обновим, либо
    # честно скажем «нет токена». Возвращать протухший нельзя: он выглядит
    # рабочим, а каждый вызов с ним получает 401 — источник молча выпадает, и
    # причину не найти. Поймано 06.09.2026: справочник жанров перестал видеть
    # Beatport, а функция бодро отдавала строку.
    rt = sess.get("refresh_token") or ""
    if not rt:
        print("[beatport] токен истёк, refresh_token отсутствует — нужен повторный вход",
              flush=True)
        return ""
    try:
        import httpx
        async with httpx.AsyncClient(timeout=10, follow_redirects=True) as c:
            r = await c.post("https://api.beatport.com/v4/auth/o/token/",
                             data={"client_id": _BP_CLIENT_ID,
                                   "refresh_token": rt,
                                   "grant_type": "refresh_token"})
        if r.status_code == 200:
            j = r.json()
            _persist_refreshed_session(rt, j)   # иначе OrpheusDL придёт со старым rt → отзыв
            _BP_AT_CACHE["token"] = j["access_token"]
            _BP_AT_CACHE["exp"]   = now + max(60, int(j.get("expires_in", 3600)) - 120)
            return _BP_AT_CACHE["token"]
        print(f"[beatport] обновление токена отклонено ({r.status_code}: "
              f"{r.text[:80]}) — нужен повторный вход логином и паролем", flush=True)
    except Exception as e:
        print(f"[beatport] обновление токена не удалось ({type(e).__name__}) — "
              f"считаю, что токена нет", flush=True)
    # Ни обновить, ни подтвердить. «Нет токена» — правда; протухший был бы
    # ложью, из-за которой вызывающий пошёл бы за 401.
    return ""


# ── patterns ─────────────────────────────────────────────────────────────────
_RE_DOWNLOADING  = re.compile(r'===\s*Downloading\s+(track|release|playlist|artist)\s+(.+?)\s*(?:\(|===)', re.I)
_RE_TRACK_FILE   = re.compile(r'Downloading track file|Saving\s*:', re.I)
_RE_DONE         = re.compile(r'===\s*Done|Download complete', re.I)
_RE_ERROR        = re.compile(r'\berror\b|\bfailed\b|\bexception\b|\bTraceback', re.I)
_RE_SKIP         = re.compile(r'skip|already exist|ignore', re.I)
_RE_PROGRESS     = re.compile(r'(\d+)\s*/\s*(\d+)')
_RE_AUTH_FAIL    = re.compile(r'invalid.*creden|wrong.*password|login.*fail|auth.*fail|401|403', re.I)
_RE_SUBSCRIPTION = re.compile(r'subscription|professional|upgrade.*plan|higher.*tier', re.I)
# Явное «логин/пароль неверны» — единственное, что вправе стать BEATPORT_NOT_AUTHED
# без доказанного входа. Проверяется РАНЬШЕ сети: модуль заворачивает HTTP-отказ
# входа в `ConnectionError(<тело ответа>)`, и сетевой шаблон иначе съел бы его.
_RE_BAD_CREDS    = re.compile(r'invalid[^\n]*creden|wrong[^\n]*password|incorrect[^\n]*(?:password|creden)|'
                              r'unable to log in|invalid_grant[^\n]*password|'
                              r'password[^\n]*(?:invalid|incorrect|wrong)', re.I)
# Цепочка токенов отозвана / сессионный токен отклонён — это НЕ неверный пароль.
_RE_TOKEN_REJECTED = re.compile(r'credentials were not provided|token_not_valid|'
                                r'Given token not valid', re.I)
# Транспорт: сеть/TLS/DNS/таймаут. Такой сбой — повод повторить, а не «проверь пароль».
_RE_NET          = re.compile(r'ConnectTimeout|ReadTimeout|ConnectError|ConnectionError|'
                              r'handshake operation timed out|timed\s+out|\btimeout\b|'
                              r'Max retries exceeded|getaddrinfo|SSLError|NameResolution|'
                              r'Temporary failure in name resolution|RemoteDisconnected|'
                              r'Connection (?:aborted|reset|refused)|ProxyError', re.I)
# Голый код/фраза без пояснений. Коды — только отдельным словом: иначе «401»
# внутри ID релиза (…/track/x/19401…) тоже считался отказом входа.
_RE_AUTH_BARE    = re.compile(r'login.*fail|auth.*fail|\b401\b|\b403\b', re.I)

_NET_VERDICT = ("Beatport: сеть недоступна (таймаут соединения с api.beatport.com) — "
                "повторю. Логин и пароль тут ни при чём.")
_TOKEN_VERDICT = ("BEATPORT_SESSION_REJECTED: Beatport отклонил сохранённый токен сессии "
                  "(Authentication credentials were not provided) — сессия сброшена, при "
                  "повторе вход выполнится заново логином и паролем автоматически.")


_QUALITIES = [
    {
        "id": "hifi",    "label": "FLAC",    "engine": "orpheus_beatport",
        "sub": "FLAC 16-bit · Professional",
        "badge": "FLAC", "color": "#3ecfaa", "bitrate": "lossless",
        "ext": "flac",   "req": "professional",
    },
    {
        "id": "high",    "label": "AAC 256", "engine": "orpheus_beatport",
        "sub": "AAC 256 kbps · Professional",
        "badge": "256k", "color": "#EF9F27", "bitrate": "256 kbps",
        "ext": "aac",    "req": "professional",
    },
    {
        "id": "minimum", "label": "AAC 128", "engine": "orpheus_beatport",
        "sub": "AAC 128 kbps · Link",
        "badge": "128k", "color": "#6a6a8a", "bitrate": "128 kbps",
        "ext": "aac",    "req": "link",
    },
]

_QUALITY_ORPHEUS = {
    "hifi":     "hifi",
    "lossless": "hifi",   # UI / stale queue alias
    "flac":     "hifi",
    "high":     "high",
    # Lossy picks. The bot's Beatport list offers ("320","MP3 320"); Beatport has
    # NO native MP3 — its lossy tier is AAC 256 ("high"). Without these aliases,
    # "320"/"aac"/"mp3" fell through to the default and silently downloaded FLAC
    # instead of what the user picked (the "Beatport always returns FLAC" bug).
    "320":      "high",
    "aac":      "high",
    "mp3":      "high",
    "minimum":  "minimum",
    "128":      "minimum",
    "low":      "minimum",
}


def is_installed() -> bool:
    return ((_orpheus_dir() / "orpheus.py").exists()
            # inner orpheus/ package must be present too — a partial clone with only
            # orpheus.py crashes at runtime with "ModuleNotFoundError: orpheus.core".
            and (_orpheus_dir() / "orpheus" / "core.py").exists()
            and (_module_path() / "__init__.py").exists())


def is_authenticated(config: dict) -> bool:
    return bool(
        (config.get("beatport-username") or "").strip() and
        (config.get("beatport-password") or "").strip()
    )


def _update_orpheus_settings(quality: str, save_path: str, config: dict) -> str:
    """Возвращает предупреждение для лога ('' — всё в порядке)."""
    note = ""
    sp = _settings_path()
    if not sp.exists():
        return note
    try:
        cfg = json.loads(sp.read_text(encoding="utf-8"))
        gen = cfg.setdefault("global", {}).setdefault("general", {})
        if quality:
            gen["download_quality"] = quality
        if save_path:
            gen["download_path"] = save_path.rstrip("/\\") + "\\"

        # `disable_subscription_checks` — НЕ безобидный тумблер «не мешай».
        # В orpheusdl-beatport ровно тот же флаг гейтит ПОВЫШЕНИЕ КАЧЕСТВА
        # (interface.py, valid_account): по умолчанию весь quality_parse — это
        # "medium", то есть AAC 128, и апгрейд до "high"/"lossless" происходит
        # ТОЛЬКО внутри той же ветки, что и проверка подписки. Выключив проверку
        # ради обхода ложного отказа, мы вместе с ней выключили и апгрейд —
        # аккаунт Professional+ полгода качал 128 kbps в папку «FLAC CD».
        # Замер 05.09.2026: introspect отдаёт bp_link_pro_plus_2, а файл на диске —
        # aac 133 kbps при запрошенном hifi.
        #
        # Поэтому решает не флаг, а ФАКТ: спрашиваем тариф сами.
        #   тариф известен  → проверку ВКЛЮЧАЕМ (модуль возьмёт правду из API и
        #                     поднимет качество; ложного отказа быть не может —
        #                     мы только что видели непустую подписку);
        #   тариф неизвестен→ проверку выключаем, как раньше, чтобы сеть или
        #                     протухший токен не рвали закачку, но ЧЕСТНО
        #                     предупреждаем: качество будет 128k.
        adv = cfg["global"].setdefault("advanced", {})
        tier = _tier_now()
        adv["disable_subscription_checks"] = not tier
        if not tier:
            note = ("Beatport: не удалось подтвердить тариф аккаунта — качество будет "
                    "ограничено AAC 128 kbps (модуль поднимает FLAC/AAC 256 только по "
                    "подтверждённой подписке). Проверь сеть и логин Beatport.")
        elif not tier.startswith("bp_link_pro") and quality in ("hifi", "high"):
            note = (f"Beatport: тариф аккаунта — {tier}, это не Professional. FLAC и AAC 256 "
                    f"на нём недоступны, скачается AAC 128 kbps.")

        covers = cfg["global"].setdefault("covers", {})
        covers["embed_cover"]         = True
        # Embedded (in-audio) cover pinned to 1000×1000 across ALL services.
        covers["main_resolution"]     = 1000
        # Beatport treats every track as its own single, so OrpheusDL writes the
        # external cover named AFTER THE TRACK ("<title>.jpg") — an album of N
        # tracks ends up with N redundant sidecar images. Disable the external
        # cover: art stays embedded in each file, and the app drops ONE folder
        # cover.jpg from the embedded art (_apply_cover_to_folder). No clutter.
        covers["save_external"]       = False

        bp = cfg.setdefault("modules", {}).setdefault("beatport", {})
        username = (config.get("beatport-username") or "").strip()
        password = (config.get("beatport-password") or "").strip()
        if username:
            bp["username"] = username
        if password:
            bp["password"] = password

        sp.write_text(json.dumps(cfg, indent=4, ensure_ascii=False), encoding="utf-8")
    except Exception:
        pass
    return note


# Сколько отказов «нет прав» подряд терпим, прежде чем прекратить прогон.
# 09.08.2026: чарт Beatport на 87 треков выдал 87 таких отказов и молотил
# двадцать минут, не сохранив ничего. Движок распознавал эту ошибку ПРАВИЛЬНО,
# но только в конце прогона — то есть после того, как отвалятся все треки.
# Десять — заведомо больше, чем пара недоступных релизов в чарте.
_PERM_FAIL_LIMIT = 10


@register
class OrpheusBeatportEngine(EngineBase):
    name = "orpheus_beatport"

    def __init__(self):
        # Раннер читает abort_reason после каждой строки и глушит процесс
        # (ProcessRunner._engine_wants_abort).
        self.abort_reason = ""
        self._perm_fails = 0
        self._attempts = 0
        self._tier_note = ""
        # True, как только OrpheusDL напечатал тариф аккаунта
        # («… subscription detected …») — это доказывает, что ВХОД УДАЛСЯ.
        # После этого 401/403 — права/регион на конкретный релиз, а НЕ «неверный
        # логин»: иначе региональный отказ на треке рушился в BEATPORT_NOT_AUTHED
        # и пугал владельца «акк отвалился» при живом аккаунте (гость, «Yonder»,
        # 18.09.2026: 15:02:28 «Professional subscription detected» → 15:02:31
        # ложный BEATPORT_NOT_AUTHED; тот же аккаунт качал в 16:25).
        self._logged_in = False
        # Почему не узнали тариф в build_cmd ("" / network / auth / other). Если
        # из-за сети — проверка подписки выключена, строки «subscription detected»
        # не будет в принципе, и голый 401/403 не доказывает неверный пароль.
        self._tier_fail = ""
        self._net_seen = False

    def qualities(self) -> list[dict]:
        return list(_QUALITIES)

    def working_dir(self) -> str:
        return str(_orpheus_dir())

    def build_cmd(self, url: str, quality: str, config: dict) -> list[str]:
        if not (_orpheus_dir() / "orpheus.py").exists():
            raise ValueError("OrpheusDL не установлен — перейди в Settings → Beatport")
        if not (_module_path() / "__init__.py").exists():
            raise ValueError(
                "Модуль Beatport не установлен. Клонируй "
                "https://github.com/Dniel97/orpheusdl-beatport в orpheus/modules/beatport/"
            )
        if not is_authenticated(config):
            raise ValueError(
                "BEATPORT_NOT_AUTHED: введи логин и пароль Beatport в Settings → Beatport"
            )

        save_path = config.get("beatport-save-path") or config.get("save-path") or ""
        orpheus_quality = _QUALITY_ORPHEUS.get(quality, "hifi")
        _TIER_STATE["fail"] = ""
        self._tier_note = _update_orpheus_settings(orpheus_quality, save_path, config)
        self._tier_fail = _TIER_STATE.get("fail", "")

        # Свой каталог настроек (см. `_corridor_settings`), но ОБЩАЯ сессия:
        # loginstorage.bin остаётся один файл на весь аккаунт Beatport.
        return orpheus_cmd(_corridor_config_dir("beatport"), url, save_path,
                           session_file=_session_path())

    def iter_events(self, line: str, *, progress: tuple[int, int]):
        clean = _strip_ansi(line).strip()
        if not clean:
            return

        # Предупреждение о тарифе рождается в build_cmd, где отдавать события
        # некуда. Отдаём его первой же строкой прогона, чтобы человек увидел
        # причину ДО того, как получит на диск не то качество.
        if self._tier_note:
            yield Event(kind=EventKind.LINE, message=self._tier_note, level=LineLevel.WARN)
            self._tier_note = ""

        # Ранняя отсечка: чарт, на который у аккаунта нет прав, не станет
        # доступнее к 87-му треку. Две части условия обязательны — если хоть
        # что-то сохранилось, отдельные отказы это обычная недоступность
        # релиза, и рвать из-за неё весь чарт нельзя.
        # СЧИТАЕМ ПОПЫТКИ, а не «сохранения». «Downloading track file» печатается
        # БЕЗУСЛОВНО прямо перед отказом — об этом прямо предупреждает комментарий
        # в is_finished ниже, и первая версия этой отсечки на нём и споткнулась:
        # счётчик «сохранённых» рос вместе с отказами и глушил защиту.
        # Признак безнадёжности — не «ноль сохранений», а «каждая попытка
        # закончилась отказом».
        if re.search(r'Downloading track file|Saving\s*:', clean, re.I):
            self._attempts += 1
        if "do not have permission" in clean.lower():
            self._perm_fails += 1
            if (self._perm_fails >= _PERM_FAIL_LIMIT
                    and self._perm_fails >= self._attempts and not self.abort_reason):
                self.abort_reason = (
                    f"Beatport отказал в правах на {self._perm_fails} треках подряд и не "
                    f"сохранил ни одного. Подписка активна, но аккаунт не может начать "
                    f"поток по этим релизам — обычно так ведёт себя чарт, который целиком "
                    f"вне прав аккаунта. Дальше пробовать бессмысленно."
                )

        # Строка тарифа («… subscription detected …») = успешный вход. С этого
        # момента любой 401/403 — это права/регион на конкретный релиз, а НЕ логин.
        if re.search(r'subscription\s+detected', clean, re.I):
            self._logged_in = True

        # Литеральный BEATPORT_NOT_AUTHED — собственный сигнал OrpheusDL о том, что
        # войти НЕ удалось (появляется только ДО входа). Голый 401/403 считаем
        # авторизацией лишь пока не вошли; после входа его разбирает is_finished
        # (Territory-first + «нет прав»), не рвём поток здесь ложным FATAL.
        #
        # 19.09.2026: сетевой сбой (TLS-таймаут до api.beatport.com) НИКОГДА не
        # auth — ни сам, ни через голый 401/403, пока сеть в этом прогоне уже
        # сбоила или тариф не узнали из-за сети/прочего. Отозванный токен
        # («credentials were not provided») — тоже не пароль: вердикт даёт
        # is_finished, прогон не рвём, чтобы раннер мог повторить.
        if _RE_NET.search(clean) and not _RE_BAD_CREDS.search(clean):
            self._net_seen = True
        _bare_is_auth = (_RE_AUTH_BARE.search(clean) and not self._logged_in
                         and not self._net_seen
                         and self._tier_fail not in ("network", "other")
                         and not _RE_TOKEN_REJECTED.search(clean))
        if "BEATPORT_NOT_AUTHED" in clean or _RE_BAD_CREDS.search(clean) or _bare_is_auth:
            yield Event(
                kind=EventKind.FATAL,
                message="BEATPORT_NOT_AUTHED: неверный логин/пароль Beatport — проверь Settings",
                level=LineLevel.ERROR,
            )
            return

        yield from super().iter_events(clean, progress=progress)

    def classify_line(self, line: str) -> str:
        if _RE_ERROR.search(line):        return "error"
        if _RE_AUTH_FAIL.search(line):    return "error"
        if _RE_SUBSCRIPTION.search(line): return "warn"
        if _RE_SKIP.search(line):         return "warn"
        if _RE_DOWNLOADING.search(line) or _RE_TRACK_FILE.search(line):
            return "success"
        return "stdout"

    def parse_progress(self, line: str, current: int, total: int) -> tuple[int, int]:
        m = _RE_PROGRESS.search(line)
        if m:
            cur, tot = int(m.group(1)), int(m.group(2))
            return max(0, cur - 1), tot
        return current, total

    def is_finished(self, log_text: str, rc: int = -1) -> EngineResult:
        # Territory restriction is a 403 too, but it is NOT an auth failure — must be
        # checked first so it isn't mislabelled "неверный логин" (which sent the bot
        # down the wrong path / looked like a freeze). The track exists but isn't
        # licensed in this Beatport account's region; retrying can't help → no-retry.
        if re.search(r'Territory\s+Restricted|territory.?restrict|region\s+locked|BeatportError.*region|not.*available.*your.*(region|country)', log_text, re.I):
            return EngineResult(False, error="Beatport: трек недоступен в регионе твоего аккаунта "
                                             "(Territory Restricted) — нужен Beatport-аккаунт/прокси в "
                                             "разрешённой стране. Скачать нельзя.")
        # Голый 401/403 = «неверный логин» ТОЛЬКО если вход не удался. Если тариф
        # уже определился («… subscription detected …»), вход прошёл — тогда 403
        # падает НИЖЕ, в ветку прав/региона (иначе она недостижима: _RE_AUTH_FAIL
        # ловит «403» раньше неё). Литеральный BEATPORT_NOT_AUTHED — всегда auth.
        _login_ok = self._logged_in or bool(re.search(r'subscription\s+detected', log_text, re.I))
        # Порядок (19.09.2026): явный отказ по логину/паролю → отозванный токен →
        # сеть → и только потом голый 401/403, да и то лишь когда тариф был
        # проверен (иначе «subscription detected» не могло появиться, и отсутствие
        # этой строки ничего не доказывает).
        if "BEATPORT_NOT_AUTHED" in log_text or _RE_BAD_CREDS.search(log_text):
            return EngineResult(False, error="BEATPORT_NOT_AUTHED: неверный логин/пароль Beatport")
        if _RE_TOKEN_REJECTED.search(log_text):
            # Цепочка отозвана — пароль верный, мёртв токен. Сбрасываем сессию,
            # чтобы следующий запуск вошёл заново логином и паролем.
            _invalidate_bp_session()
            return EngineResult(False, error=_TOKEN_VERDICT)
        _net = bool(_RE_NET.search(log_text)) or self._net_seen
        _downloads = len(re.findall(r'Downloading track file|Saving\s*:', log_text, re.I))
        if _net and _downloads == 0:
            return EngineResult(False, error=_NET_VERDICT)
        if (_RE_AUTH_BARE.search(log_text) and not _login_ok and not _net
                and self._tier_fail not in ("network", "other")):
            return EngineResult(False, error="BEATPORT_NOT_AUTHED: неверный логин/пароль Beatport")

        # Beatport's download-URL request can 403 with a permission error that
        # is DISTINCT from a bad login (the login/subscription check above
        # already passed, or we wouldn't be here) — the account's OAuth grant
        # doesn't cover starting a stream for this track. Must be checked
        # before the "Downloading track file" success gate below: that gate
        # only tests whether a download was ATTEMPTED, not completed, and
        # "Downloading track file" prints unconditionally right before this
        # error — a track hitting it was silently recorded "done" with zero
        # bytes ever written (caught live, 2026-07-23, every attempt on one
        # track failed identically while history kept showing green).
        if re.search(r'You do not have permission to perform this action', log_text, re.I):
            return EngineResult(False, error="Beatport: аккаунту не хватает прав на скачивание "
                                             "этого трека (403 на запросе потока, не на логине — "
                                             "подписка активна). Возможно, ограничение по конкретному "
                                             "релизу; повтори с другим треком, чтобы понять масштаб.")

        # Success/skip markers MUST be checked before the subscription gate:
        # Orpheus prints "Professional subscription detected, allowing high and
        # lossless quality" on EVERY successful run, and _RE_SUBSCRIPTION matches
        # the word "Professional"/"subscription" in it — so checking subscription
        # first flagged real downloads (and skips of already-present files) as a
        # false "subscription required" error. Real subscription-lack errors emit a
        # different message AND produce no "Downloading track file" + rc!=0, so they
        # still reach the gate below.
        downloads = len(re.findall(r'Downloading track file|Saving\s*:', log_text, re.I))
        if downloads > 0:
            errs = len(re.findall(r'\berror\b|\bfailed\b', log_text, re.I))
            # "Downloading track file" only proves an attempt STARTED, not that
            # it finished — if the SAME log also shows failure markers (most
            # commonly this exact permission 403, but keep it general), don't
            # report success just because a download was attempted. Only a
            # true partial (some tracks_ok, some tracks_err) should reach the
            # caller as success=True; if every attempt failed, say so.
            if errs >= downloads:
                return EngineResult(False, error=f"OrpheusDL Beatport: скачивание начиналось, но "
                                                 f"не завершилось ({errs} ошибка/ошибок в логе) — "
                                                 f"файл не сохранён.")
            return EngineResult(success=True, tracks_ok=downloads - errs, tracks_err=errs)

        if rc == 0 and log_text.strip():
            skips = len(re.findall(r'skip|already exist', log_text, re.I))
            if skips:
                return EngineResult(success=True, tracks_ok=0)
            return EngineResult(success=True)

        if _RE_SUBSCRIPTION.search(log_text):
            return EngineResult(False, error="Требуется подписка Beatport Professional для FLAC/AAC 256")

        if rc == 0 and not log_text.strip():
            return EngineResult(False, error="OrpheusDL: нет вывода — проверь логин Beatport")

        # Surface the REAL exception from a Python traceback instead of a bare
        # "exit code 1" — the last "SomeError: message" line names the actual cause
        # (a connection drop, a parse error, a missing track), which the generic
        # message hid. A bare network error is also flagged so it reads as transient.
        _exc = ""
        for ln in reversed(log_text.splitlines()):
            s = ln.strip()
            if re.match(r'^[A-Za-z_][\w.]*(Error|Exception|Warning):\s', s):
                _exc = s[:200]
                break
        if _exc:
            if re.search(r'Connection|Timeout|terminated|Max retries|ConnectError|'
                         r'temporarily|Read timed out', _exc, re.I):
                return EngineResult(False, error=f"Beatport: сетевой обрыв ({_exc}) — повтори позже.")
            return EngineResult(False, error=f"OrpheusDL Beatport: {_exc}")
        return EngineResult(False, error=f"OrpheusDL Beatport: завершился с кодом {rc}")
