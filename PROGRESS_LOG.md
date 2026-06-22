# Ripster — лог прогресса (тест → рефактор → GitHub-сборка)

Живой журнал работ. Дополняется по ходу. Парный файл — `ROADMAP.md` (статус-чеклист).
Тест-сеть: `cd /c/dev/apple_music && .venv/Scripts/python.exe -m pytest -q`.

---

## Сессия 2026-06-19 (продолжение «последняя сессия.md»)

### 0. Инцидент с сервисом (устранён)
- Прошлая сессия оборвалась по лимиту сразу после правок #10 (вывод MOBILE_ATMOS
  из TV-входа, переименование `_save_tidal_tv_session` → `_save_tidal_session`).
- Проверка целостности: синтаксис всех ключевых файлов ОК, висячих ссылок нет,
  все 81 модуля `ripster.*` импортируются, символы #10 связаны. Живой сервер
  (Py312) стартовал ПОСЛЕ правок → код #10 загружен.
- ⚠️ Ошибка диагностики: принял `.venv`-app за «стрэй» (он не держал порт) и убил
  его — оказалось это **bootstrap-РОДИТЕЛЬ** Py312-сервера, цепочка упала, лаунчер
  сам не поднял. Восстановил: `Start-Process .venv\Scripts\python.exe app.py -Hidden`.
  Цепочка снова `26348(.venv)→5772(Py312, :7799)`, HTTP 303. Память
  `ripster-restart-clean` дополнена жирным предупреждением.

### 1. Тест-сеть (фундамент рефакторинга)
Раньше у `ripster/` (~30k строк) тестов НЕ было. Добавлено:
- `pytest.ini`, `tests/conftest.py` (root на sys.path).
- `tests/test_imports.py` — импорт КАЖДОГО `ripster.*` модуля (регресс-сеть на
  сломанные импорты — то, что роняло сервис).
- `tests/test_app_builds.py` — `import app` строит FastAPI + роуты.
- Юнит-тесты на чистое ядро: `test_resolver.py`, `test_service_config.py`
  (вкл. регрессию на двойную вложенность ALAC), `test_service_layer.py`,
  `test_helpers.py` (region-rewrite, qobuz-quality, mixcue, fmt_bytes, norm, tier).
- Итог: **145 тестов, <1 с, зелёные.**

### 2. Модуль M1 — ядро URL/конфиг — ЗАКРЫТ ✅
Состав: `resolver.py`, `service_config.py`, `service_layer.py`.
Рефактор под зелёной сетью:
- **Дедуп:** `_parse_deezer/_parse_qobuz/_parse_tidal` были байт-в-байт идентичны
  → одна `_parse_path_service`, три имени — алиасы. (−16 строк дублей.)
- **Баг-фикс (qobuz-slug):** web-форма `qobuz.com/<lang>/album/<slug>/<id>`
  возвращала slug вместо id. Теперь id = последний чисто-числовой сегмент после
  ключевого слова (canonical-формы и tidal-UUID-плейлисты не задеты, покрыто
  тестами).
- App НЕ перезапускал: правки поведенчески-идентичны (доказано тестами), новый
  код подхватится при следующем штатном рестарте — лишний риск рестарта не нужен.
- Снапшоты вычищенных модулей + тесты скопированы в `github_setup/`.

### Отложено (взять на соответствующих этапах конвейера)
- Дедуп Qobuz app-id `798273057` (4 модуля: qobuz.py, metadata/qobuz.py,
  resolver.py, discovery.py) → делаем на этапе движка **qobuz** (трогает его файлы).
- 3× `_update_orpheus_settings` (beatport/spotify/tidal) — оценить на этапе движков.
- 3× `_sanitize` — НЕ мержить (разное поведение).

### 3. Движок apple — ЗАКРЫТ ✅
Состав: `apple_router.py`, `amd.py`, `engines/amd.py`, `engines/zhaarey.py`, `engines/base.py`.
- **Тесты (28):** `tests/test_apple_engine.py` — apple_router (`url_storefront`,
  `_apple_id`, `is_apple_music_video`); AMDEngine (`qualities`, `build_cmd`
  strip-affiliate/codec-map, `_amd_sanitize`, `extract_save_dir`, и главное —
  `is_finished`: conn-fail / summary OK / all-failed / **no-lossless-asset
  (анти-фантом-успех)** / per-track); ZhaereyEngine (`classify_line`,
  `parse_progress`, `is_finished`, `extract_save_dir`).
