# -*- coding: utf-8 -*-
"""Пул key-серверов своего реле: выбор, тариф, штрафы, честный повтор.

Здесь нет ни одного обращения к Apple и к реальным контейнерам: вместо клиента
подставляется заглушка. Проверяем РЕШЕНИЯ, а не сетевой стек — у кого просить,
когда не просить ни у кого и что при этом сказать клиенту.
"""
import asyncio
import time

import pytest

from ripster import lite, pacing, relay_pool as rp

A = "http://127.0.0.1:12340"
B = "http://127.0.0.1:12341"
CFG = {"relay-instances": [{"url": A, "label": "ca", "account": "ca@ex"},
                           {"url": B, "label": "us", "account": "us@ex"}]}


class FakeLite:
    """Заглушка LiteClient: пишет, ЧТО и СКОЛЬКО раз у него просили."""

    def __init__(self, url, behaviours=None):
        self.url = url
        self.calls = []
        self.behaviours = behaviours or {}

    def close(self):
        pass

    def _do(self, what, *args):
        self.calls.append((what,) + args)
        b = self.behaviours.get(what)
        if isinstance(b, Exception):
            raise b
        return b if b is not None else {"regions": ["us"]}

    def status(self):
        return self._do("status")

    def key(self, adam, uri):
        return self._do("key", adam, uri)

    def m3u8_url(self, adam):
        return self._do("m3u8", adam) or "https://aod-itunes.apple.com/x/master.m3u8"

    def webplayback(self, adam):
        return self._do("webplayback", adam)

    def get_data(self, path, params=None, timeout=None):
        return self._do("get" + path, params)

    def post_data(self, path, payload=None, timeout=None):
        return self._do("post" + path, payload)


@pytest.fixture
def pool(tmp_path, monkeypatch):
    monkeypatch.setenv("RIPSTER_BASE_DIR", str(tmp_path))
    fake: dict[str, FakeLite] = {}

    def _client(url):
        return fake.setdefault(url, FakeLite(url))

    monkeypatch.setattr(rp, "_client", _client)
    rp.reset()
    yield rp, fake
    rp.reset()


def run(coro):
    return asyncio.run(coro)


# ── конфигурация ─────────────────────────────────────────────────────────────

def test_default_pool_is_the_lite_container_we_already_run(pool):
    """Без `relay-instances` реле обязано работать на том единственном
    контейнере, что уже поднят, — иначе «включи реле» означало бы «сначала
    перенастрой полдюжины ключей конфига»."""
    assert rp.instances({"apple-lite-url": "127.0.0.1:12340"}) == [
        {"url": A, "label": "lite", "account": "", "streams": 0}]
    assert rp.instances({})[0]["url"] == A          # дефолт lite.lite_url


def test_instances_skip_disabled_and_broken(pool):
    cfg = {"relay-instances": [
        {"url": A, "enabled": False, "label": "ca"},
        {"url": " ", "label": "мусор"},
        "не словарь",
        {"url": "http://127.0.0.1:12341/", "label": "us", "account": "u@ex",
         "streams": 6},
    ]}
    got = rp.instances(cfg)
    assert len(got) == 1 and got[0]["url"] == B        # слэш в конце не дублирует инстанс
    assert got[0]["streams"] == 6


def test_ident_never_contains_the_url(pool):
    """Имя в dist/pacing.json — хэш, а не адрес: файл лежит в dist/ и
    показывается владельцу в отчёте."""
    inst = rp.instances(CFG)[0]
    ident = rp.ident_of(inst)
    assert ident and len(ident) == 12 and inst["url"] not in ident


# ── тариф и потолок одновременных ────────────────────────────────────────────

def test_cap_follows_the_plan_and_an_explicit_number_outranks_it(pool, monkeypatch):
    monkeypatch.setattr(rp.apple_plan, "cap_for", lambda acct, **k: 6 if acct == "ca@ex" else 1)
    by_acct = {i["account"]: i for i in rp.instances(CFG)}
    assert rp.cap_of(by_acct["ca@ex"]) == 6
    assert rp.cap_of(by_acct["us@ex"]) == 1
    assert rp.cap_of({"url": A, "account": "ca@ex", "streams": 2}) == 2
    # учётка без измерения тарифа → 1, а не «шесть, авось пронесёт»
    assert rp.cap_of({"url": A, "account": "", "streams": 0}) == 1
    monkeypatch.setattr(rp.apple_plan, "cap_for",
                        lambda acct, **k: (_ for _ in ()).throw(RuntimeError("нет файла")))
    assert rp.cap_of({"url": A, "account": "ca@ex", "streams": 0}) == 1


def test_all_busy_raises_busy_with_retry_after(pool):
    for url in (A, B):
        rp._hold(url)
    with pytest.raises(rp.RelayNoInstance) as e:
        rp.pick(CFG)
    assert e.value.reason == "busy" and e.value.retry_after > 0


