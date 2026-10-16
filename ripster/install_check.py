"""Самопроверка установки Ripster: что запускается установщиком перед кнопкой «Готово».

    python -m ripster.install_check [--base ПАПКА] [--report ФАЙЛ] [--no-boot] [--components node,orpheus]

Печатает построчно `OK|WARN|FAIL <id> <описание>` и пишет то же в файл отчёта. Код выхода: 0 — критичных
проблем нет (WARN допустимы), 1 — есть FAIL. Ничего не меняет в установке, кроме временных файлов при
пробном запуске сервера (он идёт на свободном порту, а не на 7799, и убирается целиком вместе с детьми).

Что проверяем:
  · пакеты Python, без которых приложение не стартует (импортом, как это сделает само приложение);
  · встроенные программы: ffmpeg, ffprobe, mp4decrypt, mp4extract — реально запускаются (`-version`);
  · config.yaml читается; папка сохранения создаётся и доступна для записи;
  · все скрипты, на которые ссылается index.html, лежат на месте (иначе экран «чёрный»);
  · пробный запуск сервера на запасном порту: /api/ping отвечает, главная страница и скрипты отдаются;
  · выбранные при установке компоненты (--components): Node.js, OrpheusDL и т. д. — на месте и запускаются.
"""
from __future__ import annotations

import argparse
import importlib
import os
import re
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(errors="replace")
    except Exception:
        pass

_NO_WIN = 0x08000000 if os.name == "nt" else 0
OK, WARN, FAIL = "OK", "WARN", "FAIL"

# Без этих пакетов приложение не стартует или теряет целый слой (регулятор нагрузки, расшифровка Deezer…).
_CRITICAL_IMPORTS = ["fastapi", "uvicorn", "httpx", "yaml", "mutagen", "websockets", "multipart", "psutil", "numpy"]
_OPTIONAL_IMPORTS = [("Cryptodome", "расшифровка Deezer/Яндекса"), ("certifi", "проверка HTTPS-сертификатов")]

# (имя файла, обязателен ли, для чего)
_BUNDLED_TOOLS = [("ffmpeg.exe", True, "перекодирование, склейка миксов"),
                  ("ffprobe.exe", True, "определение качества файлов"),
                  ("mp4decrypt.exe", False, "расшифровка Apple ALAC/видео"),
                  ("mp4extract.exe", False, "извлечение дорожек Apple ALAC")]


class Report:
    def __init__(self):
        self.rows: list[tuple[str, str, str]] = []

    def add(self, status: str, ident: str, text: str) -> None:
        self.rows.append((status, ident, text))

    def failed(self) -> int:
        return sum(1 for r in self.rows if r[0] == FAIL)

    def lines(self) -> list[str]:
        return [f"{s} {i} {t}".rstrip() for s, i, t in self.rows]


def _run(cmd: list, timeout: int = 20, cwd: str | None = None) -> tuple[int, str]:
    try:
        r = subprocess.run(cmd, capture_output=True, timeout=timeout, cwd=cwd, creationflags=_NO_WIN)
    except Exception as e:  # catch-all-ok: нет файла/не запустился — это и есть результат проверки
        return -1, f"{type(e).__name__}: {e}"
    out = (r.stdout or b"") + (r.stderr or b"")
    return r.returncode, out.decode("utf-8", "replace")


def check_imports(rep: Report) -> None:
    for name in _CRITICAL_IMPORTS:
        try:
            importlib.import_module(name)
            rep.add(OK, f"py.{name}", "импорт")
        except Exception as e:
            rep.add(FAIL, f"py.{name}", f"не импортируется: {type(e).__name__}: {str(e)[:80]}")
    for name, why in _OPTIONAL_IMPORTS:
        try:
            importlib.import_module(name)
            rep.add(OK, f"py.{name}", "импорт")
        except Exception:
            rep.add(WARN, f"py.{name}", f"нет пакета ({why})")
    try:
        importlib.import_module("ripster")
        rep.add(OK, "py.ripster", "пакет приложения виден")
    except Exception as e:
        rep.add(FAIL, "py.ripster", f"пакет приложения не виден интерпретатору: {e}")


