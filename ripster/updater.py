"""Ripster self-updater — pull new SOURCE from the project's GitHub repo, reconcile
pinned pip deps, verify the tree imports, signal a restart. Heavy deps and user
data are NEVER touched (separate / gitignored). See github_setup/DEPENDENCIES.md.

═══ Activation model — how NEW code starts working after files land ═══
A release is a coherent whole-tree snapshot, so a new file never arrives "unknown":
  • New ENGINE → AUTO-DISCOVERED. app.py iterates `ripster.engines` via
    pkgutil.iter_modules and imports each module; the engine's @register adds it to
    REGISTRY. Drop the file → it works on next restart. No other edit needed.
  • New ROUTE / feature module → wired by an explicit `<mod>.install(app, ctx)`
    line in app.py — which ships IN THE SAME update (the updated app.py). The
    wiring always travels with the new module.
  • New pip DEPENDENCY → declared in requirements.txt. The updater re-runs pip when
    that file changed. A new HEAVY dep is declared in setup.check_tools() and
    fetched via the Setup wizard, not here.
  • ACTIVATION BOUNDARY → restart. Python imports the whole tree fresh on start;
    the running process is replaced (restart_app), then the new modules/deps are live.
"""
from __future__ import annotations

import importlib
import pkgutil
import re
import subprocess
import sys
from pathlib import Path

# The launcher runs the server WINDOWLESS; any console child spawned without this
# flag pops a fresh cmd window (the "cmd flashes on update" bug). 0 on non-Windows.
_CNW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

# A version is any dot-separated run of numbers (≥2 components preferred so a
# stray digit in prose isn't mistaken for a version); a bare number is the
# fallback. ANY component may be tens/hundreds — comparison is NUMERIC, so
# 3.9 < 3.10 < 3.100 (never the string-order trap where "10" < "9").
_VER_DOTTED = re.compile(r"\d+(?:\.\d+)+")
_VER_BARE   = re.compile(r"\d+")


# ── pure version / requirements logic (unit-tested) ──────────────────────────
def parse_version(s: str) -> tuple[int, ...]:
    """Parse a version of ANY component count into an int tuple.
    'v3.1.0' → (3,1,0); '3.10' → (3,10); 'v3.10.2.5-beta' → (3,10,2,5);
    'Ripster 3.100.0' → (3,100,0). No version found → (0,)."""
    m = _VER_DOTTED.search(s or "") or _VER_BARE.search(s or "")
    if not m:
        return (0,)
    return tuple(int(p) for p in m.group(0).split("."))


def is_newer(remote: str, local: str) -> bool:
    """True iff `remote` is a strictly newer version than `local`. Components are
    compared numerically; tuples are zero-padded to equal length so 3.10 == 3.10.0
    and 3.10.1 > 3.10. Works regardless of how many digits/components changed."""
    a, b = parse_version(remote), parse_version(local)
    n = max(len(a), len(b))
    a += (0,) * (n - len(a))
    b += (0,) * (n - len(b))
    return a > b


def _pins(text: str) -> set[str]:
    out: set[str] = set()
    for line in (text or "").splitlines():
        line = line.split("#", 1)[0].strip()
        if line and not line.startswith("-"):
            out.add(line.lower().replace(" ", ""))
    return out


def requirements_changed(old_text: str, new_text: str) -> bool:
    """True iff the set of pinned requirements differs (ignoring comments/order/
    whitespace) — i.e. the updater must re-run pip after this update."""
    return _pins(old_text) != _pins(new_text)


# ── runtime gate: the post-update smoke check (reuses the test-net idea) ──────
def verify_import_smoke() -> tuple[bool, str]:
    """Import EVERY ripster.* module — the runtime gate that catches a broken
    update (the exact failure class that downs the server). On any failure the
    caller must roll back to the pre-update snapshot. Returns (ok, detail)."""
    import ripster
    failed: list[str] = []
    for m in pkgutil.walk_packages(ripster.__path__, "ripster."):
        try:
            importlib.import_module(m.name)
        except Exception as e:                       # noqa: BLE001 — report, don't crash
            failed.append(f"{m.name}: {type(e).__name__}: {e}")
    if failed:
        return False, "; ".join(failed[:5])
    return True, "all modules import"


# ── network / git orchestration (best-effort; integration, not unit-tested) ──
_DEFAULT_REPO = "Raccoon-Trashpanda/Raccoon-Ripster"
def _repo(config) -> str:
    # Default so a fresh install (whose config.yaml may predate the ripster-repo
    # key) can still self-update without the user configuring anything.
    return (config.get("ripster-repo") or "").strip() or _DEFAULT_REPO


