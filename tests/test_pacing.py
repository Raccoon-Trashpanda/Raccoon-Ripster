"""Пейсинг запросов (ripster/pacing.py) — счётчики, потолки, штраф и ПРОВОД.

Проверяем не только модуль: тест на проводе доказывает, что гейт реально стоит
в пути раскрытия ссылки, а не написан рядом. Модуль без вызывающего — это ровно
тот класс дефектов, на котором уже обжигались (`session_feedback` в станциях).
"""
import json
import time

import pytest

from ripster import pacing


@pytest.fixture(autouse=True)
def _isolated_state(tmp_path, monkeypatch):
    """Каждый тест — свой `dist/`, иначе счётчики перетекают из теста в тест
    и «ноль запросов» начинает зависеть от порядка прогонов."""
    monkeypatch.setenv("RIPSTER_BASE_DIR", str(tmp_path))


def _raw() -> dict:
    try:
        return json.loads(pacing._path().read_text(encoding="utf-8"))
    except Exception:                                       # noqa: BLE001
        return {}


def _napper(sink: list):
    """Подмена `asyncio.sleep`: и счёт отдаёт, и по-настоящему не спит — иначе
    тест «спим ровно ступень штрафа» шёл бы полдня."""
    async def _sleep(seconds, *a, **kw):
        sink.append(seconds)
    return _sleep


# ── потолки ──────────────────────────────────────────────────────────────────

def test_caps_default_and_are_documented():
    assert pacing.caps("apple") == {"hour": 1500, "day": 15000}
    assert pacing.caps("qobuz_playlist") == {"hour": 120, "day": 1200}
    assert pacing.caps("relay-upstream") == {"hour": 1200, "day": 12000}
    # ключей конфига ровно столько, сколько сервисов, и все они объявлены в
    # config_service (см. следующий тест)
    keys = {d["cfg_hour"] for d in pacing.DEFAULTS.values()} | \
           {d["cfg_day"] for d in pacing.DEFAULTS.values()}
    assert keys == {"apple-requests-per-hour", "apple-requests-per-day",
                    "qobuz-playlist-per-hour", "qobuz-playlist-per-day",
                    "relay-upstream-per-hour", "relay-upstream-per-day"}


def test_config_service_declares_the_pacing_keys():
    """Потолок без строки в дефолтах — настройка, которой нет: её не покажут и
    не сохранят. Проверяем связку, а не только свой модуль."""
    from ripster import config_service, security
    for svc in pacing.DEFAULTS.values():
        for key in (svc["cfg_hour"], svc["cfg_day"]):
            assert key in config_service.DEFAULT_CONFIG, f"{key} не объявлен в дефолтах"
            assert any(str(key).startswith(p)
                       for p in security.CONFIG_WRITABLE_PREFIXES), \
                f"{key} не проходит белый список записи — поле сохранялось бы «успешно» и молча"


def test_caps_config_wins_zero_means_off():
    cfg = {"apple-requests-per-hour": 10, "apple-requests-per-day": 0}
    assert pacing.caps("apple", cfg) == {"hour": 10, "day": 0}


def test_caps_of_an_unregistered_service_is_no_cap():
    """Сервис, которого нет в DEFAULTS, — не ошибка, а «потолка нет». Раньше
    `caps` падал KeyError('cfg_hour') на первом же обращении нового сервиса и
    ронял дорогу целиком, а не только лимит."""
    assert pacing.caps("never_seen_before") == {"hour": 0, "day": 0}
    assert pacing.wait_seconds("never_seen_before", "acct", {}) == 0.0
    assert pacing.blocked("never_seen_before", "acct", {}) is False


def test_caps_garbage_falls_back_to_default():
    """Мусор в конфиге не обязан ронять приложение или молча снимать потолок:
    дефолт остаётся."""
    assert pacing.caps("apple", {"apple-requests-per-hour": "много"})["hour"] == 1500


# ── счётчики ─────────────────────────────────────────────────────────────────

def test_note_counts_hour_and_day():
    now = time.time()
    for _ in range(3):
        pacing.note("apple", "acct", now=now)
    c = pacing.counts("apple", "acct", now=now)
    assert (c["hour"], c["day"]) == (3, 3)


