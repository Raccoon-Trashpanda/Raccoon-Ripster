# -*- coding: utf-8 -*-
"""Контракты download canary: серии, РОВНО ОДНО сообщение на смену состояния,
восстановление, часы без обнуления на рестарте и проверка скачанного файла.

Сеть, движки и Telegram не нужны: download_one/`_notify` подменены — тесты
ловят ровно те ошибки, из-за которых канарейку и заводили 24.09.2026
(12 часов «Invalid CKC» никто не заметил): недомер, лишнее молчание и спам.
"""
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))
import download_canary as C   # noqa: E402


@pytest.fixture(autouse=True)
def isolate(tmp_path, monkeypatch):
    monkeypatch.setattr(C, "STATE_FILE", tmp_path / "canary_state.json")
    yield tmp_path


class Recorder:
    def __init__(self):
        self.sent = []

    async def __call__(self, text, enabled=True):
        if enabled:
            self.sent.append(text)


def _svc(st, svc):
    return st["services"].setdefault(svc, {})


# ── серии и оповещения ───────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_two_plain_failures_alert_once_three_stay_silent():
    """Первое падение — молчим (одиночный сбой сети не будит владельца),
    второе подряд — РОВНО ОДНО письмо, третье — уже не пишем."""
    rec = Recorder()
    st = {"version": 1, "services": {}}
    orig = C._notify
    C._notify = rec
    try:
        await C.record_results(st, {"qobuz": {"ok": False, "error": "connection reset"}})
        assert rec.sent == []                       # 1-е падение — тихо
        await C.record_results(st, {"qobuz": {"ok": False, "error": "connection reset"}})
        assert len(rec.sent) == 1                   # 2-е — одно письмо
        assert "QOBUZ" in rec.sent[0] and "мертво" in rec.sent[0]
        for _ in range(5):
            await C.record_results(st, {"qobuz": {"ok": False, "error": "connection reset"}})
        assert len(rec.sent) == 1                   # больше — молчим до смены состояния
        assert st["services"]["qobuz"]["streak_fail"] == 7
    finally:
        C._notify = orig


@pytest.mark.asyncio
async def test_dead_account_signature_alerts_on_first_failure():
    """Invalid CKC — это НЕ «может, сеть»: 24.09 этот вердикт висел 12 часов.
    Первая же подпись «учётка мертва» будит владельца сразу."""
    rec = Recorder()
    st = {"version": 1, "services": {}}
    orig = C._notify; C._notify = rec
    try:
        await C.record_results(st, {"apple": {"ok": False,
                                              "error": "Failed to obtain CKC: Invalid CKC"}})
        assert len(rec.sent) == 1
        assert "APPLE" in rec.sent[0]
        assert "учётка мертва" in rec.sent[0]
    finally:
        C._notify = orig


@pytest.mark.asyncio
async def test_recovery_sends_exactly_one_message_and_resets():
    """«Восстановлен» — одно письмо, и только если до этого ПИСАЛИ про падение.
    Тихая серия из одного сбоя не требует тихого же отчёта о выздоровлении."""
    rec = Recorder()
    st = {"version": 1, "services": {}}
    orig = C._notify; C._notify = rec
    try:
        # падение без оповещения (1-й сбой, не «мёртвая подпись»)
        await C.record_results(st, {"deezer": {"ok": False, "error": "timeout"}})
        await C.record_results(st, {"deezer": {"ok": True, "file": "t.flac",
                                               "duration": 169.2, "codec": "flac"}})
        assert rec.sent == []                       # восстанавливать было нечего
        # теперь как в бою: две неудачи → down → успех → ровно одно «восстановлен»
        await C.record_results(st, {"deezer": {"ok": False, "error": "timeout"}})
        await C.record_results(st, {"deezer": {"ok": False, "error": "timeout"}})
        assert len(rec.sent) == 1
        await C.record_results(st, {"deezer": {"ok": True, "file": "t.flac",
                                               "duration": 169.2, "codec": "flac"}})
        assert len(rec.sent) == 2 and "восстановлен" in rec.sent[1]
        await C.record_results(st, {"deezer": {"ok": True, "file": "t.flac",
                                               "duration": 169.2, "codec": "flac"}})
        assert len(rec.sent) == 2                   # повторных «ожил» не бывает
        assert st["services"]["deezer"]["streak_fail"] == 0
    finally:
        C._notify = orig