def _git_remote_is_repo(base_dir: Path, repo: str) -> bool:
    """True only if base_dir is a git clone whose `origin` IS the Ripster repo.
    A STRAY .git from another project (observed in the wild: the zhaarey Go
    `apple-music-downloader` clone leaves an origin pointing at zhaarey, NOT
    Raccoon-Ripster) must NOT hijack apply_update into a no-op `git pull
    --ff-only` ("Already up to date" → false success, the real release-zip overlay
    never runs). When the remote doesn't match, callers fall through to the
    zipball overlay path."""
    try:
        r = subprocess.run(["git", "-C", str(base_dir), "remote", "get-url", "origin"],
                           capture_output=True, text=True, timeout=15, creationflags=_CNW)
        url  = (r.stdout or "").strip().lower()
        slug = (repo or "").strip().lower()
        return bool(url) and bool(slug) and (slug in url or slug.split("/")[-1] in url)
    except Exception:                                  # noqa: BLE001
        return False


def _gh_headers(config) -> dict:
    """GitHub API headers, with a Bearer token when one is configured. A token is
    only needed for PRIVATE repos (public-repo self-update works token-less). Read
    from config `ripster-repo-token` or the GITHUB_TOKEN env var — never hardcoded,
    so the distributable ships none and the owner can opt in."""
    import os
    h = {"Accept": "application/vnd.github+json"}
    tok = (config.get("ripster-repo-token") or os.environ.get("GITHUB_TOKEN") or "").strip()
    if tok:
        h["Authorization"] = f"Bearer {tok}"
    return h


def _http_hint(code: int) -> str:
    if code == 404:
        return " — релиз не найден или репозиторий приватный (нужен ripster-repo-token)"
    if code in (401, 403):
        return " — нет доступа (приватный репозиторий? проверь ripster-repo-token)"
    return ""


async def check_for_update(config, current_version: str) -> dict:
    """Query GitHub for the latest release and compare to current_version."""
    repo = _repo(config)
    if not repo:
        return {"ok": False, "error": "репозиторий не настроен (ripster-repo в Settings)"}
    import httpx
    try:
        async with httpx.AsyncClient(timeout=10) as c:
            r = await c.get(f"https://api.github.com/repos/{repo}/releases/latest",
                            headers=_gh_headers(config))
        if r.status_code != 200:
            return {"ok": False, "error": f"GitHub HTTP {r.status_code}{_http_hint(r.status_code)}"}
        j = r.json()
        tag = j.get("tag_name", "")
        return {
            "ok": True, "current": current_version, "latest": tag,
            "available": is_newer(tag, current_version),
            "changelog": (j.get("body") or "")[:2000],
            "url": j.get("html_url", ""), "zip": j.get("zipball_url", ""),
        }
    except Exception as e:                            # noqa: BLE001
        return {"ok": False, "error": str(e)}


def _snapshot_source(base_dir: Path) -> Path | None:
    """Best-effort backup of the source tree before applying an update so a failed
    verify can roll back. Mirrors the existing backups/RESTORE_* convention."""
    try:
        import shutil
        from datetime import datetime  # noqa: F401 — only used if a stamp is passed in
        dst = base_dir / "backups" / "pre_update_source"
        if dst.exists():
            shutil.rmtree(dst, ignore_errors=True)   # fresh snapshot, no stale files
        dst.mkdir(parents=True, exist_ok=True)
        for sub in _SNAPSHOT_PATHS:
            src = base_dir / sub
            if src.is_dir():
                shutil.copytree(src, dst / sub, dirs_exist_ok=True)
            elif src.is_file():
                (dst / sub).parent.mkdir(parents=True, exist_ok=True)  # nested paths (tools/lucida/…)
                shutil.copy2(src, dst / sub)
        return dst
    except Exception as e:                            # noqa: BLE001
        print(f"[updater] snapshot failed: {e}", flush=True)
        return None


def _restore_snapshot(base_dir: Path, snap) -> bool:
    """Undo a failed update by copying the pre-update snapshot back over the tree.
    Mirrors _SNAPSHOT_PATHS exactly, so it heals whatever the overlay touched."""
    if not snap:
        return False
    snap = Path(snap)
    if not snap.is_dir():
        return False
    try:
        import shutil
        for sub in _SNAPSHOT_PATHS:
            s, d = snap / sub, base_dir / sub
            if s.is_dir():
                shutil.rmtree(d, ignore_errors=True)
                shutil.copytree(s, d)
            elif s.is_file():
                shutil.copy2(s, d)
        _clear_pycache(base_dir / "ripster")
        return True
    except Exception as e:                            # noqa: BLE001
        print(f"[updater] restore failed: {e}", flush=True)
        return False


