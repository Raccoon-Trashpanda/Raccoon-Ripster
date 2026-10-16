"""Снятая учётка не должна воскресать при следующем сохранении настроек.

03.09.2026 сторож здоровья снял ARL `...947f15`: три прохода подряд Deezer
отвечал «отвергнут», строка ушла в DEAD_ACCOUNTS.txt, запись — из config.yaml.
04.09.2026 ARL снова лежал в конфиге и снова копил streak.

Руками его никто не возвращал. Сторож — отдельный процесс
(tools/ripster_healthcheck.py), а приложение держит конфиг в памяти и при
сохранении пишет config.yaml ЦЕЛИКОМ из своей копии. Копия про внешнюю правку
не знала — и первое же сохранение настроек возвращало мёртвую учётку. Само
удаление работало корректно; отменял его другой процесс.

Тесты держат обе стороны: снятое не возвращается ни через сохранение, ни через
загрузку, а осознанное решение владельца (`unretire`) сильнее автоматики.
"""
import yaml

from ripster import retired_credentials as retired


def _fresh(tmp_path, monkeypatch):
    """Свой каталог реестра на каждый тест — общий файл склеил бы их."""
    monkeypatch.setenv("RIPSTER_BASE_DIR", str(tmp_path))
    return tmp_path


def test_saving_config_does_not_resurrect_a_retired_arl(tmp_path, monkeypatch):
    _fresh(tmp_path, monkeypatch)
    from ripster import config_service as cs

    retired.retire("deezer_arl", "DEAD-ARL", "ARL отвергнут Deezer")
    # Словарь приложения ещё помнит снятую учётку — так и было 03.09.
    live = {"deezer-arl": "MAIN", "deezer-accounts": [{"arl": "DEAD-ARL"}, {"arl": "GOOD"}]}
    cfg_file = tmp_path / "config.yaml"
    cs.save_config(live, cfg_file, tmp_path / "tokens")

    on_disk = yaml.safe_load(cfg_file.read_text(encoding="utf-8"))
    assert [a["arl"] for a in on_disk["deezer-accounts"]] == ["GOOD"]
    # И в памяти тоже — иначе поле в настройках так и осталось бы занятым.
    assert [a["arl"] for a in live["deezer-accounts"]] == ["GOOD"]


def test_primary_slot_is_freed_not_just_emptied_on_disk(tmp_path, monkeypatch):
    _fresh(tmp_path, monkeypatch)
    from ripster import config_service as cs

    retired.retire("deezer_arl", "DEAD-MAIN", "ARL отвергнут Deezer")
    live = {"deezer-arl": "DEAD-MAIN"}
    cfg_file = tmp_path / "config.yaml"
    cs.save_config(live, cfg_file, tmp_path / "tokens")

    assert live["deezer-arl"] == ""
    assert yaml.safe_load(cfg_file.read_text(encoding="utf-8"))["deezer-arl"] == ""


def test_loading_config_ignores_a_retired_arl(tmp_path, monkeypatch):
    """Даже если строка каким-то путём снова оказалась в файле."""
    _fresh(tmp_path, monkeypatch)
    from ripster import config_service as cs

    retired.retire("deezer_arl", "DEAD-ARL", "ARL отвергнут Deezer")
    cfg_file = tmp_path / "config.yaml"
    cfg_file.write_text(yaml.safe_dump(
        {"deezer-arl": "MAIN", "deezer-accounts": [{"arl": "DEAD-ARL"}, {"arl": "GOOD"}]},
        allow_unicode=True), encoding="utf-8")

    merged = cs.load_config(cfg_file, tmp_path / "tokens")
    assert [a["arl"] for a in merged["deezer-accounts"]] == ["GOOD"]


def test_owner_can_bring_a_credential_back(tmp_path, monkeypatch):
    """Продлил подписку — та же строка снова рабочая. Человек старше сторожа."""
    _fresh(tmp_path, monkeypatch)
    retired.retire("deezer_arl", "RENEWED", "ARL отвергнут Deezer")
    assert retired.is_retired("deezer_arl", "RENEWED")
    assert retired.unretire("deezer_arl", "RENEWED") is True
    assert retired.is_retired("deezer_arl", "RENEWED") is False

    cfg = {"deezer-accounts": [{"arl": "RENEWED"}]}
    assert retired.strip_from_config(cfg) == []
    assert cfg["deezer-accounts"] == [{"arl": "RENEWED"}]


def test_secret_is_never_written_to_the_ledger(tmp_path, monkeypatch):
    """Реестр лежит рядом с конфигом и попадает в бэкапы."""
    _fresh(tmp_path, monkeypatch)
    secret = "super-secret-arl-value-1234567890"
    retired.retire("deezer_arl", secret, "ARL отвергнут Deezer")
    raw = retired._path().read_text(encoding="utf-8")
    assert secret not in raw
    assert secret[-6:] in raw          # хвост для опознания — можно


def test_untouched_config_is_left_alone(tmp_path, monkeypatch):
    _fresh(tmp_path, monkeypatch)
    cfg = {"deezer-arl": "A", "deezer-accounts": [{"arl": "B"}, {"arl": "C"}]}
    before = yaml.safe_dump(cfg, allow_unicode=True)
    assert retired.strip_from_config(cfg) == []
    assert yaml.safe_dump(cfg, allow_unicode=True) == before


def test_retired_arl_is_never_offered_by_the_pool(tmp_path, monkeypatch):
    """Между снятием и следующим сохранением учётка ещё лежит в памяти —
    маршрутизация обязана обходить её и в этот промежуток."""
    _fresh(tmp_path, monkeypatch)
    from ripster import deezer_pool as dp

    retired.retire("deezer_arl", "DEAD-ARL", "ARL отвергнут Deezer")
    monkeypatch.setattr(dp, "_warm_health", lambda *_a, **_k: None)
    accounts = dp._configured_accounts(
        {"deezer-arl": "DEAD-ARL", "deezer-accounts": [{"arl": "STILL-GOOD"}]})
    pool = dp.DeezerPool(accounts, tmp_path)
    assert pool.acquire()[1] == "STILL-GOOD"
