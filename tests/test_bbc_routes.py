"""BBC route pure helpers: image URLs, timecode/duration parsing, name sanitize,
match tokenization, the MixesDB relevance scorer (episode-number gating — what
stops a stranger's tracklist from matching), and CUE generation."""
from ripster.routes import bbc


# ── _img ─────────────────────────────────────────────────────────────────────
def test_img():
    assert bbc._img("http://x/{recipe}.jpg") == "http://x/320x320.jpg"
    assert bbc._img("http://x/{recipe}.jpg", 640) == "http://x/640x640.jpg"
    assert bbc._img("") == ""


# ── _parse_timecodes ─────────────────────────────────────────────────────────
def test_parse_timecodes():
    out = bbc._parse_timecodes("00:00 Intro\n03:45 Track Two\n1:02:30 Track Three")
    assert [t["seconds"] for t in out] == [0, 225, 3750]
    assert out[0]["title"] == "Intro"
    assert out[2]["title"] == "Track Three"


# ── _parse_dur ───────────────────────────────────────────────────────────────
def test_parse_dur():
    assert bbc._parse_dur(7200) == 7200
    assert bbc._parse_dur({"value": 3600, "label": "1h"}) == 3600
    assert bbc._parse_dur(None) == 0
    assert bbc._parse_dur({}) == 0


# ── _safe ────────────────────────────────────────────────────────────────────
def test_safe():
    assert bbc._safe("Hello/World:Mix") == "Hello_World_Mix"
    assert bbc._safe(' a*b?c "x" ') == "a_b_c _x_"   # only special chars → _, spaces kept


# ── _match_toks / _match_nums ────────────────────────────────────────────────
def test_match_toks():
    toks = bbc._match_toks("Anjunadeep 500 Live")
    assert "anjunadeep" in toks and "500" in toks   # lowercased alnum tokens
    assert "a" not in bbc._match_toks("a bb")        # single-char dropped


def test_match_nums():
    assert bbc._match_nums("Edition 500") == {"500"}
    assert bbc._match_nums("a1b22c") == {"1", "22"}


# ── _score_mixesdb_hit (episode-number gating) ───────────────────────────────
def test_score_right_episode_beats_wrong():
    q_title, q_artist = "Anjunadeep Edition 500", ""
    right = bbc._score_mixesdb_hit(q_title, q_artist, {"artist": "", "show": "Anjunadeep Edition 500"})
    wrong = bbc._score_mixesdb_hit(q_title, q_artist, {"artist": "", "show": "Anjunadeep Edition 499"})
    assert right > 0.5 > wrong       # right confident, wrong rejected by number gate
    assert right > wrong


# ── _sec_to_ts ───────────────────────────────────────────────────────────────
def test_sec_to_ts():
    assert bbc._sec_to_ts(0) == "0:00"
    assert bbc._sec_to_ts(225) == "3:45"
    assert bbc._sec_to_ts(3750) == "1:02:30"


# ── _build_cue ───────────────────────────────────────────────────────────────
def test_build_cue():
    cue = bbc._build_cue("My Mix", "DJ", [
        {"offset": 0, "title": "T1", "artist": "A1"},
        {"offset": 225, "title": "T2"},   # no artist → defaults to mix artist
    ])
    assert 'TITLE "My Mix"' in cue
    assert 'PERFORMER "DJ"' in cue
    assert "TRACK 01 AUDIO" in cue and "TRACK 02 AUDIO" in cue
    assert 'PERFORMER "A1"' in cue        # per-track artist
    assert "INDEX 01 00:00:00" in cue     # track 1 at 0s
    assert "INDEX 01 03:45:00" in cue     # track 2 at 225s


# ── Прогноз записи: что человек узнаёт ДО того, как поставил план ─────────────
#
# Это не «украшательство карточки»: у планировщика одно обещание на весь цикл,
# и оно обязано быть измеренным числом. Здесь и проверяется, что в ответ уходят
# числа с сервера, а «не знаем» не превращается в «320».

