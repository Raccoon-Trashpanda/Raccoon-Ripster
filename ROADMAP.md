# Ripster → GitHub: staging & roadmap

Цель: собрать **публичный дистрибутив** (как deemix) в этой папке. Сюда
**последовательно** складывается только то, что протестировано и работает
безоговорочно. Порядок: для каждого модуля/движка — `тест → рефактор → закрыть
→ скопировать вычищенную версию сюда`.

## Что входит в публичную версию (вкладки)
Очередь · Поиск · История · BBC · SoundCloud (SC) · Кодер · Спектр · Настройки ·
Setup · Консоль.

## Что НЕ улетает на GitHub
- Telegram-бот (`tgbot/`, `bot.py`) — приватный аддон.
- Гостевой режим (`guest_manager.py`, `routes/guest.py`, guest-моды UI).
- Вкладки **Библиотека**, **Теггер**, **Статистика** (по решению владельца).
- Боевые токены/сессии/cookies, приватные конфиги.

## Конвейер (статус)
Легенда: ☐ не начато · ◐ тесты есть · ✅ протестировано+отрефакторено+в github_setup

### Ядро
- ✅ **M1 core URL/config** — `resolver.py`, `service_config.py`, `service_layer.py`
  (144 теста; дедуп 3 парсеров → `_parse_path_service`; починен qobuz-slug URL).

### Движки (test → refactor → close)
- ✅ **apple** (`apple_router.py`, `amd.py`, `engines/amd.py`, `engines/zhaarey.py`,
  `engines/base.py`) — 28 тестов (URL-хелперы + AMD/zhaarey `is_finished`/parsers).
  Рефактор не потребовался: мёртвого кода нет, `_strip_ansi` шарится из base.
- ✅ **tidal** (`engines/tidal.py`) — 22 теста (URL-норм, cover, qualities,
  classify/parse/`is_finished` с анти-фантом-успехом). Рефактор не потребовался;
  `_update_orpheus_settings` отложен (3 разные сигнатуры — оценить на beatport).
- ✅ **qobuz** (`engines/qobuz.py`, `metadata/qobuz.py`, `engines/streamrip_utils.py`)
  — 12 тестов (qualities, `is_finished` ветки + общие streamrip-парсеры). Чисто,
  classify/parse уже делегируют в streamrip_utils. **app-id `798273057` дедуп
  перенесён в финал** — он в 11 местах (вкл. вне-scope tagger_routes), не piecemeal.
- ✅ **deezer** (`engines/deezer.py`) — 15 тестов (qualities, classify/parse,
  `is_finished`: success, «All done но 0 треков»=провал, bitrate-gate, bad-ARL,
  unexpected-end). Чисто, dead-code нет.
- ✅ **spotify** (`engines/orpheus_spotify.py`) — 16 тестов (qualities, classify/parse,
  `is_finished`: not-authed, new-settings, librespot-fail, 401, partial-success,
  skip, premium). ⚠️ ФЛАГ: `delete_creds()` не вызывается — `/api/spotify/logout`
  чистит только metadata-токен, librespot-блоб остаётся. Возможно намеренно (блоб
  само-хилит OGG 401). Решение/wiring — на фазе Настроек, с подтверждением владельца.
- ✅ **soundcloud** (вся вкладка SC) — `engines/soundcloud.py` (18), `engines/sc_widevine.py`
  (18), `routes/soundcloud.py` (15) = 51 тест. Движки: qualities/classify/parse/
  `is_finished` (DRM-FairPlay, no-wvd/revoked). Роут: `_artwork`, `_split_artist_title`,
  `_parse_sc_tracklist`, **`_sc_host_ok` (SSRF allow-list, вкл. suffix-spoof)**,
  `_norm_track`. ⚠️ ФЛАГ: `node_available()` (sc engine) не подключён → статус-хелпер
  Node.js для Setup. Чисто, dead-code нет.
- ✅ **beatport** (`engines/orpheus_beatport.py`, `routes/beatport.py`) — 20 тестов
  (движок qualities/classify/parse/`is_finished` с анти-фолс-сабскрипшн; роут
  `_auth_headers`/`_preview_url`/`_dict_name`/`_fmt_track`). Чисто.
  **Вердикт по `_update_orpheus_settings`×3: НЕ дедуплицировать** — общий скелет
  мал, расхождения большие (spotify ~80 строк уникальны), экстракция = индирекция
  + поведенческий риск без выгоды.
- ✅ **amazon / yandex** (`engines/amazon.py`, `engines/yandex.py`) — 23 теста
  (qualities/classify/parse/`is_finished`, `_ym_cover`). Чисто.
