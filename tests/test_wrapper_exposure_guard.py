"""Сторж портов Apple-враппера: разбор публикации, пересоздание, регистрация проверки.

21.09.2026 порт 30020 без авторизации отдавал `dev_token` и media-user-token
активной учётки любому в локальной сети: `docker port amd-wrapper` показывал
0.0.0.0. Причина — не настройка (в ней стоял `127.0.0.1`), а код, который брал
из строки только хвост после двоеточия. `ripster/amd.py` починили, но починка
кода не лечит УЖЕ поднятый контейнер и не лечит любой будущий контейнер,
поднятый не из этого кода. Поэтому `tools/ripster_healthcheck.py` меряет
публикацию, firewall и досягаемость с другой сетевой стеки — а эти тесты
держат в узде сам разбор и пересоздание.

Самое дорогое здесь — `_republish_cmd`: он ПЕРЕСОЗДАЁТ живой контейнер с
персональной identity-папкой учётки. Потерять том — значит угнать аккаунт в
«device limit» (см. skill ripster-apple-wrapper), то есть починить дыру ценой
поломки сервиса. Тесты на это и написаны.
"""
import json
import pathlib
import sys

import pytest

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(errors="replace")
    except Exception:                                   # noqa: BLE001
        pass

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))
_argv = sys.argv
sys.argv = ["ripster_healthcheck.py"]                  # импорт читает флаги
import ripster_healthcheck as hc                       # noqa: E402
sys.argv = _argv


# ── разбор `docker port` ──────────────────────────────────────────────────────

def test_empty_host_is_not_loopback():
    """Главная ловушка: HostIp "" в docker — это «все интерфейсы», а не «что-то
    местное». На этом и получили 0.0.0.0 при верной настройке."""
    assert hc._is_loopback("127.0.0.1")
    assert hc._is_loopback("::1")
    assert hc._is_loopback("[::]") is False
    assert not hc._is_loopback("")
    assert not hc._is_loopback("0.0.0.0")
    assert not hc._is_loopback(None)


def test_parse_docker_port_keeps_worst_interface_per_port():
    out = hc._parse_docker_port(
        "10020/tcp -> 127.0.0.1:10020\n"
        "20020/tcp -> 0.0.0.0:20020\n"
        "20020/tcp -> [::]:20020\n")
    assert out[10020] == (10020, "127.0.0.1")
    # v6-строка не должна перезатирать худший v4-хост, и наоборот
    assert out[20020] == (20020, "0.0.0.0")


def test_parse_docker_port_maps_container_to_host_ports():
    """Порт контейнера и порт хоста РАЗНЫЕ (слот пула: 10021->10020). Публикуем
    обратно обязаны сохранить пару, иначе пересозданный слот смотрит не туда."""
    out = hc._parse_docker_port("10020/tcp -> 0.0.0.0:10021")
    assert out == {10020: (10021, "0.0.0.0")}


def test_port_spec_expansion_covers_ranges_and_lists():
    got = hc._expand_port_specs(hc.WRAPPER_GUARD_PORT_SPECS)
    assert {10020, 10021, 10049, 20020, 20049, 30020} <= got   # слоты пула внутри
    assert 10050 not in got                                     # и без запаса вправо
    assert hc._expand_port_specs(["30020", "", "bad"]) == {30020}


# ── пересоздание контейнера ───────────────────────────────────────────────────

INSPECT = {
    "Config": {
        "Image": "ripster-wrapper",
        "Env": ["args=-H 0.0.0.0 -L someone@example.com:secret", "PATH=/usr/bin"],
        "Entrypoint": None,
        "Cmd": ["bash", "-c", "/app/wrapper $args"],
    },
    "HostConfig": {
        "Binds": ["C:\\dev\\apple_music\\dist\\docker\\rootfs_id\\apca1m2m\\data"
                  ":/app/rootfs/data"],
        "PortBindings": {"10020/tcp": [{"HostIp": "", "HostPort": "10020"}]},
        "RestartPolicy": {"Name": "unless-stopped"},
    },
}


def _cmd_for(inspect=INSPECT):
    spec = hc._run_spec_from_inspect(inspect)
    bindings = hc._parse_docker_port("10020/tcp -> 0.0.0.0:10020\n"
                                     "20020/tcp -> 0.0.0.0:20020")
    return hc._republish_cmd("amd-wrapper", spec, bindings)


def test_republish_publishes_only_loopback():
    cmd = _cmd_for()
    ports = [cmd[i + 1] for i, a in enumerate(cmd) if a == "-p"]
    assert ports == ["127.0.0.1:10020:10020", "127.0.0.1:20020:20020"]
    assert not any(":10020:10020" == p or p.startswith("0.0.0.0") for p in ports)


def test_republish_keeps_the_account_identity_volume():
    """Том — личность учётки. `rootfs_working` вместо `rootfs_id/<login>` — это
    общий baked-in adi.pb и мгновенный «device limit»: починка портов,
    угнавшая аккаунт, хуже непричиненных портов."""
    cmd = _cmd_for()
    vols = [cmd[i + 1] for i, a in enumerate(cmd) if a == "-v"]
    # Binds в docker inspect — уже полная пара «хост:контейнер», переносим её
    # как есть, ничего не доклеивая (иначе получили бы двойной путь).
    assert vols == [INSPECT["HostConfig"]["Binds"][0]]
    assert "rootfs_working" not in " ".join(vols)


