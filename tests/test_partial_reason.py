"""Partial-download reason plumbing (issue #5).

When a release comes back short, the runner classifies WHY in one canonical
token (`runner._classify_partial_reason`) and stamps it onto the download
manifest (`download_manifest.set_partial`) so the bot card / web history can
show "N/M — region-locked" instead of a vague "some tracks missing".
"""
from ripster.runner import _classify_partial_reason
from ripster import download_manifest as dm


# ── classifier ──────────────────────────────────────────────────────────────
def test_classify_decryption():
    log = "Track 3: Decryption is not available for media ID 123"
    assert _classify_partial_reason(log, permanent=True) == "decryption"


def test_classify_region():
    log = "This track is not available in your country"
    assert _classify_partial_reason(log, permanent=True) == "region"


def test_classify_no_flac():
    log = "Track not found at desired bitrate, no alternative found"
    assert _classify_partial_reason(log, permanent=False) == "no-flac"


def test_classify_removed():
    log = "Resource not found (404) — no longer available"
    assert _classify_partial_reason(log, permanent=False) == "removed"


def test_classify_unavailable():
    log = "Failed to dl aac-lc: Unavailable"
    assert _classify_partial_reason(log, permanent=False) == "unavailable"


def test_classify_permanent_fallback():
    # No specific pattern, but the runner flagged it permanent → not "postprocess".
    assert _classify_partial_reason("nothing specific here", permanent=True) == "region"


def test_classify_transient_fallback():
    assert _classify_partial_reason("some odd glitch", permanent=False) == "postprocess"


# ── manifest persistence ─────────────────────────────────────────────────────
def test_record_includes_partial_fields(tmp_path):
    dm.init(tmp_path)
    task = {"id": "abc123", "service": "deezer", "quality": "flac",
            "url": "https://x", "meta": {"title": "T", "artist": "A"}}
    assert dm.record("abc123", str(tmp_path), [], task)
    ent = dm.lookup("abc123")
    assert ent["partial"] is False
    assert ent["partial_reason"] == ""


def test_set_partial_patches_entry(tmp_path):
    dm.init(tmp_path)
    task = {"id": "def456", "service": "apple", "quality": "aac",
            "url": "https://y", "meta": {"title": "Alb", "artist": "Art"}}
    dm.record("def456", str(tmp_path), [], task)
    assert dm.set_partial("def456", got=4, expected=6, missing=2, reason="decryption")
    ent = dm.lookup("def456")
    assert ent["partial"] is True
    assert ent["got"] == 4 and ent["expected"] == 6 and ent["missing"] == 2
    assert ent["partial_reason"] == "decryption"


def test_set_partial_missing_entry_is_noop(tmp_path):
    dm.init(tmp_path)
    assert dm.set_partial("nope", 1, 2, 1, "region") is False
