"""Гарантировать, что ffmpeg/ffprobe видны процессу app — и всем его детям.

Зачем. Около тридцати мест в коде (спектрограммы, Coder, транскод, BBC,
проверка целостности, движки-подпроцессы) зовут `ffmpeg`/`ffprobe` голым
именем и целиком зависят от PATH процесса. А PATH зависит от того, КТО
запустил app.py: лончер, PowerShell, bash-nohup (известная ловушка — теряет
ffmpeg), планировщик. Итог — плавающий «ffmpeg/ffprobe не найден», хотя ffmpeg
на машине стоит (18.09.2026: спектрограммы в боте). Чинить тридцать вызовов по
одному бессмысленно — чиним окружение один раз на старте.

Порядок поиска: уже в PATH → путь из конфига (`gamdl-ffmpeg-path`) → типовые
места установки на Windows (WinGet Gyan, choco/scoop, C:\\ffmpeg) → tools/ffmpeg
проекта. Найденная папка ДОБАВЛЯЕТСЯ в начало os.environ["PATH"], поэтому её
видят и subprocess-вызовы, и дочерние движки.
"""
from __future__ import annotations

import glob
import os
import shutil
from pathlib import Path

_EXE = ".exe" if os.name == "nt" else ""


def _has_both(d: Path) -> bool:
    return (d / f"ffmpeg{_EXE}").is_file() and (d / f"ffprobe{_EXE}").is_file()


def _candidates(config: dict | None) -> list[Path]:
    out: list[Path] = []
    cfg_path = ((config or {}).get("gamdl-ffmpeg-path") or "").strip()
    if cfg_path:
        p = Path(cfg_path)
        out.append(p.parent if p.suffix else p)
    local = os.environ.get("LOCALAPPDATA", "")
    if local:
        pattern = os.path.join(local, "Microsoft", "WinGet", "Packages",
                               "*FFmpeg*", "*", "bin")
        out += [Path(x) for x in sorted(glob.glob(pattern), reverse=True)]
        out.append(Path(local) / "Microsoft" / "WinGet" / "Links")
    for fixed in (r"C:\ffmpeg\bin", r"C:\ProgramData\chocolatey\bin",
                  os.path.expanduser(r"~\scoop\shims")):
        out.append(Path(fixed))
    out.append(Path(__file__).resolve().parent.parent / "tools" / "ffmpeg" / "bin")
    return out


def ensure_ffmpeg_on_path(config: dict | None = None) -> str:
    """Вернуть папку с ffmpeg (и положить её в PATH), либо "" если не нашли.

    Никогда не бросает: отсутствие ffmpeg — не повод ронять старт app, а повод
    честно сказать об этом в логе один раз.
    """
    try:
        found = shutil.which("ffmpeg")
        if found and shutil.which("ffprobe"):
            return str(Path(found).parent)
        for d in _candidates(config):
            try:
                if d and _has_both(d):
                    os.environ["PATH"] = str(d) + os.pathsep + os.environ.get("PATH", "")
                    print(f"[ffmpeg] не было в PATH процесса — добавил {d}", flush=True)
                    return str(d)
            except Exception:                                  # noqa: BLE001
                continue
        print("[ffmpeg] ffmpeg/ffprobe не найдены ни в PATH, ни в типовых местах — "
              "спектрограммы, Coder и транскод работать не будут", flush=True)
    except Exception as e:                                     # noqa: BLE001
        print(f"[ffmpeg] проверка пути упала: {e!r}", flush=True)
    return ""
