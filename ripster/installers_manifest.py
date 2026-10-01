"""ripster.installers_manifest — ЕДИНЫЙ реестр всех автоустановщиков Ripster.

Зачем. До 25.09.2026 каждый установщик жил сам по себе: URL в `ripster/setup`,
URL в `ripster/routes/setup.py`, URL в `github_setup/installer/*.ps1`, пин в
`VERSIONS.md`, пин в `requirements.txt`, а `requirements.lock` молча противоречил
обоим. Проверить «всё ли это живо» можно было только рукой по каждому месту, и
именно поэтому дерались пользователи, а не сторож.

Этот файл — единственный список. Он НЕ ставит и НЕ качает: только описывает.
Отсюда живут:

  * tools/check_installers.py  — страж (fast: URLs/пины/хэши; --full: реальные
                                 установки в изолированный temp-каталог);
  * tests/test_installers_contract.py — офлайн-контракт: у каждого установщика
                                 есть пин, источник хэша, таймаут и ключ
                                 сообщения об отказе;
  * ripster/setup + routes — читают отсюда таймауты и ожидаемые SHA256.

Поля записи:
    id, title       — что это и как называется по-человечески
    trigger         — КТО и КОГда запускает (кнопка / движок / старт приложения)
    kind            | archive | installer | git | pip | oci | none
    url             — источник ({ver} / {arch} подставляет установщик) или "—"
    pin             — закреплённая версия/коммит, либо "rolling:<где смотреть>"
    sha256          — ожидаемый хэш артефакта, либо "" (см. hash_source)
    hash_source     — ОтКУДА берётся хэш: pinned | provider-api | sidecar |
                      none-published | git-head | pypi-pin | oci-digest
    target          — куда кладётся результат
    timeout_s       — сколько секунд дано скачиванию/запуску (0 = не задан = дефект)
    msg_key         — ключ i18n для сообщения об отказе (ru+en обязательно)
    verify          — аргументы, доказывающие, что инструмент реально исполняется
    isolated_ok     — можно ли безопасно прогнать в temp-каталоге (False = пишет
                      в Program Files / C:\\Android / docker-демон)
    note            — честная сноска о том, что здесь не так
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

# Общая граница: скачивание, которое не отдаёт ни байта 60 секунд, мёртво.
DOWNLOAD_TIMEOUT = 900        # самый большой артефакт — ffmpeg, ~115 МБ
GIT_TIMEOUT = 300
PIP_TIMEOUT = 900
DOCKER_TIMEOUT = 1800


@dataclass(frozen=True)
class Installer:
    id: str
    title: str
    trigger: str
    kind: str
    url: str
    pin: str
    sha256: str
    hash_source: str
    target: str
    timeout_s: int
    msg_key: str
    verify: tuple[str, ...] = ()
    isolated_ok: bool = True
    live: bool = False            # пин проверяется по HTTP в fast-режиме стража
    note: str = ""
    extra: dict = field(default_factory=dict)


INSTALLERS: list[Installer] = [
    # ── бинари, которые Ripster качает сам ────────────────────────────────────
    Installer(
        id="go",
        title="Go (движок zhaarey)",
        trigger="Setup → «Apple (zhaarey)»; ripster.setup.install_go_windows",
        kind="archive",
        url="https://go.dev/dl/go{ver}.windows-{arch}.zip",
        pin="rolling:https://go.dev/dl/?mode=json",
        sha256="",
        hash_source="provider-api",     # go.dev/dl/?mode=json отдаёт sha256 на файл
        target="tools/go",
        timeout_s=DOWNLOAD_TIMEOUT,
        msg_key="setup.err.go",
        verify=("{exe}", "version"),
        extra={"fallback": "go1.27.1"},
        note="Версия выбирается из go.dev/dl/?mode=json (самый новый стабильный "
             "релиз с windows-{arch} archive); хэш берётся из той же записи. "
             "Если JSON недоступен — fallback из extra['fallback'] этого реестра.",
    ),
    Installer(
        id="gpac",
        title="MP4Box (GPAC)",
        trigger="Setup → «Apple (zhaarey)»; ripster.setup.install_gpac_windows",
        kind="installer",
        url="https://download.tsi.telecom-paristech.fr/gpac/new_builds/"
            "gpac_latest_head_win{arch}.exe",
        pin="rolling:nightly permalink (fallback: release/26.02)",
        sha256="",
        hash_source="none-published",   # GPAC не публикует хэшей для .exe-сборки
        target="C:\\Program Files\\GPAC",
        timeout_s=DOWNLOAD_TIMEOUT,
        msg_key="setup.err.gpac",
        verify=("{exe}", "-version"),
        isolated_ok=False,              # NSIS-установщик пишет в Program Files
        note="Единственный установщик продукта, который требует прав администратора "
             "и не может быть проверен в temp-каталоге. Быстрая проверка стража — "
             "только живость URL.",
    ),
    Installer(
        id="bento4",
        title="Bento4 (mp4decrypt + mp4extract)",
        trigger="Setup → компоненты «mp4decrypt»/«Apple (zhaarey)»; "
                "ripster.setup.install_mp4decrypt_windows",
        kind="archive",
        url="https://www.bok.net/Bento4/binaries/"
            "Bento4-SDK-1-6-0-641.x86_64-microsoft-win32.zip",
        pin="1.6.0-641",
        sha256="6916a390f75878872594be74554b8b54ab220bb29812424441a8e1ecc9a6ac5e",
        hash_source="pinned",
        target="tools/ (все *.exe из SDK/bin)",
        timeout_s=DOWNLOAD_TIMEOUT,
        msg_key="setup.err.bento4",
        verify=("{exe}",),
        note="Хэш ЗАМЕРЕН 25.09.2026 скачиванием с PRIMARY_URL (проверка запуска: "
             "у инструментов Bento4 НЕТ флага --version и rc всегда 1 — живой бинарь "
             "доказывает баннер «Bento4 Version», см. _verify_runs(banner=…)). "
             "Сторона bok.net статична с 2023 года. mp4extract обязателен: без него "
             "ALAC-декрипт умирает на каждом регионе.",
    ),
    Installer(
        id="ffmpeg",
        title="FFmpeg + ffprobe (Apple-ремукс)",
        trigger="Setup → «ffmpeg»; ripster.setup.install_ffmpeg_windows",
        kind="archive",
        url="https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip",
        pin="rolling:gyan release-essentials",
        sha256="",
        hash_source="none-published",   # .sha512 у gyan отдаёт 404-страницу
        target="tools/ffmpeg.exe, tools/ffprobe.exe",
        timeout_s=DOWNLOAD_TIMEOUT,
        msg_key="setup.err.ffmpeg",
        verify=("{exe}", "-version"),
        note="Хэш закрепить НЕЧЕМ: gyan.dev пересобирает `release-essentials` "
             "регулярно, любое записанное значение завтра станет ложным ожиданием "
             "(25.09.2026 в реестре лежал фейковый плейсхолдер — убран, это ломало "
             "бы установку). Страж проверяет живость URL и исполняемость бинаря.",
    ),
    Installer(
        id="node",
        title="Node.js ≥20 (SoundCloud/Lucida)",
        trigger="Setup → «soundcloud»; ripster.setup.install_node_windows",
        kind="archive",
        url="https://nodejs.org/dist/{ver}/node-{ver}-win-{arch}.zip",
        pin="v20.18.1",
        sha256="56e5aacdeee7168871721b75819ccacf2367de8761b78eaceacdecd41e04ca03",
        hash_source="provider-api",     # nodejs.org/dist/<ver>/SHASUMS256.txt
        target="tools/node",
        timeout_s=DOWNLOAD_TIMEOUT,
        msg_key="setup.err.node",
        verify=("{exe}", "--version"),
        note="Node 18 давал `Error: terminated` на первом же резолве SoundCloud — "
             "поэтому минимум 20, и системный старый Node подменяется portable.",
    ),
    Installer(
        id="spotiflac",
        title="SpotiFLAC (Spotify lossless)",
        trigger="Setup → «spotify» (ripster.setup.install_spotiflac_windows)",
        kind="archive",
        url="https://github.com/Nizarberyan/SpotiFLAC/releases/download/"
            "v1.1.0/SpotiFLAC{ext}",
        pin="v1.1.0",
        sha256="71022832ac7850e630768b3dca12e582cd524d7fd38437024635956c1a09c6f1",
        hash_source="pinned",
        target="tools/spotiflac.exe",
        timeout_s=DOWNLOAD_TIMEOUT,
        msg_key="setup.err.spotiflac",
        verify=("{exe}", "--help"),
        note="Хэш ЗАМЕРЕН 25.09.2026 скачиванием ассета v1.1.0. У бинаря НЕТ "
             "--version (Go-флаги: rc=2), живой запуск доказывает --help (rc=0). "
             "До 25.09.2026 движок сам говорил «бинарь не найден», а engine "
             "spotiflac.download_url() НЕ ИМЕЛ НИ ОДНОГО вызова: кнопка обещала "
             "установку, которой в продукте не было.",
    ),
    Installer(
        id="cmdline-tools",
        title="Android cmdline-tools (минт device.wvd)",
        trigger="Setup → «widevine»; ripster.setup._wvd_install_cmdline_tools",
        kind="archive",
        url="https://dl.google.com/android/repository/"
            "commandlinetools-win-11076708_latest.zip",
        pin="11076708",
        sha256="4d6931209eebb1bfb7c7e8b240a6a3cb3ab24479ea294f3539429574b1eec862",
        hash_source="pinned",
        target="C:\\Android\\Sdk\\cmdline-tools\\latest",
        timeout_s=DOWNLOAD_TIMEOUT,
        msg_key="setup.err.wvd_sdk",
        verify=("{exe}", "--version"),
        isolated_ok=False,
        note="Хэш ЗАМЕРЕН 01.10.2026 скачиванием архива (153 МБ). Пишет в "
             "C:\\Android и тянет 153 МБ + system-image; в temp не "
             "прогоняется. Проверено 25.09.2026: dl.google.com для repository/"
             "отвечает 200 (в отличие от maven2-реестра, который тут 404).",
    ),
    Installer(
        id="jre17",
        title="JRE 17 (sdkmanager/avdmanager)",
        trigger="Setup → «widevine»; ripster.setup._wvd_install_jre17",
        kind="archive",
        url="https://api.adoptium.net/v3/binary/latest/17/ga/windows/x64/jre/"
            "hotspot/normal/eclipse",
        pin="rolling:latest/17/ga",
        sha256="",
        hash_source="provider-api",     # api.adoptium.net/v3/assets/latest/17
        target="C:\\Android\\jre17",
        timeout_s=DOWNLOAD_TIMEOUT,
        msg_key="setup.err.wvd_jre",
        verify=("{exe}", "-version"),
        isolated_ok=False,
    ),
    # ── git: чужие репозитории, которые Ripster клонирует целиком ─────────────
    Installer(
        id="zhaarey",
        title="apple-music-downloader (zhaarey)",
        trigger="Setup → «Apple (zhaarey)»; ripster.setup.clone_downloader",
        kind="git",
        url="https://github.com/zhaarey/apple-music-downloader.git",
        pin="4c8b1328ee1650bcd4be4f67ccb3f1afa977a10e",
        sha256="",
        hash_source="git-head",
        target="main.go (+ дерево рядом с app.py)",
        timeout_s=GIT_TIMEOUT,
        msg_key="setup.err.clone",
        note="Клон HEAD: апстрим двигается часто и это нормально, поэтому страж "
             "на расхождение коммита предупреждает, а не ругается.",
    ),
    Installer(
        id="lucida-src",
        title="Lucida (SoundCloud-раннер)",
        trigger="Setup → «soundcloud»; routes/setup._install_soundcloud_component",
        kind="git",
        url="https://codeberg.org/lucida/lucida.git",
        pin="b4f4eb259a946a80435c59d151e15f89ad0fa9eb",
        sha256="",
        hash_source="git-head",
        target="tools/lucida/lucida-src",
        timeout_s=GIT_TIMEOUT,
        msg_key="setup.err.lucida",
    ),
    Installer(
        id="orpheus",
        title="OrpheusDL",
        trigger="Setup → «orpheus»; routes/setup._install_orpheus_component",
        kind="git",
        url="https://github.com/OrfiTeam/OrpheusDL",
        pin="a45ff47913508d4c09971bdb847d5845984f1e64",
        sha256="",
        hash_source="git-head",
        target="orpheus/",
        timeout_s=GIT_TIMEOUT,
        msg_key="setup.err.orpheus",
    ),
    Installer(
        id="orpheus-spotify",
        title="Модуль Orpheus: Spotify",
        trigger="Setup → «orpheus»",
        kind="git",
        url="https://github.com/bascurtiz/orpheusdl-spotify",
        pin="c64429f3447ce622c999d01cf30a06ca67354989",
        sha256="",
        hash_source="git-head",
        target="orpheus/modules/spotify",
        timeout_s=GIT_TIMEOUT,
        msg_key="setup.err.orpheus",
    ),
    Installer(
        id="orpheus-beatport",
        title="Модуль Orpheus: Beatport",
        trigger="Setup → «beatport»",
        kind="git",
        url="https://github.com/Dniel97/orpheusdl-beatport",
        pin="259979f913d8246f6073c88ce7685ac6d9eee9ef",
        sha256="",
        hash_source="git-head",
        target="orpheus/modules/beatport",
        timeout_s=GIT_TIMEOUT,
        msg_key="setup.err.beatport",
    ),
    Installer(
        id="orpheus-jiosaavn",
        title="Модуль Orpheus: JioSaavn",
        trigger="Setup → «jiosaavn»",
        kind="git",
        url="https://github.com/bunnykek/orpheusdl-jiosaavn",
        pin="0c20361935f83c61972b7eddccda446be57090ec",
        sha256="",
        hash_source="git-head",
        target="orpheus/modules/jiosaavn",
        timeout_s=GIT_TIMEOUT,
        msg_key="setup.err.jiosaavn",
        note="Ответвление Dniel97/orpheusdl-jiosaavn мёртво (404) — жив форк "
             "bunnykek; REPO_URL в engines/orpheus_jiosaavn.py.",
    ),
    Installer(
        id="wrapper-lite-src",
        title="Wrapper-Lite (WorldObservationLog)",
        trigger="tools/wrapper_lite/fetch_source.py (ручной/сборка)",
        kind="git",
        url="https://github.com/WorldObservationLog/wrapper",
        pin="b7058ec529156335cc7141319f3ca9d0e9baa95e",
        sha256="",
        hash_source="git-head",
        target="tools/wrapper_lite/src",
        timeout_s=GIT_TIMEOUT,
        msg_key="setup.err.wrapper_lite",
        note="Единственный git-установщик, который УЖЕ сверяет коммит перед "
             "сборкой — образец для остальных.",
    ),
    # ── pip: ставится в рантайм-интерпретатор или в изолированный venv ────────
    Installer(
        id="pip-gamdl",
        title="gamdl (Python, Apple)",
        trigger="Setup → движок gamdl; ripster.setup._run_full_setup_inner step 1",
        kind="pip",
        url="PyPI: gamdl",
        pin="3.7.4",
        sha256="",
        hash_source="pypi-pin",
        target="рантайм-интерпретатор (site-packages)",
        timeout_s=PIP_TIMEOUT,
        msg_key="setup.err.gamdl",
        live=True,
        note="Установщик кричал `pip install gamdl --upgrade` БЕЗ пина — один "
             "клик уносил движок на новый мажор. Пин взят из requirements.lock; "
             "в живом .venv 25.09.2026 стоит 3.3 (страж это и показывает).",
    ),
    Installer(
        id="pip-wvdvenv",
        title="pywidevine + httpx + mutagen (isolated wvdvenv)",
        trigger="Setup → «widevine»; ripster.setup._ensure_wvd_venv",
        kind="pip",
        url="PyPI: pywidevine httpx mutagen",
        pin="pywidevine==1.9.0",
        sha256="",
        hash_source="pypi-pin",
        target="tools/wvdvenv",
        timeout_s=PIP_TIMEOUT,
        msg_key="setup.err.wvd_venv",
        live=True,
        note="pywidevine изолирован от protobuf-конфликта намеренно; httpx/mutagen "
             "остаются транзитивными — их пин задаёт requirements.lock.",
    ),
    Installer(
        id="pip-orpheusvenv",
        title="Orpheus-venv (virtualenv-bootstrap)",
        trigger="Setup → «orpheus»/«beatport»; routes/setup._ensure_orpheus_venv",
        kind="pip",
        url="PyPI: virtualenv, -r orpheus/requirements.txt",
        pin="orpheus/requirements.txt (вендорный лок)",
        sha256="",
        hash_source="none-published",
        target="tools/orpheusvenv",
        timeout_s=PIP_TIMEOUT,
        msg_key="setup.err.orpheus",
        isolated_ok=False,
    ),
    Installer(
        id="pip-fix-gamdl-deps",
        title="Латка зависимостей gamdl/pywidevine",
        trigger="POST /api/fix-gamdl-deps (кнопка «Починить gamdl»)",
        kind="pip",
        url="PyPI: protobuf pywidevine construct",
        pin="protobuf>=4.21.0 (upgrade), pywidevine==1.9.0, construct==2.8.8",
        sha256="",
        hash_source="pypi-pin",
        target="рантайм-интерпретатор (site-packages)",
        timeout_s=PIP_TIMEOUT,
        msg_key="setup.err.fix_gamdl",
        live=True,
        note="Кнопка трогает ОБЩИЙ интерпретатор с --force-reinstall и без "
             "отката — единственный установщик продукта, у которого провал "
             "меняет окружение необратимо.",
    ),
    Installer(
        id="pip-protobuf-startup",
        title="protobuf (самолатка на старте app.py)",
        trigger="import app.py → _ensure_protobuf_runtime()",
        kind="pip",
        url="PyPI: protobuf",
        pin="6.33.4",
        sha256="",
        hash_source="pypi-pin",
        target="рантайм-интерпретатор (site-packages)",
        timeout_s=180,
        msg_key="setup.err.protobuf",
        live=True,
        note="Единственный установщик, который срабатывает САМ при запуске. "
             "Держит пол 6.33.4 — ровно то, что требует VERSIONS.md.",
    ),
    Installer(
        id="pip-streamrip",
        title="streamrip (движки Qobuz/Tidal)",
        trigger="bootstrap окружения: requirements.txt / requirements.lock",
        kind="pip",
        url="PyPI: streamrip",
        pin="2.0.5",
        sha256="",
        hash_source="pypi-pin",
        target="рантайм-интерпретатор (site-packages)",
        timeout_s=PIP_TIMEOUT,
        msg_key="setup.err.streamrip",
        live=True,
        note="requirements.lock на 25.09.2026 содержал 2.1.0 — версию, которую "
             "VERSIONS.md прямо запрещает (Tidal-краш). scripts/setup_env.ps1 "
             "ставит именно lock → fresh-машина получала сломанный Tidal.",
    ),
    # ── окружение, которое Ripster НЕ ставит, а обязывает иметь ───────────────
    Installer(
        id="git",
        title="Git",
        trigger="любой clone: routes/setup, ripster.setup.ensure_git",
        kind="installer",
        url="winget: Git.Git",
        pin="rolling:winget",
        sha256="",
        hash_source="none-published",
        target="C:\\Program Files\\Git",
        timeout_s=GIT_TIMEOUT,
        msg_key="setup.err.git",
        verify=("{exe}", "--version"),
        isolated_ok=False,
    ),
    Installer(
        id="wrapper-image",
        title="Docker-образ публичного враппера",
        trigger="POST /api/wrapper/pull | /api/wrapper/build (amd.py)",
        kind="oci",
        url="ghcr.io/itouakirai/wrapper:x86",
        pin="x86 (тег, не digest)",
        sha256="",
        hash_source="oci-digest",
        target="docker-демон",
        timeout_s=DOCKER_TIMEOUT,
        msg_key="setup.err.wrapper_image",
        isolated_ok=False,
        live=True,
        note="Тег `x86` меняемый: страж сверяет, что тег существует, и показывает "
             "digest. `docker build` и `docker pull` до 25.09 ждали процесса ввек.",
    ),
    Installer(
        id="updater-overlay",
        title="Самообновление Ripster (zipball-оверлей)",
        trigger="POST /api/update/apply → updater.apply_update",
        kind="archive",
        url="https://api.github.com/repos/{repo}/releases/latest",
        pin="rolling:release tag",
        sha256="",
        hash_source="none-published",
        target="дерево приложения",
        timeout_s=DOWNLOAD_TIMEOUT,
        msg_key="setup.err.update",
        isolated_ok=False,
        live=True,
        note="Оверлей кода без подписи и хэша (открытый пункт SECURITY_AUDIT.md); "
             "после накатки ставится СКАЧАННЫЙ requirements.txt — пип-пин из "
             "доверенного источника превращается в входной параметр сети.",
    ),
]

BY_ID: dict[str, Installer] = {i.id: i for i in INSTALLERS}


def by_id(iid: str) -> Optional[Installer]:
    return BY_ID.get(iid)


# ─── тексты отказа (ru+en) ─────────────────────────────────────────────────────
# У каждого установщика обязан быть msg_key; здесь его человекочитаемое тело.
# Страж (tools/check_installers.py) и офлайн-контракт
# (tests/test_installers_contract.py) проверяют: ключ из реестра существует,
# обе строки непустые, и в тексте есть ЧТО ДЕЛАТЬ (не только «упало»).
FAILURE_MSGS: dict[str, dict[str, str]] = {
    "setup.err.go": {
        "ru": "Go не скачался/не распаковался. Проверь сеть и поставь вручную: https://go.dev/dl/",
        "en": "Go failed to download/extract. Check the network or install manually: https://go.dev/dl/"},
    "setup.err.gpac": {
        "ru": "GPAC (MP4Box) не установился — нужен запуск от администратора. Поставь вручную: https://gpac.io/downloads/gpac-nightly-builds/",
        "en": "GPAC (MP4Box) did not install — it needs elevation. Install manually: https://gpac.io/downloads/gpac-nightly-builds/"},
    "setup.err.bento4": {
        "ru": "Bento4 (mp4decrypt/mp4extract) не скачался или хэш не совпал. Скачай вручную: https://www.bento4.com/downloads/ и положи bin/*.exe в tools/",
        "en": "Bento4 (mp4decrypt/mp4extract) download failed or checksum mismatch. Download manually: https://www.bento4.com/downloads/ and put bin/*.exe into tools/"},
    "setup.err.ffmpeg": {
        "ru": "FFmpeg не скачался с gyan.dev. Поставь вручную: winget install Gyan.FFmpeg (или положи ffmpeg.exe и ffprobe.exe в tools/)",
        "en": "FFmpeg failed to download from gyan.dev. Install manually: winget install Gyan.FFmpeg (or drop ffmpeg.exe and ffprobe.exe into tools/)"},
    "setup.err.node": {
        "ru": "Node.js не скачался или не запускается. Поставь вручную: winget install OpenJS.NodeJS.LTS",
        "en": "Node.js failed to download or won't run. Install manually: winget install OpenJS.NodeJS.LTS"},
    "setup.err.spotiflac": {
        "ru": "SpotiFLAC не скачался с GitHub-релиза. Скачай вручную: https://github.com/Nizarberyan/SpotiFLAC/releases и положи SpotiFLAC.exe в tools/",
        "en": "SpotiFLAC failed to download from its GitHub release. Download manually: https://github.com/Nizarberyan/SpotiFLAC/releases and place SpotiFLAC.exe in tools/"},
    "setup.err.wvd_sdk": {
        "ru": "Android cmdline-tools не скачались с dl.google.com. Проверь сеть; вручную: https://developer.android.com/studio#command-line-tools-only",
        "en": "Android cmdline-tools failed to download from dl.google.com. Check the network; manually: https://developer.android.com/studio#command-line-tools-only"},
    "setup.err.wvd_jre": {
        "ru": "JRE 17 (Adoptium) не скачался. Проверь сеть; вручную: https://adoptium.net/temurin/releases/?version=17",
        "en": "JRE 17 (Adoptium) failed to download. Check the network; manually: https://adoptium.net/temurin/releases/?version=17"},
    "setup.err.clone": {
        "ru": "git-репозиторий не склонировался (сеть/антивирус?). Повтори установку; при живом прокси проверь, что git видит github.com",
        "en": "The git repository could not be cloned (network/antivirus?). Retry the install; behind a proxy make sure git can reach github.com"},
    "setup.err.lucida": {
        "ru": "Lucida не установилась (git clone, npm install или сборка). Проверь сеть и Node ≥20 и повтори",
        "en": "Lucida failed to install (git clone, npm install or build). Check the network and Node ≥20, then retry"},
    "setup.err.orpheus": {
        "ru": "OrpheusDL не установился (git clone или его venv). Проверь сеть и повтори; логи — в консоли Setup",
        "en": "OrpheusDL failed to install (git clone or its venv). Check the network and retry; logs are in the Setup console"},
    "setup.err.beatport": {
        "ru": "Модуль Beatport для OrpheusDL не установился. Проверь сеть и повтори установку",
        "en": "The Beatport module for OrpheusDL failed to install. Check the network and retry"},
    "setup.err.jiosaavn": {
        "ru": "Модуль JioSaavn для OrpheusDL не установился (форк bunnykek). Проверь доступ к github.com и повтори",
        "en": "The JioSaavn module for OrpheusDL failed to install (bunnykek fork). Check access to github.com and retry"},
    "setup.err.wrapper_lite": {
        "ru": "Wrapper-Lite не собралась (коммит не совпал с реестром или npm/go упали). Обнови пин в installers_manifest вместе с апстримом",
        "en": "Wrapper-Lite failed to build (commit mismatch or npm/go failure). Update the pin in installers_manifest together with upstream"},
    "setup.err.gamdl": {
        "ru": "gamdl не установился из PyPI. Проверь сеть; вручную: pip install gamdl==3.7.4",
        "en": "gamdl failed to install from PyPI. Check the network; manually: pip install gamdl==3.7.4"},
    "setup.err.wvd_venv": {
        "ru": "Изолированный venv для pywidevine не создался. Проверь сеть и диск; пересоздай tools/wvdvenv кнопкой переустановки",
        "en": "The isolated pywidevine venv could not be created. Check network and disk; recreate tools/wvdvenv via the reinstall button"},
    "setup.err.fix_gamdl": {
        "ru": "Латка зависимостей gamdl/pywidevine не применилась — окружение могло остаться смешанным. Перезапусти приложение и повтори; если снова нет — scripts/setup_env.ps1",
        "en": "The gamdl/pywidevine dependency patch did not apply — the environment may be mixed. Restart the app and retry; if it fails again run scripts/setup_env.ps1"},
    "setup.err.protobuf": {
        "ru": "protobuf не поднят до 6.33.4 при старте — тексты/метаданные Apple могут падать. Вручную: pip install protobuf==6.33.4",
        "en": "protobuf could not be raised to 6.33.4 at startup — Apple lyrics/metadata may fail. Manually: pip install protobuf==6.33.4"},
    "setup.err.streamrip": {
        "ru": "streamrip==2.0.5 не стоит (движки Qobuz/Tidal). Вручную: pip install streamrip==2.0.5; НЕ бери 2.1.x — там краш Tidal",
        "en": "streamrip==2.0.5 is missing (Qobuz/Tidal engines). Manually: pip install streamrip==2.0.5; do NOT take 2.1.x — Tidal crashes there"},
    "setup.err.git": {
        "ru": "Git не найден и winget не поставил его. Поставь вручную: https://git-scm.com/download/win и повтори",
        "en": "Git was not found and winget could not install it. Install manually: https://git-scm.com/download/win and retry"},
    "setup.err.wrapper_image": {
        "ru": "Docker-образ публичного враппера не подтянулся (ghcr.io или докер-демон). Проверь, что Docker Desktop запущен; вручную: docker pull ghcr.io/itouakirai/wrapper:x86",
        "en": "The public wrapper Docker image did not pull (ghcr.io or the Docker daemon). Make sure Docker Desktop is running; manually: docker pull ghcr.io/itouakirai/wrapper:x86"},
    "setup.err.update": {
        "ru": "Обновление не скачалось/не накатилось — приложение осталось на текущей версии. Повтори позже или возьми архив на странице релизов",
        "en": "The update failed to download/apply — the app stays on its current version. Retry later or grab the archive from the releases page"},
}


def failure_text(msg_key: str, lang: str = "ru") -> str:
    entry = FAILURE_MSGS.get(msg_key)
    if not entry:
        return ""
    return entry.get(lang) or entry.get("ru") or ""
