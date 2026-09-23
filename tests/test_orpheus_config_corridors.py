"""Движки OrpheusDL: каждый прогон — в своём каталоге настроек.

Разбор 20.09.2026. `_lock_key` в раннере делает `service` ключом полосы: у
Beatport и JioSaavn РАЗНЫЕ ключи, значит два прогона стартуют ОДНОВРЕМЕННО
(одна полоса = одна задача, разной полосы друг друга не ждут). Оба движка перед
запуском писали `download_quality`/`download_path` в ОДИН
`orpheus/config/settings.json`, а сам OrpheusDL перечитывает этот файл уже на
старте подпроцесса. Итог: гонка — Beatport уходил качать AAC 128 в папку
JioSaavn (и наоборот), плюс каждый прогон перечитывал/перезаписывал файл целиком
без блокировки.

Лечится так же, как у Spotify/Tidal: коридор конфига на движок. Сессия Beatport
при этом НЕ дублируется — refresh-токен ротируется, и вторая копия
loginstorage.bin означала бы повторный спуск уже погашенного токена.
"""
import json
import os
import time
from pathlib import Path

import pytest

from ripster import runner
from ripster.engines import orpheus_beatport as bp
from ripster.engines import orpheus_jiosaavn as js
from ripster.engines.orpheus_beatport import OrpheusBeatportEngine
from ripster.engines.orpheus_jiosaavn import OrpheusJioSaavnEngine

BASE_SETTINGS = {
    "global": {
        "general": {"download_quality": "hifi", "download_path": "D:\\base\\"},
        "covers": {"embed_cover": False, "main_resolution": 500},
        "advanced": {"disable_subscription_checks": False},
    },
    "modules": {"tidal": {"quality": "highres"}},
}


@pytest.fixture
def root(tmp_path, monkeypatch):
    """Фейковая установка OrpheusDL: свой settings.json и никаких реальных путей."""
    orpheus = tmp_path / "orpheus"
    (orpheus / "config").mkdir(parents=True)
    (orpheus / "orpheus.py").write_text("", encoding="utf-8")
    (orpheus / "orpheus").mkdir(parents=True)
    (orpheus / "orpheus" / "core.py").write_text("", encoding="utf-8")
    (orpheus / "modules" / "beatport").mkdir(parents=True)
    (orpheus / "modules" / "beatport" / "__init__.py").write_text("", encoding="utf-8")
    (orpheus / "modules" / "jiosaavn").mkdir(parents=True)
    (orpheus / "modules" / "jiosaavn" / "__init__.py").write_text("", encoding="utf-8")
    (orpheus / "modules" / "jiosaavn" / "interface.py").write_text("", encoding="utf-8")
    main = orpheus / "config" / "settings.json"
    main.write_text(json.dumps(BASE_SETTINGS), encoding="utf-8")

    monkeypatch.setattr(bp, "_orpheus_dir", lambda: orpheus)
    monkeypatch.setattr(js, "_orpheus_dir", lambda: orpheus)
    monkeypatch.setattr(bp, "_orpheus_python", lambda: "python")
    # Тариф узнаётся сетевым запросом — в тесте он просто известен.
    monkeypatch.setattr(bp, "_tier_now", lambda: "bp_link_pro_plus_2")
    return orpheus


def _quality(sp: Path) -> str:
    return json.loads(sp.read_text(encoding="utf-8"))["global"]["general"]["download_quality"]


# ── п.1: две задачи OrpheusDL реально могут идти одновременно ────────────────
class TestLanes:
    def test_beatport_and_jiosaavn_are_different_lanes(self):
        """Разные ключи ⇒ раннер запускает их параллельно — ровно поэтому
        общий settings.json и превращался в гонку."""
        k_bp = runner._lock_key({"engine": "orpheus_beatport", "service": "beatport"})
        k_jio = runner._lock_key({"engine": "orpheus_jiosaavn", "service": "jiosaavn"})
        assert (k_bp, k_jio) == ("beatport", "jiosaavn")

    def test_same_service_still_serializes(self):
        """Два Beatport с разным качеством — одна полоса, они и раньше не
        пересекались: гонка была МЕЖДУ сервисами, а не внутри них."""
        k = runner._lock_key({"engine": "orpheus_beatport", "service": "beatport"})
        assert k == runner._lock_key({"engine": "orpheus_beatport", "service": "beatport"})