- **Рефактор:** не потребовался — dead-code скан чист, `_strip_ansi` уже шарится
  из `engines/base.py`, дублей нет. Модуль закрыт как «протестирован».
- Снапшоты + тесты в `github_setup/`. Полный набор: **173 теста, зелёные.**

### 4. Движок tidal — ЗАКРЫТ ✅
`engines/tidal.py`. **22 теста** (`tests/test_tidal_engine.py`): `_to_orpheus_url`
(listen.→browse, type lower), `_tidal_cover`, `qualities`, `classify_line`,
`parse_progress` (0-based), `is_finished` (реальный маркер `=== Track … downloaded
===`, анти-фантом, auth-fail, skip+rc0, пусто). Рефактор: dead-code чист.
`_update_orpheus_settings` НЕ дедуплицирован — 3 разные сигнатуры
(beatport/spotify/tidal), решение на этапе beatport. Полный набор: **195 тестов.**

### 5. Движок qobuz — ЗАКРЫТ ✅
`engines/qobuz.py`, `metadata/qobuz.py`, `engines/streamrip_utils.py`. **12 тестов**
(`tests/test_qobuz_engine.py`): общие streamrip-парсеры (classify/parse — шарятся с
Deezer), qualities, `is_finished` (artwork-PermissionError=успех, IneligibleError,
auth-fail, download-header, already-exists, **0-треков=провал**, explicit-error).
Рефактор: чисто; classify/parse уже делегируют в streamrip_utils.
**ВАЖНО:** дедуп app-id `798273057` ПЕРЕНЕСЁН в финальную сборку — он захардкожен в
**11 местах** (qobuz, metadata/qobuz, resolver, auth, discovery, isrc×2, releases,
streaming, tagger_routes×3), часть — вне scope GitHub. Piecemeal-дедуп нарушил бы
последовательность; делаем одним проходом на финале. Полный набор: **207 тестов.**

### 6. Движок deezer — ЗАКРЫТ ✅
`engines/deezer.py` (deemix). **15 тестов** (`tests/test_deezer_engine.py`):
qualities, classify/parse, `is_finished` (success, **All-done-но-0-треков=провал**,
bitrate-gate free-ARL, bad-ARL, unexpected-end). `build_cmd` не тестируется (пишет
реальный deemix-конфиг/ARL — побочка). Чисто. Полный набор: **222 теста.**

### 7. Движок spotify — ЗАКРЫТ ✅
`engines/orpheus_spotify.py` (OrpheusDL + librespot). **16 тестов**
(`tests/test_spotify_engine.py`): qualities, classify/parse (0-based),
`is_finished` (not-authed, new-settings, **librespot-fail**, **401-токен**,
partial-success, skip+rc0, empty, premium).
**ФЛАГ (находка, не правил):** `delete_creds()` нигде не зовётся. Роут
`/api/spotify/logout` (spotify.py:1185) удаляет только metadata-токен
(`_token_file`), а librespot-блоб `reusable_credentials.json` не трогает.
`delete_creds` существует ровно для блоба, но не подключён → logout неполный.
ОДНАКО блоб ценен (само-хилит OGG 401, см. память spotify-ogg-401-selfheal) —
стирать на logout может быть нежелательно. Поведенческое + продуктовое решение:
оставил `delete_creds` как есть, wiring/решение — на фазе Настроек с владельцем.
Полный набор: **238 тестов.**

