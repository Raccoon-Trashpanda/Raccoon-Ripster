"""Правила своего аудиотракта ПК — то, что можно проверить без звука.

Владелец: «аудио движок кстати должен быть и в пк версии». Смысл движка ровно
один — отдать звук устройству таким, каким он лежит в файле. Значит и проверять
надо это: когда мы имеем право сказать «bit-perfect», а когда нет.

Замер, ради которого всё затевалось (машина владельца, 05.09.2026):
WASAPI exclusive открылся на 44100/48000/96000, а shared — только на 48000.
Браузеру доступен лишь shared, поэтому web-плеер физически не может отдать
44.1 кГц без пересчёта.
"""
from ripster.audio_engine import LOSSLESS_EXT, Playback


class TestBitPerfectClaim:
    def test_exclusive_at_the_files_own_rate_is_bit_perfect(self):
        p = Playback(file_rate=44100, granted_rate=44100, exclusive=True)
        assert p.bit_perfect is True

    def test_a_resampled_stream_is_not_bit_perfect(self):
        """Файл 44.1, устройство 48 — ровно то, что делает микшер Windows
        со всем, что играет браузер."""
        p = Playback(file_rate=44100, granted_rate=48000, exclusive=True)
        assert p.bit_perfect is False

    def test_shared_mode_is_never_bit_perfect(self):
        """Даже при совпавших частотах: в общем режиме поток идёт через
        микшер, и что он там делает — не наше знание."""
        p = Playback(file_rate=48000, granted_rate=48000, exclusive=False)
        assert p.bit_perfect is False

    def test_nothing_playing_is_not_a_claim(self):
        """Пустое состояние не должно рапортовать «bit-perfect»: это
        утверждение о звуке, которого нет."""
        assert Playback().bit_perfect is False
        assert Playback(exclusive=True).bit_perfect is False

    def test_hi_res_counts_too(self):
        assert Playback(file_rate=96000, granted_rate=96000, exclusive=True).bit_perfect


class TestWhatItAgreesToPlay:
    def test_lossless_containers_are_accepted(self):
        for ext in (".flac", ".wav", ".aiff"):
            assert ext in LOSSLESS_EXT

    def test_lossy_is_not_in_the_list(self):
        """MP3 и AAC сюда не входят намеренно: bit-perfect для сжатого с
        потерями — бессмысленное обещание, а декод у них свой."""
        for ext in (".mp3", ".m4a", ".aac", ".opus"):
            assert ext not in LOSSLESS_EXT


class TestHonestFailure:
    def test_a_missing_file_reports_why(self):
        from ripster.audio_engine import ENGINE
        st = ENGINE.play("Z:/нет-такого-файла.flac")
        assert st.playing is False
        assert st.error == "file_not_found"

    def test_lossy_is_refused_by_name_not_silently(self):
        from ripster.audio_engine import ENGINE
        st = ENGINE.play("Z:/track.mp3")
        assert st.error == "not_lossless"
        assert st.bit_perfect is False