# ── п.2: изолирование ────────────────────────────────────────────────────────
class TestCorridors:
    def test_each_engine_writes_its_own_file(self, root):
        assert bp._settings_path() != js._settings_path()
        assert bp._settings_path().is_relative_to(root / "config")
        assert js._settings_path().is_relative_to(root / "config")

    def test_the_race_no_more(self, root):
        """Сценарий аварии: Beatport записал hifi, параллельный JioSaavn — low.
        Раньше второй файл был первым, и Beatport качал 96 kbps."""
        bp._update_orpheus_settings("hifi", "D:\\beatport\\",
                                    {"beatport-username": "u", "beatport-password": "p"})
        js._update_orpheus_settings("low", "D:\\jiosaavn\\")
        assert _quality(bp._settings_path()) == "hifi"
        assert _quality(js._settings_path()) == "low"

    def test_the_shared_file_is_left_alone(self, root):
        """Общий конфиг — только источник базы: трогать его некому, иначе
        соседние движки (Tidal/Spotify) лишились бы своих значений."""
        main = root / "config" / "settings.json"
        before = main.read_text(encoding="utf-8")
        js._update_orpheus_settings("low", "D:\\jiosaavn\\")
        assert main.read_text(encoding="utf-8") == before

    def test_corridor_starts_as_a_copy_of_the_base(self, root):
        """Коридор наследует НЕ только качество: cover-настройки и чужие модули
        берутся из общего конфига, иначе прогон молча получил бы другие правила."""
        sp = bp._settings_path()
        cfg = json.loads(sp.read_text(encoding="utf-8"))
        assert cfg["global"]["covers"]["main_resolution"] == 500
        assert cfg["modules"]["tidal"]["quality"] == "highres"

    def test_engine_own_keys_land_in_the_corridor(self, root):
        bp._update_orpheus_settings("high", "D:\\bp\\",
                                    {"beatport-username": "user@bp", "beatport-password": "sekret"})
        cfg = json.loads(bp._settings_path().read_text(encoding="utf-8"))
        assert cfg["modules"]["beatport"]["username"] == "user@bp"
        assert cfg["global"]["general"]["download_path"] == "D:\\bp\\"
        # тариф подтверждён → проверку подписки НЕ выключаем (иначе снова 128k)
        assert cfg["global"]["advanced"]["disable_subscription_checks"] is False


