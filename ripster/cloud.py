"""Anonymous cloud upload — Gofile.io backend.

Public API:
    upload_to_gofile(path: Path) -> str   upload a single file, return download URL
    upload_task_files(task: dict) -> str  zip task audio files if needed, upload, return URL
"""
from __future__ import annotations

import os
import tempfile
from pathlib import Path
from typing import Optional

import httpx


# ── Gofile helpers ────────────────────────────────────────────────────────────

async def _get_best_server() -> str:
    """Return the fastest Gofile upload server (e.g. 'store1')."""
    try:
        async with httpx.AsyncClient(timeout=8) as c:
            r = await c.get("https://api.gofile.io/servers")
            d = r.json()
            # API v2: {"status":"ok","data":{"servers":[{"name":"store1","zone":"eu"}...]}}
            servers = d.get("data", {}).get("servers") or []
            if servers:
                return servers[0]["name"]
    except Exception:
        pass
    return "store1"  # reasonable default


async def _get_guest_token() -> Optional[str]:
    """Create an anonymous Gofile guest account and return its token.

    Gofile's current API rejects truly-anonymous uploads (the server fails with
    HTTP 500 `error-createFolderResponse` because it can't auto-create a folder
    without an account). Creating a throwaway guest account first and uploading
    with `Authorization: Bearer <token>` fixes this."""
    try:
        async with httpx.AsyncClient(timeout=10) as c:
            r = await c.post("https://api.gofile.io/accounts")
            d = r.json()
            if d.get("status") == "ok":
                return (d.get("data") or {}).get("token")
    except Exception as e:
        print(f"[gofile] guest-account creation failed: {e}", flush=True)
    return None


async def upload_to_gofile(path: Path, filename: Optional[str] = None) -> str:
    """Upload *path* to Gofile and return the download page URL.

    Raises RuntimeError on failure.
    """
    server = await _get_best_server()
    url    = f"https://{server}.gofile.io/contents/uploadfile"
    fname  = filename or path.name
    token  = await _get_guest_token()
    headers = {"Authorization": f"Bearer {token}"} if token else {}

    file_size = path.stat().st_size
    mb = file_size / 1024 / 1024
    print(f"[gofile] uploading {fname} ({mb:.1f} MB) → {url} "
          f"(token={'yes' if token else 'NONE'})", flush=True)

    async with httpx.AsyncClient(timeout=600) as c:
        with open(path, "rb") as fh:
            r = await c.post(url, files={"file": (fname, fh, "application/octet-stream")},
                             headers=headers)

    if r.status_code != 200:
        raise RuntimeError(f"Gofile HTTP {r.status_code}: {r.text[:200]}")

    data = r.json()
    if data.get("status") != "ok":
        raise RuntimeError(f"Gofile error: {data.get('status')} — {data}")

    d = data.get("data", {})
    # API v3: downloadPage is returned directly; v2: code field
    link = (
        d.get("downloadPage")
        or (f"https://gofile.io/d/{d['parentFolderCode']}" if d.get("parentFolderCode") else None)
        or (f"https://gofile.io/d/{d['code']}" if d.get("code") else None)
    )
    if not link:
        raise RuntimeError(f"Gofile: no download link in response: {data}")
    print(f"[gofile] uploaded OK → {link}", flush=True)
    return link


# ── Task-level upload ─────────────────────────────────────────────────────────

async def upload_task_to_gofile(task: dict) -> str:
    """Find task output files, zip if multiple, upload, return download URL.

    Raises RuntimeError on any failure.
    """
    from ripster.routes.download import _get_task_dir, _find_audio_files, _write_zip_file

    d = _get_task_dir(task)
    if not d:
        raise RuntimeError("Директория загрузки не найдена — файлы могли быть перемещены.")

    files = _find_audio_files(d)
    if not files:
        raise RuntimeError("Аудио файлы не найдены в директории загрузки.")

    if len(files) == 1:
        return await upload_to_gofile(files[0])

    # Multiple files → zip first
    from ripster.guest_manager import _sanitize
    meta  = task.get("meta") or {}
    title = meta.get("title") or task.get("id", "download")
    zip_name = _sanitize(title) + ".zip"
    tmp_path = _write_zip_file(files)
    try:
        return await upload_to_gofile(Path(tmp_path), filename=zip_name)
    finally:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