### 8. Движок beatport — ЗАКРЫТ ✅
`engines/orpheus_beatport.py`, `routes/beatport.py`. **20 тестов**
(`tests/test_beatport_engine.py`): движок (qualities, classify, parse,
`is_finished` — **success проверяется ДО subscription-gate**, т.к. Orpheus печатает
"Professional subscription detected" на каждом успехе); роут-хелперы
(`_auth_headers`, `_preview_url`, `_dict_name`, `_fmt_track` с {w}/{h}→400).
**ВЕРДИКТ `_update_orpheus_settings`×3 (tidal/spotify/beatport): НЕ дедуплицировать.**
Общий скелет ~12 строк (read→quality/path→embed_cover=1000→write), но расхождения
большие: spotify ~80 строк уникальны (formatting, MP3-конверсия, PKCE-username,
идемпотентная запись), beatport/tidal — свои covers + module-блоки. Callback-
экстракция = индирекция + поведенческий риск (идемпотентная запись только у spotify)
без покрытия тестами. Дублирование поверхностное. Полный набор: **258 тестов.**

### 9. Движок soundcloud — ЗАКРЫТ (движок) ◐ вкладка частично
`engines/soundcloud.py` (Lucida/Node). **18 тестов** (`tests/test_soundcloud_engine.py`):
qualities, classify, parse, extract_save_dir, `is_finished` (all-ok, some-failed,
**DRM-FairPlay-ветка**, rc0-no-summary, no-marker).
**ФЛАГ:** `node_available()` нигде не вызывается — вероятно намеренный статус-хелпер
для Setup (проверка Node.js для SC-раннера), не подключён. Как `delete_creds` —
оставил, wiring на фазе Setup. Полный набор: **276 тестов.**
ОСТАЁТСЯ для полной вкладки SC: `engines/sc_widevine.py`, `routes/soundcloud.py` (1127 стр).

### 10. Вкладка SoundCloud — ЗАКРЫТА полностью ✅
+ `engines/sc_widevine.py` (**18 тестов**: pywidevine CDM, `is_finished` no-wvd/
revoked/summary) и `routes/soundcloud.py` (**15 тестов**): `_artwork` (дефолт size
t500x500), `_split_artist_title`, `_parse_sc_tracklist` (numbered/timestamped,
≥3-порог, drop-URL), **`_sc_host_ok` — SSRF allow-list прокси m3u8/key/license,
вкл. отбой suffix-spoof `sndcdn.com.evil.com`**, `_norm_track`. Чисто.
Полный набор: **312 тестов.**

### 🐞 БАГ-ФИКС (по репорту владельца): Tidal «выбрал 320 → скачался FLAC»
Гость, прямая Tidal-ссылка, выбрано «AAC 320» → файл пришёл FLAC, метка «320».
**Корень:** фронтенд шлёт коды качества Tidal, которых НЕТ в `_QUALITY_ORPHEUS`
движка → `.get(code, "lossless")` молча откатывался в lossless (FLAC), а карточка
показывала выбранный код. Несовпадения: `app.js:3208` (гостевой пикер) и
`player.js:678` шлют **`hires`** (а ключ был `hi_res`) и **`mp3`** (для AAC-320).
**Фикс:** `engines/tidal.py` `_QUALITY_ORPHEUS` дополнен алиасами
`hires/320/aac/mp3` → каждый код фронтенда теперь маппится явно, без отката.
+9 тестов (`test_tidal_engine.py`): все коды фронтенда покрыты + регресс-страж
«ни один код не падает в lossless». Полный набор: **378 тестов.** Снапшот обновлён.
⚠️ Глубже (на потом): у фронтенда два словаря кодов Tidal (app.js `hires/high`,
player.js `hires/mp3`) — стоит привести к канону; а карточка в боту берёт метку из
запрошенного кода (`QUALITY_LABEL[quality]`), не из реального файла — лучше показывать
`_delivered_q` (реальное качество). Это фаза вкладки Очередь/доставки.

### 🔧 Фича бота (приватный, вне публичной версии): owner-рассылка
По просьбе владельца — команда **`/broadcast <текст>`** (алиас `/say`) в `tgbot/bot.py`:
owner-only (`users.is_admin`), шлёт объявление ВСЕМ approved-юзерам через готовый
`_broadcast_all` (пропускает владельцев, rate-limit 0.05с, best-effort). Фолбэк:
ответить `/broadcast` на сообщение → разошлёт его текст. Заголовок локализован per-recipient
(«📢 Объявление/Announcement»). Добавлена в owner-меню `_set_commands`. Синтаксис OK.
**Бот перезапущен** (новый PID 15836, Py314): лог `Bot @Raccoon_Ripster_bot started …
Start polling`. `/broadcast` активен.