# ── п.3: общий файл — не мастер, а Scratchpad Spotify ────────────────────────
class TestSharedBaseDoesNotLeakAfterSeed:
    """Коридор должен наследовать общий конфиг ОДИН раз (при создании), а далее
    догонять только СВОЙ `modules.<engine>`. До правки `_corridor_settings`
    перечитывал общий файл ПО MTIME и копировал его ЦЕЛИКОМ (`shutil.copy2`).

    Но общий `orpheus/config/settings.json` в конце каждого прогона пишет движок
    Spotify (orpheus_spotify.py: `_settings_path()` жёстко = этот файл, `_update_orpheus_settings`
    — write_text без атомарности). Значит `base.mtime` растёт на каждом Spotify-
    прогоне, и Beatport/JioSaavn «досеивались» Scratchpad'ом Spotify: Spotify не
    трогает `general.search_limit`, `formatting.*` и `advanced.codec_conversions`
    движка Beatport, а Beatport не перезаписывает их сам — они оставались
   spotify-ными. Ровно ту же гонку Tidal уже лечит догоном одного раздела
    (tests/test_tidal_config_corridor.py::test_a_newer_shared_file...).
    """

    def _run_spotify_scratch(self, root, **overrides):
        """Симуляция прогона Spotify: правит общий файл и поднимает его mtime
        выше коридора (как делает write_text в реальном прогоне)."""
        main = root / "config" / "settings.json"
        cfg = json.loads(main.read_text(encoding="utf-8"))
        g = cfg["global"]
        g["general"]["search_limit"] = 99
        g["advanced"]["enable_undesirable_conversions"] = True
        g["advanced"]["codec_conversions"] = {"vorbis": "mp3"}
        g["formatting"] = {"force_album_format": True, "album_format": "SPOTIFY/{name}"}
        g["covers"]["save_external"] = True
        g["covers"]["external_resolution"] = 3000
        for path, val in overrides.items():
            cur = g
            *keys, last = path.split(".")
            for k in keys:
                cur = cur.setdefault(k, {})
            cur[last] = val
        main.write_text(json.dumps(cfg), encoding="utf-8")
        st = main.stat()
        os.utime(main, ns=(st.st_atime_ns, (int(time.time()) + 30) * 10**9))
        return main

    def test_beatport_keeps_its_own_globals_after_a_spotify_run(self, root):
        """После Spotify-прогонаBeatport-коридор НЕ обязан зеркалить чужие
        глобальные настройки — только свой раздел модуля."""
        bp._update_orpheus_settings("hifi", "D:\\bp\\",
                                    {"beatport-username": "u", "beatport-password": "p"})
        corridor = bp._settings_path()
        self._run_spotify_scratch(root)
        # следующий прогон Beatport перечитывает/пересеивает коридор
        bp._update_orpheus_settings("hifi", "D:\\bp\\",
                                    {"beatport-username": "u", "beatport-password": "p"})
        cfg = json.loads(corridor.read_text(encoding="utf-8"))
        assert cfg["global"]["general"].get("search_limit") != 99, "утёк search_limit Spotify"
        assert cfg["global"]["advanced"].get("codec_conversions") != {"vorbis": "mp3"}, \
            "утекла mp3-конвертация Spotify → FLAC Beatport могли гнать в mp3"
        assert cfg["global"]["advanced"].get("enable_undesirable_conversions") is not True
        assert cfg["global"].get("formatting", {}).get("album_format") != "SPOTIFY/{name}"

    def test_jiosaavn_keeps_its_own_covers_after_a_spotify_run(self, root):
        """JioSaavn трогает в конфиге ТОЛЬКО quality+path (orpheus_jiosaavn.py:
        `_update_orpheus_settings`) — значит всё остальное он целиком получает с
        посева. Если посев обновляемый по mtime, внешние обложки Spotify
        (save_external) приехали бы в JioSaavn."""
        js._update_orpheus_settings("high", "D:\\jio\\")
        corridor = js._settings_path()
        self._run_spotify_scratch(root)
        js._update_orpheus_settings("high", "D:\\jio\\")
        cfg = json.loads(corridor.read_text(encoding="utf-8"))
        assert cfg["global"]["covers"].get("save_external") is not True
        assert cfg["global"]["covers"].get("external_resolution") != 3000
        assert cfg["global"]["general"]["download_quality"] == "high"   # своё — на месте

    def test_a_newer_shared_file_does_not_revert_this_runs_quality(self, root):
        """Прямо как у Tidal: свежее mtime общего файла не должно откатывать
        качество текущего прогона Beatport."""
        bp._update_orpheus_settings("hifi", "D:\\bp\\",
                                    {"beatport-username": "u", "beatport-password": "p"})
        self._run_spotify_scratch(root, **{"general.download_quality": "normal"})
        assert _quality(bp._settings_path()) == "hifi"

    def test_beatport_still_catches_up_only_its_own_module(self, root):
        """Догон остаётся: правка логина в общем конфиге доходит до коридора —
        но только раздел `modules.beatport`, без чужих глобалов."""
        bp._update_orpheus_settings("hifi", "D:\\bp\\",
                                    {"beatport-username": "u", "beatport-password": "p"})
        corridor = bp._settings_path()
        main = root / "config" / "settings.json"
        shared = json.loads(main.read_text(encoding="utf-8"))
        shared.setdefault("modules", {}).setdefault("beatport", {})["username"] = "owner@bp"
        main.write_text(json.dumps(shared), encoding="utf-8")
        st = main.stat()
        os.utime(main, ns=(st.st_atime_ns, (int(time.time()) + 30) * 10**9))
        cfg = json.loads(bp._settings_path().read_text(encoding="utf-8"))
        assert cfg["modules"]["beatport"]["username"] == "owner@bp"
        assert cfg["global"]["general"].get("search_limit") != 99

    def test_a_broken_shared_file_never_reaches_the_corridor(self, root):
        """Spotify пишет общий файл НЕатомарно (write_text) — пойманный на
        середине записи обрезок не должен копироваться в коридор."""
        good = bp._settings_path().read_bytes()
        (root / "config" / "settings.json").write_text('{"global": {"gen', encoding="utf-8")
        st = (root / "config" / "settings.json").stat()
        os.utime(root / "config" / "settings.json",
                 ns=(st.st_atime_ns, (int(time.time()) + 30) * 10**9))
        assert bp._settings_path().read_bytes() == good

    def test_seed_leaves_no_temp_files(self, root):
        bp._settings_path()
        assert list((root / "config" / "beatport").glob("*.tmp")) == []


