"""Переиспользование скачанного не должно запирать человека в плохом качестве.

Живой разбор 05.09.2026. Владелец трижды ставил один и тот же релиз Beatport
в FLAC и трижды получал AAC 128. В журнале видно главное: перед отметкой
«recorded done» НЕТ строки запуска движка — движок не стартовал ни разу.

Сработал `_find_completed_duplicate`: он сверял url + ЯРЛЫК качества + движок и
отдавал готовую папку. Но ярлык — это то, что ПРОСИЛИ, а не то, что лежит на
диске: первая (сломанная) загрузка положила AAC 128 в папку с именем «FLAC CD»,
и дальше каждая попытка перекачать натыкалась на неё же. Обратной связи не было
никакой: задача честно зеленела.

Отсюда свойство, которое тут закрепляется: чем хуже скачалось в первый раз, тем
прочнее это закреплялось — а выйти из ямы повтором было нельзя в принципе.
Поэтому переиспользуем только после вопроса САМОМУ ФАЙЛУ, и «не знаю» считаем
поводом перекачать, а не переиспользовать.
"""
import pytest

from ripster import runner


class TestWantsLossless:
    @pytest.mark.parametrize("q", ["lossless", "hifi", "flac", "alac", "hires", "FLAC CD"])
    def test_these_promise_lossless(self, q):
        assert runner._wants_lossless(q) is True

    @pytest.mark.parametrize("q", ["aac", "mp3", "320", "minimum", "high", ""])
    def test_these_do_not(self, q):
        assert runner._wants_lossless(q) is False


@pytest.fixture
def history(tmp_path, monkeypatch):
    """Одна завершённая загрузка в истории + её папка на диске."""
    d = tmp_path / "FLAC CD" / "POWLO!" / "Come On People EP"
    d.mkdir(parents=True)
    (d / "01. track.m4a").write_bytes(b"x")
    entry = {
        "url": "https://www.beatport.com/release/come-on-people-ep/7291539",
        "quality": "lossless", "engine": "orpheus_beatport", "status": "done",
        "_save_dir": str(d), "got": 1,
    }
    monkeypatch.setattr(runner, "_download_history", [entry])
    return entry


def _find(quality="lossless"):
    return runner._find_completed_duplicate(
        "https://www.beatport.com/release/come-on-people-ep/7291539",
        quality, "orpheus_beatport")


class TestReuseAsksTheFileNotTheLabel:
    def test_a_lossy_file_in_a_flac_folder_is_not_reused(self, history, monkeypatch):
        """Тот самый случай владельца."""
        monkeypatch.setattr("ripster.integrity_verify.probe_codec",
                            lambda p: {"codec": "aac", "lossless": False})
        assert _find() is None

    def test_a_real_lossless_file_is_reused(self, history, monkeypatch):
        """Починка не должна убить саму фичу: настоящий FLAC переиспользуем,
        иначе каждый повтор качал бы всё заново."""
        monkeypatch.setattr("ripster.integrity_verify.probe_codec",
                            lambda p: {"codec": "flac", "lossless": True})
        assert _find() is history

    def test_alac_counts_as_lossless(self, history, monkeypatch):
        monkeypatch.setattr("ripster.integrity_verify.probe_codec",
                            lambda p: {"codec": "alac", "lossless": True})
        assert _find() is history

    def test_an_unreadable_file_is_re_downloaded(self, history, monkeypatch):
        """Пустой ответ ffprobe — это «не знаю». Перекачать дешевле, чем
        молча отдать не то; выдавать «не знаю» за «да» нельзя."""
        monkeypatch.setattr("ripster.integrity_verify.probe_codec", lambda p: {})
        assert _find() is None

    def test_a_lossy_request_does_not_probe_at_all(self, history, monkeypatch):
        """Просили 320 — что там лежит FLAC или AAC, неважно, файлы на месте.
        Лишний ffprobe на каждую задачу нам не нужен."""
        history["quality"] = "320"

        def _boom(p):
            raise AssertionError("ffprobe не должен вызываться для lossy-запроса")

        monkeypatch.setattr("ripster.integrity_verify.probe_codec", _boom)
        assert _find("320") is history
