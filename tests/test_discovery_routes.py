"""Search (discovery) route: pure cover-URL builders. Most of discovery is async
network search (covered indirectly); the unit-testable surface is the cover
helpers. NOTE: `_ym_cover`/`_tidal_cover` functionally duplicate the same-named
engine helpers (differ only in default size / docstring) — flagged for the final
cross-cutting dedup pass, not merged here (cross-module coupling, small gain)."""
from ripster.routes import discovery as d


def test_ym_cover_default_400():
    assert d._ym_cover("avatars.yandex/x%%.jpg") == "https://avatars.yandex/x400x400.jpg"
    assert d._ym_cover("http://x%%.jpg", "200x200") == "http://x200x200.jpg"
    assert d._ym_cover("") == ""


def test_tidal_cover():
    assert d._tidal_cover("a-b-c") == "https://resources.tidal.com/images/a/b/c/160x160.jpg"
    assert d._tidal_cover("a-b-c", 320) == "https://resources.tidal.com/images/a/b/c/320x320.jpg"
    assert d._tidal_cover("") == ""


def test_sp_cover_picks_medium():
    imgs = [{"url": "big-640"}, {"url": "med-300"}, {"url": "small-64"}]
    assert d._sp_cover(imgs) == "med-300"          # index 1 (medium) to cut traffic


def test_sp_cover_single_image():
    assert d._sp_cover([{"url": "only"}]) == "only"


def test_sp_cover_empty():
    assert d._sp_cover([]) == ""


# ── Convert-first artist-guard (wrong same-titled album by another artist) ────

def test_same_artist_rejects_different_artist():
    # Real bug: Spotify "New Love" by Kloyd matched Deezer "New Love" by Ziggy
    # Alberts (same title, different artist) → wrong album delivered.
    assert d._same_artist("Kloyd", "Ziggy Alberts") is False
    assert d._same_artist("The Weeknd", "The Beatles") is False   # 'the' is a stopword


def test_same_artist_accepts_same_or_variant():
    assert d._same_artist("Kloyd", "Kloyd") is True
    assert d._same_artist("Daft Punk", "Daft Punk feat. Pharrell") is True
    assert d._same_artist("Ziggy Alberts", "Ziggy Alberts (Live)") is True
    assert d._same_artist("A$AP Rocky", "ASAP Rocky") is True     # punctuation-normalised


def test_same_artist_unknown_source_allows():
    # Spotify geo-block leaves only the oembed title (no artist) → can't guard,
    # so we must NOT block every fuzzy result (would kill VPN convert-first).
    assert d._same_artist("", "Dua Lipa") is True


def test_same_artist_missing_candidate_artist_rejected():
    # We know the source artist but the candidate has none → unconfirmed → reject.
    assert d._same_artist("Dua Lipa", "") is False


def test_artist_tokens_drops_stopwords():
    assert d._artist_tokens("The The") == set()          # all stopwords → empty
    assert "feat" not in d._artist_tokens("Kloyd feat. X")


# ── Бюджет фоновых запросов Spotify + независимость ▶ от Spotify ──────────────
#
# Баг 23.09: радар фоново сверял лейблы тем же app-токеном, что и интерактивное
# ▶; обход выбивал 429, токен уходил в бан на часы, а ▶ отвечало сырым JSON
# «лимит запросов Spotify — блокировка на 2 ч 11 мин». Три инварианта ниже:
#   1) фон останавливается на ПЕРВОМ 429 и фиксирует бан;
#   2) интерактивное ▶ под баном уходит на Deezer-копию, а не в отказ;
#   3) когда копии нет — отказ выглядит человекочитаемым объектом i18n, не дампом.
import pytest
from fastapi import HTTPException


class _Resp:
    def __init__(self, status_code=200, payload=None, headers=None):
        self.status_code = status_code
        self._payload = payload if payload is not None else {}
        self.headers = headers or {}
        self.content = b"x" if status_code else b""

    def json(self):
        return self._payload


class _Router:
    """Клиент-заглушка: отвечает по подстроке URL, считает обращения к Spotify."""
    def __init__(self, routes, default=None):
        self.routes, self.default = routes, default or _Resp(404)
        self.hits = []

    def _match(self, url):
        for key, resp in self.routes.items():
            if key in url:
                return resp
        return self.default

    async def get(self, url, **kw):
        self.hits.append(url)
        r = self._match(url)
        return r() if callable(r) else r

    @property
    def spotify_hits(self):
        return [h for h in self.hits if "api.spotify.com" in h]

    @property
    def deezer_hits(self):
        return [h for h in self.hits if "api.deezer.com" in h]


class _Ctx:
    def __init__(self, client):
        self.client = client
    async def __aenter__(self):
        return self.client
    async def __aexit__(self, *a):
        return False


async def _token():
    return "app-token"


@pytest.fixture(autouse=True)
def _reset_sp_state():
    d._sp_rate_limit_until = 0.0
    d._sp_bg_state_reset()
    yield
    d._sp_rate_limit_until = 0.0
    d._sp_bg_state_reset()


def test_bg_admit_enforces_gap():
    # Первое разрешение есть, следующее сразу — нет: фон обязан дышать, а не долбить.
    assert d._sp_bg_admit() is True
    assert d._sp_bg_admit() is False