class TestSessionStaysSingle:
    def test_beatport_session_is_the_shared_file(self, root):
        """Логин-хранилище НЕ переезжает в коридор: ротация refresh-токена
        живёт в одном файле, копия = повторный спуск погашенного токена."""
        assert bp._session_path() == root / "config" / "loginstorage.bin"

    def test_launch_cmd_pins_the_session_back_to_it(self, root):
        cmd = OrpheusBeatportEngine().build_cmd(
            "https://www.beatport.com/track/x/1", "hifi",
            {"beatport-username": "u", "beatport-password": "p",
             "beatport-save-path": "D:\\bp"})
        boot = cmd[cmd.index("-c") + 1]
        assert f"os.environ['ORPHEUS_SESSION_STORAGE'] = {str(root / 'config' / 'loginstorage.bin')!r}" in boot

    def test_jiosaavn_launch_cmd_has_no_session_override(self, root):
        """У JioSaavn входа нет — его пустой loginstorage остаётся в коридоре и
        не должен вообще смотреть в общий файл."""
        cmd = OrpheusJioSaavnEngine().build_cmd(
            "https://www.jiosaavn.com/song/x", "high", {"jiosaavn-save-path": "D:\\jio"})
        boot = cmd[cmd.index("-c") + 1]
        assert "ORPHEUS_SESSION_STORAGE" not in boot


class TestLaunchCmd:
    def test_the_two_launch_cmds_point_at_different_configs(self, root):
        bp_cmd = OrpheusBeatportEngine().build_cmd(
            "https://www.beatport.com/track/x/1", "hifi",
            {"beatport-username": "u", "beatport-password": "p",
             "beatport-save-path": "D:\\bp"})
        jio_cmd = OrpheusJioSaavnEngine().build_cmd(
            "https://www.jiosaavn.com/song/x", "high", {"jiosaavn-save-path": "D:\\jio"})
        bp_boot, jio_boot = bp_cmd[bp_cmd.index("-c") + 1], jio_cmd[jio_cmd.index("-c") + 1]
        assert f"os.environ['ORPHEUS_CONFIG_DIR'] = {str(root / 'config' / 'beatport')!r}" in bp_boot
        assert f"os.environ['ORPHEUS_CONFIG_DIR'] = {str(root / 'config' / 'jiosaavn')!r}" in jio_boot

    def test_output_and_url_still_passed(self, root):
        cmd = OrpheusJioSaavnEngine().build_cmd(
            "https://www.jiosaavn.com/song/x", "high", {"jiosaavn-save-path": "D:\\jio"})
        assert cmd[cmd.index("-o") + 1] == "D:\\jio"
        assert cmd[-1] == "https://www.jiosaavn.com/song/x"

    def test_working_dir_still_the_install_root(self, root):
        """CWD менять нельзя: modules/ и extensions/ OrpheusDL ищет относительно
        рабочей папки — коридор задаётся ТОЛЬКО переменной каталога конфига."""
        assert Path(OrpheusJioSaavnEngine().working_dir()) == root


class TestVendoredPatch:
    """core.py — вендоренный файл, и публичная сборка ставит его СВЕЖИМ клоном с
    GitHub. Если патч перестал ложиться (upstream переписал строки), коридор
    молча перестанет действовать: движок будет писать качество в файл, который
    прогон не читает. Поэтому гард и на текст патча, и на сам файл."""

    def test_patch_turns_pristine_core_into_a_corridor_reader(self):
        pristine = (
            "        self.data_folder_base = 'config'\n"
            "        self.settings_location = os.path.join(self.data_folder_base, 'settings.json')\n"
            "        self.session_storage_location = os.path.join(self.data_folder_base, 'loginstorage.bin')\n"
            "\n"
            "        os.makedirs('config', exist_ok=True)\n")
        out = bp.orpheus_core_corridor_patch(pristine)
        assert "os.environ.get('ORPHEUS_CONFIG_DIR')" in out
        assert "os.makedirs(self.data_folder_base, exist_ok=True)" in out
        assert bp.orpheus_core_corridor_patch(out) == out      # идемпотентно

    def test_the_patched_core_on_disk_is_exactly_what_the_patch_produces(self):
        """Ловит и потерю патча обновлением OrpheusDL, и расхождение формулировок
        между патчем и файлом.

        В голом публичном клоне (github_setup/) OrpheusDL появляется только
        после Setup, поэтому без файла на диске гард честно пропускается —
        так же, как в tests/test_tidal_config_corridor.py.
        """
        core_path = (Path(bp.__file__).resolve().parent.parent.parent
                     / "orpheus" / "orpheus" / "core.py")
        if not core_path.is_file():
            pytest.skip("OrpheusDL ещё не установлен")
        core = core_path.read_text(encoding="utf-8")
        assert bp.orpheus_core_corridor_patch(core) == core
        assert "os.environ.get('ORPHEUS_SESSION_STORAGE')" in core

