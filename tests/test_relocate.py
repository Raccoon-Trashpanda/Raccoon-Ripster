"""Post-download layout fix — `runner._relocate_to_service_folder`.

Apple's Go downloaders (zhaarey/AMD) write `<base>/<quality>/<artist>/<album>`,
skipping the `<service>/` segment, leaving the downloads folder a mix of
`<base>/<service>/<quality>` and bare `<base>/<quality>` folders. The relocation
re-homes the bare case under the service folder and leaves everything else alone.
"""
from pathlib import Path

from ripster.runner import _relocate_to_service_folder
from ripster.service_config import get_save_path


def _mk(p: Path):
    p.mkdir(parents=True, exist_ok=True)
    (p / "01 - track.m4a").write_text("x", encoding="utf-8")
    return p


def test_relocates_apple_lossless_into_service_folder(tmp_path):
    cfg = {"save-path": str(tmp_path)}
    # Apple lossless landed at <base>/ALAC (Lossless)/<artist>/<album> (no apple/).
    qfolder = Path(get_save_path(cfg, "apple", "alac")).name          # "ALAC (Lossless)"
    sd = _mk(tmp_path / qfolder / "Artist" / "Album")
    new = _relocate_to_service_folder(str(sd), "apple", "alac", cfg)
    canon = Path(get_save_path(cfg, "apple", "alac"))           # <base>/apple/ALAC (Lossless)
    assert Path(new).resolve() == (canon / "Artist" / "Album").resolve()
    assert (Path(new) / "01 - track.m4a").is_file()
    # The bare <base>/<quality> folder is gone (tidied since it was emptied).
    assert not (tmp_path / qfolder).exists()


def test_leaves_correctly_placed_output_untouched(tmp_path):
    cfg = {"save-path": str(tmp_path)}
    # Qobuz already under <base>/qobuz/<quality>/<album> — must NOT move.
    canon = Path(get_save_path(cfg, "qobuz", "27"))
    sd = _mk(canon / "Some Album")
    new = _relocate_to_service_folder(str(sd), "qobuz", "27", cfg)
    assert Path(new).resolve() == sd.resolve()
    assert sd.is_dir()


def test_leaves_flat_service_quality_dir_untouched(tmp_path):
    cfg = {"save-path": str(tmp_path)}
    # SoundCloud flat case: save_dir == <base>/soundcloud/<quality> itself.
    canon = Path(get_save_path(cfg, "soundcloud", "best"))
    _mk(canon)
    new = _relocate_to_service_folder(str(canon), "soundcloud", "best", cfg)
    assert Path(new).resolve() == canon.resolve()
    assert canon.is_dir()


def test_ignores_dir_outside_base(tmp_path):
    cfg = {"save-path": str(tmp_path / "downloads")}
    (tmp_path / "downloads").mkdir()
    outside = _mk(tmp_path / "elsewhere" / "Album")
    new = _relocate_to_service_folder(str(outside), "apple", "alac", cfg)
    assert Path(new).resolve() == outside.resolve()
