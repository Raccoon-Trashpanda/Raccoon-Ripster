"""Apple «0 из 0» = по ссылке ничего нет, и повторять это нечего.

20.09.2026: ссылка с несуществующим id трека дала «загрузчик … не назвал
причину» и ТРИ одинаковых прогона по 70 секунд. Загрузчик причину назвал —
он вообще ничего не поставил в очередь, а это и есть причина.
"""
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from ripster.engines.zhaarey import _last_reason  # noqa: E402
from ripster.runner import _RE_NO_RETRY  # noqa: E402

EMPTY_LOG = """Queue 1 of 1: Album
Daft Punk
Random Access Memories
=======  [OK] Completed: 0/0  |  [!] Warnings: 0  |  [X] Errors: 0  =======
null
"""


def test_nothing_queued_is_named_honestly():
    why = _last_reason(EMPTY_LOG, 0)
    assert "в каталоге ничего нет" in why
    assert "не назвал причину" not in why


def test_that_verdict_is_not_retried():
    assert _RE_NO_RETRY.search(_last_reason(EMPTY_LOG, 0)), \
        "пустой каталог обязан падать сразу, а не трижды"


def test_a_real_shortfall_still_reports_the_count():
    """Треки были, но не скачались — это другой случай, счёт нужен."""
    why = _last_reason("Completed: 0/12\n", 12)
    assert "из 12" in why


def test_an_engine_error_line_wins_over_the_fallback():
    log = EMPTY_LOG + "\nERROR: track is Unavailable in this storefront\n"
    why = _last_reason(log, 0)
    assert "Unavailable" in why
