"""Матрица выбора SC-транскодов и метка папки по фактическому битрейту.

Повод 25.09.2026: часовик Anjunadeep 612 лёг в «AAC 256», а ffprobe показал
160 кбит/с — движок брал ПЕРВЫЙ попавшийся *-encrypted-hls, а DRM-ветка ходила
бесплатным primary-токеном, listing которого про 256 не знает. Эти тесты —
контракт: лучший пресет из видимого ЭТОЙ УЧЁТКОЙ, free-потолок, отрывки
позади целого, и папка/метка только от замера файла.
"""
from ripster import sc_transcoding as sct

from pathlib import Path

import pytest

import ripster.runner as R


def _t(preset, proto="hls", quality="hq", snipped=False, url="https://x/m3u8"):
    return {"preset": preset, "quality": quality, "snipped": snipped,
            "url": url, "format": {"protocol": proto, "mime_type": "audio/mp4"}}


# ── pick: матрица тариф × доступные пресеты ─────────────────────────────────

def test_go_plus_takes_256_even_when_160_listed_first():
    trans = [_t("aac_160k", quality="sq"), _t("aac_256k")]
    assert sct.pick(trans, go_plus=True)["preset"] == "aac_256k"
    # тариф не измеряли — берём лучшее из видимого этим токеном
    assert sct.pick(trans)["preset"] == "aac_256k"


def test_free_account_never_reaches_for_hq():
    # кэш listing'а снят другим токеном и показал 256 — бесплатной учётке
    # обмен на URL вернул бы 403, поэтому отсев здесь обязан сработать.
    trans = [_t("aac_160k", quality="sq"), _t("aac_256k")]
    assert sct.pick(trans, go_plus=False)["preset"] == "aac_160k"


def test_free_matrix_only_low_presets():
    for trans, want in (
        ([_t("abr_sq", quality="low"), _t("mp3_1_0", proto="progressive", quality="low")], "mp3_1_0"),
        ([_t("aac_96k", quality="low"), _t("abr_sq", quality="low")], "abr_sq"),
        ([_t("aac_160k", quality="sq")], "aac_160k"),
    ):
        assert sct.pick(trans, go_plus=False)["preset"] == want


def test_full_listing_matrix_picks_top():
    trans = [_t("aac_160k", quality="sq"), _t("abr_hq"), _t("aac_256k"),
             _t("mp3_1_0", proto="progressive", quality="low")]
    assert sct.pick(trans)["preset"] == "aac_256k"


# ── pick: tie-breakы внутри одного битрейта ─────────────────────────────────

def test_non_drm_wins_over_drm_same_bitrate():
    trans = [_t("aac_256k", proto="ctr-encrypted-hls"), _t("aac_256k", proto="hls")]
    assert sct.pick(trans)["format"]["protocol"] == "hls"


def test_explicit_aac_wins_over_adaptive_same_bitrate():
    trans = [_t("abr_hq"), _t("aac_256k")]
    assert sct.pick(trans)["preset"] == "aac_256k"


def test_progressive_wins_over_hls_same_bitrate_and_class():
    trans = [_t("mp3_1_0", proto="hls", quality="low"),
             _t("mp3_1_0", proto="progressive", quality="low")]
    assert sct.pick(trans)["format"]["protocol"] == "progressive"


# ── pick: отрывки и мусор ────────────────────────────────────────────────────

def test_snipped_always_behind_full_track_even_higher_bitrate():
    trans = [_t("aac_256k", snipped=True, quality="preview"), _t("aac_160k", quality="sq")]
    assert sct.pick(trans)["preset"] == "aac_160k"


def test_only_snipped_left_takes_best_snippet_instead_of_nothing():
    trans = [_t("aac_160k", quality="preview", snipped=True),
             _t("aac_96k", quality="preview", snipped=True)]
    assert sct.pick(trans)["preset"] == "aac_160k"


def test_unknown_preset_loses_to_measured_and_empty_input_is_none():
    trans = [_t("mystery_999"), _t("aac_96k", quality="low")]
    assert sct.pick(trans)["preset"] == "aac_96k"
    assert sct.pick([]) is None
    assert sct.pick(None) is None
    # без URL транскод нечем обменять — он не кандидат
    assert sct.pick([_t("aac_256k", url="")]) is None


# ── метка папки: только от фактического ffprobe-замера ──────────────────────

def test_folder_quality_from_measured_bitrate_not_request():
    # ровно кейс 25.09: просили aac_256k, файл 160000 бит/с → папка «AAC 160»
    assert sct.folder_quality("aac", "160000") == "aac_160"
    assert sct.folder_quality("aac", "256000") == "aac_256"
    assert sct.folder_quality("aac", 255800) == "aac_256"
    assert sct.folder_quality("aac", "96000") == "aac_96"
    assert sct.folder_quality("mp3", "128000") == "mp3"


