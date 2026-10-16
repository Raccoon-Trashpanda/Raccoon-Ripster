#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""tools/check_installers.py — сторож всех автоустановщиков Ripster (реестр
`ripster/installers_manifest.py`). Жалоба владельца 25.09.2026: «проверить ВСЕ
системы автоустановщиков, чтобы раз и навсегда перестать за это переживать».

Режимы
  fast (по умолчанию, ~30–60 с, только HEAD/GET-метаданные, ничего не качает):
    * контракт реестра: у каждого установщика есть пин, таймаут, msg_key с
      текстами ru+en, легитимный sha256 (пустой или 64 hex);
    * живость URL источника (HEAD/Range GET; для {ver}/{arch} — подстановка);
    * pip-пины проверяются на PyPI (существует ли закреплённая версия);
    * git-пины — `git ls-remote` (расхождение = предупреждение: апстримы живут);
    * OCI-тег враппера — анонимный токен ghcr + HEAD манифеста;
    * сверка pip-пинов с VERSIONS.md и requirements.txt.
  --full (рукой, по просьбе): для изолируемых установщиков — НАСТОЯЩАЯ установка
    продуктовым кодом (`ripster.setup`) в temp-каталог, проверка запуска,
    ВТОРОЙ прогон на идемпотентность (не должно ничего перекачиваться) и
    имитация отказа (нет сети / висящее соединение) — ясный быстрый отказ.

