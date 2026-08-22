"""Autonomous Spotify web-player Bearer keeper (fixes the overnight-OGG death).

OGG downloads need a web-player Bearer in orpheus/config/spotify-token.txt for the
api-partner GraphQL metadata calls. The browser extension pushes it every few
minutes — but ONLY while its tab is awake. Overnight the tab idles, the Bearer
(~1 h life) expires, and guests get "Spotify token expired (401)".

This keeper mints a fresh Bearer from the DURABLE librespot blob (created once via
tools/spotify_pair.py, long-lived, browser-independent) and writes it to the same
file — so the Bearer stays fresh with no browser.

Design choices that matter:
  * It only mints when the file is STALE (> _STALE_AFTER). While the extension is
    active and pushing fresh tokens, the keeper sees a recent file and does
    NOTHING — zero extra Spotify API pressure. It acts only when the extension is
    gone (the overnight case). This respects the account-ban risk of hammering
    Spotify (the dev-API release scanner is what gets banned; this uses the
    web-player keymaster via librespot, a different/cheaper path, and at most
    ~once/45 min).
  * The actual mint runs in a SEPARATE process (tools/spotify_mint_token.py) so
    librespot's global protobuf flag + network session never touch the app.
"""
from __future__ import annotations

import asyncio
import os
import sys
import time
from pathlib import Path

_CHECK_EVERY  = 900     # wake every 15 min
_STALE_AFTER  = 2400    # mint if the Bearer file is older than 40 min (life ~60)
_MINT_TIMEOUT = 100     # hard cap on the librespot mint subprocess

_MINT_PY_CACHE: dict = {}   # модуль -> интерпретатор, который его импортирует


def _orpheus_dir(base_dir: Path) -> Path:
    return base_dir / "orpheus"


def _blob_path(base_dir: Path) -> Path:
    return _orpheus_dir(base_dir) / "config" / ".librespot_cache" / "reusable_credentials.json"


def _token_path(base_dir: Path) -> Path:
    return _orpheus_dir(base_dir) / "config" / "spotify-token.txt"


def _helper_path(base_dir: Path) -> Path:
    return base_dir / "tools" / "spotify_mint_token.py"


def _age_seconds(p: Path) -> float:
    try:
        return time.time() - p.stat().st_mtime
    except OSError:
        return 1e9   # missing → infinitely stale → mint


def _mint_env() -> dict:
    """Окружение минта. `librespot` НЕ импортируется без чистого-питоновского
    protobuf: с нативной реализацией его сгенерённые `_pb2` падают на
    «Descriptors cannot be created directly». Один и тот же env обязателен и для
    запуска хелпера, и для пробы наличия модуля — иначе проба говорит «модуля
    нет» там, где он есть, и выбирает не тот интерпретатор."""
    env = dict(os.environ)
    env.setdefault("PROTOCOL_BUFFERS_PYTHON_IMPLEMENTATION", "python")
    return env


def _interpreter_has(py: str, module: str) -> bool:
    import subprocess
    try:
        return subprocess.run([py, "-c", f"import {module}"], capture_output=True,
                              timeout=60, env=_mint_env()).returncode == 0
    except Exception:
        return False


def _mint_python(base_dir: Path, module: str) -> str:
    """Интерпретатор, которым запускать хелпер, требующий `module`.

    ЗАЧЕМ. `librespot` намеренно НЕ стоит в общем рантайме (VERSIONS.md: его
    транзитивные деп конфликтуют с protobuf, на котором держится Apple-декрипт),
    он живёт в `.venv`. Пока app.py запускали из `.venv`, `sys.executable`
    случайно совпадал с нужным — и зависимость от этого совпадения была
    невидимой. 10.08.2026 app.py перезапустили под `C:\\Python314`, где
    librespot нет, и Bearer перестал обновляться: минт молча падал с
    «No module named 'librespot'» 6 часов, при зелёной проверке токенов.
    Поэтому интерпретатор для минта ВЫБИРАЕТСЯ по факту наличия модуля, а не
    наследуется от того, чем запустили приложение.
    """
    cached = _MINT_PY_CACHE.get(module)
    if cached and _interpreter_has(cached, module):
        return cached
    env_py = os.environ.get("RIPSTER_MINT_PYTHON", "").strip()
    candidates = [p for p in (
        env_py,
        sys.executable,
        str(base_dir / ".venv" / "Scripts" / "python.exe"),
        str(base_dir / ".venv" / "bin" / "python"),
    ) if p]
    for py in candidates:
        if (py == sys.executable or Path(py).exists()) and _interpreter_has(py, module):
            if py != sys.executable:
                print(f"[sp-keeper] {module} нет в {Path(sys.executable).parent.name} — "
                      f"минтую через {py}", flush=True)
            _MINT_PY_CACHE[module] = py
            return py
    return sys.executable          # пусть хелпер честно скажет, чего ему не хватает


