"""BBC в боте (01.10.2026): «Engine error: sequence item 0: expected str instance, list found».
`yt_dlp_cmd()` возвращает СПИСОК [python, -m, yt_dlp], а `build_cmd` клал его первым элементом команды вложенным списком;
сборка строки запуска падала до старта. Команда обязана быть плоским списком строк."""
from ripster.engines import bbc


def test_build_cmd_is_a_flat_list_of_strings(monkeypatch, tmp_path):
    monkeypatch.setattr(bbc, "yt_dlp_cmd", lambda: ["python.exe", "-m", "yt_dlp"])
    monkeypatch.setattr(bbc._Q, "ladder_sync", lambda url: {"ok": True, "best_kbps": 102, "best_codec": "HE-AAC"})
    eng = bbc.BbcEngine() if hasattr(bbc, "BbcEngine") else [c for c in vars(bbc).values() if isinstance(c, type) and getattr(c, "name", "") == "bbc"][0]()
    cmd = eng.build_cmd("https://example.invalid/x.m3u8", "mp3", {"save-path": str(tmp_path), "_bbc_title": "T", "_bbc_artist": "A", "_bbc_pid": "p1"})
    assert all(isinstance(x, str) for x in cmd), [type(x).__name__ for x in cmd]
    assert cmd[:3] == ["python.exe", "-m", "yt_dlp"]
    assert " ".join(cmd)          # именно это падало
