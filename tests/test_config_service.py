"""Settings core: ConfigService (typed view over the config dict, with int/bool
coercion) and the atomic YAML save/load (losing config.yaml = losing every token,
so the write must be crash-safe)."""
import pytest

from ripster.config_service import ConfigService, _atomic_write_yaml, load_config


# ── typed property coercion ──────────────────────────────────────────────────
def test_str_property_and_default():
    assert ConfigService({"engine": "gamdl"}).engine == "gamdl"
    assert ConfigService({}).engine == "zhaarey"      # _s default
    assert ConfigService({}).quality == "alac"
    assert ConfigService({"save-path": None}).save_path == ""   # None → default


def test_int_property_coercion():
    assert ConfigService({"atmos-max": "100"}).atmos_max == 100   # str → int
    assert ConfigService({"atmos-max": "bad"}).atmos_max == 2448  # bad → default
    assert ConfigService({}).atmos_max == 2448


@pytest.mark.parametrize("raw,expected", [
    ("true", True), ("1", True), ("yes", True), ("on", True),
    (True, True), (1, True),
    ("false", False), ("off", False), (0, False), (False, False),
])
def test_bool_property_coercion(raw, expected):
    assert ConfigService({"use-go-run": raw}).use_go_run is expected


# ── dict protocol ────────────────────────────────────────────────────────────
def test_mapping_protocol():
    cs = ConfigService({"a": 1})
    assert cs["a"] == 1
    assert cs.get("a") == 1
    assert cs.get("missing") is None          # no default → None
    assert cs.get("missing", "def") == "def"
    assert "a" in cs and "z" not in cs
    cs["b"] = 2
    assert cs["b"] == 2
    del cs["b"]
    assert "b" not in cs


# ── atomic save + load roundtrip ─────────────────────────────────────────────
def test_atomic_write_and_load(tmp_path):
    cfgf = tmp_path / "config.yaml"
    tokens = tmp_path / "tokens"
    tokens.mkdir()
    assert _atomic_write_yaml(cfgf, {"engine": "tidal", "quality": "lossless"}) is True
    assert cfgf.exists()
    merged = load_config(cfgf, tokens)
    assert merged["engine"] == "tidal"
    assert merged["quality"] == "lossless"


def test_load_missing_file_returns_defaults(tmp_path):
    # no config.yaml → defaults only, must not raise
    merged = load_config(tmp_path / "nope.yaml", tmp_path / "tokens")
    assert isinstance(merged, dict) and len(merged) > 0