@pytest.mark.asyncio
async def test_message_is_html_escaped(monkeypatch):
    """Ошибка движка прилетает живьём и содержит <> — неэкранированный <
    ломает ВСЁ сообщение Telegram, и владелец не получает ничего (2026-08-07)."""
    seen = {}
    async def fake_post(text, enabled=True):
        seen["text"] = text
    orig = C._notify; C._notify = fake_post
    try:
        st = {"version": 1, "services": {}}
        _svc(st, "tidal")["alerted"] = None
        await C.record_results(st, {"tidal": {"ok": False,
                                              "error": "<urlopen error _ssl.c:1063>"}})
        await C.record_results(st, {"tidal": {"ok": False, "error": "<urlopen error _ssl.c:1063>"}})
        assert "&lt;urlopen" in seen["text"] and "<urlopen" not in seen["text"].replace("&lt;", "")
    finally:
        C._notify = orig


# ── состояние и часы ─────────────────────────────────────────────────────────

def test_state_roundtrip(tmp_path):
    st = {"version": 1, "services": {"apple": {"ok": True, "streak_fail": 0}}}
    C.save_state(st)
    assert C.load_state() == st


def test_clock_survives_restart():
    """Рестарт через 2.5 часа после прохода = спать полчаса, а не три часа
    (урок 09.08 с watchlist: обнулённые часы = канарейка, которая никогда
    не успевает спеть до следующей аварии)."""
    now = datetime.now(timezone.utc)
    st = {"last_run": (now - timedelta(hours=2, minutes=30)).strftime(
        "%Y-%m-%dT%H:%M:%SZ"), "services": {}}
    assert 1700 <= C.next_delay(st, now=now) <= 1900
    assert C.next_delay({"services": {}}, now=now) == C.BOOT_GRACE      # никогда не было → скоро
    old = {"last_run": (now - timedelta(hours=9)).strftime(
        "%Y-%m-%dT%H:%M:%SZ"), "services": {}}
    assert C.next_delay(old, now=now) == 0.0                            # просрочено → немедленно


# ── проверка файла ───────────────────────────────────────────────────────────

def _touch(tmp: Path, name: str, size: int) -> Path:
    p = tmp / name
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(b"x" * size)
    return p


def test_verify_rejects_stub_and_honours_expectations(tmp_path, monkeypatch):
    track = {"duration_s": 169, "codecs": ["flac"], "quality": "flac"}
    # нет файлов
    r = C.verify_downloads(tmp_path, track, {})
    assert not r["ok"] and "ни одного аудиофайла" in r["error"]
    # файл-заглушка меньше порога
    _touch(tmp_path, "stub.flac", 900)
    r = C.verify_downloads(tmp_path, track, {})
    assert not r["ok"] and "не загрузка" in r["error"]
    # нормальный размер, но длительность из ffprobe мимо ±2 с
    _touch(tmp_path, "good.flac", 200_000)
    monkeypatch.setattr(C, "probe_media", lambda p, cfg: {"codec": "flac", "duration": 30.1})
    r = C.verify_downloads(tmp_path, track, {})
    assert not r["ok"] and "длительность" in r["error"]
    # кодек не тот: 30-секундный preview отдали вместо FLAC
    monkeypatch.setattr(C, "probe_media", lambda p, cfg: {"codec": "aac", "duration": 169.0})
    r = C.verify_downloads(tmp_path, track, {})
    assert not r["ok"] and "кодек" in r["error"]
    # всё честно
    monkeypatch.setattr(C, "probe_media", lambda p, cfg: {"codec": "flac", "duration": 169.4})
    r = C.verify_downloads(tmp_path, track, {})
    assert r["ok"] and r["codec"] == "flac" and r["size"] == 200_000