def test_busy_instance_is_not_picked_while_a_free_one_exists(pool):
    """Занятый в пределах тарифа инстанс не получает лишнего запроса — шесть
    потоков family это шесть, а не «сколько прислали»."""
    rp._hold(A)
    assert rp.pick(CFG)["url"] == B


def test_cooling_instance_is_skipped(pool):
    """429 от враппера → penalty в пейсинге → инстанс не выбирают, и клиент
    получает retry_after, а не бесконечное ожидание."""
    inst = rp.instances(CFG)[0]
    ident = rp.ident_of(inst)
    pacing.penalty(rp.PACE_SERVICE, ident, 429)
    cands = rp.candidates(CFG)
    cooled = [c for c in cands if c["url"] == A][0]
    assert cooled["ready"] is False and cooled["wait"] >= 25.0
    assert rp.pick(CFG)["url"] == B            # просим у того, кто не остывает
    with pytest.raises(rp.RelayNoInstance) as e:
        rp.pick(CFG, exclude=[B])              # а теперь остывают оба
    assert e.value.reason == "cooling" and e.value.retry_after >= 1.0


# ── обход пула ───────────────────────────────────────────────────────────────

def test_key_goes_through_the_cache_path(pool):
    """/key обязан идти в LiteClient.key (кэш + единый замок), а не напрямую в
    HTTP: попадание в кэш — это ноль обращений к Apple."""
    _, fake = pool
    data = {"adamId": "1", "keyUri": "skd://x", "contentKey": "aa", "ctx": "…"}
    fake.setdefault(A, FakeLite(A, {"key": data}))
    fake.setdefault(B, FakeLite(B, {"key": data}))
    got = run(rp.call("key", CFG, {"adamId": "1", "uri": "skd://x"}))
    assert got == data
    assert [c for f in fake.values() for c in f.calls] == [("key", "1", "skd://x")]


def test_m3u8_response_shape_matches_wrapper_lite(pool):
    """Конверт `data` для /m3u8 — {adamId, m3u8}; переименуем поле, и AMDL
    перестанет нас понимать. Проверяем форму, а не нежность к ней."""
    _, fake = pool
    url = "https://aod-itunes.apple.com/1/master.m3u8"
    fake.setdefault(A, FakeLite(A, {"m3u8": url}))
    assert run(rp.call("m3u8", CFG, {"adamId": "7"})) == {"adamId": "7", "m3u8": url}


def test_transport_failure_retries_on_another_instance(pool):
    """Контейнер не ответил — это НЕ ответ Apple: пробуем у соседа и не
    считаем это расходом учётки."""
    _, fake = pool
    dead = FakeLite(A, {"key": lite.LiteError("Wrapper Lite недоступен: ConnectError")})
    live = FakeLite(B, {"key": {"contentKey": "bb"}})
    fake[A], fake[B] = dead, live
    got = run(rp.call("key", CFG, {"adamId": "1", "uri": "skd://x"}))
    assert got == {"contentKey": "bb"}
    assert len(dead.calls) == 1 and len(live.calls) == 1
    assert rp._down_until.get(A), "сдохший инстанс должен быть выведен из выбора"


def test_envelope_error_is_not_retried_on_a_second_account(pool):
    """Враппер ОТВЕТИЛ ошибкой — вторая лицензия на тот же трек положение не
    исправит, а расход учётки удвоит. Повтора обязано не быть."""
    _, fake = pool
    err = lite.LiteError("key retrieval failed", code=500, http_status=500)
    fake.setdefault(A, FakeLite(A, {"key": err}))
    fake.setdefault(B, FakeLite(B, {"key": {"contentKey": "нет, не просили"}}))
    with pytest.raises(rp.RelayUpstreamError) as e:
        run(rp.call("key", CFG, {"adamId": "1", "uri": "skd://x"}))
    assert e.value.code == 500 and e.value.http_status == 500
    assert len(fake[B].calls) == 0


def test_upstream_429_penalizes_only_that_instance(pool):
    """Штраф — по конкретному инстансу: 429 от одного враппера не должен
    сажать весь пул на паузу."""
    _, fake = pool
    fake.setdefault(A, FakeLite(A, {"key": lite.LiteError(
        "too many", code=429, http_status=429)}))
    with pytest.raises(rp.RelayUpstreamError) as e:
        run(rp.call("key", CFG, {"adamId": "1", "uri": "skd://x"}))
    assert e.value.http_status == 429
    ident_a = rp.ident_of({"url": A})
    ident_b = rp.ident_of({"url": B})
    assert pacing.counts(rp.PACE_SERVICE, ident_a)["strikes"] == 1
    assert pacing.counts(rp.PACE_SERVICE, ident_b)["strikes"] == 0


