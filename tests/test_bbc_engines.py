"""BBC-движки: что из мерящего модуля доходит до командной строки ffmpeg.

ripster/bbc_quality.py безупречен сам по себе — и бесполезен, если движок его
не спрашивает. Поэтому здесь проверяется не функция, а аргумент в cmd:

* on-demand: MP3-цель берётся из фактической лестницы варианта (потолок BBC
  Sounds — 102 кбит/с HE-AAC), а не константой «320K», с которой жили годы;
* live: пишем `-c copy` с того варианта, который канал отдаёт сейчас, и никакой
  перекодировки lossy-потока — второе поколение потерь вместо честных 320.
"""
import pytest

from ripster import bbc_quality as _Q
from ripster.engines.bbc import BBCEngine
from ripster.engines.bbc_live import BBCLiveEngine


def _ladder(kbps, codec, *, ok=True, lc=None):
    return {"ok": ok, "best_kbps": kbps, "best_codec": codec,
            "lc": (codec == "AAC-LC") if lc is None else lc,
            "has_320_lc": kbps >= 320 and codec == "AAC-LC",
            "variants": [{"kbps": kbps, "codec": codec}], "error": "" if ok else "http_403"}


# ── on-demand: цель = измеренный источник, а не шильдик ───────────────────────

def _cmd(monkeypatch, engine, ladder, cfg, url="https://example.invalid/x.m3u8"):
    from ripster.engines import bbc as _e
    monkeypatch.setattr(_e, "yt_dlp_cmd", lambda: ["yt-dlp"])
    monkeypatch.setattr(_Q, "ladder_sync", lambda u: dict(ladder))
    return engine.build_cmd(url, "mp3", cfg)


def test_ondemand_target_comes_from_the_ladder_not_from_the_label(monkeypatch, tmp_path):
    """Суть правки: «--audio-quality 320K» брал 102 кбит/с и писал 320 — полоса
    не расширялась, а файл врал о себе и весил втрое."""
    e = BBCEngine()
    cfg = {"save-path": str(tmp_path), "_bbc_title": "Essential Mix",
           "_bbc_artist": "BBC Radio 1", "_bbc_pid": "m00315gp"}
    cmd = _cmd(monkeypatch, e, _ladder(102, "HE-AAC"), cfg)
    assert cmd[cmd.index("--audio-quality") + 1] == "96K"
    assert (e._source_kbps, e._target_kbps) == (102, 96)
    # и уж подавно не 320K
    assert "320K" not in cmd


def test_ondemand_unreadable_ladder_aims_at_the_ceiling_not_320(monkeypatch, tmp_path):
    """Плейлист не прочитался — цель по потолку on-demand (96K). Молчаливый
    провал здесь выглядел бы как «320K» на каждом файле сразу."""
    e = BBCEngine()
    cfg = {"save-path": str(tmp_path), "_bbc_title": "Mix", "_bbc_pid": "m00000aa"}
    cmd = _cmd(monkeypatch, e, _ladder(0, "", ok=False), cfg)
    assert cmd[cmd.index("--audio-quality") + 1] == "96K"
    assert "320K" not in cmd


def test_ondemand_quality_label_admits_the_source(monkeypatch):
    """Ярлык в интерфейсе обязан говорить то же, что считает движок: «MP3 320»
    для источника 102 — та самая ложь, с которой всё началось."""
    labels = [q["label"] for q in BBCEngine().qualities()]
    assert any("102" in l for l in labels)
    assert not any("320" in l for l in labels)


# ── live: copy с фактического варианта ────────────────────────────────────────

def _live_cmd(monkeypatch, pick, tmp_path, duration=3600):
    e = BBCLiveEngine()
    monkeypatch.setattr(_Q, "best_live_variant", lambda ch, want: dict(pick))
    cfg = {"save-path": str(tmp_path), "_bbc_live_channel": "bbc_radio_one",
           "_bbc_duration": duration, "_bbc_title": "Essential Mix"}
    return e, e.build_cmd("https://www.bbc.co.uk/sounds/live/bbc_radio_one",
                          "live320", cfg)


def test_live_copies_the_variant_the_channel_actually_offers(monkeypatch, tmp_path):
    url = "https://example.invalid/audio/-audio%3d320000.norewind.m3u8"
    e, cmd = _live_cmd(monkeypatch, {"url": url, "kbps": 320, "codec": "AAC-LC",
                                     "lc": True, "wanted_kbps": 320}, tmp_path)
    assert cmd[cmd.index("-i") + 1] == url
    assert cmd[cmd.index("-c") + 1] == "copy"
    # ни одного перекодирования lossy-потока: -c copy и нет -b:a
    assert "-b:a" not in cmd and "-c:a" not in cmd
    assert cmd[cmd.index("-t") + 1] == "3600"


def test_live_without_the_320_rung_writes_what_exists_and_says_its_number(monkeypatch,
                                                                          tmp_path):
    """Ступени 320 сейчас нет — пишем лучшее из доступного и называем числом:
    сорвать окно эфира ошибкой хуже, чем снять 128 честно (вердикт по файлу всё
    равно спрашивает файл)."""
    url = "https://example.invalid/audio/-audio%3d128000.norewind.m3u8"
    e, cmd = _live_cmd(monkeypatch, {"url": url, "kbps": 128, "codec": "AAC-LC",
                                     "lc": True, "wanted_kbps": 320}, tmp_path)
    assert cmd[cmd.index("-i") + 1] == url
    assert cmd[cmd.index("-c") + 1] == "copy"


def test_live_dead_master_falls_back_to_the_engine_url(monkeypatch, tmp_path):
    """Лестницу не прочитать — не валим запись: идём рабочим адресом канала."""
    from ripster import bbc_live_channels as _ch
    e, cmd = _live_cmd(monkeypatch, {"url": "", "error": "http_403"}, tmp_path)
    assert cmd[cmd.index("-i") + 1] == _ch.stream_url("bbc_radio_one")


def test_live_actual_quality_is_the_file_number_not_the_task_label(monkeypatch, tmp_path):
    """Ярлык «live320» задача получает только если файл его держит. Ступень
    могла упасть посреди ночи — тогда на карточке то, что померили."""
    e = BBCLiveEngine()
    f = tmp_path / "x.m4a"
    f.write_bytes(b"\0" * 1024)
    monkeypatch.setattr(_Q, "verdict",
                        lambda p, k: {"state": "below", "kbps": 96, "codec": "HE-AAC"})
    assert e._actual_quality(f) == "live96k"
    monkeypatch.setattr(_Q, "verdict",
                        lambda p, k: {"state": "as_promised", "kbps": 320,
                                      "codec": "AAC-LC"})
    assert e._actual_quality(f) == "live320"
    # не сумели померить — не вешаем ярлык вовсе (пустая строка ≠ «320»)
    def boom(p, k):
        raise OSError("no ffprobe")
    monkeypatch.setattr(_Q, "verdict", boom)
    assert e._actual_quality(f) == ""