- ✅ **варианты** (`engines/gamdl.py`, `spotiflac.py`, `zotify.py`, `registry.py`,
  `streamrip_utils.py`) — 34 теста. zotify bad-creds монипатчен (зовёт `_delete_creds`).

### Маршруты вкладок (только in-scope)
- ✅ queue (`routes/queue.py`) — 3 теста на `_make_task` (контракт формы). Хендлеры —
  тонкая async-оркестрация над инъектами (runner/resolver — покрыты отдельно). Чисто.
  ОСТАЁТСЯ `runner.py`/`process_runner.py` (крупные, отдельным заходом).
- ✅ search (`routes/discovery.py`) — 5 тестов (cover-хелперы `_ym_cover`/`_tidal_cover`/
  `_sp_cover`). ⚠️ `_ym_cover`/`_tidal_cover` дублируют движковые — в финальный
  cross-cutting дедуп (вместе с app-id `798273057`).
- ◐ history (`routes/history.py`) — тонкие async-хендлеры (api_history/clear/delete),
  чистой логики нет; покрыто app-builds + потоками данных в других тестах. Снапшот сделан.
- ✅ bbc (`routes/bbc.py`) — 11 тестов (`_img`, `_parse_timecodes`, `_parse_dur`,
  `_safe`, `_match_toks/_match_nums`, **`_score_mixesdb_hit` эпизод-гейтинг**, `_sec_to_ts`,
  `_build_cue`). Чисто.
- ✅ coder (`routes/ripster_coder.py` + `mixcue.py`) — 4 теста (`_parse_folder_name`,
  `mixcue._fmt_name`); mixcue clean/cue/sanitize уже в test_helpers. Чисто.
- ✅ spectrum (`routes/spectrogram.py`) — 9 тестов (`_format_duration`,
  **`_verdict` lossless/lossy/suspicious** по кодеку+расширению). Чисто.
- ✅ settings-ядро (`config_service.py`) — 15 тестов (`ConfigService` типизир. аксессоры
  `_s/_i/_b` + коэрция int/bool, dict-протокол, **атомарная запись/загрузка YAML**). Чисто.
- ◐ setup (`ripster/setup/`) — 3 теста на `_gamdl_flag` (guard неизвестных флагов).
  Остальное — авто-докачка (network/subprocess), орк-эндпоинты; это ядро дистрибутива
  (см. DEPENDENCIES.md). Снапшот `setup/__init__.py` сделан.
- ☐ console (тонкий лог-стример; чистой логики нет)

### Финальная сборка  → подробности в DEPENDENCIES.md + SESSION_LOG_FULL.md
- ☐ **«из коробки» + авто-докачка тяжёлого** — Ripster тянет сам (`ripster/setup/`
  уже есть); 2 режима: GitHub-инсталлятор / portable-архив с докачкой в Setup.
- ✅ **Лаунчер из исходников** — `ripster/launcher.py` + корневой `ripster_launcher.py`,
  **7 тестов** (`config_port`/`server_url`/`bootstrap_python` + **webview-vs-browser** выбор).
  **pywebview 6.2.1 установлен и закреплён в `requirements.txt`** → реальное нативное окно
  (Edge WebView2; фолбэк в браузер если нет). Поднимает bootstrap-цепочку → ждёт сервер →
  окно → закрытие гасит сервер; first-run подсказка по `check_tools`. Проверено вживую:
  `server_alive` против живого сервера = True. Запуск: `.venv\Scripts\pythonw.exe ripster_launcher.py`.
  ОСТАЁТСЯ (deploy-полиш, не блокеры): вывести старый PyQt `launcher.py`/35МБ-.exe; ярлык автозапуска.
- ☐ **`.wvd`/токены** — не бандлить/не авто-качать CDM (legal); управляемый Setup-флоу
  (`sc_upload_wvd`/`sc_wvd_status` уже есть) + гайд по извлечению своего L3 CDM.
- ☐ first-run Setup wizard (`check_tools` + `run_full_setup`); недостающие
  авто-установщики (ffmpeg/amz/N_m3u8DL-RE); кросс-платформа или «Windows-only».
- ☐ `.gitignore` дистрибутива: бинари, `orpheus/modules/*`, `.wvd`, токены, node_modules.
- ☐ **унификация путей** `<save-path>/<service>/<quality>` + **аудит/вырезание настроек** —
  полный план и категоризация в **SETTINGS_AUDIT.md** (атомарный path-рефактор:
  service_config + 10 движков + UI; cut-list owner-настроек).
- ☐ вырезать бот/гостей/Библиотеку/Теггер/Статистику из UI и роутера.
- ☐ pinned `requirements.txt`, `config.example.yaml` без секретов, README+лицензия.
- ☐ глобальный дедуп app-id `798273057` (11 мест) одним проходом.
- ☐ дымовой прогон чистого клона (clone → pip install → запуск → Setup тянет тяжёлое → загрузка).

