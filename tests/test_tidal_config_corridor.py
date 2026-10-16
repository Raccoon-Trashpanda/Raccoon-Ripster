"""Коридор конфига Tidal и его взаимодействие с пулом учёток.

Разбор 20.09.2026. `_lock_key` в раннере делает `service` ключом полосы, поэтому
`tidal` и `orpheus_spotify` стартуют ОДНОВРЕМЕННО. Оба перед прогоном писали
`download_quality`/`download_path` в ОДИН `orpheus/config/settings.json`, а
OrpheusDL перечитывает файл уже на старте подпроцесса — то есть сосед перебивал
качество соседу. Beatport/JioSaavn от этой гонки уже защищены коридором
(tests/test_orpheus_config_corridors.py); здесь — тот же коридор для Tidal.

Чего коридор НЕ трогает: сессию. `loginstorage.bin` остаётся общим для основного
аккаунта — в него пишет вход из Settings (routes/core.py::_save_tidal_session),
и переезд файла разлогинил бы владельца. Слоты пула (>0) живут своей сессией, как
жили до правки: иначе две учётки затирали бы refresh-токены друг друга.
"""
import json
import os
from pathlib import Path

import pytest

from ripster import tidal_pool
from ripster.engines import orpheus_spotify as osp
from ripster.engines import tidal as td

BASE_SETTINGS = {
    "global": {
        "general": {"download_quality": "lossless", "download_path": "D:\\base\\"},
        "covers": {"embed_cover": False, "main_resolution": 500},
        "advanced": {"disable_subscription_checks": False},
    },
    # Ключи входа владельца — именно то, что коридор ОБЯЗАН унаследовать.
    "modules": {
        "tidal": {
            "tv_atmos_token": "ownerTvCid",
            "tv_atmos_secret": "ownerTvSecret",
            "mobile_atmos_hires_token": "ownerMobileAtmosCid",
        },
        "beatport": {"username": "owner@bp"},
    },
}


@pytest.fixture
def root(tmp_path, monkeypatch):
    """Фейковая установка OrpheusDL: общий settings.json, общая сессия, движки
    без реальных путей."""
    orpheus = tmp_path / "orpheus"
    (orpheus / "config").mkdir(parents=True)
    (orpheus / "orpheus.py").write_text("", encoding="utf-8")
    (orpheus / "modules" / "tidal").mkdir(parents=True)
    (orpheus / "modules" / "tidal" / "interface.py").write_text("", encoding="utf-8")
    main = orpheus / "config" / "settings.json"
    main.write_text(json.dumps(BASE_SETTINGS), encoding="utf-8")
    # Живая сессия владельца — файл, который коридор трогать не вправе.
    session = orpheus / "config" / "loginstorage.bin"
    session.write_bytes(b"\x80\x04owner-session-blob")

    monkeypatch.setattr(td, "_orpheus_dir", lambda: orpheus)
    monkeypatch.setattr(osp, "_orpheus_dir", lambda: orpheus)
    # команду сборки одолжает beatport-реализация — её пути тоже надо в тест
    from ripster.engines import orpheus_beatport as bp
    monkeypatch.setattr(bp, "_orpheus_dir", lambda: orpheus)
    monkeypatch.setattr(td, "_orpheus_python", lambda: "python")
    monkeypatch.setattr(td, "_session_path", lambda: session)
    return orpheus


def _quality(p: Path) -> str:
    return json.loads(p.read_text(encoding="utf-8"))["global"]["general"]["download_quality"]


def _boot(cmd: list[str]) -> str:
    return cmd[cmd.index("-c") + 1]


