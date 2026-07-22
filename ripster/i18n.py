"""Server-side i18n for broadcast/console messages.

The server can't know each connected client's UI language at broadcast time (the
console is watched per-browser, every client with its own `S.lang`). So instead
of translating server-side, a localizable log carries a KEY + params and each
client translates it via `static/js/i18n.js` (the 5-language dictionary). This
module owns the KEY registry and a Russian fallback text for the `msg` field, so
a client that hasn't loaded the new i18n.js (or any non-UI consumer) still shows
readable text.

Usage in engines/runner:
    from ripster import i18n
    await _broadcast(i18n.log_event("console.transcode_start", level="info",
                                    task_id=tid, label=_tc_label))

The 5-language translations for these keys live in static/js/i18n.js under the
SAME key names. Keep the two in sync: add a key here (with its RU fallback) AND
in i18n.js (ru/en/hi/ja/zh).
"""
from __future__ import annotations

# key -> Russian fallback text (the historical hardcoded string). {named} params
# are filled via str.format(**params). The real per-language strings are in
# static/js/i18n.js; this is only the backward-compat fallback for `msg`.
#
# Keep this dict in 1:1 sync with the `console.*` keys in static/js/i18n.js
# (ru/en/hi/ja/zh). The RU text here is the canonical source for translations.
FALLBACK: dict[str, str] = {
    # ── transcode / disc organization ─────────────────────────────────────────
    "console.transcode_start":  "⏳ Конвертирую в {label}…",
    "console.transcode_done":   "✓ {label}: сконвертировано {n} файл(ов)",
    "console.discs_organized":  "🗂 Многодисковый релиз: разложено по папкам ({n} трек(ов))",
    # ── auto-mix (DJ Coder) ───────────────────────────────────────────────────
    "console.automix_skipped_lossy": "🎚 Авто-mix пропущен: источник lossy. Для бесшовного DJ-микса качай в ALAC/FLAC.",
    "console.automix_start":         "🎚 Авто-mix: склеиваю «{name}»…",
    "console.automix_done":          "✓ Авто-mix: {names} + .cue → {out_dir}",
    "console.automix_done_discs":    "✓ Авто-mix ({discs} диск(ов)): {names} + .cue → {out_dir}",
    "console.automix_error":         "Авто-mix ошибка: {err}",
    # ── post-process retag ────────────────────────────────────────────────────
    "console.mix_tags_fixed":   "🍎 Исправлены теги микса по каталогу: {n} трек(ов)",
    "console.tags_fixed_isrc":  "♻ Теги исправлены по ISRC: {n} файл(ов)",
    # ── engine lifecycle ──────────────────────────────────────────────────────
    "console.cmd_start":        "▶ {cmd}…",
    "console.timeout":          "✗ Таймаут: процесс завис и был завершён",
    "console.done_tracks":      "✓ Done ({n} tracks)",
    "console.queue_finished":   "✓ Queue finished!",
    "console.reused_existing":  "⏭ Уже скачано ранее ({n} треков) — файлы переиспользованы, повторная загрузка пропущена",
    # ── partial / retries ─────────────────────────────────────────────────────
    "console.topup_missing":    "⚠ {got}/{expected} — добираю {miss} недостающих автоматически (попытка {attempt}/{max})…",
    "console.partial_permanent": "⚠ Готово ЧАСТИЧНО: {got}/{expected} — {miss} трек(ов) недоступны через этот движок (AAC без wrapper не расшифровывает часть треков). Скачай альбом как ALAC, чтобы добрать остаток через wrapper.",
    "console.partial_region":   "⚠ Готово ЧАСТИЧНО: {got}/{expected} треков — {miss} не догрузилось (недоступны в регионе или сбой постобработки). Повтори задачу — уже скачанное пропустится, доберутся только недостающие.",
    "console.partial_retry":    "⚠ Частично: {n_ok} скачано, {n_err} ошибок — повтор через 3 с…",
    "console.autoretry_partial": "⟳ Авто-повтор (частичная загрузка) — та же плитка",
    "console.error_retry":      "⚠ Ошибка: {msg} — повтор {n}/{max} через {delay}с…",
    "console.autoretry_n":      "⟳ Авто-повтор {n}/{max} — та же плитка",
    "console.salvaged_disk":    "⚠ Процесс прервался ({msg}), но на диске {n} файл(ов) — отдаю их",
    # ── wrapper / engine fallbacks ────────────────────────────────────────────
    "console.wrapper_local_drm_fail": "✗ Локальный wrapper не смог расшифровать (DRM/CKC). Публичный wrapper отключён (режим «local») — перелогинь премиум-wrapper или временно выбери «auto».",
    "console.drm_retry_amd":    "⚡ -1002: DRM — автоматически повторяю через AMD v2…",
    "console.amd_alac_fallback": "⚡ AMD: ALAC недоступен — автоматически пробую zhaarey {quality}…",
    "console.orpheus_retry":    "⟳ OrpheusDL: новые настройки — автоматический повтор…",
    # ── AMD engine (amd.py) ───────────────────────────────────────────────────
    "console.amd_segments":     "⬦ AMD: {n} сегм. [{elapsed}]",
    "console.amd_instance_hint": "  💡 Убедись что instance = wm.wol.moe",
}


def tr(key: str, lang: str = "ru", **params) -> str:
    """Server-side translate to the RU fallback (only RU is kept here; full
    per-language strings live in i18n.js). Safe on missing key/params."""
    s = FALLBACK.get(key, key)
    try:
        return s.format(**params) if params else s
    except Exception:
        return s


def log_event(key: str, level: str = "info", task_id=None, **params) -> dict:
    """Build a localizable `log` broadcast: clients translate `msg_key` (with
    `params`) via i18n.js; `msg` is the RU fallback for non-i18n consumers."""
    d = {
        "type": "log",
        "msg_key": key,
        "params": params,
        "msg": tr(key, "ru", **params),
        "level": level,
    }
    if task_id is not None:
        d["task_id"] = task_id
    return d
