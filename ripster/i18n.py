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
    # ── добавлено 19.09.2026: ключи, которые код логирует, а FALLBACK не знал —
    # в server-лог уходил сырой ключ (напр. console.bbc_bad_pid) ──
    "console.amd_config_written": "📄 config.toml записан в {dir} (кодек {codec})",
    "console.amd_not_installed": "✗ AppleMusicDecrypt не установлен. Открой Setup → Auto-install",
    "console.amd_pool_recovered": "✅ Публичный wrapper-manager снова отдаёт треки — снова участвует в маршрутизации",
    "console.amd_runner_missing": "✗ amd_runner.py не найден — переустанови AMD через Настройки",
    "console.amd_wm_checking": "🌐 Проверяю wrapper-manager: {instance}…",
    "console.amd_wm_down": "✗ Wrapper-manager недоступен: {err}",
    "console.amd_wm_not_ready": "⚠ Wrapper-manager «{instance}» не готов (клиентов: {clients}, регионов: {regions}) — продолжаю",
    "console.amd_wm_ready": "✓ Wrapper-manager готов — регионы: {regions}",
    "console.bbc_api_fail": "✗ BBC: API программ ответил {code} для {pid}",
    "console.bbc_bad_pid": "✗ BBC: не удалось разобрать идентификатор из ссылки: {url}",
    "console.bbc_ms_fail": "✗ BBC: MediaSelector ответил {code} для {vpid}",
    "console.bbc_no_hls": "✗ BBC: не нашёл HLS-поток для {vpid}",
    "console.bbc_no_versions": "✗ BBC: нет доступных версий для {pid}",
    "console.bbc_resolve_err": "✗ BBC: ошибка получения потока: {err}",
    "console.bento4_failed": "⚠ Авто-установка Bento4 не удалась: {err}",
    "console.bento4_installing": "📦 Bento4 не найден — ставлю автоматически…",
    "console.bento4_manual": "✗ Bento4 не установился — расшифровка ALAC/AAC невозможна. Открой Setup → «Bento4 (mp4decrypt)» и установи вручную (или проверь интернет и файрвол)",
    "console.bento4_ok": "✓ Bento4 установлен — продолжаю",
    "console.lucida_failed": "⚠ Авто-установка Lucida не удалась: {err}",
    "console.lucida_installing": "📦 SoundCloud: устанавливаю движок Lucida (один раз, 1–2 мин)…",
    "console.lucida_manual": "✗ Движок SoundCloud (Lucida) не установлен. Открой Setup → SoundCloud и установи вручную (нужны интернет и git)",
    "console.node_failed": "⚠ Авто-установка Node не удалась: {err}",
    "console.node_installing": "📦 SoundCloud: ставлю Node.js 20 (старый или отсутствующий Node ломает Lucida)…",
    "console.node_missing": "✗ Node.js не найден для SoundCloud — установка не удалась",
    "console.run_aborted": "⛔ Прогон прерван: {reason}",
    "console.sc_fb_miss": "  ✗ {title} — не найдено",
    "console.sc_fb_no_list": "🔁 SoundCloud: не удалось получить список треков для запасного пути",
    "console.sc_fb_queued": "🔁 SoundCloud, запасной путь: {n}/{total} поставлено в очередь",
    "console.sc_fb_search": "🔁 SoundCloud: ищу {n} трек(ов) на Deezer/Qobuz/Apple…",
    "console.sc_fb_try": "🔁 SoundCloud: пробую найти трек(и) на других сервисах…",
    # ── 23.09.2026: запасной путь Spotify («не отдаёт ЭТОЙ УЧЁТКЕ») ──────────
    # docs/SPOTIFY_UNAVAILABLE_2026-09-23.md, раздел D
    "console.sp_fb_fail": "⚠ Spotify: запасной путь не удался ({err})",
    "console.sp_fb_miss": "🔁 Spotify: {title} — на Qobuz/Tidal/Deezer не нашлось",
    "console.sp_fb_no_id": "🔁 Spotify: у релиза нет ISRC/UPC — подтвердить идентичность нечем, вслепую не ищу",
    "console.sp_fb_queued": "🔁 Запасной путь: {title} → {svc}",
    "console.sp_fb_ratelimited": "🔁 Spotify просит паузу (429) — запасной путь пропущен, чтобы не продлевать бан",
    "console.sp_fb_try": "🔁 Spotify не отдаёт этот релиз — ищу его же на Qobuz/Tidal/Deezer…",
    "console.sp_fb_unsupported": "🔁 Spotify: запасной путь есть для трека или альбома, не для плейлиста",
    # ── 24.09.2026: автоматический запасной путь при ПОСТОЯННОМ отказе ──────
    # Тот же механизм, что у Spotify и Apple-отказа по CKC, для Qobuz/Deezer/
    # Tidal/Yandex/Beatport: «не хватает прав», гео-лок и мёртвая сессия после
    # перебора учёток — свойства ВИТРИНЫ, а не музыки; берём ту же запись
    # (только ISRC/UPC) там, где её отдают.
    "console.pr_fb_try": "🔁 {svc} не отдаёт этот релиз — ищу его же на Qobuz/Tidal/Deezer…",
    "console.pr_fb_miss": "🔁 {svc}: {title} — на Qobuz/Tidal/Deezer не нашлось",
    "console.pr_fb_no_id": "🔁 {svc}: у релиза нет ISRC/UPC — подтвердить идентичность нечем, вслепую не ищу",
    "console.fb_resolved": "✅ {origin} отказала → скачано из {svc} ({quality})",
    # ── 24.09.2026: тот же запасной путь для Apple (отказ по CKC на релизе) ──
    # Сессия жива, все свои витрины отказали — значит отказ свойство релиза,
    # а не учётки; берём ту же запись (только ISRC/UPC) там, где ключ дают.
    "console.ap_fb_try":  "🔁 Apple не отдала ключ на этот релиз — ищу его же на Qobuz/Tidal/Deezer…",
    "console.ap_fb_miss": "🔁 Apple: {title} — на Qobuz/Tidal/Deezer не нашлось",
    "console.ap_fb_no_id":"🔁 Apple: у релиза нет ISRC/UPC — подтвердить идентичность нечем, вслепую не ищу",
    "console.wrapper_local_sf_retry": "⚠ Ключ не выдан: ссылка в витрине «{frm}», а свой аккаунт в «{to}». Пробую тот же релиз в своей витрине — публичный wrapper для этого не нужен.",
    "console.wrapper_other_slot": "⚠ Витрина «{frm}» ключ не дала — пробую свой аккаунт в «{to}» (слот {slot}). Публичный wrapper не нужен.",
    # ── transcode / disc organization ─────────────────────────────────────────
    "console.transcode_start":  "⏳ Конвертирую в {label}…",
    "console.transcode_done":   "✓ {label}: сконвертировано {n} файл(ов)",
    "console.discs_organized":  "🗂 Многодисковый релиз: разложено по папкам ({n} трек(ов))",
    "console.integrity_fixed":  "✓ Автопроверка: починено {n} файл(ов) (дефект ALAC-потока)",
    "console.integrity_corrupt": "⚠ Автопроверка: {n} файл(ов) не проходят decode-check, возможна повреждённая запись — проверь вручную",
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
    "console.low_disk":         "⚠ Мало места: {free} GB свободно (порог {floor} GB) — новые загрузки на паузе, продолжатся, когда место освободится",
    # ── partial / retries ─────────────────────────────────────────────────────
    "console.topup_missing":    "⚠ {got}/{expected} — добираю {miss} недостающих автоматически (попытка {attempt}/{max})…",
    "console.partial_permanent": "⚠ Готово ЧАСТИЧНО: {got}/{expected} — {miss} трек(ов) недоступны через этот движок (AAC без wrapper не расшифровывает часть треков). Скачай альбом как ALAC, чтобы добрать остаток через wrapper.",
    "console.partial_region":   "⚠ Готово ЧАСТИЧНО: {got}/{expected} треков — {miss} не догрузилось (недоступны в регионе или сбой постобработки). Повтори задачу — уже скачанное пропустится, доберутся только недостающие.",
    "console.partial_retry":    "⚠ Частично: {n_ok} скачано, {n_err} ошибок — повтор через 3 с…",
    "console.autoretry_partial": "⟳ Авто-повтор (частичная загрузка) — та же плитка",
    "console.error_retry":      "⚠ Ошибка: {msg} — повтор {n}/{max} через {delay}с…",
    "console.autoretry_n":      "⟳ Авто-повтор {n}/{max} — та же плитка",
    "console.salvaged_disk":    "⚠ Процесс прервался ({msg}), но на диске {n} файл(ов) — отдаю их",
    # ── восстановление битой BBC-live записи (24.09.2026) ───────────────────
    "console.bbc_live_recover_missing": "✗ Восстановление: повреждённый файл не найден на диске",
    "console.bbc_live_salvage_start":   "🧹 {name}: ищу в повреждённой записи честные куски…",
    "console.bbc_live_salvage_ok":      "✓ 320 спасено: {clean} мин чистых из {total} ({damaged} повреждено) → {out}",
    "console.bbc_live_salvage_fail":    "✗ 320 спасти не удалось: {reason}",
    "console.bbc_live_ondemand_queued": "⚠ Качаю копию «{title}» из BBC Sounds — это 96–102 кбит/с, запасная подпись, не замена эфира",
    "console.bbc_live_ondemand_done":   "✓ Копия BBC Sounds: {out} — измерено {kbps} кбит/с",
    "console.bbc_live_ondemand_fail":   "✗ Запасная копия BBC Sounds не получилась: {reason}",
    # ── wrapper / engine fallbacks ────────────────────────────────────────────
    "console.wrapper_local_drm_fail": "✗ Локальный wrapper не смог расшифровать (DRM/CKC). Публичный wrapper автоматически не подключается — перелогинь премиум-wrapper.",
    "console.wrapper_local_region_fail": "✗ Ключ на этот релиз не выдала ни одна своя Apple-учётка (у аккаунта нет прав в его регионе). Публичный wrapper автоматически не подключается — если он нужен, включи вручную: Настройки → Apple → Wrapper → «public». Либо возьми ссылку из своей витрины.",
    "console.drm_retry_amd":    "⚡ -1002: DRM — автоматически повторяю через AMD v2…",
    "console.amd_alac_fallback": "⚡ AMD: ALAC недоступен — автоматически пробую zhaarey {quality}…",
    "console.orpheus_retry":    "⟳ OrpheusDL: новые настройки — автоматический повтор…",
    # ── AMD engine (amd.py) ───────────────────────────────────────────────────
    "console.amd_segments":     "⬦ AMD: {n} сегм. [{elapsed}]",
    "console.amd_instance_hint": "  💡 Убедись что instance = wm.wol.moe",
    # ── лестница витрин Apple: ступень не вышла ───────────────────────────────
    "console.sf_rung_failed":   "─── витрина '{cc}' тоже не дала релиз → иду дальше по своим слотам ───",
    "console.slot_rung_failed": "─── слот {slot} ({cc}) не дал релиз ───",
    # 22.09.2026: «не дал релиз» и «не дал ключ» — РАЗНЫЕ вещи, а одна строка на
    # обе убедила владельца, что альбом не издан в его витрине, хотя каталог
    # Apple показывал его в четырёх. Разводим по-настоящему.
    "console.slot_rung_no_key": "─── слот {slot} ({cc}) релиз там есть, но ключ не выдан ───",
    "console.slot_rung_no_release": "─── слот {slot} ({cc}): в этой витрине релиз не издан (каталог Apple) ───",
    # Терминальный вердикт перебора: без утверждения про права региона (его
    # опровергает каталог) и без совета про публичный wrapper, который в этом
    # режиме выключен. `{mode}` — значение настройки apple-wrapper.
    "console.wrapper_own_accounts_fail": "✗ Ни одна своя Apple-учётка не дала ключ: {tried}. Публичный wrapper не участвует (режим «{mode}» — включается только вручную).",
    "console.wrapper_own_accounts_none": "✗ Перебирать своих Apple-учёток было некого — ключ не выдан. Публичный wrapper не участвует (режим «{mode}» — включается только вручную).",
    "console.wrapper_strict_no_ladder": "✗ Своя сессия жива, но ключа нет, а перебор других своих учёток запрещён строгим локальным режимом (Настройки → Apple → «только локально, без перебора»).",
    "console.rung_revived":     "─── попытка не вышла ({why}) → следующая ступень ───",
    # `{tried}` обязателен. Без него строка выглядит противоречием: 24.08.2026
    # релиз был издан в «br, ca, fr, …», аккаунты числились в «ca, gb» — и рядом
    # стояло «ни одна не подходит». Владелец решил, что сломан подбор. На деле ca
    # УЖЕ попробовали и получили Invalid CKC, поэтому её исключили; ответ был в
    # stdout (`excluded='ca'`) и до человека не доходил.
    "console.no_own_slot":      "─── ни одна своя витрина не подходит: релиз издан в {have}, мои аккаунты в {mine}, уже отказали {tried}. Публичный wrapper — только вручную (Настройки → Apple → Wrapper = «public») ───",
    # ── имена служб в обходе учёток (уходит в бот) ────────────────────────────
    "svc.apple":                "Apple · веб-токены",
    "svc.apple_wrapper":        "Apple · слоты враппера",
    "console.amd_no_instances": "⚠ Публичный wrapper-manager: сейчас нет ни одного живого инстанса в пуле (не наша поломка, сторонний сервис) — временно исключён из роутинга",
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
