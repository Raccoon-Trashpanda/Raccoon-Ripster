"""Обложка BBC доходит до ФАЙЛА, а не только до интерфейса.

Движки BBC (yt-dlp/ffmpeg) звук картинкой не дополняют, поэтому встраивание
делает metadata.bbc.attach_artwork — а зовут его оба движка из is_finished().
Тест проверяет именно провод: config-view → build_cmd → is_finished → embed.
"""
from pathlib import Path

import ripster.metadata.bbc as md
from ripster.engines import bbc as eng_bbc
from ripster.engines.bbc import BBCEngine
from ripster.engines import bbc_live as live_mod
from ripster.engines.bbc_live import BBCLiveEngine


class _Resp:
    status_code = 200

    def __init__(self, content: bytes):
        self.content = content


def test_attach_artwork_writes_sidecar_and_upgrades_size(tmp_path, monkeypatch):
    # 320-й адрес из карточки очереди вырастает до размера для тегов по лесенке
    # ichef. Ожидание изменено 21.09.2026 по слову владельца: «обложки идут от
    # 320 и кратно, например 1280×1280 будет норм нам» — потолок подняли с 1024
    # до 1280 (проверено на живом объекте: 1280 отдаёт ровно 1280×1280).
    # Ключевое, что тест держал и держит: НЕ 1000×1000 — этот размер у BBC
    # отвечает 403, и именно из-за него обложек не было вовсе.
    seen = {}

    def fake_get(url, **kw):
        seen["url"] = url
        return _Resp(b"\xff\xd8\xff" + b"0" * 64)

    monkeypatch.setattr(md.httpx, "get", fake_get)
    n = md.attach_artwork(tmp_path, "https://ichef.bbci.co.uk/images/ic/320x320/p0abc.jpg")
    assert seen["url"] == "https://ichef.bbci.co.uk/images/ic/1280x1280/p0abc.jpg"
    assert "1000x1000" not in seen["url"]   # мёртвый размер не должен вернуться
    assert (tmp_path / "cover.jpg").read_bytes().startswith(b"\xff\xd8\xff")
    assert n == 0          # аудиофайлов нет — встраивать не во что, sidecar жив


def test_attach_artwork_silent_on_dead_url(tmp_path, monkeypatch):
    monkeypatch.setattr(md.httpx, "get", lambda url, **kw: _Resp(b"<html>403</html>"))
    assert md.attach_artwork(tmp_path, "https://ichef.bbci.co.uk/images/ic/320x320/x.jpg") == 0
    assert not (tmp_path / "cover.jpg").exists()


def test_attach_artwork_needs_no_url(tmp_path, monkeypatch):
    def boom(*a, **k):
        raise AssertionError("сеть не дёргается, когда адреса нет")
    monkeypatch.setattr(md.httpx, "get", boom)
    assert md.attach_artwork(tmp_path, "") == 0


def test_bbc_engine_carries_cover_from_config_to_embed(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(eng_bbc, "_attach_cover", lambda d, c: calls.append((d, c)))
    monkeypatch.setattr(eng_bbc, "yt_dlp_cmd", lambda: ["yt-dlp"])
    eng = BBCEngine()
    eng.build_cmd("https://example/master.m3u8", "mp3", {
        "save-path": str(tmp_path), "_bbc_title": "Mix", "_bbc_artist": "Show",
        "_bbc_pid": "m0001", "_bbc_cover": "https://ichef.bbci.co.uk/images/ic/1024x1024/p0abc.jpg"})
    assert eng._cover.endswith("p0abc.jpg")
    eng.is_finished("", rc=0)
    assert calls == [(str(tmp_path / "BBC" / "Show - Mix"),
                      "https://ichef.bbci.co.uk/images/ic/1024x1024/p0abc.jpg")]


def test_bbc_engine_skips_embed_on_failure(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(eng_bbc, "_attach_cover", lambda d, c: calls.append((d, c)))
    monkeypatch.setattr(eng_bbc, "yt_dlp_cmd", lambda: ["yt-dlp"])
    eng = BBCEngine()
    eng.build_cmd("https://example/master.m3u8", "mp3", {
        "save-path": str(tmp_path), "_bbc_title": "Mix", "_bbc_pid": "m0001",
        "_bbc_cover": "https://ichef.bbci.co.uk/images/ic/1024x1024/p0abc.jpg"})
    assert eng.is_finished("", rc=1).success is False
    assert calls == []


def test_live_engine_carries_cover_from_config_to_embed(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(live_mod, "_attach_cover", lambda d, c: calls.append((d, c)))
    monkeypatch.setattr(live_mod.shutil, "which", lambda name: "ffmpeg")
    eng = BBCLiveEngine()
    eng.build_cmd("", "live320", {
        "save-path": str(tmp_path), "_bbc_live_channel": "bbc_radio_one",
        "_bbc_duration": 3600, "_bbc_title": "Ночной эфир",
        "_bbc_cover": "https://ichef.bbci.co.uk/images/ic/320x320/p0abc.jpg"})
    assert eng._cover.endswith("p0abc.jpg")
    # успеха без файла не будет — проверяем, что на неудаче встраивание не зовём
    eng.is_finished("", rc=1)
    assert calls == []