async def _run_helper(base_dir: Path, helper: Path, needs: str = "") -> bool:
    if not helper.exists():
        return False
    env = _mint_env()
    py = await asyncio.to_thread(_mint_python, base_dir, needs) if needs else sys.executable
    try:
        proc = await asyncio.create_subprocess_exec(
            py, str(helper), str(_orpheus_dir(base_dir)),
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT, env=env,
        )
        try:
            out, _ = await asyncio.wait_for(proc.communicate(), timeout=_MINT_TIMEOUT)
        except asyncio.TimeoutError:
            proc.kill()
            print(f"[sp-keeper] {helper.name} timed out", flush=True)
            return False
        msg = (out or b"").decode("utf-8", "replace").strip()
        if msg:
            print(f"[sp-keeper] {msg.splitlines()[-1]}", flush=True)
        return proc.returncode == 0
    except Exception as e:
        print(f"[sp-keeper] {helper.name} error: {e}", flush=True)
        return False


async def _mint_once(base_dir: Path) -> bool:
    # PRIMARY: the CORRECT token — a web-player bearer minted from the sp_dc cookie
    # (the only kind api-partner/getTrack accepts). Works autonomously ONLY when a
    # non-RU `spotify-proxy` + current TOTP secret are configured (Spotify blocks our
    # RU IP with 403 and rotates the TOTP). See tools/spotify_web_token.py.
    web = base_dir / "tools" / "spotify_web_token.py"
    if await _run_helper(base_dir, web):
        return True
    # FALLBACK: librespot/keymaster bearer (works for some endpoints; often 401s on
    # getTrack, but kept so nothing regresses where it still helps).
    return await _run_helper(base_dir, _helper_path(base_dir), needs="librespot")


async def mint_now(base_dir: Path | None = None) -> bool:
    """Публичная точка «перевыпусти Bearer сейчас» — для сторожа и ручного вызова.

    Существует потому, что сторож (`ripster/watchdog.py`) искал именно это имя и
    не находил: без него ремонт Spotify-токена был пустышкой. Не переименовывать
    молча — проверь вызовы.
    """
    return await _mint_once(Path(base_dir) if base_dir else Path(__file__).resolve().parent.parent)


async def run(config, base_dir: Path) -> None:
    """Background loop. No-op (logs once) when no durable blob exists — the user
    hasn't paired the desktop client, so there's nothing to mint from."""
    base_dir = Path(base_dir)
    if not _blob_path(base_dir).exists():
        print("[sp-keeper] no durable Spotify blob — keeper idle "
              "(run tools/spotify_pair.py once to enable autonomous tokens)", flush=True)
        return
    print("[sp-keeper] started (mints Spotify Bearer from blob when extension is idle)", flush=True)
    while True:
        try:
            age = _age_seconds(_token_path(base_dir))
            if age >= _STALE_AFTER:
                why = "missing" if age > 1e8 else f"{int(age)//60} min old"
                print(f"[sp-keeper] Bearer stale ({why}) → minting from blob", flush=True)
                if not await _mint_once(base_dir):
                    # Не прячем провал в INFO-строке хелпера: без Bearer радар и
                    # OGG-загрузки мертвы, а «токен на месте» проверка показывала
                    # зелёным (файл-то есть, просто протух).
                    print("[sp-keeper] МИНТ НЕ УДАЛСЯ — Bearer остаётся протухшим, "
                          "радар/OGG не работают (см. строку хелпера выше)", flush=True)
            # else: extension is keeping it fresh — do nothing (no API pressure).
        except Exception as e:
            print(f"[sp-keeper] loop error: {e}", flush=True)
        await asyncio.sleep(_CHECK_EVERY)
