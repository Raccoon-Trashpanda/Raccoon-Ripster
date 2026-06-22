# Ripster — ПОЛНЫЙ лог сессий (durable handoff)

> Этот файл — для следующего агента/сессии (в т.ч. если владелец зайдёт с другого
> аккаунта). Живёт в проекте (`github_setup/`), поэтому переживёт смену аккаунта.
> Парные файлы: `ROADMAP.md` (чеклист-конвейер), `PROGRESS_LOG.md` (хронология
> текущей сессии), `DEPENDENCIES.md` (архитектура «из коробки» + авто-докачка).

---

## 0. БЫСТРЫЙ СТАРТ ДЛЯ АГЕНТА
- Проект: `C:\dev\apple_music` (НЕ `C:\Users\AR`). Это git-репо. Windows 11.
- **Тесты:** `cd /c/dev/apple_music && .venv/Scripts/python.exe -m pytest -q`
  (pytest 9.0.3 в `.venv`, Py3.12). На 2026-06-20: **378 тестов, зелёные.**
- **App жив?** `:7799` → HTTP 303 = ок (редирект на логин). Сервер — bootstrap-цепочка
  `.venv\Scripts\python.exe app.py` → дочерний `Programs\Python312\python.exe app.py`
  (Py312-дочерний держит порт). ⚠️ НЕ убивать `.venv`-процесс «как стрэй» — это
  bootstrap-РОДИТЕЛЬ; его смерть роняет сервер, лаунчер сам не поднимает. Recovery:
  `Start-Process .venv\Scripts\python.exe app.py -WorkingDirectory C:\dev\apple_music -WindowStyle Hidden`.
- Чистый рестарт: `POST 127.0.0.1:7799/api/admin/restart` с кукой из `_admincookies.txt`.
- Поведенческо-идентичные правки loaded-модулей НЕ требуют рестарта (подхватятся при
  следующем штатном); рисковать рестартом ради этого не нужно.

## 1. ПРОШЛАЯ СЕССИЯ (`последняя сессия.md`, #1–#10) — кратко
Тема: «из коробки» логины + фиксы. Всё было закрыто перед обрывом по лимиту.
- **#1 Spotify**, **#2 Tidal 13/14 ложный недосчёт** (disk-truth), **#3 гостевой
  3-state светофор** (🟢качает/🟡онлайн/○офлайн), **#4 Tidal device-flow логин**
  (link.tidal.com, кнопка в Настройках), **#5 реальное качество в карточке**,
  **#6 Beatport `save_external=False`** (нет дублей обложек), **#7 рестарт гостей**,
  **#8 Spotify ночная смерть токена** → keeper, **#9 Tidal atmos** (prefer_ac4),
  **#10 MOBILE_ATMOS-сессия** выводится из TV refresh_token (один логин = и Atmos).
- Также ранее: Tidal анти-фантом-успех (`=== Track <id> downloaded ===`), вшитые
  обложки 1000×1000, Spotify/SC метаданные, Beatport-фикс.
