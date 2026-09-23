"""Публичный wrapper: ЧЕТЫРЕ состояния, а не точка «зелёная / красная».

21.09.2026 upstream (wm.wol.moe, wrapper-lite v2) перешёл с gRPC на HTTP и
закрылся API-ключом. Проверка здоровья при этом считала «здорово» ЛЮБОМУ
HTTP-коду ниже 500 — а 401 «invalid or missing API key» это 401. То есть
роутер и интерфейс рапортовали порядок ровно тогда, когда путь был закрыт, и
отправить туда задачу значило получить ошибку через полминуты.

Ниже — по тесту на каждое состояние и главный: 401 больше нигде не засчитывается
как «работает». Ответы сервиса подделаны конвертом без единого секрета.
"""
import pytest

from ripster import apple_router as ar


class _Resp:
    def __init__(self, status, body):
        self.status_code = status
        self._body = body

    def json(self):
        if self._body is None:
            raise ValueError("no json")
        return self._body


CFG = {"amd-instance-url": "wm.example", "amd-instance-secure": True}


@pytest.fixture
def serve(monkeypatch):
    """Единственная точка выхода в сеть: `httpx.get` + сброс кэша конверта."""
    def _apply(status=None, body=None, exc=None):
        # Кэш `_pool_envelope` на 60 с пережил бы следующий тест, если его не
        # обнулять перед каждой подменой: второй тест увидел бы ответ первого.
        monkeypatch.setattr(ar, "_pool_env", {})
        monkeypatch.setattr(ar, "_pool_env_ts", 0.0)

        def _get(url, **kw):
            if exc is not None:
                raise exc
            return _Resp(status, body)

        monkeypatch.setattr(ar.httpx, "get", _get)
    return _apply


def test_working_reports_pool_contents(serve):
    serve(status=200, body={"code": 0, "data": {
        "ready": True, "clientCount": 19, "regions": ["cn", "in"]}})
    p = ar.public_wrapper_probe(CFG)
    assert p["state"] == "working" and p["reason"] == ""
    assert p["ready"] is True and p["client_count"] == 19
    assert p["regions"] == ["cn", "in"]


def test_401_is_refusing_not_healthy(serve):
    """Тот самый ответ, на котором старая проверка рисовала «здорово»."""
    serve(status=401, body={"code": -1, "msg": "invalid or missing API key"})
    p = ar.public_wrapper_probe(CFG)
    assert p["state"] == "refusing" and p["reason"] == "api_key"
    assert "API key" in p["detail"]        # причину сообщает сам сервер


def test_403_also_needs_a_key(serve):
    serve(status=403, body={"code": -1, "msg": "forbidden"})
    p = ar.public_wrapper_probe(CFG)
    assert p["state"] == "refusing" and p["reason"] == "api_key"


def test_empty_pool_is_refusing(serve):
    serve(status=200, body={"code": 0, "data": {"ready": False, "clientCount": 0}})
    p = ar.public_wrapper_probe(CFG)
    assert p["state"] == "refusing" and p["reason"] == "pool_empty"


def test_unknown_shape_is_refusing_not_working(serve):
    """Живой HTTP без `ready` — это НЕ «здорово», а «мы перестали понимать».

    Upstream уже переводил протокол с gRPC на HTTP; промолчать здесь значило бы
    снова выставить зелёную точку за собственное незнание.
    """
    serve(status=200, body={"uptime": 123})
    p = ar.public_wrapper_probe(CFG)
    assert p["state"] == "refusing" and p["reason"] == "no_status"


def test_server_error_is_refusing_with_the_code(serve):
    serve(status=503, body={"msg": "overloaded"})
    p = ar.public_wrapper_probe(CFG)
    assert p["state"] == "refusing" and p["reason"] == "http_503"


def test_non_json_body_keeps_the_code(serve):
    serve(status=404, body=None)
    p = ar.public_wrapper_probe(CFG)
    assert p["state"] == "refusing" and p["reason"] == "http_404"


def test_transport_failure_is_unreachable(serve):
    serve(exc=OSError("getaddrinfo failed"))
    p = ar.public_wrapper_probe(CFG)
    assert p["state"] == "unreachable" and p["reason"] == "transport"
    assert "getaddrinfo" in p["detail"]


def test_no_host_is_not_configured():
    p = ar.public_wrapper_probe({"amd-instance-url": "   "})
    assert p["state"] == "not_configured" and p["reason"] == "no_host"


def test_probe_emits_only_advertised_states(serve):
    """Интерфейс знает ровно те состояния, которые умеет выдавать проб.

    Появится пятое, а в PUBLIC_WRAPPER_STATES и в строке настроек его не
    добавят, — пользователь получит пустую плашку вместо ответа.
    """
    seen = set()
    for kw in (dict(status=200, body={"data": {"ready": True}}),
               dict(status=401, body={"msg": "nope"}),
               dict(exc=OSError("down"))):
        serve(**kw)
        seen.add(ar.public_wrapper_probe(CFG)["state"])
    seen.add(ar.public_wrapper_probe({"amd-instance-url": ""})["state"])
    assert seen == set(ar.PUBLIC_WRAPPER_STATES)


def test_public_wrapper_ok_rejects_401(serve, monkeypatch):
    """Главный тест: «можно ли слать туда задачи» — нет при отказе."""
    monkeypatch.setattr(ar, "_public_down", False)
    serve(status=401, body={"code": -1, "msg": "invalid or missing API key"})
    assert ar._public_wrapper_ok(CFG) is False
    serve(status=200, body={"code": 0, "data": {"ready": True, "clientCount": 3}})
    assert ar._public_wrapper_ok(CFG) is True


def test_public_wrapper_ok_respects_the_latch(serve, monkeypatch):
    """Здоровый сервер при поднятой защёлке всё равно «нельзя»: она важнее."""
    monkeypatch.setattr(ar, "_public_down", True)
    monkeypatch.setattr(ar, "_public_next_probe", ar.time.time() + 3600)
    serve(status=200, body={"code": 0, "data": {"ready": True}})
    assert ar._public_wrapper_ok(CFG) is False


def test_cache_holds_one_envelope_per_host(serve, monkeypatch):
    """Один и тот же хост спрашиваем раз в минуту, а не на каждый запрос.

    Волонтёрский сервис дергать каждую секунду — значит самим зарабатывать себе
    «слишком много запросов». Смена хоста обязана сбросить кэш прошлого.
    """
    calls = []

    def _counting(status, body):
        def _get(url, **kw):
            calls.append(url)
            return _Resp(status, body)
        return _get

    monkeypatch.setattr(ar, "_pool_env", {})
    monkeypatch.setattr(ar, "_pool_env_ts", 0.0)
    monkeypatch.setattr(ar.httpx, "get", _counting(200, {"data": {"ready": True}}))
    ar.public_wrapper_probe(CFG)
    ar.public_wrapper_probe(CFG)
    assert len(calls) == 1
    ar.public_wrapper_probe({**CFG, "amd-instance-url": "other.example"})
    assert len(calls) == 2