def test_buckets_roll_over_by_label_not_by_elapsed():
    """Счётчик сверяется по метке бакета. Иначе запрос, начатый в 23:59:59,
    на следующий час выглядел бы как «в этом часе уже 5000» — и потолок ударил
    бы по учётке, которая ничего не делала."""
    t0 = 1_800_000_000.0
    pacing.note("apple", "acct", now=t0)
    assert pacing.counts("apple", "acct", now=t0)["hour"] == 1
    assert pacing.counts("apple", "acct", now=t0 + 3599)["hour"] == 1
    assert pacing.counts("apple", "acct", now=t0 + 3600)["hour"] == 0
    # сутки — своим порогом, и час в новых сутках считается заново
    assert pacing.counts("apple", "acct", now=t0 + 86400)["day"] == 0


def test_identities_do_not_share_counters():
    pacing.note("apple", "acct-a", now=1_800_000_000.0)
    b = pacing.counts("apple", "acct-b", now=1_800_000_000.0)
    assert (b["hour"], b["day"]) == (0, 0)


def test_counters_survive_restart():
    """Счётчики на диске: перезапуск приложения не должен обнулять сутки, иначе
    суточный потолок обещает одно, а считает другое."""
    pacing.note("apple", "acct", now=time.time())
    assert _raw()["apple|acct"]["nd"] == 1


# ── мягкие потолки ───────────────────────────────────────────────────────────

def test_hour_overflow_waits_until_the_hour_rolls():
    cfg = {"apple-requests-per-hour": 2, "apple-requests-per-day": 0}
    now = 1_800_000_000.0 + 100.0            # середина часа
    for _ in range(2):
        pacing.note("apple", "acct", now=now)
    w = pacing.wait_seconds("apple", "acct", cfg, now=now)
    assert w == 3500.0            # ровно до переката часа, а не «примерно час»
    assert not pacing.blocked("apple", "acct", cfg, now=now)   # час ≠ сутки


def test_day_overflow_refuses_instead_of_sleeping():
    cfg = {"apple-requests-per-hour": 0, "apple-requests-per-day": 2}
    now = time.time()
    for _ in range(3):
        pacing.note("apple", "acct", now=now)
    assert pacing.blocked("apple", "acct", cfg, now=now) is True
    # повторный отказ не удваивает работу — он просто не спрашивает
    assert pacing.blocked("apple", "acct", cfg, now=now) is True
    assert _raw()["apple|acct"]["refused"] == 2


def test_blocked_records_nothing_when_cap_off():
    now = time.time()
    pacing.note("apple", "acct", now=now)
    assert pacing.blocked("apple", "acct", {}, now=now) is False
    assert "refused" not in _raw()["apple|acct"]


# ── штраф после 429/403 ──────────────────────────────────────────────────────

def test_penalty_doubles_then_freezes_at_the_ceiling():
    now = time.time()
    got = [pacing.penalty("apple", "acct", 429, now=now) for _ in range(3)]
    assert got == [30.0, 60.0, 120.0]
    for _ in range(20):
        pacing.penalty("apple", "acct", 403, now=now)
    assert pacing.penalty("apple", "acct", 429, now=now) == pacing.PENALTY_MAX_S


def test_penalty_ignores_status_codes_about_credentials():
    """401 — протухший токен, а не частота; штрафовать за него значит лечить не
    ту болезнь и жечь паузу, которой не просили."""
    for status in (200, 401, 404, 500):
        assert pacing.penalty("apple", "acct", status) == 0.0
    assert pacing.counts("apple", "acct")["strikes"] == 0


def test_pardon_clears_penalty_and_wait_reflects_it():
    now = time.time()
    pacing.penalty("apple", "acct", 429, now=now)
    assert pacing.wait_seconds("apple", "acct", {}, now=now) > 0
    pacing.pardon("apple", "acct", now=now)
    assert pacing.wait_seconds("apple", "acct", {}, now=now) == 0.0
    assert pacing.counts("apple", "acct", now=now)["strikes"] == 0


def test_outcome_counts_then_punishes_or_forgives():
    now = time.time()
    assert pacing.outcome("apple", "acct", 429) > 0
    assert pacing.counts("apple", "acct", now=now)["hour"] == 1
    pacing.outcome("apple", "acct", 200)
    c = pacing.counts("apple", "acct", now=now)
    assert c["hour"] == 2 and c["strikes"] == 0     # успех снял штраф