### 🔧 Фикс + фича рассылки (по проверке владельца)
- **БАГ:** первая рассылка упала — `TelegramBadRequest: can't parse entities:
  Unsupported start tag "текст"`. У бота `parse_mode=HTML`, а в подсказке было
  `<текст>`/`<text>` → принято за HTML-тег. Фикс: `«текст»` вместо `<текст>` +
  **HTML-экранирование тела** рассылки (`html.escape`) → любой текст (с `<`/`>`/`&`) безопасен.
- **ФИЧА (язык получателя):** тело рассылки теперь приходит **на языке, выбранном
  юзером** (ru/en/hi/ja/zh). Язык владельца — verbatim; остальные — машинный перевод
  (`deep-translator` GoogleTranslator, предперевод конкурентно через `asyncio.to_thread`,
  кэш по языку, **фолбэк на оригинал** если переводчик недоступен). Заголовок
  локализован на все 5 (📢 Объявление/Announcement/घोषणा/お知らせ/公告).
  `deep-translator==1.11.4` поставлен в Py314; перевод проверен (en/ja/zh/hi непусто).
  Бот перезапущен (PID 30876). _(Бот приватный — вне github_setup.)_

### 11. Движки amazon/yandex + варианты + маршруты вкладок — ЗАКРЫТЫ ✅
- **amazon/yandex** (23), **варианты** gamdl/spotiflac/zotify/registry (34).
- **Вкладки:** queue (`_make_task`), search (cover-хелперы), bbc (таймкоды/MixesDB-скоринг/
  CUE), coder (`_parse_folder_name`+`mixcue._fmt_name`), spectrum (`_verdict`),
  settings-ядро (`ConfigService` коэрция + атомарный YAML), setup (`_gamdl_flag`).
  history/console — тонкие, чистой логики нет (снапшоты сделаны).
- **Авторитетно: 423 теста, зелёные** (устный кумулятив по ходу слегка плыл — верно 423).
  Все движки + основные вкладки покрыты и в `github_setup/`.

### 12. Self-updater (прототип) — построен ✅
`ripster/updater.py` + эндпоинты `/api/update/check` и `/api/update/apply` (в setup.py).
- **Надёжное сравнение версий (по просьбе владельца):** `parse_version` парсит ЛЮБОЕ
  число компонент (`3.10`, `3.10.2.5`, `3.100.0`), `is_newer` сравнивает ЧИСЛЕННО с
  zero-pad → `3.9 < 3.10 < 3.100` (не строковый трап «10<9»), `3.10 == 3.10.0`.
  `check_for_update` отдаёт `available`/`latest`/`changelog`/`zip` (что нового + готово к скачке).
- **Чистое ядро (24 теста):** `parse_version`/`is_newer` (вкл. десятки/сотни/разные длины),
  `requirements_changed`
  (решает, перезапускать ли pip), `verify_import_smoke` (импортит ВСЕ ripster.*-модули —
  runtime-гейт против битого апдейта, прогон на текущем дереве = зелёный).
- **Оркестрация:** `check_for_update` (GitHub releases API), `apply_update`
  (снапшот в backups → `git pull --ff-only` → pip если изменился requirements →
  verify_import_smoke → откат при провале → restart_required). Heavy/данные не трогаются.
- **Документирована модель активации нового** (в докстринге updater.py): новый ДВИЖОК
  авто-discovery (`pkgutil.iter_modules`+`@register`); новый РОУТ — строкой `install()`
  в обновлённом app.py (едет в том же релизе); новый pip-DEP — через requirements diff;
  граница активации = рестарт. Новый `updater.py` сам подхватился `test_imports` — живая
  иллюстрация. **439 тестов.** Предусловие для боевого режима: свой git-репо Ripster
  (сейчас origin = апстрим Go-движка) + config `ripster-repo`.

