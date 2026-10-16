"""Каким интерпретатором запускать дочерние движки — ОДНО место на всё приложение.

Раньше девять движков брали `sys.executable`, то есть «тот питон, которым подняли
app.py». Это делало набор доступных пакетов зависимым от способа запуска: через
`run.bat` он случайно совпадал с `.venv` проекта, а при ручном старте системным
питоном — нет, и зависимость от этого совпадения была невидимой.

Чем это уже стреляло:
  • 10–11.08.2026 — Bearer Spotify не минтился шесть часов при полностью зелёной
    сводке: app.py подняли не тем питоном, и `import librespot` падал (см. скилл
    ripster-spotify-tokens). Для трёх движков Orpheus это тогда починили
    отдельной функцией — но КОПИЕЙ, в каждом файле своей;
  • 16.08.2026 — в системном Python 3.12, которым поднят сервер, НЕТ модуля
    `zotify`, а в `.venv` он есть. То есть прямо сейчас часть движка не может
    работать не из-за кода, а из-за способа запуска сервера.

Поэтому интерпретатор выбирается ЯВНО и в одном месте. `_orpheus_python()` в
движках Orpheus остаётся отдельным намеренно: ему нужен не `.venv`, а свой
изолированный `tools/orpheusvenv` (OrpheusDL пинит protobuf 3.15.8, который
ломает Apple-декрипт — см. скилл ripster-dependency-versions).
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

_cached: str | None = None


def _base_dir() -> Path:
    return Path(__file__).resolve().parent.parent


def _runs(path: Path) -> bool:
    """Интерпретатор не просто ЛЕЖИТ, а запускается.

    Проверка нужна: половина удалённого venv оставляет python.exe на месте, и
    молчаливый переход на него превратил бы «работает» в «ничего не запускается»
    по всем движкам разом.
    """
    try:
        return subprocess.run([str(path), "-c", "pass"], capture_output=True,
                              timeout=20).returncode == 0
    except Exception:
        return False


def app_python() -> str:
    """Путь к интерпретатору для дочерних процессов движков.

    Порядок: явное переопределение из окружения → `.venv` проекта → текущий.
    Результат кэшируется: проба стоит запуска процесса, а ответ за время жизни
    приложения не меняется.
    """
    global _cached
    if _cached:
        return _cached

    env = (os.environ.get("RIPSTER_ENGINE_PYTHON") or "").strip()
    if env and Path(env).is_file() and _runs(Path(env)):
        _cached = env
        return _cached

    base = _base_dir()
    for sub in (("Scripts", "python.exe"), ("bin", "python")):
        cand = base / ".venv" / sub[0] / sub[1]
        if cand.is_file() and _runs(cand):
            _cached = str(cand)
            return _cached

    # Своего venv в этой установке нет — работаем тем, чем подняли. Это по-прежнему
    # рабочий сценарий (портативная сборка), просто менее предсказуемый.
    _cached = sys.executable
    return _cached
