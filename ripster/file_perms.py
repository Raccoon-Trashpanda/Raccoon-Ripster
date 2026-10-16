"""Ограничение прав на файлы (Windows).

Общая функция для всех модулей (ripster/ и tgbot/). Убирает наследование ACL
и оставляет права только для текущего пользователя и администраторов.

01.10.2026: задача 088 — применяем ко всем секретным файлам.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path


def restrict_file_perms(path: Path) -> None:
    """Убирает наследование и оставляет права только для текущего пользователя
    и администраторов. Windows-only; на других ОС ничего не делает.

    Вызывается ПОСЛЕ записи файла. Не падает при ошибке (логирует в debug)."""
    if sys.platform != "win32":
        return
    try:
        import win32security
        import ntsecuritycon as con
        user, domain, _ = win32security.LookupAccountName("", os.getlogin())
        admins = win32security.CreateWellKnownSid(win32security.WinBuiltinAdministratorsSid)
        system = win32security.CreateWellKnownSid(win32security.WinLocalSystemSid)
        sd = win32security.GetFileSecurity(str(path), win32security.DACL_SECURITY_INFORMATION)
        dacl = sd.GetSecurityDescriptorDacl()
        if dacl is None:
            dacl = win32security.ACL()
        dacl.SetEntriesInAcl([
            {"AccessMode": con.SET_ACCESS_MODE, "AccessPermissions": con.FILE_ALL_ACCESS,
             "Inheritance": con.NO_INHERITANCE, "Trustee": {"TrusteeType": con.TRUSTEE_IS_USER,
                                                              "TrusteeForm": con.TRUSTEE_IS_SID,
                                                              "Identifier": user}},
            {"AccessMode": con.SET_ACCESS_MODE, "AccessPermissions": con.FILE_ALL_ACCESS,
             "Inheritance": con.NO_INHERITANCE, "Trustee": {"TrusteeType": con.TRUSTEE_IS_GROUP,
                                                              "TrusteeForm": con.TRUSTEE_IS_SID,
                                                              "Identifier": admins}},
            {"AccessMode": con.SET_ACCESS_MODE, "AccessPermissions": con.FILE_ALL_ACCESS,
             "Inheritance": con.NO_INHERITANCE, "Trustee": {"TrusteeType": con.TRUSTEE_IS_GROUP,
                                                              "TrusteeForm": con.TRUSTEE_IS_SID,
                                                              "Identifier": system}},
        ])
        sd.SetSecurityDescriptorDacl(1, dacl, 0)
        win32security.SetFileSecurity(str(path), win32security.DACL_SECURITY_INFORMATION, sd)
    except ImportError:
        _restrict_via_icacls(path)
    except Exception:
        pass


def _restrict_via_icacls(path: Path) -> None:
    """Fallback через icacls, если pywin32 недоступен."""
    import subprocess
    try:
        user = os.getlogin()
        subprocess.run(
            ["icacls", str(path), "/inheritance:r", "/grant:r", f"{user}:(F)",
             "/grant", "SYSTEM:(F)", "/grant", "Administrators:(F)"],
            capture_output=True, timeout=5, check=False
        )
    except Exception:
        pass