Выход: 0 — всё чисто; 1 — есть падения; предупреждения кодом не считаются.
eigencheck: weekly-хук живёт в tools/ripster_healthcheck.py (check_installers).
"""
import sys
import os
import re
import json
import time
import shutil
import socket
import tempfile
import argparse
import asyncio
import subprocess
import urllib.request

for _s in (sys.stdout, sys.stderr):
    try: _s.reconfigure(errors="replace")
    except Exception: pass

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from ripster.installers_manifest import (INSTALLERS, FAILURE_MSGS,   # noqa: E402
                                         Installer, by_id)
from ripster.setup import _go_release_sync   # noqa: E402

UA      = {"User-Agent": "Mozilla/5.0 ripster-installer-guard"}
HTTP_T  = 20          # секунды на одну сетевую пробу fast-режима
_HEX64  = re.compile(r"^[0-9a-fA-F]{64}$")

# Хосты-«мигальщики»: могут разово ответить 404/5xx, но через несколько секунд
# уже работают (замер 30.09.2026: dl.google.com, go.dev, codeberg.org).
# Для них 404/5xx повторяем несколько раз с паузой, прежде чем сказать «мёртв».
FLAKY_HOSTS = ("dl.google.com", "go.dev", "codeberg.org")
FLAKY_RETRIES = 3
FLAKY_PAUSE = 6       # секунд между попытками

results: list[tuple[str, str, str, str]] = []   # (id, статус, что проверено, детали)


def _row(iid: str, status: str, what: str, detail: str = "") -> None:
    results.append((iid, status, what, detail[:160]))


def _http(url: str, method: str = "GET", headers: dict | None = None,
          timeout: int = HTTP_T):
    req = urllib.request.Request(url, method=method,
                                 headers={**(headers or {}), **UA})
    return urllib.request.urlopen(req, timeout=timeout)


def _alive(url: str) -> tuple[bool, str]:
    """Жив ли URL, не скачивая артефакт: GET+Range 1 байт (HEAD многие зеркала
    не любят). 403/405 считаем живым — сервер отвечает, отдачу проверим в --full.
    SSL-ошибка — ДЕФЕКТ ЦЕПОЧКИ НА ИСТОЧНИКЕ (bok.net/gyan.dev отдают неполную
    цепочку без промежуточного сертификата): продукт выживает за счёт no-SSL
    фолбэка с обязательной сверкой SHA256. Пробуем relaxed-контекст и ставим
    честный warn, а не FAIL — иначе страж врал бы каждую неделю."""
    try:
        with _http(url, "GET", {"Range": "bytes=0-0"}) as r:
            return True, f"HTTP {r.status}"
    except urllib.error.HTTPError as e:
        return (e.code in (200, 206, 403, 405, 416), f"HTTP {e.code}")
    except Exception as e:
        is_ssl = isinstance(e, urllib.error.URLError) and \
                 "CERTIFICATE" in str(e.reason).upper()
        if is_ssl:
            # Порядок честный: сначала certifi-бандл (продуктовый путь), и лишь
            # затем безверификационная проба — и то только чтобы сказать, ЖИВОЙ ли
            # источник. Установка через verify=False в продукте запрещена без
            # закреплённого SHA256 (ripster/setup._fetch).
            import ssl
            for label, ctx in (("certifi", None), ("verify=False", "untrusted")):
                try:
                    if ctx is None:
                        try:
                            import certifi
                            ctx = ssl.create_default_context(cafile=certifi.where())
                        except Exception:
                            continue
                    else:
                        ctx = ssl._create_unverified_context()
                    req = urllib.request.Request(
                        url, method="GET", headers={**UA, "Range": "bytes=0-0"})
                    with urllib.request.urlopen(req, timeout=HTTP_T, context=ctx) as r:
                        return True, (f"сертификат не проходит магазин этого Python, "
                                      f"но жив по {label} (HTTP {r.status})")
                except urllib.error.HTTPError as he:
                    return (he.code in (200, 206, 403, 405, 416),
                            f"TLS ок по {label}, HTTP {he.code}")
                except Exception:
                    continue
            return False, f"{type(e).__name__}: {e}"[:120]
        return False, f"{type(e).__name__}: {e}"[:120]


def _is_flaky(url: str) -> bool:
    """Хост-«мигальщик»: разовые 404/5xx — норма, нужны ретраи."""
    try:
        from urllib.parse import urlparse
        host = urlparse(url).hostname or ""
    except Exception:
        return False
    return any(host == h or host.endswith("." + h) for h in FLAKY_HOSTS)


def _alive_with_retries(url: str) -> tuple[bool, str, str]:
    """(alive, detail, note). note — пометка о ретраях для мигальщиков:
    '' = обычный результат; 'разовый сбой (ok со N-й попытки)' = живой, но
    первый раз ответил 404/5xx; 'стабильно мёртв (N попыток)' = все ретраи
    дали 404/5xx."""
    if not _is_flaky(url):
        ok, detail = _alive(url)
        return ok, detail, ""
    last_ok, last_detail = False, ""
    for attempt in range(1, FLAKY_RETRIES + 1):
        ok, detail = _alive(url)
        if ok:
            note = (f"разовый сбой (ok со {attempt}-й попытки)" if attempt > 1 else "")
            return True, detail, note
        last_ok, last_detail = ok, detail
        # 404/5xx — повторяем; другие ошибки (нет сети, TLS) — не мигальщик
        is_retryable = False
        m = re.search(r"HTTP\s*(\d+)", detail)
        if m:
            code = int(m.group(1))
            is_retryable = code in (404, 408, 429) or 500 <= code < 600
        if not is_retryable:
            return ok, detail, ""
        if attempt < FLAKY_RETRIES:
            time.sleep(FLAKY_PAUSE)
    return last_ok, last_detail, f"стабильно мёртв ({FLAKY_RETRIES} попыток)"


# ─── контракт реестра (офлайн) ────────────────────────────────────────────────

def check_contract(i: Installer) -> list[str]:
    errs = []
    if not i.pin:                          errs.append("нет пина/rolling-ссылки")
    if i.timeout_s <= 0:                   errs.append(f"таймаут {i.timeout_s} ≤ 0")
    if i.kind not in ("archive", "installer", "git", "pip", "oci", "none"):
        errs.append(f"неизвестный kind={i.kind!r}")
    hs = i.hash_source
    if hs not in ("pinned", "provider-api", "sidecar", "none-published",
                  "git-head", "pypi-pin", "oci-digest"):
        errs.append(f"hash_source={hs!r} вне списка")
    if hs == "pinned" and not _HEX64.match(i.sha256 or ""):
        errs.append("hash_source=pinned, но sha256 пустой/не 64 hex")
    if hs == "pinned" and i.timeout_s <= 0:
        errs.append("pinned без таймаута")
    body = FAILURE_MSGS.get(i.msg_key)
    if not body:                           errs.append(f"msg_key {i.msg_key!r} нет в FAILURE_MSGS")
    else:
        for lang in ("ru", "en"):
            if not (body.get(lang) or "").strip():
                errs.append(f"{i.msg_key}: пустой текст {lang}")
    return errs


# ─── подстановка шаблонов URL ─────────────────────────────────────────────────

def _latest_go_ver() -> str:
    """Версия Go — из той же функции, что и установщик (ripster.setup._go_release_sync),
    чтобы страж и продукт не могли разойтись."""
    ver, _fname, _sha = _go_release_sync()
    return ver.lstrip("go")


def resolve_url(i: Installer) -> tuple[str, list[str]]:
    """(final_url, notes). '' = URL не проверяется по сети в fast-режиме."""
    u = i.url
    if "{repo}" in u:
        # updater-overlay: репозиторий релизов — тот, что у продукта по умолчанию
        # (ripster/updater._DEFAULT_REPO), а НЕ origin этого рабочего дерева:
        # локальный origin может быть личным зеркалом без релизов (замер 25.09:
        # origin my-git даёт 404, релизы живут в _DEFAULT_REPO).
        try:
            src = open(os.path.join(ROOT, "ripster", "updater.py"),
                       encoding="utf-8").read()
            m = re.search(r'_DEFAULT_REPO\s*=\s*"([\w./-]+)"', src)
            repo = m.group(1) if m else ""
        except Exception:
            repo = ""
        if repo:
            return u.replace("{repo}", repo), []
        return "", ["_DEFAULT_REPO не найден в updater.py"]
    if not u.startswith("http"):
        return "", [f"источник не HTTP ({u[:30]})"]
    notes = []
    if "{ver}" in u:
        if i.id == "go":
            try:
                ver = _latest_go_ver().lstrip("go")
            except Exception as e:
                return u, [f"go.dev JSON не ответил: {e}"]
        else:
            m = re.search(r"v?\d[\w.]+", i.pin)
            ver = m.group(0) if m else ""
            if i.id == "node" and not ver.startswith("v"):
                ver = "v" + ver
        u = u.replace("{ver}", ver)
    if "{arch}" in u:
        arch = {"go": "amd64", "gpac": "64", "node": "x64"}.get(i.id, "amd64")
        u = u.replace("{arch}", arch)
    if "{ext}" in u:
        u = u.replace("{ext}", ".exe")
    return u, notes


# ─── сетевые пробы по kind ───────────────────────────────────────────────────

def check_live(i: Installer) -> None:
    if i.kind in ("archive", "installer", "none"):
        u, notes = resolve_url(i)
        if not u:
            if i.url.startswith("winget"):
                _row(i.id, "ok", "winget", "ставится winget'ом — сетевой URL не применим")
                return
            _row(i.id, "warn", "url", "; ".join(notes) or "URL не задан")
            return
        if i.kind == "none" or u.startswith("winget"):
            _row(i.id, "ok", "winget/none", "проверка установки — в --full")
            return
        okk, detail, retry_note = _alive_with_retries(u)
        st = "ok" if okk else "FAIL"
        if okk and "verify=False" in detail:
            st = "warn"     # живой источник с кривой цепочкой — чинить не нам,
        if okk and retry_note:
            st = "warn"     # разовый сбой у мигальщика — видимая пометка, не FAIL
        det = f"{u} → {detail}"
        if retry_note:
            det += f" [{retry_note}]"
        _row(i.id, st, "url alive", det)
    elif i.kind == "pip":
        for pkg, pin in _pip_pairs(i):
            try:
                with _http(f"https://pypi.org/pypi/{pkg}/{pin}/json") as r:
                    j = json.load(r)
                v = j.get("info", {}).get("version", "?")
                _row(i.id, "ok" if v == pin else "warn", "pypi pin",
                     f"{pkg}=={pin} на PyPI (последняя {v})")
            except urllib.error.HTTPError as e:
                _row(i.id, "FAIL" if e.code == 404 else "warn", "pypi pin",
                     f"{pkg}=={pin}: PyPI HTTP {e.code}")
            except Exception as e:
                _row(i.id, "warn", "pypi pin", f"{pkg}=={pin}: {type(e).__name__}")
    elif i.kind == "git":
        try:
            out = subprocess.run(["git", "ls-remote", i.url, "HEAD"],
                                 capture_output=True, text=True, timeout=60)
            head = (out.stdout or "").split("\t")[0].strip()
            if out.returncode != 0 or not head:
                _row(i.id, "FAIL", "git remote",
                     f"ls-remote не ответил: {(out.stderr or '')[:80]}")
            elif head != i.pin:
                _row(i.id, "warn", "git remote",
                     f"жив, HEAD={head[:8]} ≠ пин {i.pin[:8]} (апстрим уехал — обнови осознанно)")
            else:
                _row(i.id, "ok", "git remote", f"HEAD = пин {i.pin[:8]}")
        except Exception as e:
            _row(i.id, "warn", "git remote", f"{type(e).__name__}: {e}")
    elif i.kind == "oci":
        _check_oci(i)


def _pip_pairs(i: Installer) -> list[tuple[str, str]]:
    """(pkg, ver) из полей url/pip-реестра: 'PyPI: gamdl' + pin '3.7.4' или
    перечисления 'protobuf==6.33.4, construct==2.8.8' внутри pin."""
    pairs = re.findall(r"([A-Za-z0-9_.-]+)\s*==\s*([0-9][\w.]*)", i.pin)
    if pairs:
        return pairs
    m = re.match(r"PyPI:\s*([A-Za-z0-9_.-]+)(?:\s|$)", i.url)
    if m and re.match(r"^[0-9][\w.]*$", i.pin):
        return [(m.group(1), i.pin)]
    return []


def _check_oci(i: Installer) -> None:
    m = re.match(r"ghcr\.io/([\w./-]+):([\w.\-]+)", i.url)
    if not m:
        _row(i.id, "warn", "oci", f"не разобрал образ из {i.url}")
        return
    img, tag = m.group(1), m.group(2)
    try:
        with _http(f"https://ghcr.io/token?scope=repository:{img}:pull") as r:
            tok = json.load(r).get("token", "")
        acc = "application/vnd.oci.image.index.v1+json,application/vnd.docker.distribution.manifest.list.v2+json"
        req = urllib.request.Request(
            f"https://ghcr.io/v2/{img}/manifests/{tag}",
            headers={**UA, "Authorization": f"Bearer {tok}", "Accept": acc})
        with urllib.request.urlopen(req, timeout=HTTP_T) as r:
            dg = r.headers.get("Docker-Content-Digest", "?")
        _row(i.id, "ok", "oci tag", f"{img}:{tag} жив, digest {dg[:19]}")
    except Exception as e:
        _row(i.id, "warn", "oci", f"{img}:{tag}: {type(e).__name__}: {str(e)[:80]}")


# ─── сверка с VERSIONS.md / requirements.txt ─────────────────────────────────

def check_doc_pins() -> None:
    try:
        vm = open(os.path.join(ROOT, "VERSIONS.md"), encoding="utf-8").read()
    except Exception as e:
        _row("docs", "warn", "VERSIONS.md", str(e))
        return
    try:
        rq = open(os.path.join(ROOT, "requirements.txt"), encoding="utf-8").read()
    except Exception as e:
        rq = ""
        _row("docs", "warn", "requirements.txt", str(e))
    doc_pins = dict(re.findall(r"\|\s*\*\*([\w.-]+)\*\*\s*\|\s*`==([\w.]+)`", vm))
    req_pins = dict(re.findall(r"^([\w.-]+)==([\w.]+)", rq, re.M))
    for pkg, want in doc_pins.items():
        got = req_pins.get(pkg)
        if got and got != want:
            _row("docs", "FAIL", "pins", f"{pkg}: VERSIONS.md =={want}, requirements.txt =={got}")
        else:
            _row("docs", "ok", "pins", f"{pkg}=={want} ({'сверено с txt' if got else 'в txt нет строки — не противоречит'})")
    # реестр против VERSIONS.md по pip-установщикам
    for i in INSTALLERS:
        if i.kind != "pip":
            continue
        for pkg, pin in _pip_pairs(i):
            want = doc_pins.get(pkg)
            if want and want != pin:
                _row(i.id, "FAIL", "pin vs VERSIONS", f"{pkg}: реестр {pin}, VERSIONS.md {want}")
            elif want:
                _row(i.id, "ok", "pin vs VERSIONS", f"{pkg}=={pin}")


# ─── --full: настоящие установки в изолированный temp ────────────────────────

FULL_INSTALLERS = {   # id → (функция ripster.setup, имя бинаря для verify)
    "bento4":     ("install_mp4decrypt_windows", "mp4decrypt.exe"),
    "spotiflac":  ("install_spotiflac_windows",  "spotiflac.exe"),
    "ffmpeg":     ("install_ffmpeg_windows",     "ffmpeg.exe"),
    "node":       ("install_node_windows",       "node.exe"),
    "go":         ("install_go_windows",         "go.exe"),
}


def _setup_for_sandbox(tmp: str):
    """Wire ripster.setup on a sandbox base_dir — продуктовый код, никакой
    самодеятельности: те же загрузчики, те же хэши, те же verify."""
    import ripster.setup as S
    S.install(cfg={}, broadcast_fn=None, save_config_fn=lambda *a, **k: None,
              base_dir=__import__("pathlib").Path(tmp), is_windows=os.name == "nt")
    return S


async def _run_full_one(iid: str, only: list[str]) -> None:
    fn_name, exe_name = FULL_INSTALLERS[iid]
    tmp = tempfile.mkdtemp(prefix=f"ripster_install_{iid}_")
    try:
        S = _setup_for_sandbox(tmp)
        fn = getattr(S, fn_name)
        t0 = time.monotonic()
        r1 = await fn()
        first = time.monotonic() - t0
        log1 = [e.get("text", "") for e in S.install_log]
        dl1 = sum(1 for t in log1 if "Downloading" in t)
        # второй прогон: идемпотентность — ничего не качаем заново
        S.install_log.clear()
        r2 = await fn()
        dl2 = sum(1 for e in S.install_log if "Downloading" in e.get("text", ""))
        found = next((str(p) for p in (S._base_dir / "tools").rglob(exe_name)), "")
        # успех = бинарь реально появился в temp (часть функций возвращает None и при удаче);
        # «already present/уже стоит» в 1-м прогоне = принят системный или ранее
        # установленный инструмент — скачивание в песочнице не требовалось, это warn, не FAIL.
        accepted = any(("already present" in t) or ("уже стоит" in t) for t in log1)
        if found:
            st = "ok" if dl2 == 0 else "FAIL"
            det = (f"{first:.0f}с; exe={os.path.basename(found)} в temp; "
                   f"2-й прогон: {'идемпотентен' if dl2 == 0 else f'ПЕРЕКАЧАЛ ({dl2})'}")
        elif accepted:
            st, det = "warn", f"внешний инструмент принят без скачивания (idempotent-ветка); {first:.0f}с"
        else:
            st, det = "FAIL", f"{first:.0f}с; {exe_name} НЕ появился в temp — установка не состоялась"
        _row(iid, st, "full isolated install", det)
        # имитация отказа: соединение-чёрная дыра не должно вешать (STALL_TIMEOUT)
        S.install_log.clear()
        t0 = time.monotonic()
        ok_dead = await S.download_file("http://127.0.0.1:9/nothing.zip",
                                        S._base_dir / "x.zip", "тест: мёртвый URL")
        t_dead = time.monotonic() - t0
        ok_stall = None
        srv = socket.socket(); srv.bind(("127.0.0.1", 0)); srv.listen(1)
        port = srv.getsockname()[1]
        import threading
        conns = []
        def _blackhole():
            while True:
                c, _ = srv.accept(); conns.append(c)   # принимаем и молчим
        threading.Thread(target=_blackhole, daemon=True).start()
        t0 = time.monotonic()
        ok_stall = await S.download_file(f"http://127.0.0.1:{port}/stall.zip",
                                         S._base_dir / "y.zip", "тест: висящая сеть")
        t_stall = time.monotonic() - t0
        for c in conns:
            try: c.close()
            except Exception: pass
        srv.close()
        good = (not ok_dead and t_dead < 15 and not ok_stall and t_stall < S.STALL_TIMEOUT + 20)
        _row(iid + ".failure-sim", "ok" if good else "FAIL", "отказ без зависания",
             f"dead URL: {t_dead:.1f}с отказ; stalled: {t_stall:.1f}с "
             f"(STALL={S.STALL_TIMEOUT})")
    except Exception as e:
        _row(iid, "FAIL", "full", f"{type(e).__name__}: {e}")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ─── main ─────────────────────────────────────────────────────────────────────

def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--full", action="store_true",
                    help="реальные установки в изолированный temp (долго, качает)")
    ap.add_argument("--only", default="", help="список id через запятую (для --full)")
    ap.add_argument("--quiet", action="store_true", help="только проблемы (для healthcheck)")
    args = ap.parse_args()

    ids = [x.strip() for x in args.only.split(",") if x.strip()]

    for i in INSTALLERS:
        errs = check_contract(i)
        if errs:
            _row(i.id, "FAIL", "контракт", "; ".join(errs))
        else:
            _row(i.id, "ok", "контракт", f"kind={i.kind}, pin={i.pin[:24]}, t={i.timeout_s}с")
    check_doc_pins()
    for i in INSTALLERS:
        if not i.url or i.url == "—":
            continue
        check_live(i)

    if args.full:
        todo = [x for x in FULL_INSTALLERS if not ids or x in ids]
        for iid in todo:
            asyncio.run(_run_full_one(iid, ids))
    elif ids:
        print("(--only имеет смысл с --full)")

    fails = [r for r in results if r[1] == "FAIL"]
    warns = [r for r in results if r[1] == "warn"]
    if not args.quiet or fails or warns:
        print(f"{'id':<22}{'статус':<8}{'проверка':<24}детали")
        for iid, st, what, det in results:
            if args.quiet and st == "ok":
                continue
            print(f"{iid:<22}{st:<8}{what:<24}{det}")
    print(f"\nИТОГО: установщиков {len(INSTALLERS)}, "
          f"проверок {len(results)}, падений {len(fails)}, предупреждений {len(warns)}")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