def check_bundled_tools(rep: Report, base: Path) -> None:
    tools = base / "tools"
    for fname, required, why in _BUNDLED_TOOLS:
        exe = tools / fname
        tid = f"tool.{fname.rsplit('.', 1)[0]}"
        if not exe.is_file():
            rep.add(FAIL if required else WARN, tid, f"нет файла tools\\{fname} ({why})")
            continue
        args = ["-version"] if fname.startswith("ff") else []
        rc, out = _run([str(exe), *args])
        banner = out.lower()
        ok = (rc == 0 and ("ffmpeg" in banner or "ffprobe" in banner)) if fname.startswith("ff") \
            else ("bento4" in banner or "usage" in banner)
        if ok:
            rep.add(OK, tid, "запускается")
        else:
            rep.add(FAIL if required else WARN, tid, f"не запускается (код {rc}): {out.strip()[:80]}")


def check_config(rep: Report, base: Path) -> None:
    cfg = base / "config.yaml"
    if not cfg.is_file():
        rep.add(FAIL, "config", "нет config.yaml")
        return
    try:
        import yaml
        data = yaml.safe_load(cfg.read_text(encoding="utf-8")) or {}
        rep.add(OK, "config", "config.yaml читается")
    except Exception as e:
        rep.add(FAIL, "config", f"config.yaml не читается: {str(e)[:80]}")
        return
    save = str(data.get("save-path") or "").strip() or str(Path.home() / "Music" / "Ripster")
    try:
        p = Path(save)
        p.mkdir(parents=True, exist_ok=True)
        probe = p / ".ripster_write_test"
        probe.write_text("x", encoding="utf-8")
        probe.unlink()
        rep.add(OK, "savepath", f"папка загрузок доступна: {save}")
    except Exception as e:
        rep.add(FAIL, "savepath", f"в папку загрузок {save} нельзя писать: {str(e)[:80]}")


def check_frontend_files(rep: Report, base: Path) -> None:
    idx = base / "static" / "index.html"
    if not idx.is_file():
        rep.add(FAIL, "ui.index", "нет static/index.html")
        return
    html = idx.read_text(encoding="utf-8", errors="replace")
    missing = []
    for m in re.finditer(r'<script[^>]+src="(/static/[^"?]+)', html):
        if not (base / m.group(1).lstrip("/")).is_file():
            missing.append(m.group(1))
    for m in re.finditer(r'<link[^>]+href="(/static/[^"?]+\.css)', html):
        if not (base / m.group(1).lstrip("/")).is_file():
            missing.append(m.group(1))
    if missing:
        rep.add(FAIL, "ui.files", f"нет файлов интерфейса: {', '.join(missing[:4])}" + (" …" if len(missing) > 4 else ""))
    else:
        rep.add(OK, "ui.files", "все скрипты и стили интерфейса на месте")


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _kill_tree(pid: int) -> None:
    try:
        if os.name == "nt":
            subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True, timeout=20, creationflags=_NO_WIN)
        else:
            os.kill(pid, 9)
    except Exception:
        pass


def check_boot(rep: Report, base: Path, timeout_s: int = 90) -> None:
    """Запустить настоящий сервер на запасном порту и убедиться, что он живой и отдаёт интерфейс."""
    app = base / "app.py"
    if not app.is_file():
        rep.add(FAIL, "boot", "нет app.py")
        return
    port = _free_port()
    env = dict(os.environ, RIPSTER_PORT=str(port))
    log = Path(tempfile.gettempdir()) / f"ripster.install_check_{port}.log"
    with open(log, "wb") as lf:
        proc = subprocess.Popen([sys.executable, str(app)], cwd=str(base), env=env, stdout=lf, stderr=lf,
                                creationflags=_NO_WIN)
        try:
            ping, deadline = None, time.time() + timeout_s
            while time.time() < deadline and proc.poll() is None:
                try:
                    ping = urllib.request.urlopen(f"http://127.0.0.1:{port}/api/ping", timeout=3).read().decode()
                    break
                except Exception:
                    time.sleep(1.0)
            if ping is None:
                tail = ""
                try:
                    tail = log.read_text(encoding="utf-8", errors="replace")[-300:].replace("\n", " ")
                except Exception:
                    pass
                why = f"процесс завершился (код {proc.returncode})" if proc.poll() is not None else f"нет ответа за {timeout_s} с"
                rep.add(FAIL, "boot", f"сервер не поднялся: {why}. {tail}"[:360])
                return
            if '"ripster"' not in ping:
                rep.add(FAIL, "boot", f"/api/ping ответил не Ripster: {ping[:60]}")
                return
            rep.add(OK, "boot", "сервер запускается и отвечает")
            bad = []
            for path in ("/", "/static/js/app.js", "/static/js/i18n.js", "/static/js/views.js"):
                try:
                    code = urllib.request.urlopen(f"http://127.0.0.1:{port}{path}", timeout=10).status
                    if code != 200:
                        bad.append(f"{path}:{code}")
                except Exception as e:
                    bad.append(f"{path}:{type(e).__name__}")
            rep.add(FAIL if bad else OK, "boot.ui", ("не отдаются: " + ", ".join(bad)) if bad else "главная страница и скрипты отдаются")
            lt = log.read_text(encoding="utf-8", errors="replace") if log.exists() else ""
            errs = [ln for ln in lt.splitlines() if "Traceback" in ln or "ModuleNotFoundError" in ln or "ImportError" in ln]
            if errs:
                rep.add(WARN, "boot.log", f"в журнале запуска есть ошибки ({len(errs)}), первая: {errs[0][:100]}")
        finally:
            _kill_tree(proc.pid)
            try:
                proc.wait(timeout=10)
            except Exception:
                pass
            try:
                log.unlink()
            except Exception:
                pass