- Память (machine-local, `C:\Users\AR\.claude\...\memory\`): ripster-amd-naming-bug,
  ripster-telegram-bot, ripster-bot-panel-cache-gotcha, ripster-tidal-orpheusdl,
  ripster-restart-clean, ripster-spotify-ogg-401-selfheal, ripster-out-of-box-logins,
  ripster-test-suite.

## 2. ТЕКУЩАЯ СЕССИЯ (2026-06-19/20)
### 2.1 Инцидент с сервисом (устранён)
Принял `.venv`-app за «стрэй» (порт не держал) и убил → оказалось это bootstrap-
родитель Py312-сервера, цепочка упала. Поднял заново. Память `ripster-restart-clean`
дополнена предупреждением. См. §0.

### 2.2 Тест-сеть (создана с нуля — раньше тестов НЕ было)
`pytest.ini` + `tests/`: `test_imports.py` (импорт ВСЕХ 81 модулей — регресс-сеть на
сломанные импорты), `test_app_builds.py`, и юниты на ядро. См. память ripster-test-suite.

### 2.3 Конвейер «тест → рефактор → закрыть → снапшот в github_setup/» — ЗАКРЫТО
- **M1 ядро** (resolver/service_config/service_layer): дедуп 3 парсеров →
  `_parse_path_service`; **починен qobuz web-URL** (slug вместо id).
- **Движки (все):** apple (28), tidal (31), qobuz+streamrip_utils (12), deezer (15),
  spotify (16), beatport+route (20), soundcloud (18), sc_widevine (18),
  amazon+yandex (23), gamdl/spotiflac/zotify/registry (34). Везде: qualities/
  classify/parse/`is_finished` (фокус — анти-фантом-успех).
- **SC-роут (15):** `_artwork`, `_parse_sc_tracklist`, **`_sc_host_ok` (SSRF allow-list)**.
- **Итого 378 тестов, зелёные.** Снапшоты вычищенных модулей + тесты — в `github_setup/`.

### 2.4 Находки (задокументированы)
- **Баг-фикс Tidal «выбрал 320 → скачался FLAC»**: фронтенд шлёт коды `hires`/`mp3`,
  которых не было в `_QUALITY_ORPHEUS` → молчаливый откат в lossless. Добавил алиасы
  `hires/320/aac/mp3`. Хвост: у фронтенда 2 словаря кодов (app.js/player.js) — свести
  к канону; карточка берёт метку из запрошенного кода, а не из файла (`_delivered_q`).
- **Отложено:** дедуп app-id `798273057` (11 мест, вкл. вне-scope) — отдельным проходом
  на финале. `_update_orpheus_settings`×3 — НЕ дедупить (поверхностное дублирование).
  `delete_creds` (spotify) и `node_available` (sc) — намеренные неподключённые API.

### 2.5 Q&A владельца (зафиксировано)
- **Spotify токены:** закрыто. `spotify_token_keeper.py` (app.py lifespan) минтит Bearer
  из долговечного librespot-блоба, когда токен устарел (>40 мин) — браузер не нужен.
  **Расширение больше не обязательно** (блоб спарен 12.06 через `tools/spotify_pair.py`);
  оно лишь запасной источник на случай ревока блоба. + OGG 401 self-heal.
- **Гостевой кэш:** команды отключения у гостей НЕТ — гостевые загрузки кэшируются
  ВСЕГДА (`_cache_enabled_for_chat`: гость→`True`). `/cache` — только для владельца.
  (Можно добавить тумблер, если попросят.)

## 3. ДИРЕКТИВЫ ПО ДИСТРИБУТИВУ (см. DEPENDENCIES.md — главное)
- Публичная версия: вкладки **Очередь · Поиск · История · BBC · SC · Кодер · Спектр ·
  Настройки · Setup · Консоль**. БЕЗ бота, гостей, Библиотеки, Теггера, Статистики.
- **Из коробки**, кроме тяжёлого — Ripster докачивает сам (уже есть `ripster/setup/`).
  Два режима: GitHub-инсталлятор (тянет при установке) / portable-архив (тянет в Setup).
- **Лаунчер:** `RipsterLauncher.exe` (35 МБ) переработать и **спрятать в исходники**;
  проект **открывается автономно, как deemix** — НО Ripster многофайловый (не 1 .exe).
  **UI — в собственном окне, не в браузере** (рекоменд. pywebview; см. DEPENDENCIES.md).
- **`.wvd`/токены:** НЕ бандлить и НЕ авто-качать рабочий CDM (legal+ревок); управляемый
  one-time setup, юзер кладёт своё. Загрузка+валидация уже есть (`sc_upload_wvd`/`sc_wvd_status`).
  `.wvd` опционален (только SC HQ-DRM). См. DEPENDENCIES.md §приватные артефакты.

## 3.1 ФИНАЛЬНЫЙ ПУНКТ — Bandcamp-движок как боевой тест апдейтера
Внедрить новый движок **Bandcamp** и выкатить ЧЕРЕЗ апдейтер = end-to-end проверка
авто-discovery (новый движок сам попадает в `REGISTRY`) + всего цикла обновления.
Спец: без аккаунтов/auth; API Bandcamp — стрим+скачка **MP3 128**; дискография артиста;
теги+обложки полностью. Файлы: `engines/bandcamp.py` (@register) + bandcamp-URL в
`resolver.py` + артист-листинг в `discovery.py`; стрим-URL из встроенного JSON страницы
(`data-tralbum`, mp3-128, без ключей). Приёмка: после апдейта виден в `/api/qualities`,
качает с тегами, `verify_import_smoke` зелёный. Подробности — ROADMAP.md (последний пункт).

## 4. СЛЕДУЮЩИЕ ШАГИ (конвейер)
Маршруты вкладок (queue/discovery-search/history/bbc/ripster_coder+mixcue/spectrum/
config-settings/setup/console) → затем ФИНАЛ: `.gitignore` дистрибутива, first-run
Setup wizard (`check_tools`+`run_full_setup`), недостающие авто-установщики
(ffmpeg/amz/N_m3u8DL-RE), лаунчер из исходников + автономный запуск, глобальный дедуп
app-id, вырезание вне-scope, `config.example.yaml`, README, дымовой тест чистого клона.