# ── wire: канарейка идёт СОБСТВЕННЫМ путём задач и убирает за собой ──────────

@pytest.mark.asyncio
async def test_download_one_uses_runner_and_cleans_temp(monkeypatch, tmp_path):
    """Проверяем ПРОВОД: download_one зовёт runner.run_task (тот же код, что
    у гостя), кладёт путь в _canary_save_path и сносит temp целиком — даже
    когда задача упала."""
    from ripster import runner
    calls = []

    canary_root = tmp_path / "ripster_canary"

    monkeypatch.setattr(C.tempfile, "gettempdir", lambda: str(tmp_path))

    async def fake_run_task(task):
        calls.append(dict(task))
        d = Path(task["_canary_save_path"]); d.mkdir(parents=True, exist_ok=True)
        (d / "track.flac").write_bytes(b"y" * 150_000)
        task["status"] = "done"

    monkeypatch.setattr(runner, "run_task", fake_run_task)
    monkeypatch.setattr(C, "probe_media", lambda p, cfg: {"codec": "flac", "duration": 169.0})
    res = await C.download_one("deezer", {"url": "https://www.deezer.com/track/1",
                                          "duration_s": 169, "codecs": ["flac"]}, {})
    assert res["ok"], res
    assert calls and calls[0]["source"] == "download_canary"
    assert calls[0]["service"] == "deezer" and calls[0]["url"].startswith("https://")
    assert not canary_root.joinpath("deezer").exists()   # за собой сметено

    # и при падении — тоже сметено, и вердикт честный
    async def failing_run_task(task):
        d = Path(task["_canary_save_path"]); d.mkdir(parents=True, exist_ok=True)
        (d / "partial.flac").write_bytes(b"y" * 150_000)
        task["status"] = "error"; task["error"] = "Invalid CKC"

    monkeypatch.setattr(runner, "run_task", failing_run_task)
    res = await C.download_one("deezer", {"url": "https://www.deezer.com/track/1",
                                          "duration_s": 169, "codecs": ["flac"]}, {})
    assert not res["ok"] and "Invalid CKC" in res["error"]
    assert not canary_root.joinpath("deezer").exists()

    # без task["error"] диагноз собирается из хвоста лога (24.09: soundcloud
    # вернулся «error» с пустым полем — вердикт был глухой «статус error»)
    async def silent_fail(task):
        task["status"] = "error"
        task["log"] = ["строка движка", "ORPHEUS_NOT_AUTHED"]
    monkeypatch.setattr(runner, "run_task", silent_fail)
    res = await C.download_one("deezer", {"url": "https://www.deezer.com/track/1",
                                          "duration_s": 169, "codecs": ["flac"]}, {})
    assert not res["ok"] and "ORPHEUS_NOT_AUTHED" in res["error"]


@pytest.mark.asyncio
async def test_download_one_follows_requeue(monkeypatch, tmp_path):
    """Авто-повтор/перебор учёток возвращают run_task со статусом «queued» —
    в приложении задачу подхватывает очередь, канарейка обязана подхватить
    сама, иначе временная авария засчитается падением (24.09: tidal упал
    «queued», хотя на следующем слоте качался)."""
    from ripster import runner
    rounds = {"n": 0}
    monkeypatch.setattr(C.tempfile, "gettempdir", lambda: str(tmp_path))

    async def fake_run_task(task):
        rounds["n"] += 1
        if rounds["n"] == 1:
            task["status"] = "queued"
            task["_retry_count"] = 1
            return
        d = Path(task["_canary_save_path"]); d.mkdir(parents=True, exist_ok=True)
        (d / "track.flac").write_bytes(b"y" * 150_000)
        task["status"] = "done"

    monkeypatch.setattr(runner, "run_task", fake_run_task)
    monkeypatch.setattr(C, "probe_media", lambda p, cfg: {"codec": "flac", "duration": 169.0})
    res = await C.download_one("deezer", {"url": "https://www.deezer.com/track/1",
                                          "duration_s": 169, "codecs": ["flac"]}, {})
    assert res["ok"] and rounds["n"] == 2