def test_republish_keeps_env_entrypoint_and_cmd():
    cmd = _cmd_for()
    envs = [cmd[i + 1] for i, a in enumerate(cmd) if a == "-e"]
    assert envs == ["args=-H 0.0.0.0 -L someone@example.com:secret"]  # PATH не тащим
    assert cmd[-3:] == ["bash", "-c", "/app/wrapper $args"]
    assert "--entrypoint" not in cmd          # у образа его не было
    assert "-e" in cmd and cmd[cmd.index("-e") + 1].startswith("args=")


def test_republish_restores_custom_entrypoint_with_its_args():
    insp = json.loads(json.dumps(INSPECT))
    insp["Config"]["Entrypoint"] = ["/entrypoint.sh", "--keep"]
    insp["Config"]["Cmd"] = ["-c", "sleep 1"]
    cmd = _cmd_for(insp)
    assert cmd[cmd.index("--entrypoint") + 1] == "/entrypoint.sh"
    tail = cmd[cmd.index("ripster-wrapper") + 1:]
    assert tail == ["--keep", "-c", "sleep 1"]


def test_republish_drops_restart_policy():
    """`--restart` врапперу не возвращаем: после ребута контейнер поднимаем сами
    (`ensure_container`, `check_apple_wrapper`), а автоматикой движка порты
    пересоздаются тем, что в момент перезапуска посчитает нужным."""
    assert "--restart" not in _cmd_for()


def test_secrets_never_reach_the_report():
    """Пароль учётки живёт в env контейнера, то есть ЕСТЬ в пересоздающей команде.
    Значит `_heal_publish` не имеет права эхомить ни саму команду, ни её вывод с
    параметрами: ругаться он обязан без них."""
    assert "secret" in " ".join(_cmd_for())          # в команде он есть
    src = (ROOT / "tools" / "ripster_healthcheck.py").read_text(encoding="utf-8")
    heal = src[src.index("def _heal_publish"):src.index("def _lan_ipv4")]
    echoed = [ln.strip() for ln in heal.splitlines()
              if ("bad(" in ln or "warn(" in ln or "fixed(" in ln)
              and "join(cmd" in ln]
    assert not echoed, f"секреты утекают в отчёт: {echoed}"


# ── проверка должна ЖИТЬ в прогоне (правило «мерить провод, а не модуль») ─────

def test_check_is_registered_in_main_sweep():
    """`session_feedback()` в станциях была написана, покрыта тестом и не
    вызывалась ниоткуда. Регистрация — часть починки, а не деталь."""
    src = (ROOT / "tools" / "ripster_healthcheck.py").read_text(encoding="utf-8")
    main = src[src.index("def main():"):]
    assert "check_wrapper_exposure()" in main
    # ПОСЛЕ того, что контейнеры поднимает: `docker start`/`_heal_wrapper`
    # пересоздают враппер и обязаны попадать под замер.
    assert main.index("check_apple_wrapper()") < main.index("check_wrapper_exposure()")
    assert main.index("check_apple_pool_slots()") < main.index("check_wrapper_exposure()")


def test_wrapper_container_filter_covers_pool_slots(monkeypatch):
    """Слоты пула (`rip-wrapper-N`) текут так же, как основной враппер, и обязаны
    попасть под проверку, а не остаться «не нашей зоной»."""
    def fake_docker(*args, **kw):
        assert args[:2] == ("ps", "--format")
        return 0, "app\namd-wrapper\nrip-wrapper-1\nrip-wrapper-3\ntg-bot-api\n"
    monkeypatch.setattr(hc, "_docker", fake_docker)
    assert hc._wrapper_containers() == ["amd-wrapper", "rip-wrapper-1", "rip-wrapper-3"]


def test_firewall_problem_is_computed_from_coverage(monkeypatch):
    """Правило, накрывающее только 10020/20020/30020, — это НЕ починка: слоты
    пула остаются открытыми. Проверка считает покрытие множеством, а не «есть ли
    вообще правило»."""
    monkeypatch.setattr(hc, "_firewall_guard_state", lambda: {
        "known": True, "exists": True, "enabled": True, "action": "block",
        "ports": hc._expand_port_specs(("10020", "20020", "30020"))})
    monkeypatch.setattr(hc, "_wrapper_containers", lambda: [])
    monkeypatch.setattr(hc, "_lan_ipv4", lambda: "")
    # Заводить настоящее правило Windows из теста никто не должен — только
    # регистрируем, что проверка ПОПРОСИЛА это сделать.
    asked = []
    monkeypatch.setattr(hc, "_ensure_firewall_guard",
                        lambda: (asked.append(1) or (False, "тест не имеет прав")))
    hc._report.clear()
    hc.check_wrapper_exposure()
    assert any("выпадают" in line for line in hc._report), hc._report
    assert asked, "непокрытые порты обязаны заводить правило, а не только шуметь"


def test_full_coverage_rule_is_reported_healthy(monkeypatch):
    monkeypatch.setattr(hc, "_firewall_guard_state", lambda: {
        "known": True, "exists": True, "enabled": True, "action": "block",
        "ports": hc._expand_port_specs(hc.WRAPPER_GUARD_PORT_SPECS)})
    monkeypatch.setattr(hc, "_wrapper_containers", lambda: [])
    monkeypatch.setattr(hc, "_lan_ipv4", lambda: "")
    hc._report.clear()
    hc.check_wrapper_exposure()
    assert any("закрыты извне" in line for line in hc._report), hc._report
    assert not any(line.startswith(("⚠️", "❌")) for line in hc._report), hc._report
