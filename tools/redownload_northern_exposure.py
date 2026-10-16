"""Повторная загрузка двух изданий NORTHERN EXPOSURE REDUX через ШТАТНЫЙ API
приложения (POST /api/queue/add) — после того как фикс коллизий поедет в бой.

Запускать ТОЛЬКО после перезапуска app.py оркестратором: до фикса вторая
загрузка в ту же папку снова съест первую.

  .venv\\Scripts\\python.exe tools\\redownload_northern_exposure.py

Скрипт ставит обе ссылки в очередь, ждёт завершения обеих и ПРОВЕРЯЕТ результат:
папки разные, в каждой — свои файлы, многодисковое издание разложено по «CD N».
Ничего не удаляет и никуда не пишет, кроме очереди самого приложения.
"""
import json
import sys
import time
import hmac
import hashlib
import urllib.request
from pathlib import Path

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(errors="replace")
    except Exception:
        pass

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import yaml  # noqa: E402

BASE = "http://127.0.0.1:7799"

RELEASES = [
    ("MIXED",   "https://www.qobuz.com/fr-fr/album/"
                "northern-exposure-redux-sasha-john-digweed/uvxkonsapjsbb"),
    ("UNMIXED", "https://www.qobuz.com/fr-fr/album/"
                "northern-exposure-redux-sasha-john-digweed/e6r7vcsf378jq"),
]
POLL_TIMEOUT_S = 60 * 60          # трёхдисковый Hi-Res вечер, не минутное дело
DONE = {"done", "completed", "finished", "failed", "error", "stopped"}


def _cookie() -> str:
    """Бот-фича та же, что в tools/test_download.py: сессию печатаем из
    session-secret, приложение не перезапускаем."""
    cfg = yaml.safe_load((ROOT / "config.yaml").read_text(encoding="utf-8"))
    secret = (cfg.get("session-secret") or "").strip()
    if not secret:
        raise SystemExit("В config.yaml нет session-secret — нечем авторизоваться")
    i = int(time.time())
    return f"{i}.{hmac.new(secret.encode(), str(i).encode(), hashlib.sha256).hexdigest()}"


COOKIE = _cookie()


def _call(path, method="GET", body=None, timeout=60):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(BASE + path, method=method, data=data)
    req.add_header("Cookie", f"ripster-session={COOKIE}")
    req.add_header("Origin", BASE)
    if data:
        req.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())


def _task_id(res):
    return (res.get("task_id") or res.get("id")
            or (res.get("task") or {}).get("id"))


def _find(queue, tid):
    for t in queue:
        if t.get("id") == tid:
            return t
    return None


def _manifest_dir(tid):
    mf = ROOT / "downloads_manifest.json"
    try:
        ent = json.loads(mf.read_text(encoding="utf-8")).get(tid)
        return (ent or {}).get("dir")
    except Exception:
        return None


def main() -> int:
    try:
        _call("/api/queue")
    except Exception as e:
        print(f"Приложение не отвечает на {BASE}: {e}")
        print("Дождись перезапуска app.py оркестратором и повтори.")
        return 2

    ids = {}
    for label, url in RELEASES:
        res = _call("/api/queue/add", "POST", {"url": url, "source": "owner"})
        tid = _task_id(res)
        if not tid:
            print(f"{label}: добавление не вернуло id: "
                  f"{json.dumps(res, ensure_ascii=False)[:200]}")
            return 1
        ids[label] = tid
        print(f"{label}: поставлено в очередь, task {tid}")
        time.sleep(1)                     # развести старты: вторая видит суффикс первой

    deadline = time.time() + POLL_TIMEOUT_S
    left = dict(ids)
    while left and time.time() < deadline:
        time.sleep(10)
        queue = _call("/api/queue")
        for label, tid in list(left.items()):
            t = _find(queue, tid)
            status = (t or {}).get("status")
            if status in DONE or t is None:
                d = _manifest_dir(tid)
                n = len(list(Path(d).rglob("*.flac"))) if d and Path(d).is_dir() else 0
                print(f"{label}: {status or 'ушла из очереди'}, папка {d or '?'}, flac {n}")
                left.pop(label)
    if left:
        print("Таймаут ожидания:", ", ".join(left))
        return 1

    dirs = {label: _manifest_dir(tid) for label, tid in ids.items()}
    print("\n— проверка раздельности —")
    ok = True
    (a, da), (b, db) = list(dirs.items())[0], list(dirs.items())[1]
    if not da or not db:
        print("Одна из папок не записана в манифест — проверить вручную."); ok = False
    elif Path(da) == Path(db):
        print(f"ПРОВАЛ: оба релиза в одной папке {da}"); ok = False
    else:
        print(f"{a}: {da}")
        print(f"{b}: {db}")
        for label in (a, b):
            d = Path(dirs[label])
            flac = list(d.rglob("*.flac"))
            discs = sorted(p.name for p in d.iterdir() if p.is_dir()
                           and p.name.upper().replace(" ", "") in {"CD1", "CD2", "CD3",
                                                                   "DISC1", "DISC2", "DISC3"})
            print(f"{label}: файлов {len(flac)}" + (f", диски: {', '.join(discs)}" if discs else ""))
            if not flac:
                print(f"ПРОВАЛ: {label} — ни одного файла"); ok = False
    print("\nИТОГ:", "разведены, качаются порознь" if ok else "ТРЕБУЕТ ВНИМАНИЯ")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
