"""
Tidal engine — downloads via **OrpheusDL** (orpheus/modules/tidal).

Why not streamrip: streamrip can only refresh a Tidal access_token with its own
built-in client_id. A token pasted from the browser (cid=8049) or an old config
refresh-token (different uid/cid) can never be refreshed by streamrip → every
~16 h the access_token expired and downloads died with 401. It also can't parse
Tidal's DASH manifests (Hi-Res 24-bit), so MQA/Hi-Res silently fell back.

OrpheusDL keeps its OWN self-refreshing session (orpheus/config/loginstorage.bin)
and understands DASH, so it delivers true 24-bit Hi-Res FLAC and is the path to
Atmos (AC-4). The session is created once via:
    * TV login   — link.tidal.com device code (needs a browser once), OR
    * Mobile     — username + password (no browser; Tidal may block it, TV is
                   the reliable fallback)
Both are driven from Settings → Tidal; after that the session refreshes itself.

Download path: OrpheusDL CLI (`orpheus.py <tidal.com/browse/.../ID>`), cwd =
orpheus/. Quality is taken from the engine's OWN config corridor,
``orpheus/config/tidal/settings.json``, seeded from the shared
``orpheus/config/settings.json`` (see `seed_settings` for what is inherited);
the session stays the shared ``orpheus/config/loginstorage.bin``.

Search / album / artist metadata still use Tidal's public API with the pasted
``tidal-token`` (access) — that path is unchanged and independent of downloads.
"""
from __future__ import annotations

import json
import os
import re
import sys
import time
from pathlib import Path

from .base import EngineBase, EngineResult, Event, EventKind, LineLevel, _strip_ansi
from .errors import classify_download_error
from .registry import register
# Формирование команды OrpheusDL (bootstrap + коридор конфига + пин сессии) —
# одна реализация на все движки Orpheus, иначе они расходятся молча.
from .orpheus_beatport import orpheus_cmd
from ripster import http_client as _HTTP
from ripster.safe_pickle import safe_loads as _pickle_loads
from ripster.py_runtime import app_python


# ── OrpheusDL paths ───────────────────────────────────────────────────────────
def _base_dir() -> Path:
    return Path(sys.argv[0]).resolve().parent if sys.argv else Path(".").resolve()

def _orpheus_dir() -> Path:
    return _base_dir() / "orpheus"

def _orpheus_python() -> str:
    """OrpheusDL runs in its OWN venv (tools/orpheusvenv) — its protobuf==3.15.8
    pin would break AMD/pywidevine in the shared bundled python. The venv (made
    via virtualenv) is also non-isolated, so `python orpheus.py` finds orpheus.core.
    Falls back to the current interpreter. See ripster-dependency-versions skill."""
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

def _main_config_dir() -> Path:
    """Каталог конфига OrpheusDL по умолчанию — он же хранилище общей сессии."""
    return _orpheus_dir() / "config"

def _corridor_config_dir() -> Path:
    return _main_config_dir() / "tidal"

def seed_settings(dst: Path, src: Path | None = None) -> Path:
    """Посеять в коридор/слот копию общего settings.json — и только валидный JSON.

    Прямой `shutil.copy2` здесь не годился бы: общий файл пишет и OrpheusDL на
    финальной стадии прогона (core.py: `open(...,'w').write(json.dumps(...))`).
    Пойманный на середине записи обрезок скопировался бы в коридор и прогон
    упал бы уже там, в файле, которого до аварии не было. Поэтому читаем,
    проверяем `json.loads` и пишем атомарно: временный файл рядом + `os.replace`.

    Наследуется ВСЁ содержимое, а не только качество: в частности `modules.tidal`
    с идентификаторами клиентов (`tv_atmos_token` и др.) — без них вход владельца
    перестал бы обслуживаться. `src` передаёт пул: у него свой взгляд на то, где
    лежит установка (см. `tidal_pool._base_dir`).
    """
    src = src or (_main_config_dir() / "settings.json")
    try:
        if not src.is_file() or dst.exists():
            return dst
        _copy_verbatim(src, dst)
    except Exception:
        # Посев — удобство, а не условие работоспособности: без него прогон
        # получит defaults самого OrpheusDL, и это честнее падающего движка.
        pass
    return dst