### 13. Лаунчер из исходников + своё окно (pywebview) — ядро готово ◐
`ripster/launcher.py` (логика, в smoke-сети) + корневой `ripster_launcher.py` (entry).
Заменяет 35 МБ PyQt `RipsterLauncher.exe`: лёгкий, из исходников.
- Поднимает bootstrap-цепочку (`.venv app.py → Py312`), ждёт `:7799`, открывает
  **pywebview-окно** (системный webview, без Chromium); фолбэк в браузер если pywebview нет.
  Закрытие окна гасит поднятый сервер. First-run: подсказка по `check_tools` (чего тяжёлого нет).
- **5 тестов** (`config_port`/`server_url`/`bootstrap_python`); сам подхватился `test_imports`.
- **ДОБИТО:** `pywebview==6.2.1` установлен в `.venv` + закреплён в `requirements.txt`
  (тянет pythonnet+clr_loader на Windows). Реальное нативное окно (Edge WebView2).
  +2 теста на выбор webview/browser (фейковый webview-модуль). `server_alive` против
  живого сервера = True. Запуск: `.venv\Scripts\pythonw.exe ripster_launcher.py`. **456 тестов.**
- ОСТАЁТСЯ (deploy-полиш): вывод старого PyQt-лаунчера/35МБ-.exe, ярлык автозапуска.

### 14. Унификация путей сохранения — КОД готов ✅ (UI остаётся)
Директива: один общий путь, внутри `<service>/<quality>` (beatport/320, deezer/FLAC).
- **`get_save_path` переписан** → `Path(<save-path>, <service>, <quality_folder>)`.
  `all_save_paths` упрощён (один base + legacy для миграции).
- **Открытие:** движки трогать НЕ пришлось — `runner.py` строит `_cfg_view`, затеняя
  ВСЕ path-ключи значением `_base_save_path` (=get_save_path) перед `build_cmd`. Одна
  правка унифицировала движки И disk-truth согласованно (исключён класс «файлов нет»).
- Тесты переписаны (13), вкл. «игнор legacy per-service ключей». Демо вживую: пути
  как заказано. **460 тестов.** ⚠️ Live-эффект — на рестарте app (новая раскладка для НОВЫХ загрузок).
- Полный аудит настроек + cut-list (убрать Удалённый доступ; Радар оставить) — `SETTINGS_AUDIT.md`.
- ОСТАЁТСЯ (косmetика, без риска): UI — убрать per-service path-инпуты + вырезать
  owner-блоки (Бот/Гости/auto-delete/Docker/Удалённый доступ); чистка DEFAULT_CONFIG.

### Сессия 2026-06-20 (доработка ядра, НЕ финал — владелец: «дистриб не финалим, дорабатываем»)
Автономный проход по багам/UI. Все правки протестированы, app перезапущен чисто, снапшоты в github_setup.
- **Apple куки/wrapper разводка.** `zhaarey.is_finished` теперь детектит `Invalid CKC`
  → финальная ошибка задачи = понятный текст («сессия wrapper'а протухла, КУКИ ни при
  чём — они для AAC/видео») вместо `unknown finish state`. Доходит до карточки/бота/гостя
  (runner кладёт в task["error"]). +UI-подписи в «🔑 Токены» и «⚙️ Wrapper» (токены≠lossless-сессия).
- **Tidal GUI + бэкенд.** Резолвер (поиск/мета) переведён на device-flow токен
  (`engines.tidal._tidal_token_country`) — ручной listen.tidal токен больше не нужен.
  Индикатор «🔴 токен истёк» → «🟢 device-flow, само-рефреш» (`/api/admin/token-expiry`
  смотрит `_read_tv_session` первым). Ручной блок токенов свёрнут в `<details>`,
  убран дубль-индикатор. Проверено live: `{"tidal":{"session":"device-flow",...}}`.
- **БАГ Apple /song/ = альбом (1/10).** `metadata/apple.py::fetch_meta`: для одиночного
  трека iTunes отдаёт `trackCount` родительского альбома (10). Фикс: `trackCount/totalTracks
  = 1` для is_track (и для api_type songs/music-videos в catalog-fallback). Live: 1 трек.