# ── allow(): то, что реально тормозит вызывающего ────────────────────────────

@pytest.mark.asyncio
async def test_allow_refuses_when_the_day_cap_is_eaten():
    cfg = {"apple-requests-per-hour": 0, "apple-requests-per-day": 1}
    pacing.note("apple", "acct", now=time.time())
    assert await pacing.allow("apple", "acct", cfg) is False
    # отказ НЕ считается запросом: счётчик стоит на месте, а «отказ» — в своей
    # графе, иначе потолок сам себя кормил бы и никогда не перекатывался
    assert pacing.counts("apple", "acct")["day"] == 1


@pytest.mark.asyncio
async def test_allow_refuses_rather_than_sleeping_forever(monkeypatch):
    """Проспать перекат суток — это «задача зависла» для владельца, а зависшая
    задача потом повторяет запрос. Честнее отказать."""
    cfg = {"apple-requests-per-hour": 1, "apple-requests-per-day": 0}
    slept = []
    monkeypatch.setattr(pacing.asyncio, "sleep", _napper(slept))
    now = 1_800_000_000.0 + 100.0                           # середина часа
    pacing.note("apple", "acct", now=now)
    assert await pacing.allow("apple", "acct", cfg, now=now) is False
    assert slept == []                                      # и не спал ни секунды


@pytest.mark.asyncio
async def test_single_request_is_not_delayed_by_the_min_gap(monkeypatch):
    """Ровный шаг между запросами нужен в БУРЕ (веер ссылок), а не в одиночном
    «открой мне эту ссылку». Иначе UI и тесты платят за защиту, которой пока не
    от чего защищаться."""
    slept = []
    monkeypatch.setattr(pacing.asyncio, "sleep", _napper(slept))
    assert await pacing.allow("apple", "acct", {}, now=time.time()) is True
    assert slept == []


@pytest.mark.asyncio
async def test_burst_keeps_the_min_gap(monkeypatch):
    slept = []
    monkeypatch.setattr(pacing.asyncio, "sleep", _napper(slept))
    now = time.time()
    for _ in range(pacing.BURST_MIN_PER_HOUR):
        pacing.note("apple", "acct", now=now)
    assert await pacing.allow("apple", "acct", {}, now=now) is True
    assert slept == [pacing.MIN_GAP_S]


@pytest.mark.asyncio
async def test_allow_sleeps_the_penalty_then_lets_through(monkeypatch):
    slept = []
    monkeypatch.setattr(pacing.asyncio, "sleep", _napper(slept))
    now = time.time()
    pacing.penalty("apple", "acct", 429, now=now)
    assert await pacing.allow("apple", "acct", {}, now=now) is True
    assert slept == [30.0]          # ровно ступень штрафа, а не «примерно»


@pytest.mark.asyncio
async def test_amp_album_tracks_is_gated_by_pacing(monkeypatch):
    """Третья точка — треклист альбома внутри движка zhaarey: до шести страниц
    заходом на КАЖДУЮ задачу очереди. Здесь особенно важно, что отказ означает
    пустой список, а не исключение: вызывающий и без amp-api живёт с iTunes-
    результатом."""
    from ripster.engines.zhaarey import ZhaereyEngine

    async def boom(*a, **kw):
        raise AssertionError("страницы amp-api пошли поверх отказа пейсинга")

    asked = {}

    async def fake_allow(service, ident="", config=None, **kw):
        asked["service"] = service
        return False

    monkeypatch.setattr(pacing, "allow", fake_allow)
    monkeypatch.setattr("httpx.AsyncClient", boom)
    eng = ZhaereyEngine()
    cfg = {"authorization-token": "bearer-y", "media-user-token": "mut-y"}
    assert await eng._amp_album_tracks("123", "US", cfg) == []
    assert asked["service"] == "apple"


# ── что видит владелец ───────────────────────────────────────────────────────

def test_ident_for_never_carries_the_token():
    tok = "eyJhbGciOi.private-secret-value"
    ident = pacing.ident_for(tok)
    assert ident and len(ident) == 12
    assert tok not in ident and "private" not in ident
    assert ident == pacing.ident_for(tok)          # стабильно: счётчик тот же
    assert pacing.ident_for("") == ""