# ── portable overlay: apply a GitHub release zipball without git ──────────────
# Only CODE + STATIC are overlaid. Heavy deps (python/, tools/), secrets
# (config.yaml, tokens/, *.wvd) and user data (downloads/) are NEVER in this list,
# so an update can't clobber them. A path absent from the release is skipped, not
# deleted — a partial archive can't strip the install.
_OVERLAY_PATHS = ("ripster", "static", "app.py", "amd_runner.py",
                  "sc_widevine_runner.py", "requirements.txt", "main.go", "README.md",
                  # SoundCloud/Lucida CLI wrapper — small CODE, not a heavy dep (the
                  # heavy parts, lucida-src/build + node_modules, are built locally and
                  # NOT overlaid). Ship the runner so updates keep SoundCloud working.
                  "tools/lucida/runner.mjs", "tools/lucida/package.json",
                  # Widevine L3 minter scripts (wvd.bat / wvd_console.ps1) — small CODE,
                  # not the heavy toolchain (JRE/SDK/emulator stay untouched). Without
                  # this, testers on an old install never receive minter pipeline fixes
                  # (e.g. the -Auto one-click path) through self-update.
                  "_widevine_setup")
# Snapshot covers everything the overlay may write so rollback is exact.
_SNAPSHOT_PATHS = _OVERLAY_PATHS


def _clear_pycache(root: Path) -> None:
    """Drop stale __pycache__ so freshly-overlaid .py source wins on next import."""
    try:
        import shutil
        if root.exists():
            for pc in root.rglob("__pycache__"):
                shutil.rmtree(pc, ignore_errors=True)
    except Exception:                                 # noqa: BLE001
        pass


async def _fetch_release_zip(config) -> tuple[bytes | None, str]:
    """Download the latest release's zipball bytes. Returns (bytes, '') or (None, err)."""
    repo = _repo(config)
    if not repo:
        return None, "репозиторий не настроен (ripster-repo в Settings)"
    import httpx
    try:
        headers = _gh_headers(config)
        async with httpx.AsyncClient(timeout=60, follow_redirects=True) as c:
            r = await c.get(f"https://api.github.com/repos/{repo}/releases/latest",
                            headers=headers)
            if r.status_code != 200:
                return None, f"GitHub HTTP {r.status_code}{_http_hint(r.status_code)}"
            zip_url = r.json().get("zipball_url")
            if not zip_url:
                return None, "у релиза нет zipball_url"
            z = await c.get(zip_url, headers=headers)
            if z.status_code != 200:
                return None, f"zipball HTTP {z.status_code}{_http_hint(z.status_code)}"
            return z.content, ""
    except Exception as e:                            # noqa: BLE001
        return None, str(e)


def _apply_overlay(zip_bytes: bytes, base_dir: Path) -> tuple[bool, str]:
    """Extract the release zip and overlay _OVERLAY_PATHS onto base_dir. GitHub wraps
    the tree in a single top-level dir (owner-repo-<sha>/); we strip it. Files are
    merge-copied (overwrite listed paths, leave unlisted user files alone)."""
    import io
    import shutil
    import tempfile
    import zipfile
    try:
        zf = zipfile.ZipFile(io.BytesIO(zip_bytes))
    except Exception as e:                            # noqa: BLE001
        return False, f"zip не открылся: {e}"
    names = [n for n in zf.namelist() if n]
    if not names:
        return False, "пустой архив"
    root = names[0].split("/", 1)[0]
    with tempfile.TemporaryDirectory() as td:
        try:
            zf.extractall(td)
        except Exception as e:                        # noqa: BLE001
            return False, f"распаковка не удалась: {e}"
        src_root = Path(td) / root
        if not src_root.is_dir():
            src_root = Path(td)
        copied = 0
        for rel in _OVERLAY_PATHS:
            src, dst = src_root / rel, base_dir / rel
            if not src.exists():
                continue
            try:
                if src.is_dir():
                    for p in src.rglob("*"):
                        if p.is_dir():
                            continue
                        target = dst / p.relative_to(src)
                        target.parent.mkdir(parents=True, exist_ok=True)
                        shutil.copy2(p, target)
                        copied += 1
                else:
                    dst.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(src, dst)
                    copied += 1
            except Exception as e:                    # noqa: BLE001
                return False, f"копирование {rel}: {e}"
    _clear_pycache(base_dir / "ripster")
    return True, f"overlay {root}: {copied} файлов"


