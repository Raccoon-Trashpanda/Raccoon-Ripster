"""Self-updater core logic: semver compare, requirements-diff (decides whether to
re-run pip), and the import smoke gate that rolls back a broken update."""
import pytest

from ripster import updater


# ── parse_version / is_newer ─────────────────────────────────────────────────
@pytest.mark.parametrize("s,expected", [
    ("v3.1.0", (3, 1, 0)),
    ("3.0.0-beta", (3, 0, 0)),
    ("Ripster 3.2.5 release", (3, 2, 5)),
    ("3.10", (3, 10)),                  # any component count
    ("v3.10.2.5-beta", (3, 10, 2, 5)),
    ("3.100.0", (3, 100, 0)),           # hundreds
    ("garbage", (0,)),
])
def test_parse_version(s, expected):
    assert updater.parse_version(s) == expected


@pytest.mark.parametrize("remote,local,newer", [
    ("3.1.0", "3.0.0", True),
    ("v3.0.1", "3.0.0", True),
    ("3.0.0", "3.0.0", False),
    ("3.0.0", "3.1.0", False),
    ("2.9.9", "3.0.0", False),
    # the numeric-comparison cases the owner flagged (NOT string order):
    ("3.10.0", "3.9.0", True),          # 10 > 9
    ("3.9.0", "3.10.0", False),
    ("3.100.0", "3.99.0", True),        # hundreds
    ("1.10.0", "1.2.0", True),          # classic lexical trap avoided
    # different component counts compare correctly (zero-padded):
    ("3.10", "3.10.0", False),          # equal
    ("3.10.1", "3.10", True),           # longer & newer
    ("3.10", "3.10.1", False),
])
def test_is_newer(remote, local, newer):
    assert updater.is_newer(remote, local) is newer


# ── requirements_changed ─────────────────────────────────────────────────────
def test_requirements_unchanged():
    assert updater.requirements_changed("a==1\nb==2", "a==1\nb==2") is False


def test_requirements_ignores_comments_order_whitespace():
    assert updater.requirements_changed("a==1\n# note", "a==1") is False
    assert updater.requirements_changed("a==1\nb==2", "b==2\na==1") is False
    assert updater.requirements_changed("a==1", "a == 1") is False


def test_requirements_detects_added_pin():
    assert updater.requirements_changed("a==1", "a==1\nc==3") is True


def test_requirements_detects_version_bump():
    assert updater.requirements_changed("a==1", "a==2") is True


# ── verify_import_smoke (the runtime rollback gate) ──────────────────────────
def test_verify_import_smoke_on_healthy_tree():
    ok, detail = updater.verify_import_smoke()
    assert ok is True, f"tree should import cleanly, got: {detail}"
    assert "all modules import" in detail
