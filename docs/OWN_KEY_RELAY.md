# Свои ключи: реле `/relay/*` на хозяйских учётках

Мы сами для себя тот же wm.wol.moe — но ТОЛЬКО на учётках, которые завёл
хозяин. Чужие учётки в пул не принимаются принципиально: маршрута
«залогинься и получи квоту» нет и не будет.

Код: `ripster/routes/relay.py` (API), `ripster/relay_pool.py` (пул
key-серверов), `ripster/relay_store.py` (ключи и квоты), `ripster/lite.py`
(клиент). Выдачка: бот `tgbot/` — `/apikey`. Админка: Настройки → «Свои
ключи (реле)».

## Архитектура: сервер — только ключи

Схема сознательно повторяет `am-hook · wrapper-lite` (изучена 24.09 по
`/assets/decrypt.js`, read-only): наружу уходят ТОЛЬКО мастер-плейлист и
шаблон расшифровки ключа. Аудио-сегменты каждый клиент тянет сам у Apple CDN
(`aod-*.itunes.apple.com` отпускает CORS и Range), расшифровка — локально:

| кто | чем расшифровывает |
|---|---|
| Ripster PC (движок lite) | Temari через `lite_shim` (уже работает) |
| браузер | wasm-воркеры (образец — am-hook, `crates/am-wasm`) |
| Ripster Mobile | Temari или нативная библиотека — задача следом |

Серверная полоса — килобайты ключевых конвертов на трек, а не мегабайты аудио.

Key-серверы (контейнеры Wrapper Lite, `tools/wrapper_lite/`) торчат ИСКЛЮЧИТЕЛЬНО
на `127.0.0.1` (условие 2 вердикта `docs/WRAPPER_LITE_AUDIT_2026-09-24.md`).
Публичный адрес есть только у реле. Одно учётка = один контейнер = один
device-info: враппер выводит device-info из хэша логина, два аккаунта в одном
контейнере — один device-info на двоих, ровно то, за что Apple режет пачками.

## Настройка пула (`config.yaml`, через хозяйский интерфейс, не руками)

```yaml
relay-enabled: true            # по умолчанию выключено: наружу ничего не торчит
relay-instances:               # пусто = один инстанс по apple-lite-url
  - url: "http://127.0.0.1:12340"
    label: "учётка A"
    account: "login-a@example.com"   # только для тарифного потолка
    streams: 0                       # 0 = взять из тарифа; N = явно
relay-default-qps: 2           # квоты НОВЫХ ключей; 0 = потолок снят
relay-default-concurrency: 2
relay-default-per-hour: 120
relay-default-per-day: 2000
relay-max-keys: 50
relay-license-allow: false     # /license закрыт по умолчанию (FairPlay-челленджи наружу не нужны)
relay-unauth-per-minute: 30    # перебор ключа с одного пира должен быть дорогим
relay-upstream-per-hour: 1200  # расход ОДНОГО key-сервера к Apple (pacing `relay-upstream`)
```

Потолок одновременности инстанса: явный `streams` → тариф учётки (individual /
student = 1, family = 6; «неизвестен» = 1 — право на шесть надо ЗНАТЬ) → 1.
Пейсинг против блокировки — тот же `ripster/pacing.py`, что бережёт учётки на
дороге закачки (штраф за 403/429, удвоение паузы, прощение после 2xx).
Повтор запроса к ключу на ДРУГУЮ учётку — это вторая лицензия Apple на тот же
трек, поэтому «попробуем у следующего» молча повторяем только транспортный
сбой (контейнер не ответил), и только на другом инстансе.

## API (wm.wol.moe-совместимый конверт `{code,msg,data}`)

Все ручки — под `/relay/`. Ключ: `Authorization: Bearer rk_…` или basic-форма
`https://rk_…@host/relay/…` (браузер шлёт `Basic base64("ключ:")`). Ключ в
query-параметре НЕ принимается — URL'ы оседают в логах прокси.

| ручка | параметры | назначение |
|---|---|---|
| `GET /relay/status` | — | живость: `{ready, regions, instances, readyInstances}`. Ни адресов, ни аккаунтов, ни ошибок upstream. Без ключа — но под общим лимитером |
| `GET /relay/m3u8` | `adamId` | мастер-плейлист (единственное, что сервер отдаёт про контент) |
| `GET /relay/key` | `adamId`, `uri` (`skd://…`) | шаблон расшифровки: `contentKey` + контекст Temari (ctx/state/регистры) |
| `GET /relay/lyrics` | `adamId`, `language`, `syllable` | тексты |
| `GET /relay/webplayback` | `adamId` | dev-токен плейлиста WebPlay: `{adamId, m3u8}` — путь для браузера без хозяйского media-user-token |
| `POST /relay/license` | `{adamId, challenge, uri, drmType}` | закрыт по умолчанию (`relay-license-allow`) |

