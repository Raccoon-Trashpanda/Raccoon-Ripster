"""Запасной аудиопоток для music-video в gamdl (ripster/gamdl_mv_patch.py).

Старые клипы не отдают `audio-stereo-256` — только HE-AAC. Прежний точный матч
возвращал None, дальше шёл пустой ключ и «invalid argument for --key». Проверяем:
точный матч жив, запас выбирает лучшую аудиогруппу, патч идемпотентен, незнакомая
форма файла не трогается, и ПРОВОД дотянут: MV-команда вызывает ensure().
"""
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from ripster import gamdl_mv_patch as gmp                     # noqa: E402

# Ровно та форма, что лежит в gamdl 3.3 (проверено вживую в site-packages).
SRC = '''
class MusicVideoInterface:
    def _get_best_stereo_audio_playlist(
        self,
        playlist_master_data: dict,
    ) -> dict | None:
        audio_playlist = next(
            (
                media
                for media in playlist_master_data["media"]
                if media["group_id"] == "audio-stereo-256"
            ),
            None,
        )
        return audio_playlist
'''


def _iface_class(src: str):
    ns: dict = {}
    exec(compile(src, "<patched gamdl>", "exec"), ns)
    return ns["MusicVideoInterface"]()


def test_exact_match_still_wins():
    iface = _iface_class(SRC)
    media = [{"group_id": "audio-stereo-256", "uri": "u256"},
             {"group_id": "audio-stereo-512", "uri": "u512"}]
    assert iface._get_best_stereo_audio_playlist({"media": media})["uri"] == "u256"


def test_fallback_picks_best_available_audio():
    patched = gmp.patch_source(SRC)
    assert patched and gmp.MARKER in patched
    iface = _iface_class(patched)
    # Старый клип: только HE-AAC-группы; без uri — не кандидат.
    media = [{"group_id": "video-hvc1", "uri": "v"},
             {"group_id": "audio-aac", "uri": None},
             {"group_id": "audio-aac", "uri": "aac"},
             {"group_id": "audio-stereo-64", "uri": "s64"}]
    got = iface._get_best_stereo_audio_playlist({"media": media})
    assert got["uri"] == "s64"          # больший битрейт из имени группы
    # Ни одной числовой группы — берём любую аудио, а не None.
    got = iface._get_best_stereo_audio_playlist(
        {"media": [{"group_id": "audio-stereo", "uri": "st"},
                   {"group_id": "video-avc1", "uri": "v"}]})
    assert got["uri"] == "st"


def test_no_audio_at_all_returns_none():
    iface = _iface_class(gmp.patch_source(SRC))
    assert iface._get_best_stereo_audio_playlist(
        {"media": [{"group_id": "video-hvc1", "uri": "v"}]}) is None


def test_idempotent_and_unknown_shape():
    once = gmp.patch_source(SRC)
    assert gmp.patch_source(once) is None          # второй раз не пишем
    assert gmp.patch_source("class X: pass\n") is None   # форму не узнали


def test_real_installed_gamdl_shape_is_recognized():
    """Патчер обязан понимать НЕ синтетический файл, а тот, что стоит в venv,
    иначе весь модуль — декорация."""
    pytest.importorskip("gamdl")
    import gamdl.interface.music_video as mv
    src = Path(mv.__file__).read_text(encoding="utf-8")
    if gmp.MARKER in src:
        return                                  # запас уже применён — это успех
    assert gmp.patch_source(src) is not None, \
        "форма _get_best_stereo_audio_playlist не распознана — патчер отстанет"


def test_ensure_patches_file_once(tmp_path, monkeypatch):
    f = tmp_path / "music_video.py"
    f.write_text(SRC, encoding="utf-8")
    monkeypatch.setattr(gmp, "_music_video_path", lambda py: str(f))
    monkeypatch.setattr(gmp, "_done", False)
    assert gmp.ensure(python="x")["verdict"] == "patched"
    assert gmp.MARKER in f.read_text(encoding="utf-8")
    assert gmp.ensure(python="x")["verdict"] == "already"
    # Незнакомая форма — файл не тронут, вердикт честный.
    other = tmp_path / "other.py"
    other.write_text("def unrelated(): pass\n", encoding="utf-8")
    monkeypatch.setattr(gmp, "_music_video_path", lambda py: str(other))
    monkeypatch.setattr(gmp, "_done", False)
    assert gmp.ensure(python="x")["verdict"] == "unknown_shape"
    assert other.read_text(encoding="utf-8") == "def unrelated(): pass\n"


def test_build_cmd_mv_calls_ensure(monkeypatch):
    """Провод: запас применяется именно на MV-задачу, а не «где-то рядом»."""
    from ripster.engines import gamdl as eg
    calls = []
    monkeypatch.setattr(gmp, "ensure", lambda python=None: calls.append(1) or {"verdict": "already"})
    monkeypatch.setattr(eg, "_FLAG_CACHE", set())
    eng = eg.GamdlEngine()
    cfg = {"save-path": "./dl", "gamdl-cookies-path": "cookies.txt"}
    eng.build_cmd("https://music.apple.com/us/music-video/x1", "mv", cfg)
    assert calls == [1]
    eng.build_cmd("https://music.apple.com/us/album/x2", "aac", cfg)
    assert calls == [1]                          # не-MV не дёргает