@pytest.mark.asyncio
async def test_background_stops_at_first_429(monkeypatch):
    # Сетка лейблов: сверка `_sp_real_labels` должна погаснуть на первом 429 —
    # один запрос, бан записан, дальше в стену не идём.
    monkeypatch.setattr(d, "_sp_label_cache_load", lambda: None)
    monkeypatch.setattr(d, "_sp_label_cache_save", lambda: None)
    monkeypatch.setattr(d, "_get_spotify_app_token", _token)
    d._sp_label_cache.clear()
    d._SP_ALBUMS_BATCH_OK = True

    client = _Router(
        {"api.spotify.com": lambda: _Resp(429, {}, {"Retry-After": "3600"})})
    monkeypatch.setattr(d._HTTP, "ashared", lambda: _Ctx(client))

    labels, verified = await d._sp_real_labels(["a1", "a2", "a3"])

    assert verified is False                      # «не проверили», а не «не сошлось»
    assert d._sp_is_rate_limited() is True        # бан зафиксирован
    assert len(client.spotify_hits) == 1          # остановка на ПЕРВОМ 429


@pytest.mark.asyncio
async def test_interactive_play_while_banned_falls_back_to_deezer(monkeypatch):
    # ▶ по Spotify-ссылке под баном: трек-лист берём с Deezer по имени и играем.
    import ripster.engines as eng_mod

    class _Eng:
        async def get_album(self, album_id, config):
            return {"error": "лимит запросов Spotify", "error_key": "err.sp_rl_hr",
                    "error_args": {"h": 2, "m": 11}}
    monkeypatch.setattr(eng_mod, "get_engine", lambda name: _Eng())

    client = _Router({
        "api.deezer.com/search/album": _Resp(200, {"data": [{
            "id": 777, "title": "Songbook", "artist": {"name": "Jane Doe"},
            "cover_medium": "cov"}]}),
        "api.deezer.com/album/777/tracks": _Resp(200, {"data": [{
            "id": 1, "title": "Track One", "artist": {"name": "Jane Doe"},
            "duration": 210, "link": "https://deezer/1",
            "disk_number": 1, "track_position": 1}]}),
    })
    monkeypatch.setattr(d._HTTP, "ashared", lambda: _Ctx(client))

    d._sp_rate_limit_until = d._time.time() + 7200   # под баном
    out = await d.api_release_expand("spotify", "https://open.spotify.com/album/XYZ",
                                     title="Songbook", artist="Jane Doe")

    assert out["ok"] and out["via"] == "deezer"
    assert out["tracks"][0]["playable_service"] == "deezer"
    assert not client.spotify_hits                    # Spotify не трогали вообще


@pytest.mark.asyncio
async def test_banned_play_without_copy_is_human_not_json(monkeypatch):
    # Под баном и без копии на Deezer — отказ обязан быть объектом i18n-контракта
    # {key, params, msg}, а не сырым дампом; и он называет причину словами.
    import ripster.engines as eng_mod

    class _Eng:
        async def get_album(self, album_id, config):
            return {"error": "лимит запросов Spotify", "error_key": "err.sp_rl_hr",
                    "error_args": {"h": 2, "m": 11}}
    monkeypatch.setattr(eng_mod, "get_engine", lambda name: _Eng())

    client = _Router({"api.deezer.com/search/album": _Resp(200, {"data": []})})  # копии нет
    monkeypatch.setattr(d._HTTP, "ashared", lambda: _Ctx(client))

    d._sp_rate_limit_until = d._time.time() + 7200
    with pytest.raises(HTTPException) as ei:
        await d.api_release_expand("spotify", "https://open.spotify.com/album/XYZ",
                                   title="Songbook", artist="Jane Doe")

    assert ei.value.status_code == 503
    det = ei.value.detail
    assert isinstance(det, dict) and det.get("key") == "err.play_sp_banned_noalt"
    assert det.get("msg")                                   # человекочитаемый fallback
    assert "t" in det.get("params", {})                     # «до HH:MM» — причина


# ── Провод до интерфейса: отказ показывается человеком, а не JSON-дампом ──────

def _read(root, *parts):
    import pathlib
    return (pathlib.Path(__file__).resolve().parents[1].joinpath(root, *parts)
            ).read_text(encoding="utf-8")


def test_playrelease_renders_detail_not_raw_json():
    body = _read("static", "js", "sc_tab.js")
    fn = body[body.index("async function playRelease"):body.index("async function playRelease") + 3000]
    # Тот самый баг: `r.text()` + `.slice(0, 120)` печатал `{"detail":{…}}` человеку.
    assert "r.text()" not in fn, "playRelease не должен печатать сырое тело ответа"
    assert "errText" in fn, "playRelease обязан резолвить detail через i18n-контракт"


def test_panel_api_resolves_error_detail():
    body = _read("static", "panel", "panel.js")
    assert "function errText" in body, "панели нужен резолвер detail"
    # prPlay ловит Error из api(); api() обязан разворачивать {key,params,msg}.
    assert "errText(b && b.detail)" in body


def test_play_error_i18n_keys_present_both_languages():
    for path, root in (("static/js/i18n.js", "static"), ("static/panel/i18n.panel.js", "static")):
        txt = _read(root, *path.split("/")[1:])
        for key in ("err.play_sp_banned_noalt", "err.play_sp_failed_noalt"):
            assert txt.count("'%s'" % key) >= 2, f"{path}: {key} должен быть в ru И en"


