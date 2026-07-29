"""Native Windows desktop notification on download completion.

Fires a REAL Windows toast (WinRT via PowerShell -EncodedCommand) so the user
sees "download finished" even when Ripster is minimized / in the tray. No extra
Python dependency (ships in the code overlay, not the bundled interpreter).

Why native (not the in-app toast): only an OS toast shows on the desktop while the
window is hidden — AND Windows Focus Assist auto-suppresses toasts while a game or
other app is fullscreen, so we get "don't interrupt games" for free.

Best-effort: silent no-op on non-Windows or any failure. Gated by the
`notify-on-done` config flag (off by default).
"""
from __future__ import annotations

import base64
import os
import subprocess

_CNW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

# Stable AppUserModelID so toasts group under one entry. The PowerShell shell AUMID
# is always present on Win10+; it shows a generic source name but never fails to
# register (a custom AUMID needs a Start-menu shortcut — deferred).
_AUMID = ("{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}"
          "\\WindowsPowerShell\\v1.0\\powershell.exe")


def _psq(s: str) -> str:
    """Escape a string for a PowerShell single-quoted literal + trim length."""
    return (s or "").replace("'", "''")[:90]


def _psq_xml(s: str) -> str:
    """То же, но без обрезки — XML уведомления целиком, резать его нельзя."""
    return (s or "").replace("'", "''")


def _xml_esc(s: str) -> str:
    return (str(s or "").replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;").replace('"', "&quot;").replace("'", "&apos;"))[:120]


def _cover_file(url: str) -> str:
    """Скачать обложку во временный файл и вернуть путь.

    Windows-уведомление НЕ умеет http-картинки для неупакованных приложений —
    только локальный файл. Поэтому качаем и кэшируем по имени: один и тот же
    релиз не должен тянуть обложку заново.
    """
    url = (url or "").strip()
    if not url.lower().startswith(("http://", "https://")):
        return ""
    try:
        import hashlib
        import tempfile
        import urllib.request
        from pathlib import Path
        d = Path(tempfile.gettempdir()) / "ripster_toast_covers"
        d.mkdir(parents=True, exist_ok=True)
        fp = d / (hashlib.sha1(url.encode()).hexdigest()[:16] + ".jpg")
        if fp.exists() and fp.stat().st_size > 0:
            return str(fp)
        req = urllib.request.Request(url, headers={"User-Agent": "Ripster"})
        with urllib.request.urlopen(req, timeout=8) as r:
            data = r.read(2_000_000)
        if not data:
            return ""
        fp.write_bytes(data)
        return str(fp)
    except Exception:
        return ""


def _ps_toast(title: str, body: str, sub: str = "", image: str = "") -> None:
    """Показать уведомление Windows.

    Раньше использовался шаблон ToastText02 — он ТОЛЬКО ТЕКСТОВЫЙ, картинку в
    него положить нельзя в принципе, поэтому обложки и не было. Собираем XML
    сами: две-три строки плюс обложка релиза сбоку.
    """
    if os.name != "nt":
        return
    img_xml = ""
    if image and os.path.exists(image):
        # У hint-crop допустимы ТОЛЬКО "none" и "circle". С посторонним значением
        # (у меня было "default") Windows не ругается — она молча выбрасывает всю
        # картинку, и уведомление приходит голым текстом. Атрибут не пишем вовсе.
        img_xml = f'<image placement="appLogoOverride" src="{_xml_esc(image)}"/>'
    lines = f"<text>{_xml_esc(title)}</text><text>{_xml_esc(body)}</text>"
    if sub:
        lines += f"<text>{_xml_esc(sub)}</text>"
    toast_xml = (f'<toast><visual><binding template="ToastGeneric">'
                 f'{lines}{img_xml}</binding></visual></toast>')
    script = (
        "$ErrorActionPreference='Stop'\n"
        "[void][Windows.UI.Notifications.ToastNotificationManager,"
        "Windows.UI.Notifications,ContentType=WindowsRuntime]\n"
        "[void][Windows.Data.Xml.Dom.XmlDocument,"
        "Windows.Data.Xml.Dom,ContentType=WindowsRuntime]\n"
        "$x=[Windows.Data.Xml.Dom.XmlDocument]::new()\n"
        f"$x.LoadXml('{_psq_xml(toast_xml)}')\n"
        "$toast=[Windows.UI.Notifications.ToastNotification]::new($x)\n"
        f"[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier('{_AUMID}').Show($toast)\n"
    )
    enc = base64.b64encode(script.encode("utf-16-le")).decode()
    try:
        subprocess.Popen(
            ["powershell", "-NoProfile", "-NonInteractive", "-EncodedCommand", enc],
            creationflags=_CNW,
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
    except Exception:
        pass


# Toast text in ru/en — same tier as the spectrogram verdicts (ru+en only, other
# languages fall back to en). Unlike console logs, an OS toast is shown on the
# owner's desktop, so the server picks the language itself from config rather
# than letting each client translate.
_TOAST_I18N = {
    "ru": {
        "dl_ok":       "✅ Загрузка готова",
        "dl_err":      "✗ Ошибка загрузки",
        "dl_tracks":   "{title} · {n} трек.",
        "rel_new":     "🎉 Новый релиз!",
        "rel_comp":    "🎉 Новый сборник!",
        "rel_body":    "{artist} — {release}",
        "rel_queued":  "{artist} — {release} · качаю",
    },
    "en": {
        "dl_ok":       "✅ Download complete",
        "dl_err":      "✗ Download failed",
        "dl_tracks":   "{title} · {n} tracks",
        "rel_new":     "🎉 New release!",
        "rel_comp":    "🎉 New compilation!",
        "rel_body":    "{artist} — {release}",
        "rel_queued":  "{artist} — {release} · downloading",
    },
}


def _tt(lang: str, key: str, **kw) -> str:
    table = _TOAST_I18N.get((lang or "en").split("-")[0].lower()) or _TOAST_I18N["en"]
    tmpl = table.get(key) or _TOAST_I18N["en"][key]
    return tmpl.format(**kw) if kw else tmpl


def toast_download_done(title: str, ok: bool, got=None, lang: str = "en") -> None:
    """Toast for a finished download. `ok` False → error toast. Plays the default
    Windows notification sound; auto-suppressed by Focus Assist over fullscreen games."""
    head = _tt(lang, "dl_ok" if ok else "dl_err")
    body = (_tt(lang, "dl_tracks", title=title, n=got) if (ok and got)
            else (title or "Ripster"))
    _ps_toast(head, body)


def toast_new_release(artist: str, release: str, compilation: bool = False,
                      queued: bool = False, lang: str = "en",
                      cover: str = "", year: str = "", label: str = "",
                      service: str = "") -> None:
    """Toast for a watchlist hit. This is the whole point of the watchlist when
    the window is closed: the in-app toast and the WS broadcast only reach a page
    that is currently open, so without this a release found at 4am is discovered
    whenever the user next happens to look.

    Обложка и подробности не украшение: по одному названию не понять, тот ли это
    релиз и откуда он — а уведомление часто единственное, что владелец увидит.
    """
    head = _tt(lang, "rel_comp" if compilation else "rel_new")
    body = _tt(lang, "rel_queued" if queued else "rel_body",
               artist=artist or "?", release=release or "")
    sub = " · ".join(x for x in (str(year or "")[:4], label, service) if x)
    _ps_toast(head, body, sub=sub, image=_cover_file(cover))
