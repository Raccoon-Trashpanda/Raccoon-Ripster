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


def _ps_toast(title: str, body: str) -> None:
    if os.name != "nt":
        return
    script = (
        "$ErrorActionPreference='Stop'\n"
        "[void][Windows.UI.Notifications.ToastNotificationManager,"
        "Windows.UI.Notifications,ContentType=WindowsRuntime]\n"
        "$x=[Windows.UI.Notifications.ToastNotificationManager]::GetTemplateContent("
        "[Windows.UI.Notifications.ToastTemplateType]::ToastText02)\n"
        "$t=$x.GetElementsByTagName('text')\n"
        f"$t.Item(0).AppendChild($x.CreateTextNode('{_psq(title)}'))>$null\n"
        f"$t.Item(1).AppendChild($x.CreateTextNode('{_psq(body)}'))>$null\n"
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
                      queued: bool = False, lang: str = "en") -> None:
    """Toast for a watchlist hit. This is the whole point of the watchlist when
    the window is closed: the in-app toast and the WS broadcast only reach a page
    that is currently open, so without this a release found at 4am is discovered
    whenever the user next happens to look."""
    head = _tt(lang, "rel_comp" if compilation else "rel_new")
    body = _tt(lang, "rel_queued" if queued else "rel_body",
               artist=artist or "?", release=release or "")
    _ps_toast(head, body)
