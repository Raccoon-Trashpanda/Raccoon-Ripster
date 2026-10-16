"""Запуск загрузки через «Премиум Ripster» отдельным процессом (так движки встроены в раннер).

    python -m ripster.flex_cli --url URL --quality Q --out DIR

Строки для раннера: `FLEX_PROGRESS <процент>`, `FLEX_SAVED <путь>`, `FLEX_ERROR <код> <причина>`.
Токен устройства читается из flex_client.json (общий с приложением файл); в аргументах секретов нет.
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


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", required=True)
    ap.add_argument("--quality", default="lossless")
    ap.add_argument("--out", required=True)
    a = ap.parse_args(argv)

    from ripster import flex_client, flex_download
    import os
    # Каталог состояния (flex_client.json): по умолчанию рядом с приложением; env нужен тестам и портативным сборкам.
    flex_client.init(Path(os.environ.get("RIPSTER_FLEX_STATE_DIR") or Path(__file__).resolve().parent.parent))

    last = {"pct": -1}

    def log(msg: str) -> None:
        print(f"[flex] {msg}", flush=True)

    def progress(done: int, total) -> None:
        if total:
            pct = int(done * 100 / total)
            if pct != last["pct"] and pct % 5 == 0:
                last["pct"] = pct
                print(f"FLEX_PROGRESS {pct}", flush=True)

    try:
        path = asyncio.run(flex_download.download_track(a.url, a.quality, Path(a.out), log, progress))
    except flex_download.DownloadError as e:
        print(f"FLEX_ERROR {e.code} {e}", flush=True)
        return 1
    except Exception as e:  # catch-all-ok: любой сбой — честная строка раннеру, а не трассировка
        print(f"FLEX_ERROR other {type(e).__name__}: {str(e)[:120]}", flush=True)
        return 1
    print(f"FLEX_SAVED {path}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
