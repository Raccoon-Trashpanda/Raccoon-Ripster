"""
Diagnostic report bundle: collect this install's logs into one zip, and (opt-in,
one click) hand it to the developer.

Why this exists: users were sending photographs of their screen. A screenshot
shows one line of a failure whose cause scrolled past ten minutes earlier, and
it never shows the version, the shell, or the environment — on 27.07.2026 two
separate users hit two separate bugs and both were only diagnosable after
interviewing them. The warn/error telemetry stream doesn't close that gap either:
it carries no context around the failure.

Nothing leaves the machine unless the user presses the button. Everything that
does leave is redacted first: credential-shaped strings inside log lines via
telemetry.redact(), and secret config values by name.

  build_bundle(...)  -> zip bytes  (also serves the local "download logs" route)
  send_report(...)   -> {ok, code} (uploads to the owner's ingest)
"""
from __future__ import annotations

import io
import json
import platform
import sys
import zipfile
from datetime import datetime
from pathlib import Path

from ripster import telemetry as _t

# Логи, которые реально помогают: консоль приложения + её ротации, ошибки
# движков, лаунчер, вывод установки. bot.log сознательно не берём — он огромный
# и это личная переписка, к чужой установке отношения не имеет.
_WANTED = ["console.log", "console.log.1", "console.log.2", "console.log.3",
           "errors.log", "launcher.log", "app_err.log", "install.log"]

_MAX_FILE_BYTES = 3 * 1024 * 1024          # хвост каждого файла


def _secret_values(base_dir: Path) -> list:
    """Фактические значения секретов из config.yaml.

    Вычистка по шаблону всегда угадывает формат и однажды не угадает: живой
    яндекс-токен уехал в отчёт как `--token <значение>`, потому что правило
    знало «auth-token», но не голый `--token`. Значение мы знаем точно, так что
    режем его по совпадению, чем бы оно ни было окружено. Шаблоны остаются
    вторым слоем — они ловят чужие секреты, которых нет в нашем конфиге.
    """
    try:
        import yaml
        from ripster.routes.core import _SECRET_KEYS
        cfg = yaml.safe_load((base_dir / "config.yaml").read_text(encoding="utf-8")) or {}
    except Exception:
        return []
    from ripster.routes.core import _looks_secret
    out: list = []

    def walk(key, val):
        if isinstance(val, dict):
            for k, v in val.items():
                walk(k, v)
        elif isinstance(val, list):
            for v in val:
                walk(key, v)
        elif isinstance(val, str) and len(val.strip()) >= 8:
            # и по имени из плоского списка, и по «похоже на секрет» вглубь —
            # пароли внутри wrapper-accounts не видны ни тем, ни другим по
            # отдельности
            if key in _SECRET_KEYS or _looks_secret(key):
                out.append(val.strip())

    walk("", cfg)
    # длинные — первыми, иначе короткий кусок съест часть длинного и оставит хвост
    return sorted(set(out), key=len, reverse=True)


def _redact_text(raw: str, secrets: list | None = None) -> str:
    out = "\n".join(_t.redact(ln) for ln in raw.splitlines())
    for val in (secrets or []):
        if val in out:
            out = out.replace(val, "«вырезано»")
    return out


def _tail(p: Path) -> str:
    """Хвост файла: старое начало лога бесполезно, а размер архива не резиновый."""
    data = p.read_bytes()
    clipped = len(data) > _MAX_FILE_BYTES
    if clipped:
        data = data[-_MAX_FILE_BYTES:]
    text = data.decode("utf-8", errors="replace")
    if clipped:
        text = f"…обрезано, показан последний {_MAX_FILE_BYTES // 1024} КБ…\n" + text
    return text


def _safe_config(base_dir: Path) -> str:
    """config.yaml со скрытыми секретами — по нему видно, что вообще включено."""
    try:
        import yaml
        from ripster.routes.core import _redact_config
        cfg = yaml.safe_load((base_dir / "config.yaml").read_text(encoding="utf-8")) or {}
        return json.dumps(_redact_config(cfg), ensure_ascii=False, indent=2, default=str)
    except Exception as e:                                    # noqa: BLE001
        return f"config unavailable: {e}"