def _engine_installed(module: str, base: Path) -> bool:
    """Тот же вопрос, что задаёт само приложение: `is_installed()` движка (он считает папку от sys.argv[0])."""
    try:
        sys.argv[0] = str(base / "app.py")
        mod = importlib.import_module(f"ripster.engines.{module}")
        return bool(mod.is_installed())
    except Exception:
        return False


def check_components(rep: Report, base: Path, comps: list[str]) -> None:
    """Компоненты, выбранные при установке: на месте и запускаются."""
    for c in comps:
        c = c.strip().lower()
        if c in ("node", "soundcloud"):
            node = None
            for cand in (base / "tools" / "node" / "node.exe", base / "tools" / "node.exe"):
                if cand.is_file():
                    node = cand
                    break
            if node is None:
                from shutil import which
                w = which("node")
                node = Path(w) if w else None
            if not node:
                rep.add(WARN, "comp.node", "Node.js не найден (SoundCloud будет недоступен до установки в Настройках)")
            else:
                rc, out = _run([str(node), "--version"])
                rep.add(OK if rc == 0 else WARN, "comp.node", f"Node.js {out.strip()[:12]}" if rc == 0 else "Node.js не запускается")
            if c == "soundcloud":
                ok = (base / "tools" / "lucida" / "lucida-src" / "build" / "index.js").is_file()
                rep.add(OK if ok else WARN, "comp.lucida", "SoundCloud (Lucida) установлен" if ok else "SoundCloud (Lucida) не доставлен")
        elif c in ("orpheus", "beatport"):
            ok = _engine_installed("orpheus_spotify", base) if c == "orpheus" else (base / "orpheus" / "modules" / "beatport").is_dir()
            rep.add(OK if ok else WARN, "comp.orpheus", "OrpheusDL установлен" if ok else "OrpheusDL не доставлен (Spotify/Beatport — позже в Настройках)")
        elif c == "jiosaavn":
            ok = _engine_installed("orpheus_jiosaavn", base)
            rep.add(OK if ok else WARN, "comp.jiosaavn", "JioSaavn установлен" if ok else "JioSaavn не доставлен")


def run(base: Path, comps: list[str], boot: bool = True) -> Report:
    rep = Report()
    check_imports(rep)
    check_bundled_tools(rep, base)
    check_config(rep, base)
    check_frontend_files(rep, base)
    if comps:
        check_components(rep, base, comps)
    if boot and rep.failed() == 0:        # серверу нет смысла стартовать на сломанной основе — причина уже названа
        check_boot(rep, base)
    return rep


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default=str(Path(__file__).resolve().parent.parent))
    ap.add_argument("--report", default="")
    ap.add_argument("--summary", default="", help="ASCII-файл «FAIL id / WARN id» для установщика (он читает ANSI)")
    ap.add_argument("--no-boot", action="store_true")
    ap.add_argument("--components", default="")
    a = ap.parse_args(argv)
    base = Path(a.base)
    sys.path.insert(0, str(base))
    rep = run(base, [x for x in a.components.split(",") if x.strip()], boot=not a.no_boot)
    lines = rep.lines()
    for ln in lines:
        print(ln, flush=True)
    if a.report:
        try:
            Path(a.report).parent.mkdir(parents=True, exist_ok=True)
            Path(a.report).write_text("\n".join(lines) + "\n", encoding="utf-8")
        except Exception:
            pass
    if a.summary:
        try:
            Path(a.summary).parent.mkdir(parents=True, exist_ok=True)
            body = "".join(f"{st} {i}\r\n" for st, i, _ in rep.rows if st != OK)
            Path(a.summary).write_bytes(body.encode("ascii", "replace"))
        except Exception:
            pass
    return 1 if rep.failed() else 0


if __name__ == "__main__":
    sys.exit(main())
