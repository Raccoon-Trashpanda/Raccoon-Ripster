"""Правила своего аудиотракта ПК — то, что можно проверить без звука.

Владелец: «аудио движок кстати должен быть и в пк версии». Смысл движка ровно
один — отдать звук устройству таким, каким он лежит в файле. Значит и проверять
надо это: когда мы имеем право сказать «bit-perfect», а когда нет.

Замер, ради которого всё затевалось (машина владельца, 05.09.2026):
WASAPI exclusive открылся на 44100/48000/96000, а shared — только на 48000.
Браузеру доступен лишь shared, поэтому web-плеер физически не может отдать
44.1 кГц без пересчёта.
"""
from ripster.audio_engine import LOSSLESS_EXT, Playback, clamp_seek


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


class TestSeekBounds:
    """Перемотка — единственная часть тракта, проверяемая без звуковой карты.

    Обе границы здесь не теоретические: отрицательная секунда роняет
    `SoundFile.seek`, а секунда за концом файла даёт мгновенное «трек
    закончился» — и очередь пролистывает альбом целиком за пару секунд, что со
    стороны выглядит как «плеер сошёл с ума», а не как промах перемотки.
    """

    def test_negative_target_lands_at_the_start(self):
        assert clamp_seek(-5, 180.0) == 0.0

    def test_past_the_end_stops_short_of_it(self):
        assert clamp_seek(999, 180.0) == 179.5

    def test_a_normal_target_is_left_alone(self):
        assert clamp_seek(42.5, 180.0) == 42.5

    def test_unknown_duration_refuses_to_guess(self):
        """Длительности нет — перематывать некуда; ставим в начало, а не в
        произвольную точку файла, которого ещё не измерили."""
        assert clamp_seek(42.5, 0.0) == 0.0


class TestPausedAndFinishedAreDifferentThings:
    def test_fresh_state_claims_neither(self):
        p = Playback()
        assert p.paused is False and p.finished is False

    def test_paused_is_not_finished(self):
        """Пауза — это «играет, но придержали». Если интерфейс спутает её с
        концом файла, он включит следующий трек посреди текущего."""
        p = Playback(playing=True, paused=True)
        assert p.finished is False

    def test_finished_is_not_a_bit_perfect_claim(self):
        """Доигравший файл больше ничего не выводит — утверждать про вывод
        нечего, ровно как у пустого состояния."""
        assert Playback(finished=True).bit_perfect is False


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