def _environment(base_dir: Path, version: str) -> str:
    """Окружение — первое, что нужно знать, и именно его не видно на скриншоте."""
    shell = "Ripster.exe (WebView2)" if (base_dir / "Ripster.exe").exists() else "?"
    lines = [
        f"Ripster        {version}",
        f"generated      {datetime.now():%Y-%m-%d %H:%M:%S}",
        f"instance id    {(_t._cfg.get('telemetry-instance-id') or '') if _t._cfg else ''}",
        f"OS             {platform.platform()}",
        f"python         {sys.version.split()[0]}  ({sys.executable})",
        f"install dir    {base_dir}",
        f"launcher       {shell}",
        f"frozen         {getattr(sys, 'frozen', False)}",
    ]
    for name, rel in (("bundled python", "python/python.exe"),
                      ("ffmpeg",         "tools/ffmpeg/bin/ffmpeg.exe"),
                      ("OrpheusDL",      "orpheus/orpheus.py"),
                      ("auth helper",    "orpheus/_auth_helper.py"),
                      ("AMD",            "AppleMusicDecrypt/main.py"),
                      ("device.wvd",     "tools/widevine/device.wvd")):
        lines.append(f"{name:<14} {'есть' if (base_dir / rel).exists() else 'НЕТ'}")
    return "\n".join(lines)


def build_bundle(base_dir: Path, version: str, note: str = "") -> bytes:
    """Собрать zip с логами. Всё текстовое проходит редактирование."""
    secrets = _secret_values(base_dir)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("_environment.txt", _environment(base_dir, version))
        zf.writestr("_config_redacted.json", _safe_config(base_dir))
        if note.strip():
            zf.writestr("_user_note.txt", _redact_text(note.strip()[:4000], secrets))
        # errors.log лежит в корне (его пишет runner.py), остальные — в logs/
        candidates = [base_dir / "errors.log"] + [base_dir / "logs" / n for n in _WANTED]
        for p in candidates:
            try:
                if p.exists() and p.stat().st_size > 0:
                    zf.writestr(p.name, _redact_text(_tail(p), secrets))
            except Exception:
                pass
    return buf.getvalue()


async def send_report(cfg: dict, base_dir: Path, version: str, note: str = "") -> dict:
    """Отправить архив владельцу. Возвращает короткий код для человека."""
    base = _t.ingest_url()
    if not base:
        return {"ok": False, "error_key": "err.report_endpoint_unset", "error": "Адрес приёма не настроен в этой сборке"}
    if not base.startswith(("http://", "https://")):
        base = "https://" + base

    blob = build_bundle(base_dir, version, note)
    headers = {
        "Content-Type":       "application/zip",
        "X-Ripster-Token":    _t.ingest_token(),
        "X-Ripster-Instance": (cfg.get("telemetry-instance-id") or "").strip(),
        "X-Ripster-Version":  version,
        "X-Ripster-Platform": platform.platform()[:64],
        # заголовки обязаны быть latin-1: имя и заметка бывают кириллицей
        "X-Ripster-Name":     _hdr(cfg.get("telemetry-name") or ""),
        "X-Ripster-Note":     _hdr(note),
    }
    try:
        import httpx
        async with httpx.AsyncClient(timeout=120) as client:
            r = await client.post(f"{base}/api/telemetry/report", content=blob, headers=headers)
        if r.status_code != 200:
            return {"ok": False, "error_key": "err.dev_server_http", "error_args": {"code": r.status_code}, "error": f"Сервер разработчика ответил {r.status_code}"}
        data = r.json()
        if not data.get("ok"):
            return {"ok": False, "error_key": "err.report_rejected", "error": str(data.get("error") or "отказано в приёме")}
        return {"ok": True, "code": data.get("code", ""), "size": len(blob)}
    except Exception as e:                                    # noqa: BLE001
        return {"ok": False, "error_key": "err.dev_unreachable", "error_args": {"e": str(e)[:140]}, "error": f"Не удалось связаться с разработчиком: {str(e)[:140]}"}


def _hdr(s: str) -> str:
    """Кириллица в HTTP-заголовке роняет отправку — кодируем в latin-1-safe."""
    import base64
    s = str(s or "")[:300]
    if not s:
        return ""
    try:
        s.encode("latin-1")
        return s
    except UnicodeEncodeError:
        return "b64:" + base64.b64encode(s.encode("utf-8")).decode("ascii")


def decode_hdr(s: str) -> str:
    """Обратная сторона _hdr — на приёме."""
    import base64
    s = str(s or "")
    if s.startswith("b64:"):
        try:
            return base64.b64decode(s[4:]).decode("utf-8", errors="replace")
        except Exception:
            return ""
    return s