# ── п.1: коридор вместо общего файла ─────────────────────────────────────────
class TestCorridor:
    def test_tidal_and_spotify_writes_do_not_collide(self, root):
        """Сценарий аварии: параллельные прогоны. Раньше второй перезаписывал
        качество первого, и Tidal уходил качать то, что попросил Spotify."""
        td._update_orpheus_settings("hifi", "D:\\tidal\\", {})
        osp._update_orpheus_settings("normal", "D:\\spotify\\", {})
        assert _quality(td._settings_path()) == "hifi"
        assert _quality(root / "config" / "settings.json") == "normal"

    def test_the_shared_file_is_left_alone(self, root):
        """Общий конфиг — только источник базы: движок Tidal в него не пишет,
        иначе соседние движки лишились бы своих значений."""
        main = root / "config" / "settings.json"
        before = main.read_text(encoding="utf-8")
        td._update_orpheus_settings("hifi", "D:\\tidal\\", {})
        assert main.read_text(encoding="utf-8") == before

    def test_corridor_lives_inside_the_install(self, root):
        sp = td._settings_path()
        assert sp == root / "config" / "tidal" / "settings.json"
        assert sp.is_relative_to(root / "config")

    def test_the_owner_login_settings_are_inherited(self, root):
        """Коридор наследует НЕ только качество: `modules.tidal.*` с клиентскими
        идентификаторами приходят из общего файла — без них вход владельца
        перестал бы обслуживаться."""
        mod = td._tidal_module_settings()
        assert mod["tv_atmos_token"] == "ownerTvCid"
        assert mod["mobile_atmos_hires_token"] == "ownerMobileAtmosCid"
        assert td._tv_client() == ("ownerTvCid", "ownerTvSecret")
        # и чужие модулы едут целиком — прогон не должен получить другой конфиг
        assert json.loads(td._settings_path().read_text(encoding="utf-8"))["modules"]["beatport"]

    def test_owner_edit_of_client_keys_reaches_the_corridor(self, root):
        """Что именно наследуется при живом коридоре: раздел `modules` догоняет
        общий файл сам — по СОДЕРЖИМОМУ, а не по mtime. Правка client_id
        владельцем («сохранил в 4 часа дня») обязана дойти до прогона."""
        assert td._tv_client() == ("ownerTvCid", "ownerTvSecret")
        main = root / "config" / "settings.json"
        shared = json.loads(main.read_text(encoding="utf-8"))
        shared["modules"]["tidal"]["tv_atmos_token"] = "rotatedCid"
        main.write_text(json.dumps(shared), encoding="utf-8")
        assert td._tv_client() == ("rotatedCid", "ownerTvSecret")

    def test_a_newer_shared_file_does_not_revert_this_runs_quality(self, root):
        """Общий файл в конце каждого прогона пишет сам OrpheusDL (core.py:365),
        поэтому «пересеять по mtime» = отдать качество прогона на откуп соседнему
        сервису. Раздел `modules` при этом продолжает догонять."""
        td._update_orpheus_settings("hifi", "D:\\tidal\\", {})
        corridor = td._settings_path()
        main = root / "config" / "settings.json"
        shared = json.loads(main.read_text(encoding="utf-8"))
        shared["global"]["general"]["download_quality"] = "normal"     # прогон Spotify
        shared["modules"]["tidal"]["tv_atmos_token"] = "rotatedCid"    # правка владельца
        main.write_text(json.dumps(shared), encoding="utf-8")
        os.utime(main, ns=(main.stat().st_atime_ns,
                           (main.stat().st_mtime_ns // 10**9 + 10) * 10**9))
        assert _quality(td._settings_path()) == "hifi"
        assert td._tv_client()[0] == "rotatedCid"

    def test_a_broken_shared_file_never_reaches_the_corridor(self, root):
        """Общий файл пишет и сам OrpheusDL на финальной стадии прогона. Обрезок
        скопировался бы в коридор молча — поэтому и посев, и догон проверяют JSON."""
        good = td._settings_path().read_bytes()
        (root / "config" / "settings.json").write_text('{"global": {"gen', encoding="utf-8")
        assert td._settings_path().read_bytes() == good
        # и на свежем месте битый источник не рождает коридор-обрезок
        import shutil
        shutil.rmtree(root / "config" / "tidal")
        assert not td._settings_path().exists()

    def test_seed_leaves_no_temp_files(self, root):
        td._settings_path()
        assert list((root / "config" / "tidal").glob("*.tmp")) == []

    def test_a_broken_corridor_falls_back_to_the_shared_file(self, root):
        """Клиентские идентификаторы нужны уже для входа — если коридор битый,
        читаем общий, а не подменяет их дефолтами OrpheusDL."""
        corridor = td._settings_path()
        corridor.write_text('{"modules": {"tidal": {"tv', encoding="utf-8")
        assert td._tv_client() == ("ownerTvCid", "ownerTvSecret")

    def test_a_neighbors_rotated_token_in_the_corridor_survives(self, root):
        """Догон сужен до одного модуля не случайно: в `modules` соседнего движка
        лежит ротированный refresh-токен Beatport, и общий файл про него не знает —
        вернулся бы оттуда уже ПОГАШЕННЫЙ токен (замер 19.09.2026: с таким токеном
        Beatport отзывает всю цепочку)."""
        corridor = td._settings_path()
        cfg = json.loads(corridor.read_text(encoding="utf-8"))
        cfg["modules"]["beatport"]["refresh_token"] = "rotated-live-token"
        corridor.write_text(json.dumps(cfg), encoding="utf-8")
        main = root / "config" / "settings.json"
        shared = json.loads(main.read_text(encoding="utf-8"))
        shared["modules"]["tidal"]["tv_atmos_token"] = "rotatedCid"
        shared["modules"]["beatport"]["refresh_token"] = "spent-old-token"
        main.write_text(json.dumps(shared), encoding="utf-8")
        after = json.loads(td._settings_path().read_text(encoding="utf-8"))
        assert after["modules"]["tidal"]["tv_atmos_token"] == "rotatedCid"
        assert after["modules"]["beatport"]["refresh_token"] == "rotated-live-token"


# ── п.2: пул — слот получает свой каталог, env не трогается ──────────────────
class TestPoolSlots:
    def test_slot_gets_its_own_config_dir_not_the_corridor(self, root, monkeypatch):
        """Раннер называет движку каталог слота (см. runner.py, `_tidal_config_dir`).
        Устаревание качества в слотах уходит: прогон пишет в каталог слота, и
        посев общего файла там тоже обновляемый."""
        slot_cfg = root / "dist" / "tidal_pool" / "acct2" / "config"
        slot_cfg.mkdir(parents=True)
        (slot_cfg / "settings.json").write_text(
            json.dumps(BASE_SETTINGS), encoding="utf-8")
        corridor_before = root / "config" / "tidal"
        td._update_orpheus_settings("high", "D:\\slot2\\",
                                    {"_tidal_config_dir": str(slot_cfg)})
        assert _quality(slot_cfg / "settings.json") == "high"
        assert not corridor_before.exists(), "слот не должен сейчься в коридор"

    def test_env_is_untouched_by_the_pool(self, root):
        """Ни ORPHEUS_CONFIG_DIR, ни ORPHEUS_SESSION_STORAGE в окружение процесса:
        оно общее на все движки одного интерпретатора."""
        before = {k for k in os.environ if k.startswith("ORPHEUS")}
        slot_cfg = root / "dist" / "tidal_pool" / "acct2" / "config"
        slot_cfg.mkdir(parents=True)
        (slot_cfg / "loginstorage.bin").write_bytes(b"\x80\x04slot2-session")
        td.TidalEngine().build_cmd("https://tidal.com/browse/album/1", "hifi", {
            "tidal-save-path": "D:\\slot2\\", "_tidal_config_dir": str(slot_cfg),
            "_tidal_session_storage": str(slot_cfg / "loginstorage.bin")})
        assert {k for k in os.environ if k.startswith("ORPHEUS")} == before

    def test_slot_config_is_seeded_from_the_shared_file(self, tmp_path, monkeypatch):
        """`ensure_slot` больше не копирует вслепую и не замороживает копию."""
        orpheus = tmp_path / "orpheus"
        (orpheus / "config").mkdir(parents=True)
        (orpheus / "config" / "settings.json").write_text(json.dumps(BASE_SETTINGS),
                                                          encoding="utf-8")
        monkeypatch.setattr(tidal_pool, "_base_dir", lambda: tmp_path)
        d = tidal_pool.ensure_slot(2)
        seeded = json.loads((d / "config" / "settings.json").read_text(encoding="utf-8"))
        assert seeded["modules"]["tidal"]["tv_atmos_token"] == "ownerTvCid"

    def test_an_older_slot_follows_rotated_client_keys(self, tmp_path, monkeypatch):
        """Устаревание слота уходит: у созданного давно слота свои глобальные
        настройки (качество той последней задачи), но ключи учётки догоняют
        общий конфиг — иначе после переустановки OrpheusDL слот качал бы чужими
        client_id."""
        orpheus = tmp_path / "orpheus"
        (orpheus / "config").mkdir(parents=True)
        main = orpheus / "config" / "settings.json"
        main.write_text(json.dumps(BASE_SETTINGS), encoding="utf-8")
        monkeypatch.setattr(tidal_pool, "_base_dir", lambda: tmp_path)
        slot_cfg = tidal_pool.ensure_slot(2) / "config"
        own = json.loads((slot_cfg / "settings.json").read_text(encoding="utf-8"))
        own["global"]["general"]["download_quality"] = "hifi"
        (slot_cfg / "settings.json").write_text(json.dumps(own), encoding="utf-8")

        shared = json.loads(main.read_text(encoding="utf-8"))
        shared["modules"]["tidal"]["tv_atmos_token"] = "rotatedCid"
        main.write_text(json.dumps(shared), encoding="utf-8")

        tidal_pool.ensure_slot(2)
        after = json.loads((slot_cfg / "settings.json").read_text(encoding="utf-8"))
        assert after["modules"]["tidal"]["tv_atmos_token"] == "rotatedCid"
        assert after["global"]["general"]["download_quality"] == "hifi"

    def test_slot_helpers_point_where_the_engine_expects(self, tmp_path, monkeypatch):
        monkeypatch.setattr(tidal_pool, "_base_dir", lambda: tmp_path)
        assert tidal_pool.slot_config_dir(0) == tmp_path / "orpheus" / "config"
        assert tidal_pool.slot_session(0) == tmp_path / "orpheus" / "config" / "loginstorage.bin"
        assert tidal_pool.slot_session(1) == (tmp_path / "dist" / "tidal_pool" / "acct1"
                                              / "config" / "loginstorage.bin")


# ── п.3: сессия ──────────────────────────────────────────────────────────────
class TestSession:
    def test_main_run_pins_the_shared_session(self, root):
        """Вход владельца продолжает работать БЕЗ ручных действий: прогон читает
        ровно тот loginstorage.bin, куда пишет Settings → Tidal."""
        assert td._slot_session_path({}) == root / "config" / "loginstorage.bin"
        cmd = td.TidalEngine().build_cmd("https://tidal.com/browse/album/1", "hifi", {})
        assert ("os.environ['ORPHEUS_SESSION_STORAGE'] = "
                f"{str(root / 'config' / 'loginstorage.bin')!r}") in _boot(cmd)

    def test_a_pool_slot_keeps_its_own_session(self, root):
        """Слот >0 НЕ должен читать сессию владельца: иначе два аккаунта затирали
        бы refresh-токены друг друга в одном pickle."""
        slot_sess = root / "dist" / "tidal_pool" / "acct2" / "config" / "loginstorage.bin"
        cmd = td.TidalEngine().build_cmd("https://tidal.com/browse/album/1", "hifi", {
            "_tidal_config_dir": str(slot_sess.parent),
            "_tidal_session_storage": str(slot_sess)})
        assert f"os.environ['ORPHEUS_SESSION_STORAGE'] = {str(slot_sess)!r}" in _boot(cmd)
        assert str(root / "config" / "loginstorage.bin") not in _boot(cmd)

    def test_the_session_file_is_not_moved_or_rewritten(self, root):
        before = (root / "config" / "loginstorage.bin").read_bytes()
        td._update_orpheus_settings("hifi", "D:\\t\\", {})
        td.TidalEngine().build_cmd("https://tidal.com/browse/album/1", "hifi", {})
        assert (root / "config" / "loginstorage.bin").read_bytes() == before


# ── п.4: команда запуска ─────────────────────────────────────────────────────
class TestLaunchCmd:
    def test_cmd_points_at_the_corridor(self, root):
        cmd = td.TidalEngine().build_cmd("https://tidal.com/browse/album/1", "hifi", {})
        assert ("os.environ['ORPHEUS_CONFIG_DIR'] = "
                f"{str(root / 'config' / 'tidal')!r}") in _boot(cmd)

    def test_url_and_output_still_passed(self, root):
        cmd = td.TidalEngine().build_cmd("https://tidal.com/browse/album/12345",
                                         "lossless", {"tidal-save-path": "D:\\tidal\\"})
        assert cmd[cmd.index("-o") + 1] == "D:\\tidal"
        assert cmd[-1] == "https://tidal.com/browse/album/12345"

    def test_working_dir_still_the_install_root(self, root):
        """CWD менять нельзя: modules/ и extensions/ OrpheusDL ищет относительно
        рабочей папки — коридор задаётся ТОЛЬКО переменной каталога конфига."""
        assert Path(td.TidalEngine().working_dir()) == root

    def test_corridor_needs_the_vendored_patch(self, root):
        """Без патча core.py не смотрит на ORPHEUS_CONFIG_DIR — прогон писал бы
        качество в файл, который не читает. Гард на сам файл на диске.

        OrpheusDL ставится установщиком во время первого запуска, поэтому в
        голом клоне репозитория гард молчит — но не врёт: он обязан сработать на
        живой установке (где его и ловит tests/test_orpheus_config_corridors.py).
        """
        core = (Path(td.__file__).resolve().parent.parent.parent
                / "orpheus" / "orpheus" / "core.py")
        if not core.is_file():
            pytest.skip("OrpheusDL ещё не установлен")
        src = core.read_text(encoding="utf-8")
        assert "os.environ.get('ORPHEUS_CONFIG_DIR')" in src
        assert "os.environ.get('ORPHEUS_SESSION_STORAGE')" in src