def test_probe_media_ignores_cover_art_stream(monkeypatch):
    """Обложка в контейнере — поток mjpeg, НЕ звук. Без codec_type в
    `-show_entries` ffprobe его не возвращает, и фильтрующий
    .get(...,"audio") молча пропускал обложку первым (yandex-прогон 24.09)."""
    class _R:
        stdout = '{"streams": [{"codec_name": "mjpeg", "codec_type": "video"},' \
                 ' {"codec_name": "aac", "codec_type": "audio"}],' \
                 ' "format": {"duration": "170.1"}}'
        returncode = 0
    seen = {}
    def fake_run(cmd, **kw):
        seen["cmd"] = cmd
        return _R()
    monkeypatch.setattr(C.subprocess, "run", fake_run)
    info = C.probe_media("fictitious.mp4", {})
    assert "codec_type" in " ".join(seen["cmd"]), "ffprobe не вернёт codec_type — фильтр вслепую"
    assert info["codec"] == "aac" and abs(info["duration"] - 170.1) < 0.01


def test_probe_media_args_are_what_ffprobe_accepts(tmp_path, monkeypatch):
    """Прогон НАСТОЯЩИМ ffprobe, без моков.

    Мок выше проверял наличие `codec_type` в argv и пропускал строку
    `stream=codec_name:codec_type:format=duration`: ffprobe разбирает
    `-show_entries` так, что ДВОЕТОЧИЕМ делятся СЕКЦИИ, а ЗАПЯТОЙ — поля
    внутри секции. На неверном разделителе он печатал
    «No match for section 'codec_type'» в stderr, пустой stdout и rc=1, а
    парсер молча возвращал duration 0.0 — canary валил здоровые файлы
    «длительность 0.0 с ≠ 169.0 с». 24.09.2026 на этом прогоне лежали
    qobuz, deezer и yandex, то есть пройтись не мог ни один сервис.

    Гарантия кода, которую держит этот тест: argv разбирается самим ffprobe,
    пустой вывод при ненулевом коде — авария инструмента ({}), а не нулевая
    длительность.
    """
    ffprobe = C._ffprobe_path({})
    ffmpeg = C._ffprobe_path.__globals__["Path"](ffprobe).with_name(
        "ffmpeg" + (".exe" if ffprobe.lower().endswith(".exe") else ""))
    if not (C.os.path.exists(ffmpeg) or C.shutil.which("ffmpeg")):
        pytest.skip("ffmpeg недоступен на этой машине")
    f = tmp_path / "canary.flac"
    r = C.subprocess.run([str(ffmpeg), "-v", "error", "-y",
                          "-f", "lavfi", "-i", "sine=frequency=440:duration=2",
                          "-ac", "2", str(f)],
                         capture_output=True, text=True, timeout=60)
    assert r.returncode == 0 and f.stat().st_size > 0, r.stderr[-200:]
    info = C.probe_media(str(f), {})
    assert info, "ffprobe не смог разобрать аргументы — argv неверен"
    assert info["codec"] == "flac" and abs(info["duration"] - 2.0) < 0.2

    # Пустой stdout при rc!=0 — сломанный ffprobe, а не «файл без длительности».
    class _Broken:
        stdout, stderr, returncode = "", "No match for section 'codec_type'", 1
    monkeypatch_run = getattr(C.subprocess, "run")
    C.subprocess.run = lambda *a, **k: _Broken()
    try:
        assert C.probe_media(str(f), {}) == {}
    finally:
        C.subprocess.run = monkeypatch_run


def test_dead_sign_matches_apple_verdict():
    """Формулировка инцидента 24.09 — первое падение с ней обязано будить
    владельца, а не дожидаться второго подряд."""
    msg = ("Ни одна своя Apple-учётка не дала ключ: ca[слот 0: релиз там есть, "
           "но ключа не дали]. Публичный wrapper не участвует.")
    assert C._DEAD_SIGNS.search(msg)