Отказы: без ключа / неверный / отозванный — `401` (отозванный неотличим от
неизвестного); сверх квоты — `429` + `Retry-After`; реле выключено — `503` на
всё, включая `/status`; текст ошибки upstream наружу не уходит (в нём бывают
внутренности враппера).

## Ключи

* Выдача: `/apikey` в боте (одобренным пользователям; хозяин может и для
  устройств) или панель «Свои ключи» — для хозяйских.
* Хранится ТОЛЬКО HMAC-ref ключа (pepper — хозяйский session secret). Плаинтекст
  показывается ровно один раз в момент выпуска; восстановить нельзя — только
  новый ключ. В ответах админки и в логах ключа нет по построению.
* Квоты на ключ: QPS, одновременность, в час, в сутки (0 = наследовать дефолт
  при выпуске). Правка: `/apikey quota` в боте или `POST /api/relay/admin/quota`.
* Отзыв: `/apikey revoke` или панель. Счётчики отказов (`denied`) видны в
  хозяйской таблице — по ним и видно, что ключ утёк.

## Клиенты

### Ripster PC (этот репозиторий)

Настройки → «Свои ключи» → «ПК как клиент реле»: `relay-client-url` +
`relay-client-key`. Заполнены — весь Apple-путь (shim, router, кэш ключей)
ходит через реле с Bearer-ключом вместо прямого `apple-lite-url`; пусто — как
ходило всегда. Адрес реле без ключа — честно реле: получите 401, а не молча
вернётесь на прямой контейнер.

### AMDL / AppleMusicDecrypt (gRPC-клиент враппера)

`instance.url` — адрес реле (без схемы; `secure=true` по HTTPS), ключ — через
`AMD_WM_API_KEY` в окружении (не argv, не config.toml, не лог — как уже заведено
для wm.wol.moe с 10.09.2026):

```toml
[instance]
url = "tunnel-host.example/relay"
secure = true
```
```
set AMD_WM_API_KEY=rk_…
```

### Прямой HTTP (curl) — любой lite-клиент

```bash
curl -H "Authorization: Bearer $RK" https://tunnel-host.example/relay/status
curl -H "Authorization: Bearer $RK" "https://tunnel-host.example/relay/m3u8?adamId=1720704575"
curl -H "Authorization: Bearer $RK" "https://tunnel-host.example/relay/key?adamId=1720704575&uri=skd%3A%2F%2F…"
# basic-форма (браузер): https://rk_…@tunnel-host.example/relay/m3u8?adamId=…
```

### Ripster Mobile

Через тот же туннельный адрес и ключ: сервер даёт только `/relay/m3u8` и
`/relay/key`, сегменты — напрямую с Apple CDN, расшифровка на устройстве
(Temari/нативная библиотека). Проводка — отдельная мобильная задача; протокол
для неё уже описан здесь и совпадает с ПК-клиентом (`lite.LiteClient` с токеном).

## Наружу через туннель

Роуты `/relay/*` вынесены из сессионного замка самим роутом
(`auth.add_public_prefix("/relay/")`); публичный префикс — РОВНО `/relay/`,
админка `/api/relay/admin/*` остаётся под хозяйской сессией. CSRF-проверка по
Origin не трогает запросы с `Authorization: Bearer` (кросс-домен её подделать
не может) — `POST /relay/license` с bearer-ключом проходит; basic-форма
(`https://rk_…@host`) на изменяющих ручках подчиняется общему правилу Origin. Неаутентифицированные попадания лимитированы (`relay-unauth-per-minute`).
Журнал — одна строка на запрос: ref-подсказка (8 хэш-символов), ручка, HTTP-код;
ни плаинтеков ключей, ни адресов upstream.

## Гарантии, зафиксированные тестами

`tests/test_relay_routes.py` (и `test_relay_store.py`, `test_relay_pool.py`):
без ключа → 401 · garbage → 401 · отозванный неотличим от неизвестного ·
сверх QPS/суток → 429 с `Retry-After` · отказ не сжигает квоту · flood без
ключа → 429 · текст upstream-ошибки наружу не уходит · `/status` — только
регионы и числа · в успешном ответе ключ не встречается · admin — только под
хозяином · выпуск показывает ключ один раз.