def _copy_verbatim(src: Path, dst: Path) -> None:
    """Одна копия общего файла — атомарно и только если это валидный JSON.

    Проверка JSON обязательна: общий файл пишет и сам OrpheusDL на финальной
    стадии прогона (`open(...,'w').write(json.dumps(...))`), и пойманный на
    середине записи обрезок иначе переехал бы в коридор — уже в файл, которого
    до аварии не было вовсе.
    """
    text = src.read_text(encoding="utf-8")
    cfg = json.loads(text)                # битый/обрезанный → не копируем
    if not isinstance(cfg, dict) or not cfg:
        return
    st = src.stat()
    dst.parent.mkdir(parents=True, exist_ok=True)
    tmp = dst.with_name(f"{dst.name}.{os.getpid()}.tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, dst)                  # атомарно: полфайла никто не прочтёт
    # mtime переносим: иначе копия сразу выглядела бы «новее источника».
    try:
        os.utime(dst, ns=(st.st_atime_ns, st.st_mtime_ns))
    except OSError:
        pass


def seed_slot(slot_config_dir: Path, src: Path | None = None) -> Path:
    """Каталог настроек слота пула — тот же коридор, только живущий в папке
    учётки: копия общего файла при отсутствии + догон `modules.tidal` при каждой
    последующей смене. Возвращает путь к settings.json слота."""
    dst = seed_settings(Path(slot_config_dir) / "settings.json", src=src)
    _refresh_inherited_tidal_keys(dst, src=src)
    return dst


def _refresh_inherited_tidal_keys(dst: Path, src: Path | None = None) -> None:
    """Догнать из общего конфига `modules.tidal` — и ТОЛЬКО этот раздел.

    Почему не полная перезапись по mtime (как делал посев до правки): общий файл
    в конце КАЖДОГО прогона пишет сам OrpheusDL (core.py:365), то есть параллельный
    прогон Spotify обновляет его mtime, и «пересев» затёр бы качество и папку
    Tidal-прогона — ровно ту гонку, от которой коридор и защищает (поймано
    тестом: 'hifi' превращался в 'normal').

    И почему не весь раздел `modules`: там живут чужие сессии. Beatport ротит
    refresh-токен и пишет его в конфиг прогона; общую копию этого токена OrpheusDL
    не обновляет, и догон по всем модулям вернул бы в коридор уже ПОГАШЕННЫЙ
    токен. Берём только Tidal: движок правит в нём единственный флаг
    (`prefer_ac4`), и то сам, на каждом прогоне, после этого догона.

    Догон обязателен: это ключи учётки, которыми живёт вход владельца. Когда
    владелец меняет client_id в общем конфиге — коридор обязан это увидеть;
    то же после переустановки OrpheusDL, когда свежий общий файл появляется
    позже коридора.
    """
    try:
        src = src or (_main_config_dir() / "settings.json")
        if not src.is_file() or not dst.is_file():
            return
        shared = json.loads(src.read_text(encoding="utf-8")).get("modules", {}).get("tidal")
        if not isinstance(shared, dict) or not shared:
            return
        cfg = json.loads(dst.read_text(encoding="utf-8"))
        mods = cfg.get("modules")
        mods = mods if isinstance(mods, dict) else {}
        if mods.get("tidal") == shared:
            return                                       # и так всё на месте
        mods["tidal"] = shared
        cfg["modules"] = mods
        tmp = dst.with_name(f"{dst.name}.{os.getpid()}.tmp")
        tmp.write_text(json.dumps(cfg, indent=4, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, dst)
    except Exception:
        pass                                             # читаем то, что есть


def _config_dir_for(config: dict | None) -> Path:
    """Каталог конфига ЭТОГО прогона.

    Пул Tidal работает иначе, чем одиночный прогон: слот >0 — отдельная папка
    `dist/tidal_pool/acctN`, и его `config/` уже резолвится OrpheusDL относительно
    CWD прогона. Раннер говорит нам об этом напрямую (`_tidal_config_dir`), а не
    через переменную процесса: окружение у всех движков общее на один
    интерпретатор, и выставленный в нём `ORPHEUS_CONFIG_DIR` отравил бы
    параллельный прогон.
    """
    hint = (config or {}).get("_tidal_config_dir")
    if hint:
        return Path(hint)
    return _corridor_config_dir()

def _settings_path(config: dict | None = None) -> Path:
    dst = seed_settings(_config_dir_for(config) / "settings.json")
    _refresh_inherited_tidal_keys(dst)
    return dst

def _module_path() -> Path:
    return _orpheus_dir() / "modules" / "tidal"

def _session_path() -> Path:
    # OrpheusDL persists its saved sessions (TV / Mobile) here. A non-trivial
    # file means at least one session was created and can be self-refreshed.
    # В коридор НЕ переезжает: сюда пишет вход из Settings (routes/core.py),
    # и прогон обязан читать ровно тот же файл.
    return _main_config_dir() / "loginstorage.bin"

def _slot_session_path(config: dict | None) -> Path:
    """Сессия этого прогона: общая для основного аккаунта, своя у слота пула
    (`tidal_pool.write_session` выписывает её в каталог слота)."""
    hint = (config or {}).get("_tidal_session_storage")
    return Path(hint) if hint else _session_path()


# ── live access token from OrpheusDL's self-refreshing session ────────────────
# The pasted `tidal-token` dies in ~16 h and can't be refreshed (wrong
# account/client_id). OrpheusDL's TV refresh_token IS valid and long-lived, so
# search/metadata mint a fresh access_token from it (cached ~4 h) — the same
# self-healing path downloads use. Falls back to the pasted token if unavailable.
_AT_CACHE: dict = {"token": "", "exp": 0.0, "country": "", "user_id": ""}

_TOKEN_URL = "https://auth.tidal.com/v1/oauth2/token"


def _session_tv(path: Path) -> dict | None:
    """TV-session dict (refresh_token/country/user_id) из пикл-хранилища OrpheusDL.
    Обычные dict + datetime — импорт orpheus не нужен."""
    try:
        blob = _pickle_loads(path.read_bytes())
        return blob["modules"]["tidal"]["sessions"]["default"]["custom_data"]["sessions"]["TV"]
    except Exception:
        return None

def _read_tv_session() -> dict | None:
    """TV session dict (refresh_token/country/user_id) from the pickled
    loginstorage. Plain dicts + datetime only — no orpheus import needed."""
    return _session_tv(_session_path())


def session_refresh_token(session_file=None) -> str:
    """Refresh-токен, которым ЭТОТ прогон будет качать. Пусто — если сессии нет.

    Растёт из файла, а не из конфига, сознательно: расхождение этих двух
    хранилищ и есть причина аварии 23.09.2026 (см.
    docs/TIDAL_SESSION_DIAG_2026-09-23.md), и измерять здоровье надо ровно тем,
    что движок реально использует.
    """
    p = Path(session_file) if session_file else _session_path()
    return str((_session_tv(p) or {}).get("refresh_token") or "").strip()


def _refresh_form(refresh: str) -> dict:
    cid, csec = _tv_client()
    form = {"refresh_token": refresh, "client_id": cid, "grant_type": "refresh_token"}
    if csec:
        form["client_secret"] = csec
    return form


def _interpret_refresh(r) -> tuple[dict, str, bool]:
    """Ответ токен-эндпоинта → (данные, причина отказа, отозвана ли учётка).

    `revoked` — это «Tidal сказал нет по существу» (400/401/403/412: отзыв,
    блокировка `abuse_detected`, «токен выдан другому клиенту»). С этим не спорят:
    поможет только новый вход. Всё остальное (сеть, 429, 5xx) — свойство канала,
    учётку за это казнить нельзя.
    """
    try:
        j = r.json() if r.status_code == 200 else {}
    except Exception:
        j = {}
    if r.status_code == 200 and (j.get("access_token") or "").strip():
        return j, "", False
    why = ""
    try:
        body = r.json()
        why = str(body.get("error_description") or body.get("userMessage")
                  or body.get("error") or "").strip()
    except Exception:
        why = (getattr(r, "text", "") or "").strip()[:120]
    reason = f"HTTP {r.status_code}" + (f": {why[:120]}" if why else "")
    return {}, reason, r.status_code in (400, 401, 403, 412)


def _refresh_sync(refresh: str) -> tuple[dict, str, bool]:
    """Один refresh_token-грант (для синхронного pre-flight перед прогоном)."""
    if not refresh:
        return {}, "нет refresh-токена", True
    try:
        r = _HTTP.client().post(_TOKEN_URL, data=_refresh_form(refresh), timeout=20)
    except Exception as e:
        return {}, f"сеть: {type(e).__name__}", False
    return _interpret_refresh(r)


async def _refresh_async(refresh: str) -> tuple[dict, str, bool]:
    """То же, но из событийного цикла (путь поиска/станций)."""
    if not refresh:
        return {}, "нет refresh-токена", True
    try:
        async with _HTTP.ashared() as c:
            r = await c.post(_TOKEN_URL, data=_refresh_form(refresh), timeout=20)
    except Exception as e:
        return {}, f"сеть: {type(e).__name__}", False
    return _interpret_refresh(r)


# ── запись ротированных/восстановленных токенов обратно в конфиг ─────────────
# Правило владельца: «ротированные токены писать обратно». Молча жить свежим
# токеном в памяти — это тот же дефект, что убил NZ-учётку 23.09: файл и память
# разъезжаются, и следующий прогон (или другой процесс) берёт мёртвое значение.


def _token_key_files():
    from ripster import credential_health as _ch
    return _ch._yaml_files_to_check()


def persist_credentials(*, refresh: str = "", old_refresh: str = "", access: str = "",
                        country: str = "", expiry: int = 0) -> bool:
    """Сохранить новую пару токенов штатным путём продукта. True — если дошли.

    Ищем НЕ «ключ tidal-refresh», а то МЕСТО, где лежит `old_refresh`: ключ живёт
    то в config.yaml, то в tokens/tidal.yaml (это синхронизируемый снимок), и
    слепая правка config.yaml оставила бы в tokens/ прежнее значение — приложение
    при старте перетянет его обратно и вся починка обнулится.

    Записывает ТОЛЬКО ту учётку, которой принадлежит `old_refresh`. Иначе ротация
    refresh у слота пула молча переставила бы ОСНОВНУЮ учётку — а это ровно та
    подмена, из-за которой `dist/tidal_pool/acct2` хранил сессию владельца.
    """
    old = (old_refresh or "").strip()
    new = (refresh or "").strip()
    if not old or not new or old == new:
        return False                       # ротации нет — писать нечего
    primary_updates = {"tidal-refresh": new}
    for k, v in (("tidal-token", access), ("tidal-country", country),
                 ("tidal-token-expiry", str(expiry) if expiry else "")):
        if v:
            primary_updates[k] = v

    # Штатный путь продукта: точечно перечитать тот yaml, где ключ реально лежит
    # (им же пользуется сторож здоровья), а не переписать конфиг целиком — иначе
    # дефолты и секреты из tokens/ уехали бы в открытый config.yaml.
    import yaml
    from ripster.config_service import _atomic_write_yaml

    wrote = False
    for path in _token_key_files():
        try:
            data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        except Exception:
            continue
        if not isinstance(data, dict):
            continue
        if _apply_pair(data, old, new, primary_updates) and _atomic_write_yaml(path, data):
            wrote = True
    if wrote:
        # Правку сделал не основной процесс (или основной держит конфиг в памяти
        # и перезапишет файл целиком) — просим перечитать с диска.
        try:
            from ripster import credential_health as _ch
            _ch._notify_app_config_changed()
        except Exception:
            pass
    return wrote


def _apply_pair(store: dict, old: str, new: str, primary_updates: dict) -> bool:
    """Подставить `new` туда, где хранился `old`: основная учётка или запись пула.
    Возвращает True, если что-то изменилось."""
    changed = False
    if str(store.get("tidal-refresh") or "").strip() == old:
        for k, v in primary_updates.items():
            if str(store.get(k) or "") != str(v):
                store[k] = v
                changed = True
        return changed
    pool = store.get("tidal-accounts")
    if isinstance(pool, list):
        for a in pool:
            if isinstance(a, dict) and str(a.get("refresh") or a.get("tidal-refresh")
                                             or "").strip() == old:
                a["refresh"] = new
                a.pop("tidal-refresh", None)
                changed = True
    return changed


def repair_session(session_file, refresh: str, country: str = "") -> dict:
    """Перезаписать сессию OrpheusDL из заведомо живого refresh-токена.

    Одна реализация на все случаи (основной прогон, слот пула, health-мерка) —
    иначе починка разъедется с тем, чем слот заводится.
    """
    from ripster import tidal_pool as _tp
    return _tp.write_session_to(Path(session_file).parent, refresh, country)


def _adopt_minted(target: Path, stored: str, minted: dict) -> str:
    """Ответ Tidal с ДРУГИМ refresh — починить им и сессию, и конфиг.

    Tidal на этом клиенте refresh не ротирует (23.09 проверено: в ответе то же
    значение), поэтому ветка редкая — но именно редкость и опасна: ротация без
    записи обратно означала бы сессию, которую через сутки нечем перевыпустить.
    """
    new_refresh = str(minted.get("refresh_token") or "").strip()
    user = minted.get("user") or {}
    cc = str(user.get("countryCode") or "").upper()
    if new_refresh and new_refresh != stored:
        rep = repair_session(target, new_refresh, cc)
        if rep.get("ok"):
            persist_credentials(refresh=new_refresh, old_refresh=stored,
                                access=str(minted.get("access_token") or ""),
                                country=cc,
                                expiry=int(time.time()) + int(minted.get("expires_in") or 3600))
    return cc


async def _orpheus_access_token(config: dict | None = None) -> tuple[str, str]:
    """Return a FRESH (access_token, country) from the OrpheusDL TV session,
    cached ~4 h. ('', '') if no session / refresh fails."""
    now = time.time()
    if _AT_CACHE["token"] and now < _AT_CACHE["exp"]:
        return _AT_CACHE["token"], _AT_CACHE["country"]
    target = _session_path()
    tv = _session_tv(target)
    cid, csec = _tv_client()
    if not (tv and tv.get("refresh_token") and cid):
        return "", ""
    stored = str(tv["refresh_token"])
    j, why, revoked = await _refresh_async(stored)
    if not j and revoked:
        # Сессия мертва, а учётка может быть жива: 23.09.2026 хранилище OrpheusDL
        # и config.yaml разошлись, и поиск молча откатывался на протухший
        # вставленный токен. Пробуем refresh ЭТОЙ конфигурации — и при успехе
        # чиним файл, чтобы следующий прогон не повторял ту же починку.
        alt = str((config or {}).get("_tidal_refresh")
                  or (config or {}).get("tidal-refresh") or "").strip()
        if alt and alt != stored:
            j2, why2, _ = await _refresh_async(alt)
            if j2:
                j = j2
                rep = repair_session(target, alt, "")
                if rep.get("ok"):
                    persist_credentials(refresh=alt, old_refresh=stored)
    if not j:
        return "", ""
    _AT_CACHE["token"]   = j["access_token"]
    _AT_CACHE["exp"]     = now + max(60, int(j.get("expires_in", 3600)) - 120)
    _AT_CACHE["country"] = str(tv.get("country_code") or "").upper()
    _AT_CACHE["user_id"] = str(tv.get("user_id") or "")
    _adopt_minted(target, stored, j)
    if j.get("user"):
        _AT_CACHE["country"] = (str(j["user"].get("countryCode") or "")
                                or _AT_CACHE["country"]).upper()
    return _AT_CACHE["token"], _AT_CACHE["country"]


async def _tidal_token_country(config: dict) -> tuple[str, str]:
    """Prefer the fresh OrpheusDL-session token; fall back to the pasted one."""
    tok, cc = await _orpheus_access_token(config)
    if tok:
        return tok, (cc or (config.get("tidal-country") or "US").strip().upper() or "US")
    return ((config.get("tidal-token") or "").strip(),
            (config.get("tidal-country") or "US").strip().upper() or "US")


async def _api_get(c, url: str, params: dict, config: dict) -> tuple:
    """GET Bearer'ом из живого источника → (response, token, country).

    На 401 перевыпускает токен и повторяет РОВНО ОДИН раз. Раньше 401 значил
    «Tidal: токен истёк. Обнови access_token в Settings → Tidal» — совет, который
    не может помочь: access-токен продукту не нужен от слова «вручную», он
    перевыпускается из refresh. Именно так станции 23.09.2026 в 18:44 и
    отчитались смертью там, где достаточно было одного повторного запроса.
    """
    tok, cc = await _tidal_token_country(config)
    p = dict(params or {})
    p.setdefault("countryCode", cc)
    if not tok:
        # Ни живого, ни вставленного токена: запрос уйдёт анонимным и получит 401,
        # который мы иначе приняли бы за «истёк». Сказали честно — пустой ответ.
        return None, "", cc
    r = await c.get(url, headers={"Authorization": f"Bearer {tok}"}, params=p)
    if r.status_code == 401:
        _AT_CACHE["token"], _AT_CACHE["exp"] = "", 0.0
        tok2, cc2 = await _tidal_token_country(config)
        if tok2 and tok2 != tok:
            p["countryCode"] = cc2
            r = await c.get(url, headers={"Authorization": f"Bearer {tok2}"}, params=p)
            return r, tok2, cc2
    return r, tok, cc


def ensure_run_session(config: dict | None = None) -> str:
    """Pre-flight ПЕРЕД запуском OrpheusDL: сессия, которой качаем, жива?

    Движок (orpheus/modules/tidal) на отказе refresh не печатает ничего и
    возвращает False, а через две секунды падает `TidalAuthError` из проверки
    подписки — пользователь видит «переавторизуйся», тогда как настоящая причина
    лежит в одном HTTP-ответе. Проверяем сами и чиним, пока прогон не начался:
    стоит это один POST на загрузку против минутного процесса и трёх авто-повторов.

    Возвращает строку-заметку для лога. Бросает ValueError с честной причиной,
    когда починка невозможна (учётка отозвана Tidal).
    """
    config = config or {}
    target = _slot_session_path(config)
    stored = session_refresh_token(target)
    if not stored:
        return ""                                  # сессии нет — об этом скажет build_cmd
    j, why, revoked = _refresh_sync(stored)
    if j:
        cc = _adopt_minted(target, stored, j)
        return f"сессия подтверждена (country={cc or (j.get('user') or {}).get('countryCode', '?')})"
    if not revoked:
        # Канал, не учётка: не имеем права выдавать «переавторизуйся».
        raise ValueError(
            f"Tidal: токен-сервер не отвечает ({why}) — это НЕ смерть учётки, "
            f"повтор загрузки через минуту обычно помогает.")
    alt = str(config.get("_tidal_refresh") or config.get("tidal-refresh") or "").strip()
    if alt and alt != stored:
        j2, why2, _ = _refresh_sync(alt)
        if j2:
            rep = repair_session(target, alt, "")
            if rep.get("ok"):
                persist_credentials(refresh=alt, old_refresh=stored)
                return (f"сессия была мертва ({why}); перестроена из refresh-токена "
                        f"этой учётки")
    raise ValueError(
        "TIDAL_SESSION_REVOKED: Tidal отозвал сессию этой учётки "
        f"({why}) — автоматика восстановить не может, нужен новый вход в "
        "Settings → Tidal.")


# OrpheusDL-tidal ships these public TV (Atmos) client creds as its module
# defaults (orpheus/modules/tidal/interface.py). Embed them as a fallback so a
# FRESH GitHub clone — which has no orpheus/config/settings.json yet (gitignored)
# — can still run the link.tidal.com device-flow login straight out of the box.
_TV_TOKEN_DEFAULT  = "cgiF7TQuB97BUIu3"
_TV_SECRET_DEFAULT = "1nqpgx8uvBdZigrx4hUPDV2hOwgYAAAG5DYXOr6uNf8="
# Mobile-Atmos client id (orpheus tidal module default) — its session is what
# actually delivers AC-4 Atmos. A refresh_token from ANY Tidal session works with
# any client id, so we derive this session from the TV login automatically.
_MOBILE_ATMOS_TOKEN_DEFAULT = "km8T1xS355y7dd3H"

def _tidal_module_settings() -> dict:
    """Раздел `modules.tidal`: сперва из конфига этого прогона (в него посеяно
    всё из общего), а если его нет — прямо из общего. Второй путь обязателен для
    свежей установки: там `orpheus/config/settings.json` появляется раньше
    коридора, а идентификаторы клиентов нужны уже для входа."""
    for getter in (lambda: _settings_path(), lambda: _main_config_dir() / "settings.json"):
        try:
            mod = json.loads(getter().read_text(encoding="utf-8"))["modules"]["tidal"]
            if isinstance(mod, dict):
                return mod
        except Exception:
            continue
    return {}

def _tv_client() -> tuple[str, str]:
    st = _tidal_module_settings()
    return (st.get("tv_atmos_token") or _TV_TOKEN_DEFAULT,
            st.get("tv_atmos_secret") or _TV_SECRET_DEFAULT)

def _mobile_atmos_client() -> str:
    return _tidal_module_settings().get("mobile_atmos_hires_token") or _MOBILE_ATMOS_TOKEN_DEFAULT


# ── patterns ──────────────────────────────────────────────────────────────────
_RE_DOWNLOADING = re.compile(r'===\s*Downloading\s+(track|album|playlist|artist)\s+(.+?)\s*(?:\(|===)', re.I)
_RE_TRACK_FILE  = re.compile(r'Downloading track file|Saving\s*:', re.I)
_RE_TRACK_DONE  = re.compile(r'===\s*Track\s+\S+\s+downloaded|===\s*Done', re.I)
_RE_ERROR       = re.compile(r'\berror\b|\bfailed\b|\bexception\b|\bTraceback|Unsupported URL', re.I)
_RE_SKIP        = re.compile(r'skip|already exist|ignore', re.I)
_RE_PROGRESS    = re.compile(r'\bTrack\s+(\d+)\s*/\s*(\d+)', re.I)
# OrpheusDL streams a tqdm bar for the DASH segment download, e.g.
# " 33%|###2      | 21/64 [00:13<02:28, 3.45s/it]". Parse the percent so the
# progress bar actually moves per track instead of sitting at 0 (which read as
# "not downloading"). Track-level "=== Downloading track" bumps the counter.
_RE_PERCENT     = re.compile(r'(\d{1,3})%\|')
# Session/auth problems. EOFError appears when OrpheusDL hits an interactive
# login prompt (no/invalid session) with no stdin — we guard against that in
# build_cmd, but classify it too so the user gets a clear message.
# TidalAuthError / "token has expired" — found 2026-07-22: the pasted
# `tidal-token` (config key, ~16h lifetime, NOT auto-refreshed — see
# _orpheus_access_token's own docstring) expiring raised exactly this
# traceback, but neither this regex nor classify_download_error recognized
# it, so it fell all the way through to the generic "трек не докачался
# (DASH/сеть прервалась)" message — actively misleading the user toward
# retrying/switching VPN nodes, which can never fix an expired token. 3 auto-
# retries burned (15s/45s/120s backoff) before finally erroring, every time.
_RE_AUTH_FAIL   = re.compile(
    r'TIDAL_NOT_AUTHED|no saved session|relogin|invalid.*session|unauthorized|'
    r'\b401\b|\b403\b|EOFError|Choose a login method|'
    r'TidalAuthError|token has expired|token.*expired',
    re.I,
)

# Смерть сессии ПОСРЕДИ релиза. Отдельный, более узкий и одновременно более
# полный набор, чем `_RE_AUTH_FAIL`: он ловит и НАШУ СОБСТВЕННУЮ строку, которую
# ветка авторизации пишет в лог по каждому отказавшему треку. Якорь по ней —
# не прихоть: в живом логе (проверено на прогоне 15.08, альбом на 33 трека,
# оборвался на 17-м) английских маркеров в хвосте НЕТ ВОВСЕ, есть только
# «Tidal: сессия недействительна — переавторизуйся». Первая редакция этой правки
# смотрела на `_RE_AUTH_FAIL` и молча не срабатывала — ровно тот случай, когда
# детектор стоит в коде и не может сработать ни разу.
_RE_SESSION_DEAD = re.compile(
    r'сесси\w*\s+(?:истекла|оборвалась|недействительн)|переавторизуйся|'
    r'TIDAL_NOT_AUTHED|TidalAuthError|token\s+has\s+expired|no\s+saved\s+session',
    re.I,
)


# ── URL normalisation ─────────────────────────────────────────────────────────
# OrpheusDL's tidal module only accepts `tidal.com/(browse/)?<type>/<id>` — NOT
# `listen.tidal.com/...`. Rewrite whatever the user/search produced.
_RE_TIDAL_URL = re.compile(
    r'tidal\.com/(?:browse/)?(track|album|playlist|artist|mix|video)/([0-9a-fA-F-]+)',
    re.I,
)

def _to_orpheus_url(url: str) -> str:
    m = _RE_TIDAL_URL.search(url or "")
    if m:
        return f"https://tidal.com/browse/{m.group(1).lower()}/{m.group(2)}"
    return url


# ── quality mapping (Ripster id → OrpheusDL global.general.download_quality) ──
# OrpheusDL tidal: LOW=96 AAC, HIGH=320 AAC, LOSSLESS=16/44.1 FLAC,
# HI_RES (hifi)=<=24/48 FLAC + MQA.
# NB: the keys must cover EVERY quality code the front-ends emit, or an unknown
# code silently falls back to "lossless" (FLAC) while the card shows the picked
# label → "asked for AAC 320, got FLAC" mismatch. The guest picker (app.js) and
# player (player.js) send "hires" (no underscore) and "mp3" for AAC-320, so those
# aliases are mapped explicitly below alongside the canonical ids.
_QUALITY_ORPHEUS = {
    "hi_res":   "hifi",
    "hires":    "hifi",   # app.js/player.js emit "hires" without the underscore
    "atmos":    "hifi",   # Dolby Atmos rides the top FLAC tier + prefer_ac4 (AC-4)
    "mqa":      "hifi",
    "master":   "hifi",
    "hifi":     "hifi",
    "lossless": "lossless",
    "flac":     "lossless",
    "high":     "high",   # Tidal HIGH = AAC 320
    "320":      "high",   # raw "320" code from some pickers
    "aac":      "high",
    "mp3":      "high",   # player.js labels AAC-320 as "High" with code "mp3"
    "medium":   "high",
    "low":      "low",
    "minimum":  "low",
}


def _tidal_cover(uuid: str, size: int = 160) -> str:
    # Default 160px — small covers for search/release cards (less traffic, faster
    # load). Tidal CDN serves fixed sizes (80/160/320/640/1280). The downloader
    # fetches full-size art separately, so this only affects card display.
    if not uuid:
        return ""
    return f"https://resources.tidal.com/images/{uuid.replace('-', '/')}/{size}x{size}.jpg"


def is_installed() -> bool:
    return (_orpheus_dir() / "orpheus.py").exists() and (_module_path() / "interface.py").exists()


def is_authenticated(config: dict | None = None) -> bool:
    p = _session_path()
    try:
        return p.exists() and p.stat().st_size > 10
    except OSError:
        return False


def _update_orpheus_settings(quality: str, save_path: str, config: dict, atmos: bool = False) -> None:
    """Point OrpheusDL at the right quality + save folder (mirrors beatport).

    Пишется в каталог КОНКРЕТНОГО прогона (`_config_dir_for`), а не в общий
    `orpheus/config/settings.json`: полоса у Tidal своя, но прогоны разных
    сервисов идут параллельно, и общий файл означал бы гонку качества с
    соседним движком (тот же разбор, что покоридорил Beatport/JioSaavn).
    """
    sp = _settings_path(config)
    if not sp.exists():
        return
    try:
        cfg = json.loads(sp.read_text(encoding="utf-8"))
        gen = cfg.setdefault("global", {}).setdefault("general", {})
        if quality:
            gen["download_quality"] = quality
        if save_path:
            gen["download_path"] = save_path.rstrip("/\\") + "\\"

        # Единая раскладка папок: артист / релиз / файл — как у Apple, Deezer,
        # Yandex и Spotify (требование владельца 09.08.2026: единообразие по
        # ВСЕМ сервисам, сингл и альбом без разницы).
        #
        # Замер до правки показывал у Tidal три уровня вместо четырёх:
        # «tidal/FLAC CD/Group Therapy 689 (DJ Mix)/01. …» — папки артиста нет.
        #
        # Формат задаётся ЗДЕСЬ, а не только в движке Spotify, хотя settings.json
        # у них общий: иначе раскладка Tidal зависела бы от того, запускался ли
        # до него Spotify. Настройка, работающая через раз, хуже отсутствующей.
        fmt = cfg["global"].setdefault("formatting", {})
        fmt["album_format"]          = "{artist}/{name}{explicit}"
        fmt["force_album_format"]    = True     # сингл — тоже релиз, ему нужна папка
        fmt.setdefault("track_filename_format", "{track_number}. {name}")
        fmt.setdefault("enable_zfill", True)

        covers = cfg["global"].setdefault("covers", {})
        covers["embed_cover"]         = True
        # Embedded (in-audio) cover is pinned to 1000×1000 across ALL services
        # by request — uniform tag artwork. Sources natively smaller (e.g.
        # SoundCloud 500) just deliver their max.
        covers["main_resolution"]     = 1000
        # No per-track external cover file — it duplicated the SAME album art
        # once per track (~230MB of pure waste on a 14-track album) since
        # OrpheusDL writes it next to every track, not once per album. The
        # embedded cover + the one folder-level cover.jpg (written unconditionally
        # by OrpheusDL's _download_album_files, unaffected by this flag) already
        # cover both use cases. By request 2026-07-22.
        covers["save_external"]       = False

        # Atmos via AC-4 needs prefer_ac4; only request it on the top tier so
        # normal FLAC downloads stay stereo. (Mobile-Atmos session required for
        # actual AC-4 delivery — harmless when only TV session exists.)
        tm = cfg.setdefault("modules", {}).setdefault("tidal", {})
        tm["prefer_ac4"] = bool(atmos)

        # Тот же атомарный приём, что и при посеве: перечитывает этот файл уже
        # подпроцесс OrpheusDL, и полузаписанная строка была бы для него
        # `JSONDecodeError` на старте.
        tmp = sp.with_name(f"{sp.name}.{os.getpid()}.tmp")
        tmp.write_text(json.dumps(cfg, indent=4, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, sp)
    except Exception:
        pass


# Сколько отказов по правам подряд терпим, прежде чем прекратить прогон.
# Десять — заведомо больше, чем случайная пара недоступных треков, и
# кратно меньше тех 87, что набежали 09.08.2026.
_PERM_FAIL_LIMIT = 10
# Отказы «похожие на авторизацию» ПОСЛЕ первого сохранённого трека — это отдельные
# треки без прав, а не смерть сессии. Но если они пошли подряд и новых сохранений
# нет, прогон всё равно обязан закончиться (см. скилл ripster-runaway-runs).
_AUTH_FAIL_LIMIT = 10


@register
class TidalEngine(EngineBase):
    name = "tidal"

    _QUALITIES = [
        {"id": "hi_res",   "label": "MQA / Hi-Res", "sub": "Up to 24/192 MQA",  "badge": "HI-RES",   "color": "#ffd60a", "bitrate": "3000+ kbps", "ext": "flac", "req": "premium", "flag": "-q 3"},
        {"id": "atmos",    "label": "Dolby Atmos",  "sub": "AC-4 spatial (где доступно)", "sub_key": "qual.tidal.atmos.sub", "sub_args": {"codec": "AC-4 spatial"}, "badge": "ATMOS", "color": "#9090c8", "bitrate": "~768 kbps", "ext": "m4a", "req": "premium", "flag": ""},
        {"id": "lossless", "label": "FLAC",         "sub": "16-bit / 44.1 kHz", "badge": "LOSSLESS", "color": "#3ecfaa", "bitrate": "1411 kbps",  "ext": "flac", "req": "premium", "flag": "-q 2"},
        {"id": "high",     "label": "AAC 320",      "sub": "Lossy high",        "badge": "LOSSY",    "color": "#EF9F27", "bitrate": "320 kbps",   "ext": "m4a",  "req": "free",    "flag": "-q 1"},
        {"id": "low",      "label": "AAC 96",       "sub": "Lossy low",         "badge": "LOSSY",    "color": "#EF9F27", "bitrate": "96 kbps",    "ext": "m4a",  "req": "free",    "flag": "-q 0"},
    ]

    def qualities(self) -> list[dict]:
        return [{**q, "engine": self.name} for q in self._QUALITIES]

    def working_dir(self) -> str | None:
        # OrpheusDL resolves its modules relative to CWD — must run from orpheus/.
        return str(_orpheus_dir())

    def build_cmd(self, url: str, quality: str, config: dict) -> list[str]:
        # Тип ссылки, которого движок не умеет ВООБЩЕ. `_RE_TIDAL_URL` ниже
        # нормализует ещё `video` и `mix`, но модуль OrpheusDL принимает только
        # track|album|playlist|artist (`custom_url_parse`, interface.py:279) — на
        # остальном он печатает «Unsupported URL» и делает `exit()`. Наш разбор
        # хвоста этой строки не знал, поэтому прогон доезжал до общего финала и
        # получал «трек не докачался (DASH/сеть прервалась) — повтори; при
        # медленном VPN смени нод»: причина подменялась ровно противоположной по
        # смыслу — сеть тут ни при чём, и повтор не может помочь никогда.
        # 07.09.2026, https://tidal.com/browse/video/475048726: четыре захода
        # (03:55:12 → 03:58:24), три минуты ожидания, один и тот же тупик.
        # Третий случай того же класса в этом файле (см. TidalAuthError выше и
        # ветку 403 ниже), поэтому ловим ПЕРЕД запуском процесса, а не после.
        m_type = _RE_TIDAL_URL.search(url or "")
        if m_type and m_type.group(1).lower() in ("video", "mix"):
            kind = "видео" if m_type.group(1).lower() == "video" else "микс"
            raise ValueError(
                f"Tidal: {kind} не поддерживается движком — OrpheusDL умеет только "
                f"треки, альбомы, плейлисты и артистов. Повтор не поможет; "
                f"возьми ссылку на трек или альбом."
            )
        if not (_orpheus_dir() / "orpheus.py").exists():
            raise ValueError("OrpheusDL не установлен — см. Settings → Tidal")
        if not (_module_path() / "interface.py").exists():
            raise ValueError(
                "Модуль Tidal не установлен. Клонируй orpheusdl-tidal в "
                "orpheus/modules/tidal/"
            )
        if not is_authenticated(config):
            # Never let OrpheusDL reach its interactive input() prompt headless —
            # it would hang (or EOFError). Force a clear, actionable error.
            raise ValueError(
                "TIDAL_NOT_AUTHED: войди в Tidal в Settings → Tidal "
                "(TV-логин через link.tidal.com или mobile логин/пароль)"
            )
        # Pre-flight: сессия ЭТОГО прогона жива? Молча запускать OrpheusDL на
        # мёртвой сессии нельзя: он не печатает причину отказа refresh (у TV-ветки
        # её просто нет) и падает через 5 секунд с «переавторизуйся», тогда как
        # настоящая причина — в одном HTTP-ответе. Авария 23.09.2026 (403
        # abuse_detected у новозеландской учётки) выглядела ровно так.
        self._session_note = ensure_run_session(config)
        if self._session_note:
            print(f"[tidal] {self._session_note}", flush=True)

        save_path = config.get("tidal-save-path") or config.get("save-path") or ""
        orpheus_quality = _QUALITY_ORPHEUS.get(quality, "lossless")
        # Atmos (AC-4) when the user explicitly picks the atmos quality, OR on
        # hi_res with the global tidal-atmos flag (legacy behaviour preserved).
        atmos = (quality == "atmos") or (orpheus_quality == "hifi" and bool(config.get("tidal-atmos")))
        _update_orpheus_settings(orpheus_quality, save_path, config, atmos=atmos)

        # Каталог конфига — свой (см. `_config_dir_for`), а сессия — та, что
        # принадлежит ЭТОЙ учётке: для основного прогона это общий
        # `config/loginstorage.bin`, куда пишет вход из Settings, для слота пула —
        # его собственный файл, выписанный `tidal_pool.write_session`. Путь
        # сообщаем бутстрапом процесса, а не окружением app.py: окружение у всех
        # движков одно на интерпретатор.
        return orpheus_cmd(_config_dir_for(config), _to_orpheus_url(url), save_path,
                           session_file=_slot_session_path(config))

    def __init__(self):
        # Раннер читает abort_reason после каждой строки и глушит процесс
        # (см. ProcessRunner._engine_wants_abort).
        self.abort_reason = ""
        self._session_note = ""
        self._perm_fails = 0
        self._auth_fails = 0
        self._saved = 0

    def iter_events(self, line: str, *, progress: tuple[int, int]):
        clean = _strip_ansi(line).strip()
        if not clean:
            return

        # Прогон, который не может закончиться успехом, обязан прекращаться.
        # 09.08.2026: плейлист Tidal выдал 87 подряд «You do not have permission
        # to perform this action» и молотил двадцать минут, не сохранив ничего.
        # Тот же класс, что пустой публичный пул у Apple — и лечится так же.
        #
        # Условие с ДВУМЯ частями обязательно: если хоть один трек сохранился,
        # отказ по отдельным трекам — это нормальная недоступность в регионе, и
        # обрывать из-за неё весь плейлист нельзя.
        if _RE_TRACK_DONE.search(clean) or "Track download completed" in clean:
            self._saved += 1
            # «Подряд» значит подряд: новый сохранённый трек обнуляет серию, иначе
            # десяток разбросанных по альбому недоступных треков оборвал бы прогон,
            # который в остальном идёт нормально.
            self._auth_fails = 0
        if "do not have permission" in clean.lower():
            self._perm_fails += 1
            if self._perm_fails >= _PERM_FAIL_LIMIT and not self._saved and not self.abort_reason:
                self.abort_reason = (
                    f"Tidal отказал в правах на {self._perm_fails} треках подряд и не "
                    f"сохранил ни одного. Обычно это значит, что сессия не даёт "
                    f"запрошенное качество: попробуй уровень ниже (FLAC вместо Hi-Res) "
                    f"или переавторизуй Tidal."
                )
        if "TIDAL_NOT_AUTHED" in clean or _RE_AUTH_FAIL.search(clean):
            # ТОТ ЖЕ ДВУХЧАСТНЫЙ ЗАПРЕТ, ЧТО И ВЫШЕ: сессия, которая только что
            # сохранила треки, ЖИВА по определению. 15.08.2026 17:07 альбом на 33
            # трека умер на 17-м с «переавторизуйся» — после ШЕСТНАДЦАТИ выкачанных
            # FLAC. Совпало по `\b401\b|\b403\b|unauthorized`, а такую строку легко
            # даёт отдельный трек без прав в регионе. Человеку велели войти заново
            # (не поможет НИКОГДА), а 16 готовых треков уехали в «ошибка».
            #
            # Сырую строку теперь показываем: раньше она сюда входила и исчезала,
            # заменяясь вердиктом, — по логу невозможно было понять, что совпало.
            if self._saved:
                self._auth_fails += 1
                if self._auth_fails >= _AUTH_FAIL_LIMIT and not self.abort_reason:
                    self.abort_reason = (
                        f"Tidal перестал отдавать треки после {self._saved} сохранённых "
                        f"({self._auth_fails} отказов подряд). Сохранённое останется; "
                        f"остаток забери повтором или из другого сервиса."
                    )
                yield Event(
                    kind=EventKind.TRACK_ERROR,
                    message=(f"Tidal не отдал трек (вход исправен, сохранено: "
                             f"{self._saved}): {clean}"),
                    level=LineLevel.ERROR,
                )
                return
            yield Event(
                kind=EventKind.FATAL,
                message=("Tidal: сессия недействительна — переавторизуйся в "
                         f"Settings → Tidal · {clean}"),
                level=LineLevel.ERROR,
            )
            return
        yield from super().iter_events(clean, progress=progress)

    def classify_line(self, line: str) -> str:
        if _RE_ERROR.search(line):     return "error"
        if _RE_AUTH_FAIL.search(line): return "error"
        if _RE_SKIP.search(line):      return "warn"
        if _RE_DOWNLOADING.search(line) or _RE_TRACK_FILE.search(line):
            return "success"
        return "stdout"

    def parse_progress(self, line: str, current: int, total: int) -> tuple[int, int]:
        m = _RE_PROGRESS.search(line)
        if m:
            return max(0, int(m.group(1)) - 1), int(m.group(2))
        mp = _RE_PERCENT.search(line)
        if mp:
            return int(mp.group(1)), 100
        return current, total

    def is_finished(self, log_text: str, rc: int = -1) -> EngineResult:
        if "TIDAL_NOT_AUTHED" in log_text:
            return EngineResult(False, error="TIDAL_NOT_AUTHED: нужна авторизация Tidal (Settings → Tidal)")

        # Count REAL completions only. OrpheusDL prints "Downloading track file"
        # at the START of a track (music_downloader.py:389) and
        # "=== Track <id> downloaded ===" only AFTER it fully lands (line 633).
        # Counting the start marker caused FALSE SUCCESS: a DASH download that
        # dropped mid-way (slow/cut VPN) reported "done" with an empty folder.
        done = len(re.findall(r'===\s*Track\s+\S+\s+downloaded\s*===', log_text, re.I))
        if done > 0:
            errs = len(re.findall(r'\berror\b|\bfailed\b|\bexception\b|Traceback', log_text, re.I))
            # Ветка `done > 0` КОРОТИЛА всё остальное: насчитала скачанное и
            # вернула успех с ПУСТОЙ причиной. 15.08.2026 альбом на 33 трека
            # умер на 17-м (сессия Tidal), шестнадцать файлов легли на диск — и
            # раннер записал «error: (none captured)», хотя готовая причина
            # лежала строкой ниже в том же логе. Дальше по цепочке пустая строка
            # не совпадала ни с одной отсечкой, поэтому задача повторялась
            # трижды, а недобор объяснялся «сбоем постобработки, повтори».
            #
            # Файлы всё равно отдаём (success=True) — они настоящие и нужны
            # человеку. Но причину недобора называем: она и остановит повтор, и
            # объяснит, почему треков меньше, чем в релизе.
            if _RE_SESSION_DEAD.search(log_text):
                return EngineResult(
                    success=True, tracks_ok=done, tracks_err=errs,
                    error=("Tidal: сессия оборвалась на середине релиза — забрано "
                           f"{done} трек(ов), остальные не отданы. Переавторизуйся "
                           "в Settings → Tidal и добери остаток."))
            return EngineResult(success=True, tracks_ok=done, tracks_err=errs)

        if rc == 0 and _RE_SKIP.search(log_text):
            return EngineResult(success=True, tracks_ok=0)

        if _RE_AUTH_FAIL.search(log_text):
            return EngineResult(
                success=False,
                error="Tidal: сессия истекла/недействительна — переавторизуйся в Settings → Tidal.",
            )

        # Отказ по правам (403 «You do not have permission») при НУЛЕ докачанных
        # треков. Без этой ветки прогон доходил до общего хвоста функции и получал
        # «трек не докачался (DASH/сеть прервалась) — повтори»: не просто потерю
        # причины, а замену её на противоположную по смыслу — сеть тут ни при чём,
        # повтор гарантированно даст тот же отказ. Формулировка намеренно содержит
        # «не хватает прав» — по этой строке `_RE_NO_RETRY` в runner.py отсекает
        # бессмысленный авто-повтор (см. там же про вердикт `abort_reason`).
        if re.search(r'You do not have permission to perform this action', log_text, re.I):
            return EngineResult(False, error="Tidal: аккаунту не хватает прав на скачивание в "
                                             "этом качестве (403 на запросе потока, не на логине). "
                                             "Обычно помогает уровень ниже — FLAC вместо Hi-Res — "
                                             "либо переавторизация Tidal.")

        # Region-lock / phantom-removed link (e.g. OrpheusDL: "Album [X] not found.
        # This might be region-locked.") — surface the REAL cause via the shared
        # classifier, not the misleading "track didn't download (DASH/network)".
        _cls = classify_download_error(log_text)
        if _cls:
            return EngineResult(False, error=f"Tidal: {_cls[1]}")

        # То же самое, но уже из ЛОГА — на случай, если тип ссылки проскочит мимо
        # ранней отсечки в build_cmd (например, OrpheusDL сузит `custom_url_parse`
        # ещё раз). Без этой ветки строка снова утекала бы в общий финал и снова
        # звала бы человека «повторить и сменить нод».
        if re.search(r'Unsupported URL', log_text, re.I):
            return EngineResult(
                False,
                error="Tidal: такая ссылка не поддерживается движком — OrpheusDL умеет "
                      "только треки, альбомы, плейлисты и артистов. Повтор не поможет.")

        if not log_text.strip():
            return EngineResult(False, error="Tidal/OrpheusDL: нет вывода — проверь сессию Tidal")

        # No completion marker → the track did not finish. Return error so the
        # runner's salvage-from-disk can still deliver any real files that landed;
        # if none did, the user gets an honest "retry" instead of a phantom done.
        return EngineResult(
            success=False,
            error="Tidal: трек не докачался (DASH/сеть прервалась) — повтори; при медленном VPN смени нод",
        )

    @staticmethod
    def _avail_from_item(item: dict) -> list[str]:
        """Real per-release quality badges from Tidal's own fields — quality genuinely
        VARIES per album (many are lossy-only, some Hi-Res, some Atmos), so we read the
        item's audioQuality / audioModes / mediaMetadata.tags rather than assume a max."""
        tags = set((item.get("mediaMetadata") or {}).get("tags") or [])
        modes = set(item.get("audioModes") or [])
        q = (item.get("audioQuality") or "").upper()
        out: list[str] = []
        if "DOLBY_ATMOS" in modes or "DOLBY_ATMOS" in tags:
            out.append("ATMOS")
        if "HIRES_LOSSLESS" in tags or q in ("HI_RES", "HI_RES_LOSSLESS"):
            out.append("HI-RES")
        if "LOSSLESS" in tags or q == "LOSSLESS" or "HI-RES" in out:
            out.append("FLAC")
        if q in ("HIGH", "LOW") and not out:
            out.append("AAC")
        # De-dupe keep order; fall back to Tidal's lossless baseline if fields absent.
        seen, uniq = set(), []
        for t in out:
            if t not in seen:
                seen.add(t); uniq.append(t)
        return uniq or ["FLAC"]

    async def search(self, query: str, search_type: str, limit: int, config: dict) -> list[dict]:
        type_map = {"album": "albums", "track": "tracks", "artist": "artists"}
        t_type   = type_map.get(search_type, "albums")
        try:
            async with _HTTP.ashared() as c:
                r, token, country = await _api_get(
                    c, f"https://api.tidal.com/v1/search/{t_type}",
                    {"query": query, "limit": limit}, config)
                if r is None:
                    return []
                if r.status_code == 401:
                    return []
                data = r.json()
            results = []
            for item in data.get("items") or []:
                if t_type == "albums":
                    alb_id = str(item.get("id", ""))
                    results.append({
                        "id":      alb_id,
                        "title":   item.get("title", ""),
                        "artist":  (item.get("artist") or {}).get("name", ""),
                        "artist_id": str((item.get("artist") or {}).get("id", "")),
                        "type":    search_type,
                        "url":     f"https://listen.tidal.com/album/{alb_id}",
                        "cover":   _tidal_cover(item.get("cover", "")),
                        # Полная дата — карточки сортируются по ней. Раньше здесь
                        # оставался только год, и релизы Tidal в выборке по
                        # новизне вставали куда попало.
                        "date":    (item.get("releaseDate") or "")[:10],
                        "year":    (item.get("releaseDate") or "")[:4],
                        "tracks":  item.get("numberOfTracks"),
                        "available": self._avail_from_item(item),
                        "service": "tidal",
                    })
                elif t_type == "tracks":
                    tr_id = str(item.get("id", ""))
                    results.append({
                        "id":      tr_id,
                        "title":   item.get("title", ""),
                        "artist":  (item.get("artist") or {}).get("name", ""),
                        "artist_id": str((item.get("artist") or {}).get("id", "")),
                        "type":    search_type,
                        "url":     f"https://listen.tidal.com/track/{tr_id}",
                        "cover":   _tidal_cover((item.get("album") or {}).get("cover", "")),
                        "available": self._avail_from_item(item),
                        "service": "tidal",
                    })
                elif t_type == "artists":
                    art_id = str(item.get("id", ""))
                    results.append({
                        "id":      art_id,
                        "title":   item.get("name", ""),
                        "artist":  item.get("name", ""),
                        "type":    search_type,
                        "url":     f"https://listen.tidal.com/artist/{art_id}",
                        "cover":   _tidal_cover(item.get("picture", "")),
                        "service": "tidal",
                    })
            return results
        except Exception:
            return []

    async def get_artist(self, artist_id: str, types: str, config: dict) -> dict:
        wanted  = {t.strip() for t in types.split(",") if t.strip()} if types else set()
        # `all` = «без фильтра», а не тип релиза. Ниже идёт прямое сравнение
        # `type_norm not in wanted`, поэтому без этой нормализации выбор «все
        # типы» отдавал бы пустую дискографию — ровно то, что нашлось у Spotify
        # 04.09.2026. У Deezer и Qobuz эта же оговорка стоит на месте фильтра.
        if "all" in wanted:
            wanted = set()
        import asyncio as _aio
        try:
            async with _HTTP.ashared() as c:
                info_r, token, country = await _api_get(
                    c, f"https://api.tidal.com/v1/artists/{artist_id}", {}, config)
                if info_r is None:
                    return {"error_key": "err.tidal_no_token_cfg",
                            "error": "Tidal access_token не настроен", "releases": []}
                if info_r.status_code == 401:
                    return {"error_key": "err.tidal_token_expired",
                            "error": "Tidal: токен не принят даже после перевыпуска — "
                                     "обнови вход в Settings → Tidal.",
                            "releases": []}
                headers = {"Authorization": f"Bearer {token}"}
                info = info_r.json()
                aname = (info.get("name") or "").strip().lower()

                async def _albs(filt: str | None):
                    p = {"countryCode": country, "limit": 100, "offset": 0}
                    if filt:
                        p["filter"] = filt
                    rr = await c.get(f"https://api.tidal.com/v1/artists/{artist_id}/albums",
                                     headers=headers, params=p)
                    return (rr.json().get("items") or []) if rr.status_code == 200 else []

                # default = свои альбомы; EPSANDSINGLES = свои EP/синглы;
                # COMPILATIONS = «с этим артистом» (микс/сборник/радио — раньше
                # они вообще не приходили, дискография Tidal была неполной).
                own, eps, comps = await _aio.gather(
                    _albs(None), _albs("EPSANDSINGLES"), _albs("COMPILATIONS"))

                # для компиляций — чей это релиз и какой трек артиста в нём
                comp_slice = comps[:24]

                async def _who(alb):
                    cid = str(alb.get("id", ""))
                    try:
                        tr = await c.get(f"https://api.tidal.com/v1/albums/{cid}/tracks",
                                         headers=headers, params={"countryCode": country, "limit": 100})
                        items = (tr.json().get("items") or []) if tr.status_code == 200 else []
                        mine = [t.get("title", "") for t in items
                                if str((t.get("artist") or {}).get("id", "")) == str(artist_id)
                                or any(str(a.get("id", "")) == str(artist_id) for a in (t.get("artists") or []))
                                or (aname and aname in (t.get("title", "") or "").lower())]
                        return cid, "; ".join(dict.fromkeys(x for x in mine if x))
                    except Exception:
                        return cid, ""
                comp_tracks = dict(await _aio.gather(*[_who(a) for a in comp_slice])) if comp_slice else {}

            releases = []
            seen_ids: set[str] = set()

            def _add(alb, forced_type: str | None):
                alb_id = str(alb.get("id", ""))
                if not alb_id or alb_id in seen_ids:
                    return
                alb_type = alb.get("type", "ALBUM").lower()
                type_norm = forced_type or {"album": "album", "ep": "single", "single": "single",
                                            "compilation": "compilation"}.get(alb_type, "album")
                alb_artist = (alb.get("artist") or {}).get("name", "") or ""
                is_appears = type_norm == "compilation" or (
                    alb_artist and alb_artist.strip().lower() != aname
                    and alb_artist.strip().lower() != "" and forced_type is None
                    and not any(str(a.get("id", "")) == str(artist_id) for a in (alb.get("artists") or [])))
                if is_appears:
                    type_norm = "compilation"
                if wanted and type_norm not in wanted:
                    return
                seen_ids.add(alb_id)
                row = {
                    "id":      alb_id,
                    "title":   alb.get("title", ""),
                    "artist":  info.get("name", ""),
                    "type":    type_norm,
                    "url":     f"https://listen.tidal.com/album/{alb_id}",
                    "cover":   _tidal_cover(alb.get("cover", "")),
                    "year":    (alb.get("releaseDate") or "")[:4],
                    "date":    alb.get("releaseDate", ""),
                    "tracks":  alb.get("numberOfTracks"),
                    "service": "tidal",
                }
                if type_norm == "compilation":
                    if alb_artist:
                        row["album_artist"] = alb_artist
                    ap = comp_tracks.get(alb_id, "")
                    if ap:
                        row["appears_as"] = ap
                releases.append(row)

            for alb in own:
                _add(alb, None)
            for alb in eps:
                _add(alb, None)
            for alb in comps:
                _add(alb, "compilation")
            return {
                "artist": {
                    "id":      str(info.get("id", artist_id)),
                    "name":    info.get("name", ""),
                    "cover":   _tidal_cover(info.get("picture", "")),
                    "url":     f"https://listen.tidal.com/artist/{artist_id}",
                    "service": "tidal",
                },
                "releases": releases,
            }
        except Exception as e:
            return {"error": str(e), "releases": []}

    async def get_album(self, album_id: str, config: dict) -> dict:
        try:
            async with _HTTP.ashared() as c:
                alb_r, token, country = await _api_get(
                    c, f"https://api.tidal.com/v1/albums/{album_id}", {}, config)
                if alb_r is None:
                    return {"error_key": "err.tidal_no_token_cfg",
                            "error": "Tidal access_token не настроен"}
                if alb_r.status_code == 401:
                    return {"error_key": "err.tidal_token_expired",
                            "error": "Tidal: токен не принят даже после перевыпуска — "
                                     "обнови вход в Settings → Tidal."}
                headers = {"Authorization": f"Bearer {token}"}
                a = alb_r.json()
                tr_r = await c.get(f"https://api.tidal.com/v1/albums/{album_id}/tracks",
                                   headers=headers,
                                   params={"countryCode": country, "limit": 100})
                tr_data = tr_r.json()
            tracks = []
            for t in tr_data.get("items") or []:
                tr_id = str(t.get("id", ""))
                tracks.append({
                    "id":       tr_id,
                    "track_no": t.get("trackNumber"),
                    "disc":     t.get("volumeNumber"),   # multi-disc support
                    "title":    t.get("title", ""),
                    "artist":   (t.get("artist") or {}).get("name", ""),
                    "duration": t.get("duration", 0),
                    "preview":  "",
                    "explicit": t.get("explicit", False),
                    "url":      f"https://listen.tidal.com/track/{tr_id}",
                })
            real_id = str(a.get("id", album_id))
            return {
                "album": {
                    "id":      real_id,
                    "title":   a.get("title", ""),
                    "artist":  (a.get("artist") or {}).get("name", ""),
                    "cover":   _tidal_cover(a.get("cover", ""), 640),
                    "year":    (a.get("releaseDate") or "")[:4],
                    "date":    a.get("releaseDate", ""),
                    "label":   a.get("label", ""),
                    "upc":     a.get("upc", ""),
                    "genre":   a.get("genre", ""),
                    "tracks":  a.get("numberOfTracks"),
                    "url":     f"https://listen.tidal.com/album/{real_id}",
                    "service": "tidal",
                },
                "tracks": tracks,
            }
        except Exception as e:
            return {"error": str(e)}