def _verify_subprocess(base_dir: Path) -> tuple[bool, str]:
    """Verify the UPDATED tree in a FRESH interpreter — the in-process import cache
    would hide newly-broken modules, so a child process is the only honest gate.
    Imports every ripster.* module and byte-compiles app.py. Returns (ok, detail).

    base_dir is pinned to sys.path[0] EXPLICITLY: the bundled embeddable Python runs
    isolated (ignores cwd/PYTHONPATH and its ._pth may point elsewhere), so without
    this the child could import a DIFFERENT ripster and verify the wrong tree."""
    bd = str(base_dir)
    code = (
        "import sys\n"
        f"sys.path.insert(0, {bd!r})\n"
        "import importlib,pkgutil,py_compile,os,ripster\n"
        f"os.chdir({bd!r})\n"
        f"assert ripster.__file__.startswith({bd!r}), 'wrong ripster: '+ripster.__file__\n"
        "bad=[]\n"
        "for m in pkgutil.walk_packages(ripster.__path__,'ripster.'):\n"
        "    try: importlib.import_module(m.name)\n"
        "    except Exception as e: bad.append(m.name+': '+type(e).__name__+': '+str(e)[:120])\n"
        "try: py_compile.compile('app.py', doraise=True)\n"
        "except Exception as e: bad.append('app.py: '+type(e).__name__+': '+str(e)[:120])\n"
        "print('OK' if not bad else 'FAIL: '+'; '.join(bad[:6]))\n"
        "sys.exit(1 if bad else 0)\n"
    )
    try:
        r = subprocess.run([sys.executable, "-c", code], cwd=bd,
                           capture_output=True, text=True, timeout=120, creationflags=_CNW)
        out = (r.stdout + r.stderr).strip()
        return r.returncode == 0, out[-600:] or "no output"
    except Exception as e:                            # noqa: BLE001
        return False, str(e)


async def apply_update(config, base_dir: Path) -> dict:
    """Apply an update: snapshot → fetch new source (git pull when the install is a
    git clone) → re-run pip if requirements changed → verify imports → roll back on
    failure. Returns a structured result; the CALLER triggers restart_app on ok.
    Heavy deps and user data (config.yaml/tokens/.wvd/downloads) are untouched."""
    base_dir = Path(base_dir)
    req = base_dir / "requirements.txt"
    old_req = req.read_text(encoding="utf-8") if req.exists() else ""

    snap = _snapshot_source(base_dir)

    # Fetch new source. Two paths, same downstream pipeline:
    #   • git clone  → git pull --ff-only (dev installs)
    #   • portable   → download the release zipball and overlay code/static
    #                  (the distributed exe install has no .git)
    if (base_dir / ".git").exists() and _git_remote_is_repo(base_dir, _repo(config)):
        try:
            r = subprocess.run(["git", "-C", str(base_dir), "pull", "--ff-only"],
                               capture_output=True, text=True, timeout=120, creationflags=_CNW)
            if r.returncode != 0:
                return {"ok": False, "stage": "git-pull", "error": (r.stderr or r.stdout)[:500]}
        except Exception as e:                        # noqa: BLE001
            return {"ok": False, "stage": "git-pull", "error": str(e)}
    else:
        zip_bytes, err = await _fetch_release_zip(config)
        if zip_bytes is None:
            return {"ok": False, "stage": "fetch", "error": err}
        ok_o, detail_o = _apply_overlay(zip_bytes, base_dir)
        if not ok_o:
            # Overlay copy failed mid-flight — restore the snapshot immediately.
            restored = _restore_snapshot(base_dir, snap)
            return {"ok": False, "stage": "overlay", "error": detail_o,
                    "snapshot": str(snap), "rolled_back": restored}

    # Reconcile pinned pip deps only when requirements.txt actually changed.
    new_req = req.read_text(encoding="utf-8") if req.exists() else ""
    pip_ran = False
    if requirements_changed(old_req, new_req):
        pip_ran = True
        try:
            subprocess.run([sys.executable, "-m", "pip", "install", "-r", str(req)],
                           capture_output=True, text=True, timeout=600, creationflags=_CNW)
        except Exception as e:                        # noqa: BLE001
            restored = _restore_snapshot(base_dir, snap)
            return {"ok": False, "stage": "pip", "error": str(e),
                    "snapshot": str(snap), "rolled_back": restored}

    # Runtime gate: the new tree MUST import cleanly in a FRESH interpreter, else
    # roll back automatically so the install is never left bricked.
    ok, detail = _verify_subprocess(base_dir)
    if not ok:
        restored = _restore_snapshot(base_dir, snap)
        return {"ok": False, "stage": "verify", "error": detail,
                "snapshot": str(snap), "rollback_needed": not restored,
                "rolled_back": restored}

    return {"ok": True, "pip_ran": pip_ran, "verify": detail,
            "restart_required": True}