import asyncio                                        # noqa: E402
import json                                           # noqa: E402
from datetime import datetime, timedelta, timezone    # noqa: E402
from types import SimpleNamespace                     # noqa: E402

from ripster import bbc_quality as _Q                 # noqa: E402
from ripster import bbc_schedule as _bs               # noqa: E402

LIVE_320 = {"ok": True, "best_kbps": 320, "best_codec": "AAC-LC", "lc": True,
            "has_320_lc": True, "url": "https://example.invalid/master.m3u8",
            "variants": [{"kbps": 320, "codec": "AAC-LC", "lc": True}],
            "checked_utc": "2026-09-21T19:13:00Z"}
LIVE_DEAD = {"ok": False, "best_kbps": 0, "best_codec": "", "lc": False,
             "error": "http_403", "variants": []}

SLOT = "2027-01-01T03:00:00Z"


def _forecast(monkeypatch, *, ladder=None, first=None, own=None,
              channel="bbc_radio_one", start=SLOT, dur=7200, pid=""):
    """bbc._forecast с заглушками вместо сети и вместо наших журналов."""
    if ladder is not None:
        async def ladders(chs):
            return {c: dict(ladder) for c in chs}
        monkeypatch.setattr(bbc, "_live_ladders", ladders)
    if first is not None:
        async def fb(pid_, c):
            return first
        monkeypatch.setattr(bbc, "_first_broadcast", fb)
    if own is not None:
        monkeypatch.setattr(bbc, "_own_copy", lambda pid_: dict(own))
    return asyncio.run(bbc._forecast(channel, start, dur, pid))


def test_forecast_answers_with_the_measured_ladder(monkeypatch):
    fc = _forecast(monkeypatch, ladder=LIVE_320, pid="m00315gp")
    assert fc["live"]["has_320_lc"] is True and fc["live"]["best_kbps"] == 320
    # адрес потока — тот, с которого пишет движок: числа без адреса бесполезны,
    # человеку надо видеть, ЧТО именно мерили
    assert fc["stream"] == LIVE_320["url"]
    assert fc["channel"] == "bbc_radio_one" and fc["duration"] == 7200
    assert fc["ondemand"]["ceiling_kbps"] == _Q.ONDEMAND_CEILING


def test_forecast_says_it_does_not_know_instead_of_promising_320(monkeypatch):
    """Поток не прочитался (403 на мастер, сеть легла) — в ответе не должно
    найтись выдуманного качества: план на две ночи с ложным «320» человек
    заметит только утром, по файлу в 96 кбит/с."""
    fc = _forecast(monkeypatch, ladder=LIVE_DEAD)
    assert fc["live"]["ok"] is False and fc["live"]["best_kbps"] == 0
    assert fc["live"]["error"] == "http_403"
    assert fc["repeat"] == {"state": "unknown"}       # pid не дали — спрашивать нечего
    assert fc["own"] == {"checked": False}


def test_forecast_tells_a_repeat_from_a_premiere(monkeypatch):
    """Признак — first_broadcast_date из programmes JSON: у премьеры она равна
    слоту, у повтора остаётся прежней датой (промер 21.09.2026 по 35 будущим
    выпускам пяти брендов: 7…53 дня)."""
    old = datetime(2026, 11, 20, 22, 0, tzinfo=timezone.utc)
    rp = _forecast(monkeypatch, ladder=LIVE_320, pid="m00315gp", first=old)["repeat"]
    assert rp["state"] == "repeat" and rp["days"] == 42
    assert rp["first_broadcast_utc"] == "2026-11-20T22:00:00Z"
    fresh = datetime(2027, 1, 1, 3, 0, tzinfo=timezone.utc)
    assert _forecast(monkeypatch, ladder=LIVE_320, pid="m00316zz",
                     first=fresh)["repeat"]["state"] == "debut"
    # данные не пришли — «неизвестно», а не вежливое «похоже на премьеру»
    async def nothing(pid_, c):
        return None
    monkeypatch.setattr(bbc, "_first_broadcast", nothing)
    assert _forecast(monkeypatch, ladder=LIVE_320, pid="x1")["repeat"]["state"] == "unknown"