- **UI-унификация путей.** Убраны ВСЕ per-service инпуты «Путь сохранения» (8 шт:
  qobuz/deezer/amazon/orpheus/sc/beatport/yandex/tidal) + чистка их записи в `saveServiceTab`.
  Остался один общий `save-path`. (Код-унификация `<base>/<service>/<quality>` была раньше.)
- **UI-чистка.** Beatport-блок (убраны дубль про подписку, OrpheusDL-заметка, GitHub-ссылка);
  «О сервисе» — снят жёсткий `height:340px` (контент обрывался на полэкрана); Tidal/iTunes/
  Qobuz API-ссылки в «О сервисе» сделаны кликабельными.
- **Apple-станции ra.* — ОТЛОЖЕНО (не закрыто).** Исследование: DRM radio-эпизоды, движки
  не возьмут. Промежуточно: честный отказ 422 (`/api/queue/add` + `fetch_meta` + `/api/meta`).
  Будущий спайк + движок через апдейтер — см. ROADMAP.md «🔮 ОТЛОЖЕНО».
- **i18n** всего проекта (много хардкода) — записан как ПОСЛЕДНИЙ пункт перед упаковкой (ROADMAP.md).
- Снапшоты: `engines/zhaarey.py`, `resolver.py`, `metadata/apple.py`, `routes/queue.py`,
  `tests/test_apple_engine.py`, `tests/test_apple_meta.py`. (core.py/admin.py — owner-heavy, вне github_setup.)
  Статика (settings.html/app.js/views.js) — не входит в github_setup-снапшот по конвенции.

### Сессия 2026-06-20 (продолжение) — фаза «сверка файлов → github_setup»
Gap-анализ `ripster/*.py` (81 модуль) против снапшотов. `runner.py`/`process_runner.py`
оказались уже SAME/чистыми (снапшотнуты ранее / process_runner clean).
- **Снапшотнут CLEAN-батч (24 файла, py_compile OK, без owner-связей):** `__init__`×4,
  `app_context`, `http_client`, `ws_broker`, `process_runner`, `queue_manager`, `queue_service`,
  `task_marker`, `task_state`, `persistence`, `download_manifest`, `security`, `tracklist_match`,
  `spotify_token_keeper`, `metadata/{deezer,mixesdb,spotify}`, `routes/{core,isrc,apple_auth,auth}`.
  Итого github_setup/ripster = 65 .py, все компилируются.
- **🔑 КЛЮЧЕВАЯ НАХОДКА — owner-фичи ВШИТЫ в in-scope импортами** (поэтому остаток ≠ копирование, а РАСЦЕПЛЕНИЕ):
  - `tagger` ← runner, ripster_coder (runtime-dep, хотя вкладка Теггер вырезается)
  - `wrapper_pool` ← runner, admin (runtime-dep, Apple lossless)
  - `stats_collector` ← bbc, soundcloud, streaming, runner, app (хуки аналитики по всему ядру)
  - `guest_manager` ← bbc, discovery, download, admin, guest (гостевые проверки в core-роутах)
  - `cloud` (Gofile) ← download; `auto_cleanup` ← app.py; `tl1001` ← soundcloud, app
  - Coupled in-scope роуты (нельзя снапшотить как есть): `routes/{download,streaming,releases,spotify}`
  - Чистый owner-out (НЕ в дистрибутив): `routes/{admin,guest,library,stats,watchlist,tagger_routes}`,
    `ngrok_service`, `tl1001_cf` (SeleniumBase, тяжёлое)
- **ВЫВОД:** оставшийся github_setup-перенос = decoupling owner-хуков (feature-флаги/стабы) в
  in-scope роутах + продуктовые решения владельца (Радар релизов — оставить? гейтить guest-хуки?).
  Это финальная работа (ROADMAP «вырезать бот/гостей/...»), НЕ простое копирование. Делать с подтверждением границ.

### Сессия 2026-06-20 (продолжение 2) — НОЖ: расцепление owner-кода в публичных копиях
Директива владельца: «разделить проект на мой и общий»; из live `ripster/` НЕ режем ничего;
owner-код вырезаем ТОЛЬКО в копиях github_setup (выбор «жёстко/под нож» — «уникумы будут ковырять код»).
- **Снапшотнут `tagger.py`** (чистый, IN — это квалити-пайплайн скачивания: rename/fix-tags/retag/cover,
  НЕ owner-вкладка Теггер; вкладка `routes/tagger_routes.py` — OUT).
