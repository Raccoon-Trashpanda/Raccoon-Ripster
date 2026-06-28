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


def toast_download_done(title: str, ok: bool, got=None) -> None:
    """Toast for a finished download. `ok` False → error toast. Plays the default
    Windows notification sound; auto-suppressed by Focus Assist over fullscreen games."""
    head = "✅ Загрузка готова" if ok else "✗ Ошибка загрузки"
    body = (f"{title} · {got} трек." if (ok and got) else (title or "Ripster"))
    _ps_toast(head, body)