def test_forecast_reports_the_copy_we_already_hold(monkeypatch):
    own = {"checked": True, "have": True, "source": "schedule", "title": "Essential Mix",
           "ts": "2026-09-20T03:00:00Z", "status": "recorded",
           "verdict_state": "as_promised", "kbps": 320, "codec": "AAC-LC"}
    fc = _forecast(monkeypatch, ladder=LIVE_320, pid="m00315gp", own=own)
    assert fc["own"]["have"] is True
    # если копия уже записана И проверена — число видно и в подтверждении:
    # вторая запись того же выпуска ради «а вдруг теперь даст 320» — ровно та
    # ошибка, которую этот экран и обязан предотвращать
    assert fc["own"]["kbps"] == 320 and fc["own"]["verdict_state"] == "as_promised"


def test_forecast_leaks_no_signed_ondemand_urls(monkeypatch):
    """MediaSelector-адреса on-demand подписаны и живут минуты. Из прогноза
    наружу уходит ровно один URL — рабочий адрес live-потока; on-demand
    представляется числом (потолком), а не ссылкой."""
    fc = _forecast(monkeypatch, ladder=LIVE_320, pid="m00315gp")
    urls = [v for v in (fc["stream"], fc["live"].get("url", "")) if v]
    assert set(urls) == {LIVE_320["url"]}
    assert "url" not in fc["ondemand"]
    assert not [v for v in fc["live"]["variants"] if "url" in v]


# ── Повтор по данным сетки (без запроса к BBC) и наши копии ───────────────────

def _dt(y, m, d):
    return datetime(y, m, d, 3, 0, tzinfo=timezone.utc)


def test_repeat_from_published_needs_two_days():
    assert bbc._repeat_from_published(_dt(2027, 1, 5), "2026-12-01T00:00:00Z") is True
    assert bbc._repeat_from_published(_dt(2027, 1, 5), "2027-01-05T00:00:00Z") is False
    # день в день — ещё не повтор: у BBC разметка в локальном времени эфира
    assert bbc._repeat_from_published(_dt(2027, 1, 5), "2027-01-04T00:00:00Z") is False
    assert bbc._repeat_from_published(_dt(2027, 1, 5), "") is False


def test_own_copy_reads_our_own_journals(tmp_path, monkeypatch):
    """Есть ли у нас этот выпуск — решаем по НАШИМ файлам (планы, история,
    манифест), а не по запросу к BBC: ответ должен быть и тогда, когда до BBC
    не достучаться."""
    store = _bs.ScheduledStore(tmp_path / "bbc_scheduled.json")
    _bs.install(store=store, queue=[], qs=SimpleNamespace(lock=asyncio.Lock()),
                config={}, broadcast=None, process_queue=None,
                make_task=None, queue_snapshot=None)
    monkeypatch.setattr(bbc, "_own_journals",
                        lambda: [tmp_path / "history.json",
                                 tmp_path / "downloads_manifest.json"])
    row = store.add(channel="bbc_radio_one", start_utc=_bs.utcnow() + timedelta(days=1),
                    duration=7200, title="Essential Mix", pid="m00315gp")
    store.mark(row["id"], status="recorded",
               verdict={"state": "as_promised", "kbps": 320, "codec": "AAC-LC"})
    (tmp_path / "history.json").write_text(json.dumps(
        [{"url": "https://www.bbc.co.uk/sounds/play/m00316zz", "quality": "mp3"}]),
        encoding="utf-8")

    got = bbc._own_copy("m00315gp")
    assert (got["checked"], got["have"], got["source"]) == (True, True, "schedule")
    assert got["kbps"] == 320 and got["verdict_state"] == "as_promised"
    assert bbc._own_copy("m00316zz")["source"] == "history.json"
    # отсутствие честно ограничено: журналы вращаются (history — последние 500),
    # поэтому «не нашли», а не «такой записи нет»
    assert (bbc._own_copy("m0000000")["checked"],
            bbc._own_copy("m0000000")["have"]) == (True, False)
    assert bbc._own_copy("") == {"checked": False}