- **Вырезаны owner-импорты/вызовы в 6 публичных копиях** (live-версии этих файлов НЕТРОНУТЫ):
  `routes/bbc.py` (stats+guest стрим-запись), `routes/discovery.py` (guest rate-limit),
  `routes/soundcloud.py` (stats стрим), `runner.py` (stats record_download + guest activity/quota),
  `routes/queue.py` (`_guest_session_id`→стаб `""`, quota if/else→owner-путь, guest-токен/queued-логи),
  `tests/test_helpers.py` (убран `admin._fmt_bytes`).
- **Валидация:** `cd github_setup && pytest` standalone → **99 passed**, `test_imports` зелёный (ВСЕ публичные
  модули импортятся без owner-кода), всё py_compile OK. Падают 2 теста — только `test_app_builds` (нет `app.py`
  в публичном дереве; это финальная сборка).
- **Подтверждено:** live `ripster/` owner-код цел (bbc:2/discovery:1/queue:4/soundcloud:1/runner:2 ссылки),
  публичные копии = 0. Сервер жив (303).
- **ОСТАЁТСЯ для самодостаточного публичного дерева:** публичный `app.py` (регистрирует только публичные роуты,
  без admin/guest/stats/library/tagger_routes/bot) + `requirements.txt` публичной версии + перенос остальных
  in-scope роутов с ножом (download→cloud/guest, streaming→stats, releases, spotify-download без сканера).
  Owner-out НЕ копировать: `routes/{admin,guest,library,stats,watchlist,tagger_routes}`, `guest_manager`,
  `stats_collector`, `cloud`, `ngrok_service`, `tl1001_cf`.

### Сессия 2026-06-20 (продолжение 3) — ПУБЛИЧНЫЙ app.py собран, код-слой готов
- Довнесены+вычищены публичные роуты: `download` (cloud/guest/Gofile-эндпоинт сняты, `_sanitize`
  инлайнен, `_authorize_task`→owner-only, `_guest_log`→no-op), `streaming` (stats/guest сняты),
  `releases`+`spotify` (были чистые). Докопированы `tagger.py`, `tl1001.py`, `auth.py` (все чистые).
- **Публичный `app.py` создан и вычищен:** owner роут-импорты/installs убраны; lifespan-таски
  (watchlist-loop/tunnel/idle-restart/stats-init) сняты; `_guest_mgr`→`_NoGuestManager` no-op стаб;
  `/api/admin/restart-when-idle` убран; `broadcast` и `/ws` упрощены (stats+guest сняты).
- **ПРИЁМКА ЗЕЛЁНАЯ:** `cd github_setup && pytest` = **465 passed** (вкл. test_app_builds: `import app`
  ок, 144 роута, 0 owner-роутов). **0 owner-ссылок** во всём `github_setup/ripster`+`app.py`.
  Live `ripster/` НЕТРОНУТА (owner-код цел, сервер 303).
- **ОСТАЁТСЯ (не код-логика):** нож по СТАТИКЕ (`static/views/*.html`+`js/*` — вырезать owner-вкладки
  Бот/Гости/Библиотека/Теггер/Статистика), публичный `requirements.txt` (без seleniumbase), `.gitignore`,
  `config.example.yaml`, README, дымовой запуск публичного app.py. Мелочь: мёртвая строка
  `/api/cloud-upload` в auth.py allow-list; неисп. `_guest_queue_view`/`_guest_owns_task` в app.py.

### Следующий шаг (до финала)
`runner.py`/`process_runner.py` (крупные, отдельно) → ФИНАЛ (см. DEPENDENCIES.md):
лаунчер из исходников + pywebview-окно, first-run Setup-визард, `.gitignore` дистрибутива,
глобальный дедуп (app-id `798273057` + cover-хелперы), вырезание вне-scope,
`config.example.yaml`, дымовой клон-тест.
