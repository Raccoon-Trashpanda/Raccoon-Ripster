"""Сопряжение не должно предлагать адрес, по которому мы не отвечаем.

04.09.2026, живой прогон: телефон получил с ПК адрес http://192.168.1.98:7799,
человек вбил его — «failed to connect». Адрес существовал, а порт на нём был
закрыт: Ripster по умолчанию слушает 127.0.0.1 (`RIPSTER_HOST` в app.py), и все
LAN-адреса машины при этом мертвы. `_endpoints()` перечислял их безусловно.

Проверка достижимости делает настоящее соединение, поэтому здесь её подменяем:
тест про ПРАВИЛО отбора, а не про сеть.
"""
import pytest

from ripster.routes import pairing


@pytest.fixture(autouse=True)
def _no_cache():
    pairing._REACH_CACHE.clear()
    yield
    pairing._REACH_CACHE.clear()


def _patch(monkeypatch, *, lan, listening, remote=""):
    monkeypatch.setattr(pairing, "_lan_ips", lambda: list(lan))
    monkeypatch.setattr(pairing, "_listening_on", lambda ip: ip in listening)
    monkeypatch.setattr(pairing, "_remote_url", lambda: remote)


def test_loopback_only_server_offers_no_lan_address(monkeypatch):
    """Порт закрыт на LAN — таких адресов в списке быть не должно."""
    _patch(monkeypatch, lan=["192.168.1.98", "172.24.64.1"], listening=set())
    eps = pairing._endpoints()
    assert [e for e in eps if e["kind"] == "lan"] == []
    # mDNS ведёт на те же интерфейсы — тоже не предлагаем.
    assert [e for e in eps if e["kind"] == "mdns"] == []


def test_remote_survives_and_leads_when_lan_is_dead(monkeypatch):
    """Туннель — единственный рабочий путь, значит он и первый."""
    _patch(monkeypatch, lan=["192.168.1.98"], listening=set(),
           remote="https://example.serveousercontent.com")
    eps = pairing._endpoints()
    assert eps and eps[0]["kind"] == "remote"


def test_listening_lan_address_is_offered(monkeypatch):
    """Сервер открыт наружу — адрес рабочий, его и предлагаем."""
    _patch(monkeypatch, lan=["192.168.1.98", "172.24.64.1"],
           listening={"192.168.1.98"})
    eps = pairing._endpoints()
    lan = [e["url"] for e in eps if e["kind"] == "lan"]
    assert lan == [f"http://192.168.1.98:{pairing._PORT}"]
    # Живой интерфейс есть → mDNS-имя снова имеет смысл.
    assert any(e["kind"] == "mdns" for e in eps)


def test_unreachable_check_is_cached(monkeypatch):
    """Проверку зовут из /start и из /status — сокет дёргаем не на каждый вызов."""
    calls = {"n": 0}

    def fake_conn(addr, timeout=None):
        calls["n"] += 1
        raise OSError("closed")

    monkeypatch.setattr(pairing.socket, "create_connection", fake_conn)
    assert pairing._listening_on("10.1.2.3") is False
    assert pairing._listening_on("10.1.2.3") is False
    assert calls["n"] == 1, "второй вызов должен прийти из кэша"
