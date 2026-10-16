"""Поставить компонент из командной строки — тем же кодом, что кнопка во вкладке Setup. Зовёт установщик.

    python -m ripster.setup_cli --base ПАПКА --component node|soundcloud|orpheus|beatport|jiosaavn|ffmpeg|mp4decrypt

Печатает ход строками `LOG <текст>` и в конце одну строку `RESULT ok|failed <state> <причина>`. Код выхода 0 — компонент
на месте (установлен сейчас или уже был), 1 — не вышло. Сбой компонента не должен ронять установку целиком: установщик
показывает итог на последней странице, а недостающее можно поставить позже из Настроек.
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(errors="replace")
    except Exception:
        pass


async def _run(base: Path, key: str) -> dict:
    sys.path.insert(0, str(base))
    # Движки считают папку приложения от sys.argv[0] (в самом приложении это app.py). Из `-m` там оказался бы
    # путь к этому модулю, и проверка «OrpheusDL установлен?» после успешной установки отвечала бы «нет».
    sys.argv[0] = str(base / "app.py")
    from ripster import setup as S
    from ripster.routes import setup as RS

    async def bcast(msg: dict) -> None:
        if msg.get("type") == "install_log":
            e = msg.get("entry") or {}
            print("LOG " + str(e.get("text", "")).replace("\r", " ").replace("\n", " ")[:300], flush=True)

    cfg: dict = {}
    S.install(cfg, bcast, lambda *a, **k: None, base, sys.platform == "win32")
    RS._base_dir = base
    RS._cfg = cfg
    RS._broadcast = bcast
    return await RS._run_setup_component(key)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", required=True)
    ap.add_argument("--component", required=True)
    a = ap.parse_args(argv)
    try:
        res = asyncio.run(_run(Path(a.base), a.component))
    except Exception as e:  # catch-all-ok: любая неожиданность — честная строка итога, а не трассировка
        print(f"RESULT failed exception {type(e).__name__}: {str(e)[:200]}", flush=True)
        return 1
    ok = bool(res.get("ok"))
    print(f"RESULT {'ok' if ok else 'failed'} {res.get('state', '')} {str(res.get('error') or '')[:200]}".rstrip(), flush=True)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
