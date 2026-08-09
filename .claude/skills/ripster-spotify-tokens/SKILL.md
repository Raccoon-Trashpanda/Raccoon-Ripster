---
name: ripster-spotify-tokens
description: >
  Карта ЧЕТЫРЁХ разных токенов Spotify в Ripster (bearer из librespot-блоба,
  client-token, sp_dc, OAuth dev-app) — что из них живо, сколько живёт и какой
  симптом какому соответствует. Путать их = терять дни: «bearer свежий, а всё
  равно 401» почти всегда протухший client-token, а api.spotify.com/v1 для наших
  веб-токенов забанен НАВСЕГДА и ожиданием не лечится. Читать при любом Spotify
  401/403/429, при «радар не сканит», перед правкой любой токен-логики Spotify.
  Триггеры — «ORPHEUS_NOT_AUTHED», «Spotify 401», «429», «URL Blocked»,
  «client-token», «librespot», «радар пустой», «bearer».
---

# Spotify-токены Рипстера — карта, симптомы, лечение

READ THIS при любом Spotify 401/403/429, «ORPHEUS_NOT_AUTHED», «радар не сканит»,
«bearer свежий, а всё равно 401», или перед правкой токен-логики. Карта проверена
боем 2026-07-19 (оба фикса: качалка + радар).

## Токены (их ЧЕТЫРЕ, путать = терять дни)
| Токен | Откуда | Живёт | Для чего | Статус с RU-IP |
|---|---|---|---|---|
| **bearer (web-player)** | минт из librespot-блоба `orpheus/config/.librespot_cache/reusable_credentials.json` → файл `orpheus/config/spotify-token.txt` (кипер `ripster/spotify_token_keeper.py`, ~каждые 45 мин, 442 симв.) | ~60 мин | api-partner GraphQL (качалка, радар) | ✅ работает |
| **client-token** | минт БЕЗ кредов: POST `clienttoken.spotify.com/v1/clienttoken` (client_id `d8a5ed958d274c2e8ee717e6a4b0971d`) → файл `orpheus/config/spotify-client-token.txt` | **~14 дней** | обязательный заголовок api-partner | ✅ работает |
| sp_dc web-token | кука sp_dc → open.spotify.com/get_access_token | часы | старый путь радара | ❌ 403 URL Blocked (гео) |
| OAuth (dev app) | spotify-client-id/secret юзера | 1ч+refresh | /v1 Web API | ❌ /v1 забанен (см. ниже) |

## Главные выводы (2026-07-19, проверено вживую)
1. **«Bearer свежий, а всё 401» → первым делом ВОЗРАСТ client-token.** Протухший
   client-token = 401 на КАЖДЫЙ api-partner запрос. Авто-минт вшит в
   `spotify_embed_api._ensure_client_token()` (init >7дн, force при 401) и в
   `spotify.py::_sp_client_token()` (радар, >13дн).
2. **api.spotify.com/v1 для наших веб-токенов ПЕРМАНЕНТНО 429** — любой эндпоинт,
   мгновенно, любой свежести токен. НЕ лечится ожиданием. Не строить на /v1 ничего.
   Референсы работают со своим client_id из браузера — это не наш случай.
3. **api-partner.spotify.com НЕ банится** (bearer + client-token): качалка (getTrack/
   getAlbum) и радар (queryArtistDiscographyAll, hash 5e07d323…56599) живут тут.
   Полный пасс 5756 артистов = 0×429.
4. librespot-блоб может минтить bearer С ЛЮБЫМИ scope (user-follow-read и т.д.) —
   но для /v1 это бесполезно (см. п.2), scope НЕ был причиной.
5. sp_dc/TOTP-пути с RU-IP мертвы — не тратить время без прокси.

## Диагностика за 60 секунд
```powershell
# возраст токенов
Get-Item C:\dev\apple_music\orpheus\config\spotify-token.txt, C:\dev\apple_music\orpheus\config\spotify-client-token.txt | Select Name, LastWriteTime
# ручной тест api-partner (200 = транспорт жив)
# см. HANDOFF_2026-07-19_spotify_radar_release.md — готовый сниппет getTrack
```
- token.txt старше 55 мин → кипер лежит (проверь процесс app, лог `[sp-keeper]`).
- client-token старше 13 дней → протух или вот-вот; перезапусти любую споти-задачу
  (авто-минт) или дерни минт руками.
- api-partner 401 при свежих обоих → блоб отозван: перепэйрить `tools/spotify_pair.py`
  при открытом десктоп-Spotify.

## ВХОД: три кнопки, и только одна из них настоящая (31.07.2026)

Токены выше — про доступ к API. Отдельно и важнее: **чем человек логинится**.
Путать их = «✓ Авторизован» рядом с загрузками, которые не могут работать.

| Вход | Что кладёт на диск | Качает? | Где в UI |
|---|---|---|---|
| **🎧 Войти в Spotify** (librespot OAuth, `tools/spotify_oauth_login.py`, порт 5588) | `.librespot_cache/reusable_credentials.json` — тот же blob, что у десктопа | **ДА**, и кипер минтит из него Bearer'ы → радар тоже жив | первый блок вкладки Spotify |
| Запасной вход PKCE (`orpheus/_auth_helper.py`, порт 4381) | `orpheus/config/credentials.json` — токен Web API | шатко: только через хрупкий OAuth-путь librespot, Spotify его периодически отзывает | блок «Скачивание», подписан как запасной |
| Developer App (Client ID + Secret) | `spotify_token.json` | **НЕТ, никогда** | свёрнутый блок, помечен «необязательно» |

Различать программно: `orpheus_spotify.session_kind()` → `"blob" | "oauth" | ""`.
`is_authenticated()` — это просто `bool(session_kind())`, для UI его НЕ хватает.

### Миф про Premium — на него потеряли вечер вдвоём
`Active premium subscription required for the owner of the app` — это Spotify
отказывает **Developer App'у**, если у владельца приложения нет Premium. К
скачиванию отношения не имеет вообще: качалка не трогает `api.spotify.com/v1`.
Правильная реакция — не покупать подписку и не искать другой Client ID, а вообще
не заполнять Developer App.

Признак, что человек нажал не ту кнопку: имя пользователя **`orpheus_pkce_user`**.
Это не аккаунт, а запасное имя из `_auth_helper.py`, которое подставляется, когда
`/v1/me` не ответил (а он не отвечает — см. п.2 выше).

### Ловушка повторного входа
`librespot.Session.Builder.oauth()` начинается с
`if os.path.isfile(stored_credentials_file): return self.stored_file(None)` — если
blob уже лежит, браузерный вход **молча не запускается**, URL не появляется, и UI
падает в «нет auth URL — проверь порт 5588». То есть перелогин под другим аккаунтом
был невозможен. Поэтому `spotify_oauth_login.py` логинится в отдельный файл
`reusable_credentials.new.json` и подставляет его на место только после успеха.

### Что проверять первым делом
```powershell
# есть ли настоящая сессия
Test-Path C:\dev\apple_music\orpheus\config\.librespot_cache\reusable_credentials.json
# и только потом — свежесть токенов (таблица выше)
```
`[sp-keeper] no durable Spotify blob — keeper idle` в консоли = сессии нет, всё
остальное про Spotify чинить бессмысленно.

## Связанное
Скиллы: spotify-release-radar (антибан-дизайн радара),
ripster-service-auth-windows (как открывать окно входа, чтобы оно вообще
открылось). Память: [[project_spotify_orpheus]],
[[project_release_radar_apipartner_2026-07]], [[project_spotify_blob_autonomy]],
[[project_spotify_login_windows_2026-07-31]].
