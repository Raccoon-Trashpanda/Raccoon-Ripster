"""`GET /api/pair/lyrics` — лестница текстов ПК по запросу телефона.

Мобильный Ripster видит один общественный источник (LRCLIB). Владелец снова и
снова слышал «Текст не найден» там, где на ПК текст доставали подписки (Tidal,
Spotify, Deezer, Apple). Этот маршрут отдаёт телефону результат той же лестницы.

Что проверяем ПРОВОДОМ, а не вызовом функции:
  1. без device-токена — 401 (префикс `/api/pair/` публичный, гейт обязателен);
  2. в ответе есть текст и имя источника;
  3. в ответе НЕТ секретов: форма ответа фиксирована (synced/plain/source),
     ключи и токены учёток наружу не проходят, даже если источник их вернул;
  4. пустой запрос не гоняет лестницу впустую.

Играем через TestClient по образцу test_pairing_share_optin.py.
"""
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from ripster.routes import discovery, pairing


class _Ctx:
    """Контекст минимальный: конфиг, каталог, save_*. `ripster-instance-id`
    задан, иначе `_pc_id()` позовёт save_config и потянет запись config.yaml."""

    def __init__(self, base_dir):
        self.config = {"ripster-instance-id": "pc-under-test", "device-name": "PC"}
        self.save_config = lambda cfg: None
        self.base_dir = str(base_dir)
        self.broadcast = None
        self.queue = []
        self.watchlist = []
        self.download_history = []
        self.save_history = lambda h: None


PHONE = {"x-test-client": "phone"}          # запрос «с телефона»: не петля, без owner-cookie
SECRET = "SECRET-ARL-NEVER-LEAVES-PC"

# Вызовы лестницы пишутся сюда: фикстура — генератор, и повесить список на
# сам `client` внутри неё нельзя (`client` там — имя функции-фикстуры).
LADDER_CALLS: list = []


def _auth(tok):
    return {**PHONE, "Authorization": f"Bearer {tok}"}


@pytest.fixture
def client(tmp_path, monkeypatch):
    """Лестницу подменяем: тест про ПРОВОД и гейт, а не про живые подписки."""
    monkeypatch.setattr(pairing, "_lan_ips", lambda: [])
    monkeypatch.setattr(pairing, "_remote_url", lambda: "")

    async def fake_ladder(artist="", track="", album="", duration=0, isrc="",
                          words=True, **_kw):
        LADDER_CALLS.append({"artist": artist, "track": track,
                             "duration": duration, "isrc": isrc,
                             "album": album, "words": words})
        # `words=True` означало бы, что телефон платит за пословную лировку
        # второй раз, плюс тянет Apple-токен в путь, которого быть не должно.
        return {"synced": "[00:12.00]some line", "plain": "some line",
                "source": "tidal", "arl": SECRET, "token": SECRET}

    LADDER_CALLS.clear()
    monkeypatch.setattr(discovery, "lyrics_ladder", fake_ladder)

    app = FastAPI()
    pairing.install(app, _Ctx(tmp_path))

    @app.middleware("http")
    async def _locals(request, call_next):
        if request.headers.get("x-test-client") != "phone":
            request.scope["client"] = ("127.0.0.1", 0)
        return await call_next(request)

    with TestClient(app) as c:
        yield c


def _pair(client):
    code = client.post("/api/pair/start").json()["code"]
    r = client.post("/api/pair/claim", headers=PHONE,
                    json={"code": code, "mobile_id": "mob-1", "name": "Pixel"})
    assert r.status_code == 200, r.text
    return r.json()["token"]


# ── гейт ─────────────────────────────────────────────────────────────────────

def test_unpaired_phone_is_refused(client):
    """Публичный префикс `/api/pair/`: без токена путь к лестнице закрыт."""
    for r in (client.get("/api/pair/lyrics", headers=PHONE,
                         params={"artist": "CHVRCHES", "track": "Roses"}),
              # телефон, а не петля: без `x-test-client` тестовая прослойка
              # выдала бы запрос за локальный UI владельца, и он прошёл бы бы
              # по owner-ветке гейта — проверяем именно чужой телефон.
              client.get("/api/pair/lyrics", headers={**PHONE, "Authorization": "Bearer nope"},
                         params={"artist": "CHVRCHES", "track": "Roses"})):
        assert r.status_code == 401, r.text
        assert r.json() == {"error": "unauthorized"}, "401 без лишней информации"
    assert LADDER_CALLS == [], "отказ обязан предшествовать работе лестницы"


def test_paired_phone_gets_lyrics_and_source(client):
    tok = _pair(client)
    r = client.get("/api/pair/lyrics", headers=_auth(tok),
                   params={"artist": "CHVRCHES", "track": "Roses",
                           "duration": 215, "isrc": "GBDLP1500080"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["synced"] == "[00:12.00]some line"
    assert body["plain"] == "some line"
    assert body["source"] == "tidal"
    call = LADDER_CALLS[-1]
    assert (call["artist"], call["track"], call["duration"], call["isrc"]) == \
        ("CHVRCHES", "Roses", 215, "GBDLP1500080")
    assert call["words"] is False, "пословную лирику телефон просит отдельным запросом"


def test_owner_without_device_token_is_allowed(client):
    """Владелец с локального UI (owner-cookie) — тоже свой путь к лестнице."""
    r = client.get("/api/pair/lyrics",
                   params={"artist": "CHVRCHES", "track": "Roses"})
    assert r.status_code == 200, r.text
    assert r.json()["source"] == "tidal"


# ── ответ не содержит секретов ───────────────────────────────────────────────

def test_response_shape_carries_no_secrets(client):
    """Форма ответа фиксирована: даже если источник вернул ARL/токен, наружу
    уходит только текст и имя источника."""
    tok = _pair(client)
    r = client.get("/api/pair/lyrics", headers=_auth(tok),
                   params={"artist": "CHVRCHES", "track": "Roses"})
    assert set(r.json()) == {"synced", "plain", "source"}
    assert SECRET not in r.text


def test_missing_artist_or_track_skips_the_ladder(client):
    """Пустой запрос — пустой ответ, без обращения к подпискам."""
    tok = _pair(client)
    before = len(LADDER_CALLS)
    for params in ({"artist": "CHVRCHES", "track": ""},
                   {"artist": "", "track": "Roses"}):
        r = client.get("/api/pair/lyrics", headers=_auth(tok), params=params)
        assert r.status_code == 200, r.text
        assert r.json() == {"synced": "", "plain": "", "source": ""}
    assert len(LADDER_CALLS) == before


def test_broken_ladder_answers_empty_not_leaky(client, monkeypatch):
    """Упавший источник не должен ни ронять маршрут, ни показывать internals."""

    async def boom(**_kw):
        raise RuntimeError(f"auth failed with {SECRET}")

    monkeypatch.setattr(discovery, "lyrics_ladder", boom)
    tok = _pair(client)
    r = client.get("/api/pair/lyrics", headers=_auth(tok),
                   params={"artist": "CHVRCHES", "track": "Roses"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["synced"] == "" and body["plain"] == "" and body["source"] == ""
    assert SECRET not in r.text