def test_folder_quality_refuses_to_guess():
    assert sct.folder_quality("aac", "") == ""       # нет замера — не угадываем
    assert sct.folder_quality("aac", None) == ""
    assert sct.folder_quality("opus", "128000") == ""  # незнакомый кодекс
    assert sct.folder_quality("", "160000") == ""
    assert sct.folder_quality("aac", "бит") == ""


def test_folder_names_resolve_through_single_quality_map():
    from ripster.service_config import _quality_folder_name
    for fq, human in (("aac_256", "AAC 256"), ("aac_160", "AAC 160"),
                      ("aac_96", "AAC 96"), ("mp3", "MP3 128")):
        assert _quality_folder_name("soundcloud", fq) == human


def test_label_is_human_measurement():
    assert sct.label("aac", 160) == "AAC 160"
    assert sct.label("mp3", 128) == "MP3 128"
    assert sct.label("", 0) == ""


# ── _sc_relabel_to_actual: папка и метка по ЗАМЕРУ, громкая строка при цене ──

@pytest.fixture
def relabel_env(tmp_path, monkeypatch):
    """Мир раннера без сети и без реального ffprobe: конфиг на tmp_path,
    probe_codec — подставной, маркер и лог — перехвачены."""
    monkeypatch.setattr(R, "_config", {"save-path": str(tmp_path)})
    import ripster.integrity_verify as iv
    import ripster.task_marker as tm
    import ripster.routes.download as rd
    monkeypatch.setattr(tm, "write_marker", lambda *a, **k: None)
    monkeypatch.setattr(rd, "_find_audio_files",
                        lambda d, roots=None: sorted(p for p in d.rglob("*.m4a")))
    logged = []

    async def _fk(key, level="info", task_id="", **params):
        logged.append((key, level, params))
    monkeypatch.setattr(R, "_log_key", _fk)

    def _mk(qname, probe):
        monkeypatch.setattr(iv, "probe_codec", lambda p: dict(probe))
        rel = tmp_path / "soundcloud" / qname / "Anjunadeep - Mix 612"
        rel.mkdir(parents=True, exist_ok=True)
        (rel / "01.m4a").write_bytes(b"x")
        return {"service": "soundcloud", "quality": "", "_save_dir": str(rel),
                "meta": {"title": "t"}}, rel
    return _mk, tmp_path, logged


@pytest.mark.asyncio
async def test_hq_requested_160_measured_folder_moves_and_shouts(relabel_env):
    mk, tmp, logged = relabel_env
    task, rel = mk("AAC 256", {"codec": "aac", "lossless": False, "bit_rate": "160000"})
    task["quality"] = "hq"
    moved = await R._sc_relabel_to_actual(task, rel, "tid")
    new = (tmp / "soundcloud" / "AAC 160" / "Anjunadeep - Mix 612").resolve()
    assert moved and Path(moved).resolve() == new and new.is_dir()
    assert not rel.exists()
    assert (new / "01.m4a").is_file()               # файл переехал, не продублировался
    assert task["quality"] == "aac_160"
    assert task["_save_dir"] == str(new) or Path(task["_save_dir"]).resolve() == new
    assert task["quality_downgraded"] == "AAC 160"
    assert task["quality_wanted"] == "AAC 256"
    assert task["quality_note_key_override"] == "history.sc_qd_downgraded"
    assert logged and logged[0][0] == "console.sc_qd_downgraded"
    assert logged[0][1] == "warn"


@pytest.mark.asyncio
async def test_best_requested_no_promise_relabels_silently(relabel_env):
    mk, tmp, logged = relabel_env
    task, rel = mk("best", {"codec": "aac", "lossless": False, "bit_rate": "256000"})
    task["quality"] = "best"
    moved = await R._sc_relabel_to_actual(task, rel, "tid")
    assert Path(moved).name == "Anjunadeep - Mix 612"
    assert Path(moved).parent.name == "AAC 256"
    assert task["quality"] == "aac_256"
    assert "quality_downgraded" not in task and not logged


@pytest.mark.asyncio
async def test_unmeasurable_file_changes_nothing(relabel_env):
    mk, tmp, logged = relabel_env
    task, rel = mk("AAC 256", {})                   # нет ffprobe / пустой ответ
    task["quality"] = "hq"
    moved = await R._sc_relabel_to_actual(task, rel, "tid")
    assert moved is None
    assert rel.is_dir() and task["quality"] == "hq"
    assert not logged
