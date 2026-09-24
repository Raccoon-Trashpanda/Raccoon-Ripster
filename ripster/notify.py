"""Native Windows desktop notification on download completion.

Fires a REAL Windows toast (WinRT via PowerShell -EncodedCommand) so the user
sees "download finished" even when Ripster is minimized / in the tray. No extra
Python dependency (ships in the code overlay, not the bundled interpreter).

Why native (not the in-app toast): only an OS toast shows on the desktop while the
window is hidden — AND Windows Focus Assist auto-suppresses toasts while a game or
other app is fullscreen, so we get "don't interrupt games" for free.

Best-effort: silent no-op on non-Windows or any failure. Gated by the
`notify-on-done` config flag (off by default).

Спам-защита (24.09.2026, жалоба на повторяющийся тост про Beatport): все тосты
идут через этот модуль, поэтому здесь же единый журнал («каждый тост — строка в
лог»), одноразовость («каждый релиз — не чаще одного раза за всё время, отдельно
для предзаказа и для дня выхода») и общий ограничитель («не больше 3 за 10 минут,
дальше — одно итоговое уведомление»). Тост — только знакомство с находкой:
пропущенный не теряет релиз, он остаётся в вотчлисте и в очереди.
"""
from __future__ import annotations

import base64
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

# Консоль владельца — cp1251: заглушаем непереводимое (эмодзи тостов), сам
# текст не подменяем. Иначе print() падает UnicodeEncodeError уже ПОСЛЕ того,
# как уведомление ушло.
for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(errors="replace")
    except Exception:
        pass

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
        # Обложка квадратом СЛЕВА, текст справа. Владелец просил прижать её
        # вплотную к краям окна, углами в углы — родные уведомления так не
        # умеют: макет рисует система, отступ и размер значка задаёт она.
        # Остальные два места под картинку не подходят по смыслу: «шапка»
        # (hero) — широкая полоса сверху, ради неё квадратную обложку пришлось
        # бы резать, теряя верх и низ рисунка; картинка без placement уходит
        # ПОД текст во всю ширину и растягивает окно в высоту.
        # У hint-crop допустимы только "none" и "circle" — с посторонним
        # значением Windows молча выбрасывает картинку целиком, именно поэтому
        # уведомление приходило голым текстом.
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
        "rel_pre":     "🕝 Предзаказ!",
        "rel_body":    "{artist} — {release}",
        "rel_queued":  "{artist} — {release} · качаю",
        "digest":      "🔕 Ещё релизов за 10 мин: {n}. Подробности — в приложении.",
    },
    "en": {
        "dl_ok":       "✅ Download complete",
        "dl_err":      "✗ Download failed",
        "dl_tracks":   "{title} · {n} tracks",
        "rel_new":     "🎉 New release!",
        "rel_comp":    "🎉 New compilation!",
        "rel_pre":     "🕝 Pre-order!",
        "rel_body":    "{artist} — {release}",
        "rel_queued":  "{artist} — {release} · downloading",
        "digest":      "🔕 {n} more releases in 10 min — see the app.",
    },
}


def _tt(lang: str, key: str, **kw) -> str:
    table = _TOAST_I18N.get((lang or "en").split("-")[0].lower()) or _TOAST_I18N["en"]
    tmpl = table.get(key) or _TOAST_I18N["en"][key]
    return tmpl.format(**kw) if kw else tmpl


# ── Журнал тостов + одноразовость + ограничитель ────────────────────────────────
# Три вещи, которых не хватало 24.09, когда тост про Beatport вылез несколько раз
# подряд:
#   1. каждый тост пишется в лог (`[toast] kind=… service=… title=…`), чтобы
#      повторяющуюся рассылку можно было увидеть и опознать источник;
#   2. релиз уведомляет НЕ БОЛЕЕ ОДНОГО РАЗА за всё время — ключ живёт в файле
#      рядом с остальными sidecar-состояниями и переживает перезапуск; предзаказ
#      имеет два своих «раза»: один как предзаказ, один в день выхода;
#   3. не больше 3 тостов за 10 минут, дальше — одно итоговое уведомление вместо
#     individual-спама.
_TOAST_WINDOW   = 600.0     # 10 минут
_TOAST_MAX      = 3         # сколько тостов пропускаем в окне
_LEDGER_KEEP_D  = 400       # дней помнить ключ релиза (~14 месяцев)
_LEDGER_MAX     = 4000      # потолок записей, чтобы файл не распухал

