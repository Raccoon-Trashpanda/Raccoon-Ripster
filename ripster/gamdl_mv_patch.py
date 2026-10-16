"""Запасной аудиопоток клипа в установленном gamdl (самоприменяющийся патч).

24.09.2026 из чата @apple_music_alac: «Decrypt failed … invalid argument for
--key» на старых music-video. Причина — выбор аудио в gamdl
(`interface/music_video.py::_get_best_stereo_audio_playlist`) знает ровно одну
группу `audio-stereo-256`; у старых клипов её нет (только HE-AAC), gamdl
возвращает None, наружу уходит пустой URL/ключ, и mp4decrypt падает на пустом
`--key`. Тот же дефект был в Go-движке — там запас уже стоит (utils/mvaudio);
здесь он же для gamdl-ветки, которой роутер отдаёт все клипы.

gamdl — сторонняя библиотека в site-packages, правим по месту:
* идемпотентно (маркер в тексте — второй вызов ничего не пишет);
* только пока узнаём исходную форму метода; незнакомая версия gamdl —
  честный вердикт «не трогали», а не молчаливая порча файла;
* применяется лениво перед первой MV-задачей, сбой (нет прав на запись и пр.)
  не ломает загрузку — только лишает её запаса.
"""
from __future__ import annotations

import re
import subprocess
import threading

MARKER = "ripster:mv-audio-fallback"

# Тело строгого матча, каким оно стоит в gamdl 3.x (3.3 проверена вживую).
_OLD_METHOD = re.compile(
    r"    def _get_best_stereo_audio_playlist\(\n"
    r".*?"
    r"        return audio_playlist\n",
    re.S,
)

_NEW_METHOD = '''    def _get_best_stereo_audio_playlist(
        self,
        playlist_master_data: dict,
    ) -> dict | None:
        # {marker}: в старых клипах нет audio-stereo-256 — только HE-AAC
        # («audio-stereo», «audio-aac», …). Без запаса отсюда шёл None,
        # вызывающий получал пустой аудиопоток, а mp4decrypt — «invalid
        # argument for --key» на пустом ключе. Точное совпадение по-прежнему
        # главное; иначе — лучшая доступная аудиогруппа по битрейту из имени.
        audio_playlist = next(
            (
                media
                for media in playlist_master_data["media"]
                if media["group_id"] == "audio-stereo-256"
            ),
            None,
        )
        if audio_playlist is not None:
            return audio_playlist
        import re as _re
        best, best_kbps = None, -1
        for media in playlist_master_data.get("media") or []:
            gid = str(media.get("group_id") or "")
            if not gid.startswith("audio") or not media.get("uri"):
                continue
            m = _re.search(r"(\\d+)(?!.*\\d)", gid)
            kbps = int(m.group(1)) if m else 0
            if kbps > best_kbps:
                best, best_kbps = media, kbps
        return best
'''.replace("{marker}", MARKER)


def patch_source(src: str) -> "str | None":
    """Новый текст файла или None, если форма не распознана/патчить нечего.

    None при уже стоящем маркере — это «не пишем второй раз», а не отказ.
    """
    if MARKER in src:
        return None
    m = _OLD_METHOD.search(src)
    if not m:
        return None
    return src[:m.start()] + _NEW_METHOD + src[m.end():]


_lock = threading.Lock()
_done = False


def _music_video_path(python: str) -> str:
    """Файл gamdl.interface.music_video У ТОГО интерпретатора, которым поедет
    `python -m gamdl` (venv разработчика и бандл пользователя — разные)."""
    r = subprocess.run(
        [python, "-c",
         "import gamdl.interface.music_video as m; print(m.__file__)"],
        capture_output=True, text=True, timeout=20)
    return (r.stdout or "").strip()


def ensure(python: str | None = None) -> dict:
    """Один раз за процесс: применить запас к установленному gamdl.

    {"verdict": patched|already|unknown_shape|missing|write_failed|probe_failed,
     "path": str} — наружу идёт только путь и машинный вердикт, ничего секретного.
    """
    global _done
    with _lock:
        if _done:
            return {"verdict": "already", "path": ""}
        out = {"verdict": "unknown", "path": ""}
        try:
            if python is None:
                from .py_runtime import app_python
                python = app_python()
            path = _music_video_path(python)
            out["path"] = path
            if not path:
                out["verdict"] = "missing"
            else:
                from pathlib import Path
                p = Path(path)
                src = p.read_text(encoding="utf-8")
                if MARKER in src:
                    out["verdict"] = "already"
                else:
                    new = patch_source(src)
                    if new is None:
                        out["verdict"] = "unknown_shape"
                    else:
                        p.write_text(new, encoding="utf-8")
                        out["verdict"] = "patched"
        except Exception as e:                                # noqa: BLE001
            out["verdict"] = "probe_failed"
            out["error"] = f"{type(e).__name__}: {e}"[:160]
        # Сбой пробы НЕ кэшируем навсегда — следующая MV-задача попробует ещё
        # раз; успех/«уже/не узнали» — кэшируем, файл от этого не поменяется.
        if out["verdict"] != "probe_failed":
            _done = True
        return out


def reset_for_tests() -> None:
    global _done
    with _lock:
        _done = False
