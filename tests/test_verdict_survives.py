"""Вердикт движка обязан дойти до человека, а не только до консоли.

Живой случай 06.09.2026. Владелец получил в бот:

    ⛔ Ошибка. Не удалось скачать релиз.
    Задача завершилась без результата (внутреннее состояние «running»).
    Так быть не должно — повтори загрузку.

А за секунду до этого движок сказал точно и подробно: Apple не выдал ключ
(Invalid CKC) при ЖИВОЙ сессии wrapper'а, то есть у контента нет прав в регионе
аккаунта, и повтор тут не поможет в принципе.

Строка ушла в консоль и в `task["log"]`, но в `task["error"]` не попала. Сеть
безопасности увидела пустой `error` и честно подставила своё обобщение — и
получилось худшее из возможного: человеку советуют повторять то, что
повторяться не может, вместо известной причины.

Отсюда правило, которое здесь и стережётся: прежде чем сказать «так быть не
должно», посмотри, не сказал ли кто-то уже, ЧТО именно случилось.
"""
from ripster.runner import _verdict_from_log

VERDICT = ("✗ Apple не выдал ключ на этот альбом (Invalid CKC), но сессия "
           "wrapper'а ЖИВА — значит у контента нет прав в регионе аккаунта.")


class TestTheVerdictIsFound:
    def test_a_marked_verdict_is_picked_up(self):
        task = {"log": ["Downloading…", "Track 1 of 12", VERDICT]}
        got = _verdict_from_log(task)
        assert "Invalid CKC" in got
        assert not got.startswith("✗"), "крестик — пометка для журнала, не часть текста"

    def test_the_last_verdict_wins(self):
        """Движок мог сказать несколько раз по ходу; значим последний."""
        task = {"log": ["✗ первая попытка не удалась", VERDICT]}
        assert "Invalid CKC" in _verdict_from_log(task)


class TestSilenceStaysSilence:
    def test_no_verdict_means_empty(self):
        """Пусто — законный ответ: тогда обобщение сети безопасности честно,
        это и правда неизвестный случай."""
        assert _verdict_from_log({"log": ["Downloading…", "rc=1"]}) == ""

    def test_an_empty_task_does_not_crash(self):
        assert _verdict_from_log({}) == ""
        assert _verdict_from_log({"log": []}) == ""
        assert _verdict_from_log({"log": None}) == ""

    def test_a_bare_mark_is_not_a_verdict(self):
        """Один крестик без текста ничего не объясняет и вердиктом не считается."""
        assert _verdict_from_log({"log": ["✗", "✗ "]}) == ""

    def test_ordinary_lines_are_not_mistaken_for_verdicts(self):
        """Пометку ставит движок осознанно. Обычные строки, даже про ошибки,
        вердиктом не объявляются — иначе в лицо человеку полетит случайный
        технический мусор."""
        task = {"log": ["error: connection reset", "WARNING: retrying"]}
        assert _verdict_from_log(task) == ""
