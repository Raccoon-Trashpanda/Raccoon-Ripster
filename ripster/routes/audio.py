"""Маршруты своего аудиотракта ПК (bit-perfect вывод локальных файлов).

Install: audio.install(app, ctx)

Сам движок — `ripster.audio_engine`. Здесь только доступ: список устройств,
запуск, остановка и ЧЕСТНОЕ состояние. Слов для экрана тут нет: отдаются
числа (частоты, режим), текст подбирает интерфейс.
"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException

from ripster import audio_engine as _ae
from ripster.i18n_msg import imsg

router = APIRouter()

_s: dict = {}


def install(app, ctx) -> None:
    _s.update({"config": ctx.config})
    app.include_router(router)


def _state_dict() -> dict:
    st = _ae.ENGINE.state
    return {
        "available":   _ae.available(),
        "playing":     st.playing,
        "path":        st.path,
        "file_rate":   st.file_rate,
        "granted_rate": st.granted_rate,
        "channels":    st.channels,
        "bits":        st.bits,
        "exclusive":   st.exclusive,
        "bit_perfect": st.bit_perfect,
        "position":    round(st.position_sec, 2),
        "duration":    round(st.duration_sec, 2),
        "error":       st.error,
    }


@router.get("/api/audio/state")
async def audio_state():
    """Что играет и КАК именно. `bit_perfect` — вывод, а не обещание."""
    return _state_dict()


@router.get("/api/audio/devices")
async def audio_devices():
    """Устройства WASAPI. Пусто — движок на этой машине недоступен.

    Другие звуковые подсистемы сюда не попадают намеренно: эксклюзивный режим
    есть только у WASAPI, и показывать остальные значило бы предлагать
    bit-perfect там, где его не будет.
    """
    return {
        "available": _ae.available(),
        "devices": [{"index": d.index, "name": d.name, "default_rate": d.default_rate}
                    for d in _ae.devices()],
    }


@router.post("/api/audio/play")
async def audio_play(body: dict):
    path = (body.get("path") or "").strip()
    if not path:
        raise HTTPException(400, imsg("err.path_required", "нужен путь к файлу"))
    st = _ae.ENGINE.play(
        path,
        device=body.get("device"),
        exclusive=bool(body.get("exclusive", True)),
    )
    return {"ok": not st.error, **_state_dict()}


@router.post("/api/audio/stop")
async def audio_stop():
    _ae.ENGINE.stop()
    return {"ok": True, **_state_dict()}
