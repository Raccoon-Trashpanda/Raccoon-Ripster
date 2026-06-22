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
def _repo(config) -> str:
    return (config.get("ripster-repo") or "").strip()      # e.g. "owner/Ripster"


async def check_for_update(config, current_version: str) -> dict:
    """Query GitHub for the latest release and compare to current_version."""
    repo = _repo(config)
    if not repo:
        return {"ok": False, "error": "репозиторий не настроен (ripster-repo в Settings)"}
    import httpx
    try:
        async with httpx.AsyncClient(timeout=10) as c:
            r = await c.get(f"https://api.github.com/repos/{repo}/releases/latest",
                            headers={"Accept": "application/vnd.github+json"})
        if r.status_code != 200:
            return {"ok": False, "error": f"GitHub HTTP {r.status_code}"}
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
        dst.mkdir(parents=True, exist_ok=True)
        for sub in ("ripster", "app.py", "amd_runner.py", "requirements.txt"):
            src = base_dir / sub
            if src.is_dir():
                shutil.copytree(src, dst / sub, dirs_exist_ok=True)
            elif src.is_file():
                shutil.copy2(src, dst / sub)
        return dst
    except Exception as e:                            # noqa: BLE001
        print(f"[updater] snapshot failed: {e}", flush=True)
        return None


async def apply_update(config, base_dir: Path) -> dict:
    """Apply an update: snapshot → fetch new source (git pull when the install is a
    git clone) → re-run pip if requirements changed → verify imports → roll back on
    failure. Returns a structured result; the CALLER triggers restart_app on ok.
    Heavy deps and user data (config.yaml/tokens/.wvd/downloads) are untouched."""
    base_dir = Path(base_dir)
    req = base_dir / "requirements.txt"
    old_req = req.read_text(encoding="utf-8") if req.exists() else ""

    snap = _snapshot_source(base_dir)

    # Fetch new source. git pull is the clean path (git is a required dep); a
    # portable archive without git would instead overlay the release zip (TODO).
    if (base_dir / ".git").exists():
        try:
            r = subprocess.run(["git", "-C", str(base_dir), "pull", "--ff-only"],
                               capture_output=True, text=True, timeout=120)
            if r.returncode != 0:
                return {"ok": False, "stage": "git-pull", "error": (r.stderr or r.stdout)[:500]}
        except Exception as e:                        # noqa: BLE001
            return {"ok": False, "stage": "git-pull", "error": str(e)}
    else:
        return {"ok": False, "stage": "fetch",
                "error": "не git-клон — оверлей release-zip ещё не реализован (portable)"}

    # Reconcile pinned pip deps only when requirements.txt actually changed.
    new_req = req.read_text(encoding="utf-8") if req.exists() else ""
    pip_ran = False
    if requirements_changed(old_req, new_req):
        pip_ran = True
        try:
            subprocess.run([sys.executable, "-m", "pip", "install", "-r", str(req)],
                           capture_output=True, text=True, timeout=600)
        except Exception as e:                        # noqa: BLE001
            return {"ok": False, "stage": "pip", "error": str(e), "snapshot": str(snap)}

    # Runtime gate: the new tree MUST import cleanly, else roll back.
    ok, detail = verify_import_smoke()
    if not ok:
        return {"ok": False, "stage": "verify", "error": detail,
                "snapshot": str(snap), "rollback_needed": True}

    return {"ok": True, "pip_ran": pip_ran, "verify": detail,
            "restart_required": True}
