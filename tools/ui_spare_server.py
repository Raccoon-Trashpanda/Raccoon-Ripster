"""Песочный сервер Ripster для проверок интерфейса: сам сервер — настоящий,
фоновые циклы — выключены, записи в боевые файлы — запрещены.

Зачем не `python app.py` на запасном порту:

1. lifespan-блок app.py поднимает сторож, канарейку (реальные закачивания),
   обход учёток, очередь загрузок и рестарт BBC-планов по ОБЩИМ файлам
   репозитория. Второй такой процесс рядом с живым 7799 — это два сторожа и
   два раннера на одном складе, то есть риск уронить рабочую сессию владельца
   ровно тем прогоном, который должен её проверять.
2. ЕЩЁ ДО lifespan, на самом `import app`, приложение пишет `config.yaml` и
   `tokens/`: apple-роут синхронизирует media-user-token из cookies и из
   wrapper'а, телеметрия заводит свой ключ. Агенту это запрещено правилами
   репозитория, а проверке интерфейса не нужно вовсе.

Здесь подменяются ровно эти два куска: контекст lifespan (пустой) и
`config_service.save_config` (заглушка, считающая подавленное). Всё остальное —
маршруты, middleware авторизации, /ws и ретранслятор `rp_relay`, статика —
остаётся настоящим, из того же `app.py`.

Что от этого зависит честность вывода: проверка покрывает HTTP/WS-путь
приложения, но не его фоновые циклы и не персистентность настроек — их этот
стенд не доказывает.

    .venv\\Scripts\\python.exe tools/ui_spare_server.py            # порт 7805
    RIPSTER_PORT=7807 .venv\\Scripts\\python.exe tools/ui_spare_server.py
"""
from __future__ import annotations

import contextlib
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(errors="replace")
    except Exception:
        pass

#: сколько записи в config.yaml/tokens/ не пустили наружу
SUPPRESSED: list[str] = []


def block_config_writes() -> None:
    """Ставится ДО `import app`: приложение зовёт save_config на импорте."""
    import ripster.config_service as cs

    def _blocked(cfg, *a, **k):
        SUPPRESSED.append("save_config")
        return None

    cs.save_config = _blocked


def main() -> int:
    port = int(os.environ.get("RIPSTER_PORT") or "7805")
    host = os.environ.get("RIPSTER_HOST") or "127.0.0.1"
    os.environ.setdefault("RIPSTER_LAUNCHER_LOG",
                          str(Path(os.environ.get("TEMP", ".")) / "ripster_ui_spare_launcher.log"))

    block_config_writes()
    import app as appmod                                  # noqa: E402
    import uvicorn                                        # noqa: E402
    print(f"[ui_spare_server] подавлено записей в config.yaml/tokens на импорте: "
          f"{len(SUPPRESSED)}", flush=True)

    @contextlib.asynccontextmanager
    async def _no_background_loops(_app):
        print("[ui_spare_server] фоновые циклы lifespan ВЫКЛЮЧЕНЫ (сторож, "
              "канарейка, очередь, планы BBC); записи в config.yaml/tokens "
              "заблокированы", flush=True)
        yield

    appmod.app.router.lifespan_context = _no_background_loops
    print(f"[ui_spare_server] http://{host}:{port}", flush=True)
    uvicorn.run(appmod.app, host=host, port=port, log_level="warning")
    return 0


if __name__ == "__main__":
    sys.exit(main())