def test_snapshot_masks_accounts_and_lists_caps():
    cfg = {"apple-requests-per-hour": 5, "apple-requests-per-day": 7}
    now = time.time()
    pacing.note("apple", "abc123def456", now=now)
    pacing.penalty("apple", "abc123def456", 429, now=now)
    rows = pacing.snapshot(cfg, now=now)
    assert len(rows) == 1
    r = rows[0]
    assert r["service"] == "apple"
    assert r["account"] == "..." + "abc123def456"[-8:]      # хвост, не ключ
    assert (r["hour"], r["hour_cap"], r["day"], r["day_cap"]) == (1, 5, 1, 7)
    assert r["strikes"] == 1 and r["penalty_left"] > 0


def test_snapshot_ignores_unknown_services():
    """Файл состояния мог пережить удаление сервиса; в отчёт владельцу он тогда
    не приходит, чтобы не выглядеть нерасшифрованным мусором."""
    pacing.note("something_else", "x", now=time.time())
    assert pacing.snapshot() == []


def test_state_file_holds_no_secrets():
    tok = "media-user-token-value"
    pacing.note("apple", pacing.ident_for(tok), now=time.time())
    assert tok not in pacing._path().read_text(encoding="utf-8")


# ── ПРОВОД: гейт обязан стоять в пути раскрытия ссылки ───────────────────────

@pytest.mark.asyncio
async def test_apple_playlist_is_gated_by_pacing(monkeypatch):
    """Если `allow` сказал «нет», запрос не уходит вообще: не «ошибка», а пустой
    список — тем `_apple_playlist` и без пейсинга умеет возвращаться."""
    from ripster import resolver

    monkeypatch.setitem(resolver._cfg, "authorization-token", "bearer-x")
    monkeypatch.setitem(resolver._cfg, "media-user-token", "mut-x")
    seen = {}

    async def fake_allow(service, ident="", config=None, **kw):
        seen["call"] = (service, ident)
        return False

    async def boom(*a, **kw):
        raise AssertionError("HTTP-запрос ушёл поверх отказа пейсинга")

    monkeypatch.setattr(pacing, "allow", fake_allow)
    monkeypatch.setattr(resolver._HTTP, "ashared", boom)
    assert await resolver._apple_playlist("u", "us", "pl.1") == []
    assert seen["call"][0] == "apple"
    assert seen["call"][1] == pacing.ident_for("mut-x")     # считаем ПО УЧЕТКЕ


@pytest.mark.asyncio
async def test_qobuz_playlist_is_gated_by_its_own_service(monkeypatch):
    from ripster import resolver

    called = []

    async def fake_allow(service, ident="", config=None, **kw):
        called.append(service)
        return False

    async def boom(*a, **kw):
        raise AssertionError("запрос к playlist/get ушёл поверх отказа")

    monkeypatch.setattr(pacing, "allow", fake_allow)
    monkeypatch.setattr(resolver._HTTP, "ashared", boom)
    assert await resolver._qobuz_playlist("pl1", "app", {}) == []
    assert called == ["qobuz_playlist"]


@pytest.mark.asyncio
async def test_playlist_reports_its_outcome_to_pacing(monkeypatch):
    """Счётчик без кода ответа бесполезен: 429 обязан дойти до `outcome`, иначе
    штраф ни за что не наступит и «защита» останется украшением."""
    from ripster import resolver

    class _Resp:
        status_code = 429
        text = ""

        def json(self):
            return {}

    class _Client:
        async def get(self, *a, **kw):
            return _Resp()

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

    logged = []
    monkeypatch.setitem(resolver._cfg, "authorization-token", "bearer-x")
    monkeypatch.setitem(resolver._cfg, "media-user-token", "")

    async def fake_allow(service, ident="", config=None, **kw):
        return True

    def fake_outcome(service, ident="", status=0):
        logged.append((service, ident, status))
        return 0.0

    monkeypatch.setattr(pacing, "allow", fake_allow)
    monkeypatch.setattr(pacing, "outcome", fake_outcome)
    monkeypatch.setattr(resolver._HTTP, "ashared", lambda: _Client())
    assert await resolver._apple_playlist("u", "us", "pl.1") == []
    assert logged and logged[0][0] == "apple" and logged[0][2] == 429
