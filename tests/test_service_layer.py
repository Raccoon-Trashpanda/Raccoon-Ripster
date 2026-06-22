"""default_quality reads the module-global config; we install a known config and
assert the per-service mapping (the source of "AAC-256 file in MP3-128 folder"
style label/folder mismatches)."""
import pytest

from ripster import service_layer


@pytest.fixture
def cfg(monkeypatch):
    def _install(d):
        monkeypatch.setattr(service_layer, "_config", d)
        return d
    return _install


def test_default_quality_apple(cfg):
    cfg({"quality": "alac"})
    assert service_layer.default_quality("apple") == "alac"


def test_default_quality_unknown_service(cfg):
    cfg({})
    assert service_layer.default_quality("definitely-not-a-service") == "alac"


def test_default_quality_soundcloud_token_unlocks_hq(cfg):
    cfg({})
    assert service_layer.default_quality("soundcloud") == "mp3"
    cfg({"soundcloud-oauth-token": "tok"})
    assert service_layer.default_quality("soundcloud") == "hq"


def test_default_quality_spotify_orpheus(cfg):
    cfg({"spotify-engine": "orpheus_spotify", "orpheus-quality": "hifi"})
    assert service_layer.default_quality("spotify") == "hifi"


def test_default_quality_per_service_defaults(cfg):
    cfg({})
    assert service_layer.default_quality("qobuz") == "27"
    assert service_layer.default_quality("deezer") == "flac"
    assert service_layer.default_quality("tidal") == "lossless"
    assert service_layer.default_quality("amazon") == "High"
