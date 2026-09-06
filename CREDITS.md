# Credits

Ripster не написан с нуля. Ниже — всё, на чьей работе он стоит: чужие
программы, которые он запускает, и чужие сервисы, к которым он обращается.
Список ведётся честно и пополняется при каждом новом заимствовании — если
что-то внедрено, оно должно быть здесь названо.

Разделение важное и соблюдается буквально: **«используем» и «скопировали» —
разные вещи.** Там, где мы вызываем чужую программу или чужой публичный
эндпоинт, чужого кода у нас нет; где код действительно взят или портирован,
это сказано отдельно.

## Apple Music

**WorldObservationLog** — [AppleMusicDecrypt](https://github.com/WorldObservationLog/AppleMusicDecrypt),
[pywidevine](https://github.com/WorldObservationLog/pywidevine).
Его же публичные сервисы на домене `wol.moe`:

| Сервис | Что это | Как используем |
|---|---|---|
| `wm.wol.moe` | wrapper-manager: пул устройств-волонтёров, выдающих ключи | движок `amd` (`ripster/amd.py`, `ripster/engines/amd.py`); включается ТОЛЬКО вручную (`apple-wrapper = public`) |
| `amd.wol.moe` | «Apple Music Web Decrypter» — браузерный клиент к тому же пулу, расшифровка в WASM | код НЕ заимствован; из него взята только форма публичных эндпоинтов (`/status`, `/m3u8`, `/key`, `/license`, `/lyrics`, `/webplayback`) |
| `amp-api.wol.moe` | прокси каталога Apple (AMP API) без своего токена | изучен 06.09.2026, отвечает в том числе на `filter[upc]`; как запасной источник каталога пока НЕ подключён |

Что именно мы у него взяли 06.09.2026: **знание, что здоровье пула надо
спрашивать у `/status`, а не у корня сайта** — оттуда приходят `ready`,
`clientCount` и, главное, список обслуживаемых витрин. Это факт о публичном
интерфейсе, а не его код; реализация в `apple_router.public_pool_status()`
своя.

**zhaarey** — [apple-music-downloader](https://github.com/zhaarey/apple-music-downloader),
[go-mp4tag](https://github.com/zhaarey/go-mp4tag). Загрузчик на Go в корне
репозитория — форк его проекта; движок `zhaarey`.

**glomatico** — gamdl (движок `gamdl`, видеоклипы через cookies + CDM).

## Остальные сервисы

- **OrpheusDL** — [OrfiTeam/OrpheusDL](https://github.com/OrfiTeam/OrpheusDL),
  плюс модули: [Dniel97/orpheusdl-beatport](https://github.com/Dniel97/orpheusdl-beatport),
  [bascurtiz/orpheusdl-spotify](https://github.com/bascurtiz/orpheusdl-spotify).
- **streamrip** — Qobuz, Tidal, Deezer.
- **[Nizarberyan/SpotiFLAC](https://github.com/Nizarberyan/SpotiFLAC)** — движок `spotiflac`.
- **[llistochek/yandex-music-downloader](https://github.com/llistochek/yandex-music-downloader)** — движок `yandex`.
- **zotify** — движок `zotify`.
- **[cloudflared](https://github.com/cloudflare/cloudflared)** — туннель наружу.
- **Odesli / song.link** — опознание релиза по ссылке площадки, которую наш
  резолвер не разбирает (`ripster/odesli.py`). Публичный API закрыт с
  31.07.2026 (`401 PUBLIC_API_ACCESS_DEPRECATED`); читаем данные их публичной
  страницы. Берём оттуда **только идентификатор**, никогда — доступность.
- **MusicBrainz**, **Discogs**, **Beatport** — источники жанров и каталожных
  сведений (`ripster/genre_sources.py`).

## Мобильный Ripster

Нативный движок собран на: **Oboe** (Google), **dr_flac / dr_wav**
(David Reid, public domain), **Apple ALAC** (Apache-2.0, вендорен),
**WavPack 5.7.0** (David Bryant, BSD-3, вендорен).

## Go-зависимости загрузчика

`AlecAivazis/survey`, `fatih/color`, `grafov/m3u8`, `olekukonko/tablewriter`,
`spf13/pflag` — см. `go.mod`.