def test_penalty_is_per_instance(pool):
    """Остывающий инстанс не трогает ни чужой успех, ни чужой запрос."""
    _, fake = pool
    ident_a, ident_b = rp.ident_of({"url": A}), rp.ident_of({"url": B})
    pacing.penalty(rp.PACE_SERVICE, ident_a, 429)
    fake.setdefault(A, FakeLite(A, {"key": {"contentKey": "A"}}))
    fake.setdefault(B, FakeLite(B, {"key": {"contentKey": "B"}}))
    assert run(rp.call("key", CFG, {"adamId": "1", "uri": "skd://x"})) == {"contentKey": "B"}
    assert len(fake[A].calls) == 0
    assert pacing.wait_seconds(rp.PACE_SERVICE, ident_a) > 0
    assert pacing.wait_seconds(rp.PACE_SERVICE, ident_b) == 0


def test_expired_penalty_is_cleared_by_a_good_answer(pool):
    """Когда штраф истёк, успешный ответ обязан снять и ступень: иначе одна
    ночная авария Apple держала бы учётку на минимальной скорости до конца
    суток (смысл pardon в pacing.py — ровно этот)."""
    _, fake = pool
    ident_a = rp.ident_of({"url": A})
    pacing.penalty(rp.PACE_SERVICE, ident_a, 429, now=time.time() - 7200)
    assert pacing.counts(rp.PACE_SERVICE, ident_a)["strikes"] == 1
    assert pacing.wait_seconds(rp.PACE_SERVICE, ident_a) == 0
    fake.setdefault(A, FakeLite(A, {"key": {"contentKey": "A"}}))
    run(rp.call("key", {"relay-instances": [{"url": A, "label": "ca"}]},
                {"adamId": "1", "uri": "skd://x"}))
    assert pacing.counts(rp.PACE_SERVICE, ident_a)["strikes"] == 0
    assert len(fake[A].calls) == 1


def test_empty_pool_reports_empty_not_500(pool):
    """Пустой список в `relay-instances` — это «настройки пула нет», и реле
    работает на дефолтном контейнере (см. test_default_pool_...). Пустой ПУЛ
    наступает там, где хозяин список задал, но годных записей в нём не
    осталось: молча подменить его чужим враппером нельзя."""
    assert rp.instances({"relay-instances": []})[0]["url"] == A
    with pytest.raises(rp.RelayNoInstance) as e:
        run(rp.call("key", {"relay-instances": [{"url": " ", "label": "мусор"}]},
                    {"adamId": "1"}))
    assert e.value.reason == "empty"


def test_unknown_endpoint_is_a_programming_error(pool):
    with pytest.raises(rp.RelayUpstreamError):
        run(rp.call("login-for-quota", CFG, {}))


# ── health и снимок ──────────────────────────────────────────────────────────

def test_probe_reports_regions_without_credentials(pool):
    """«Жив» и «понятен» — разные ответы: пустые regions при живом HTTP это
    «враппер не залогинен», и показывать это одной краской с «контейнер лежит»
    нельзя."""
    _, fake = pool
    fake.setdefault(A, FakeLite(A, {"status": {"regions": ["us", "ca"]}}))
    fake.setdefault(B, FakeLite(B, {"status": lite.LiteError(
        "lite вернул не-JSON", code=-1, http_status=200)}))
    h_a = rp.probe({"url": A}, force=True)
    assert h_a["reachable"] and h_a["logged_in"] and h_a["regions"] == ["us", "ca"]
    h_b = rp.probe({"url": B}, force=True)
    assert h_b["reachable"] and not h_b["logged_in"] and h_b["error"]
    assert all("token" not in k.lower() for k in h_a)     # учётных полей нет вовсе


def test_snapshot_reasons_are_distinct(pool, monkeypatch):
    """«Пул пуст», «никто не отвечает» и «все остывают» — три разных ответа
    владельцу; свести их к одной красной точке значит оставить его без
    действия."""
    _, fake = pool
    for url in (A, B):
        fake.setdefault(url, FakeLite(url, {"status": {"regions": ["us"]}}))
    snap = rp.snapshot(CFG, force=True)
    assert snap["ready"] is True and snap["regions"] == ["us"] and snap["reason"] == ""
    assert {i["label"] for i in snap["instances"]} == {"ca", "us"}
    assert all("url" not in i for i in snap["instances"])   # адресов наружу нет

    snap = rp.snapshot({"relay-instances": [{"url": "", "label": "мусор"}]})
    assert snap["ready"] is False and snap["reason"] == "empty"

    for url in (A, B):
        fake[url] = FakeLite(url, {"status": lite.LiteError("нечем дышать")})
    snap = rp.snapshot(CFG, force=True)
    assert snap["reason"] == "down"

    rp._health.clear()
    ident = rp.ident_of({"url": A})
    pacing.penalty(rp.PACE_SERVICE, ident, 429)
    ident_b = rp.ident_of({"url": B})
    pacing.penalty(rp.PACE_SERVICE, ident_b, 403)
    for url in (A, B):
        fake[url] = FakeLite(url, {"status": {"regions": ["us"]}})
    snap = rp.snapshot(CFG, force=True)
    assert snap["ready"] is False and snap["reason"] == "cooling"