_BASE_DIR: "str | None" = None
_ledger: "dict | None" = None
#: Включён ли гейт. По умолчанию выключен: модуль обязан оставаться чистым
#: best-effort no-op, пока его явно не настроили (`configure`) — иначе тесты
#: контракта и любой вызов без инициализации упрели бы в накопленный журнал.
#: Приложение настраивает гейт при install() вотчлиста.
_ENABLED = False


def configure(base_dir=None) -> None:
    """Куда класть журнал тостов и включить одноразовость/ограничитель. Зовёт
    вотчлист при install(); без вызова тосты проходят как раньше — one-shot."""
    global _BASE_DIR, _ledger, _ENABLED
    new = str(base_dir) if base_dir else None
    if new != _BASE_DIR:
        _BASE_DIR, _ledger = new, None
    _ENABLED = True


def configure_off() -> None:
    """Явно выключить гейт (тесты/отладка): тосты снова идут без журнала."""
    global _ENABLED, _ledger
    _ENABLED, _ledger = False, None


def _ledger_path() -> Path:
    if _BASE_DIR:
        return Path(_BASE_DIR) / "toast_ledger.json"
    return Path(tempfile.gettempdir()) / "ripster_toast_ledger.json"


def _load_ledger() -> dict:
    global _ledger
    if _ledger is not None:
        return _ledger
    try:
        p = _ledger_path()
        _ledger = json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}
    except Exception:
        _ledger = {}
    if not isinstance(_ledger, dict):
        _ledger = {}
    _ledger.setdefault("shown", {})      # "<ключ>#<стадия>" -> epoch
    _ledger.setdefault("recent", [])     # [epoch, …] всех показанных тостов
    return _ledger


def _save_ledger() -> None:
    try:
        _ledger_path().write_text(
            json.dumps(_load_ledger(), ensure_ascii=False), encoding="utf-8")
    except Exception as e:
        print(f"[toast] ledger save failed: {e}", flush=True)


def _log(kind: str, title: str, service: str = "", note: str = "") -> None:
    msg = f"[toast] kind={kind} service={service or '-'} title={title}"
    if note:
        msg += f" ({note})"
    print(msg, flush=True)


def _prune(now: float) -> dict:
    L = _load_ledger()
    L["recent"] = [t for t in L["recent"] if now - t < _TOAST_WINDOW][-50:]
    shown = L["shown"]
    if len(shown) > _LEDGER_MAX:
        cutoff = now - _LEDGER_KEEP_D * 86400
        for k in [k for k, v in shown.items() if not isinstance(v, (int, float)) or v < cutoff]:
            del shown[k]
    return L


def _in_window(L: dict, now: float) -> int:
    return sum(1 for t in L["recent"] if now - t < _TOAST_WINDOW)


def _show_and_count(L: dict, key: str, now: float) -> None:
    """Отметить тост показанным: он тратит разрешение окна, а релизный ключ —
    ещё и закрывает дорогу этому релизу навсегда."""
    L["recent"].append(now)
    if key:
        L["shown"][key] = now