### 🎯 ФИНАЛЬНЫЙ ПУНКТ — Bandcamp-движок как БОЕВОЙ ТЕСТ апдейтера (директива владельца)
Идея: внедрить НОВЫЙ движок **Bandcamp** и выкатить его **через апдейтер** — это
end-to-end проверка всей цепочки: новый движок авто-discovery (`pkgutil.iter_modules`
+ `@register` → `REGISTRY`, см. `updater.py` docstring) → `git pull` →
`verify_import_smoke` → рестарт → движок САМ появляется в работе, без правок проводки.
Спецификация движка (как любой другой, но проще):
- **Без аккаунтов / без auth** — публичный доступ.
- **API Bandcamp:** стрим + скачивание, качество **MP3 128 kbps** (free-тир).
- **Дискография артиста** — листинг альбомов/релизов артиста (для Поиска/карточек).
- **Теги и обложки — полностью**, как у остальных движков (артист/альбом/трек/№/cover).
- Реализация: `ripster/engines/bandcamp.py` (`@register`, `qualities`=[mp3-128],
  `build_cmd`/downloader, `is_finished`, classify/parse) + поддержка bandcamp-URL в
  `resolver.py` + артист-листинг в `discovery.py`. Источник стрим-URL — встроенный
  JSON страницы Bandcamp (`data-tralbum`/`TralbumData`, mp3-128) — без ключей.
- **Приёмка:** после апдейта Bandcamp виден в `/api/qualities`, качает трек/альбом
  с тегами; `verify_import_smoke` зелёный; ничего, кроме нового файла + проводки в
  resolver/discovery, не менялось. Покрыть тестами как прочие движки (qualities/
  classify/parse/`is_finished`).

### 🔮 ОТЛОЖЕНО (будущий апдейт) — Apple-станции ra.* (директива владельца 2026-06-20)
НЕ закрыто, отложено. **Сделано сейчас (промежуточно):** честный отказ `ra.*`/
`/station/` — `/api/queue/add` отдаёт 422 с понятным текстом (блокирует бот/веб/
гостя до постановки в очередь), `fetch_meta`/`_parse_apple_url` тоже режут, `/api/meta`
теперь 422 вместо 500. Тесты `tests/test_apple_meta.py`, снапшоты в github_setup.
**Исследование (факты по Apple API):** станция = фикс. **DJ-микс-эпизод** (isLive=false,
~58 мин, `kind=radioStation`, `hasDrm=true`, `supportedDrms=fairplay/playready/widevine`,
`stationHash`, `streamingKind=1`), `relationships=[]` → треклиста через API нет.
Концептуально скачиваемо (один файл), НО другой стриминг-API + FairPlay-radio (не
песенный CKC). **Будущий спайк (1-2ч):** минтит ли локальный wrapper ключ для
radioStation-потока? Если ДА → отдельный движок (station-stream по stationHash +
FairPlay + один ~58мин файл с тегами из station-меты) и **выкат ЧЕРЕЗ апдейтер**
(как Bandcamp — боевой тест авто-discovery). Если НЕТ → отказ остаётся навсегда.

### 🌍 ПОСЛЕДНИЙ ПУНКТ ПЕРЕД УПАКОВКОЙ ИНСТАЛЛЯТОРА — полная локализация (директива владельца 2026-06-20)
Перевести **ВСЁ в проекте** на все поддерживаемые языки (сейчас ru/en/hi/ja/zh —
см. i18n.js). Сейчас в UI **много хардкод-строк** (русский текст прямо в HTML/JS,
мимо системы `data-i18n`/`i18n.js`), особенно в `static/views/*.html` (settings,
карточки, статусы) и в сообщениях движков/ошибок (`ripster/engines/*`, runner —
например свежие: CKC-сообщение zhaarey, разводка куки/wrapper, отказ Apple-станций).
План: (1) инвентаризировать хардкод-строки (grep кириллицы в static/ + Python
user-facing); (2) вынести в `i18n.js` ключи + `data-i18n`/`t()`; (3) перевести на
все языки; (4) серверные сообщения — отдавать ключ/локализовать по языку клиента
(у бота уже есть per-recipient перевод — переиспользовать подход). Делать ПОСЛЕ
функционала, ПЕРЕД упаковкой — чтобы не переводить то, что ещё меняется.

## Правила
- Файлы здесь — снапшоты вычищенных модулей; на финале пересобираются из исходников.
- Тест-сеть проекта: `cd /c/dev/apple_music && .venv/Scripts/python.exe -m pytest -q`.
- Ничего не коммитим/пушим без явной просьбы владельца.