def toast_download_done(title: str, ok: bool, got=None, lang: str = "en") -> None:
    """Toast for a finished download. `ok` False → error toast. Plays the default
    Windows notification sound; auto-suppressed by Focus Assist over fullscreen games.

    Ограничитель общий с релизными тостами (окно 10 минут), но без одноразовости:
    каждая завершённая загрузка — законное новое событие."""
    _log("download", title, note="ok" if ok else "error")
    if not _ENABLED:
        head = _tt(lang, "dl_ok" if ok else "dl_err")
        body = (_tt(lang, "dl_tracks", title=title, n=got) if (ok and got)
                else (title or "Ripster"))
        _ps_toast(head, body)
        return
    now = time.time()
    L = _prune(now)
    if _in_window(L, now) >= _TOAST_MAX:
        _show_and_count(L, "", now)     # тратим разрешение, но не плодим process
        _save_ledger()
        return
    head = _tt(lang, "dl_ok" if ok else "dl_err")
    body = (_tt(lang, "dl_tracks", title=title, n=got) if (ok and got)
            else (title or "Ripster"))
    _show_and_count(L, "", now)
    _save_ledger()
    _ps_toast(head, body)


def toast_new_release(artist: str, release: str, compilation: bool = False,
                      queued: bool = False, lang: str = "en",
                      cover: str = "", year: str = "", label: str = "",
                      service: str = "", dedupe_key: str = "",
                      preorder: bool = False) -> None:
    """Toast for a watchlist hit. This is the whole point of the watchlist when
    the window is closed: the in-app toast and the WS broadcast only reach a page
    that is currently open, so without this a release found at 4am is discovered
    whenever the user next happens to look.

    Обложка и подробности не украшение: по одному названию не понять, тот ли это
    релиз и откуда он — а уведомление часто единственное, что владелец увидит.

    `dedupe_key` — устойчивая личность релиза (сервис+id либо артист+название),
    `preorder` — что это ещё не вышедший анонс. По ним решаем, показывать ли:
    один релиз звенит один раз (предзаказ — ещё раз в день выхода), а когда за
    10 минут их набилось больше трёх, остальные сворачиваются в одно итоговое."""
    _log("release", f"{artist or '?'} — {release or ''}",
         service=service or (label or "-"))
    if not _ENABLED:
        head = _tt(lang, "rel_comp" if compilation else "rel_new")
        body = _tt(lang, "rel_queued" if queued else "rel_body",
                   artist=artist or "?", release=release or "")
        sub = " · ".join(x for x in (str(year or "")[:4], label, service) if x)
        _ps_toast(head, body, sub=sub, image=_cover_file(cover))
        return
    if not dedupe_key:
        # Без ключа нечем склеить повторы — не глушим насовсем, но и не даём
        # разрастись журналу: просто проходим через общий ограничитель окна.
        key = ""
    else:
        key = f"{dedupe_key}#{'preorder' if preorder else 'released'}"
    now = time.time()
    L = _prune(now)
    if key and key in L["shown"]:
        _log("skip", f"{artist or '?'} — {release or ''}",
             service=service or "-", note="already toasted")
        return
    if _in_window(L, now) >= _TOAST_MAX:
        if key:
            L["shown"][key] = now       # съеден итогом — больше не повторит
        L["suppressed"] = int(L.get("suppressed") or 0) + 1
        if now - float(L.get("last_digest") or 0) >= _TOAST_WINDOW:
            n = L["suppressed"]
            L["suppressed"] = 0
            L["last_digest"] = now
            _log("digest", f"{n} suppressed releases", service=service or "-")
            _show_and_count(L, "", now)
            _save_ledger()
            _ps_toast(_tt(lang, "rel_new"), _tt(lang, "digest", n=n))
        else:
            _save_ledger()
        return
    head = _tt(lang, "rel_pre" if preorder
               else ("rel_comp" if compilation else "rel_new"))
    body = _tt(lang, "rel_queued" if queued else "rel_body",
               artist=artist or "?", release=release or "")
    sub = " · ".join(x for x in (str(year or "")[:4], label, service) if x)
    image = _cover_file(cover) if os.name == "nt" else ""
    _show_and_count(L, key, now)
    _save_ledger()
    _ps_toast(head, body, sub=sub, image=image)
