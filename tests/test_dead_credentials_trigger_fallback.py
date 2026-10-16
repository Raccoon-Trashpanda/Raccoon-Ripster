"""Шов между классификатором отказов и перебором учёток.

Обе половины давно покрыты тестами по отдельности: `test_partial_reason.py`
проверяет, что классификатор выдаёт нужный токен на своих строках, а
`test_account_fallback.py` — что по токену `session` задача уходит на следующую
учётку. Шов между ними не проверял никто, и он был порван: 04.09.2026 из шести
формулировок «учётка мертва», которые реально печатают движки, паттерн `session`
ловил ровно ОДНУ (Tidal). Мёртвый Deezer-ARL и мёртвый токен Qobuz доезжали до
финального `postprocess`, которого нет в таблице перебора, — и пул из двух ARL и
трёх токенов Qobuz не получал ни одной попытки. Ровно та жалоба, ради которой
`session` в таблицу и добавляли 21.08.

Поэтому здесь не выдуманные строки, а те самые, что лежат в движках, плюс
проверка, что они там всё ещё лежат: если кто-то перефразирует ошибку в движке,
тест упадёт здесь, а не тихо отключит пул на полтора месяца.
"""
import re
from pathlib import Path

import pytest

from ripster.runner import _classify_partial_reason
from ripster.account_fallback import RETRY_WITH_NEXT_ACCOUNT

_ENGINES = Path(__file__).resolve().parents[1] / "ripster" / "engines"

# (движок, кусок реальной строки ошибки, почему это повод для следующей учётки)
DEAD_CREDENTIAL_MESSAGES = [
    ("deezer.py", "ARL не задан или протух",                 "ARL мёртв"),
    ("deezer.py", "Неверный ARL",                            "ARL отвергнут"),
    ("qobuz.py",  "токен недействителен",                    "токен Qobuz мёртв"),
    ("qobuz.py",  "бесплатный аккаунт",                      "у этой учётки нет подписки"),
    ("tidal.py",  "TIDAL_NOT_AUTHED",                        "Tidal не авторизован"),
    ("tidal.py",  "сессия истекла/недействительна",          "сессия Tidal умерла"),
    ("yandex.py", "токен недействителен/просрочен",          "токен Yandex мёртв"),
    ("yandex.py", "нужна подписка Plus",                     "у этой учётки нет Plus"),
]

# Аварии на НАШЕЙ стороне. Перебор здесь запрещён намеренно: иначе одна общая
# авария прогонит и сожжёт все учётки подряд (см. account_fallback.py).
NOT_AN_ACCOUNT_PROBLEM = [
    "Connection reset by peer",
    "ReadTimeout: HTTPSConnectionPool timed out",
    "wrapper container failed to start",
    "track not found at desired bitrate and no alternative found",
    "ModuleNotFoundError: No module named 'construct'",
]


@pytest.mark.parametrize("engine,fragment,why", DEAD_CREDENTIAL_MESSAGES)
def test_message_still_lives_in_the_engine(engine, fragment, why):
    """Строка не должна разъехаться с движком незаметно."""
    src = (_ENGINES / engine).read_text(encoding="utf-8")
    assert fragment in src, (
        f"{engine} больше не печатает «{fragment}». Формулировку перефразировали — "
        f"обнови и её здесь, и паттерн в runner._PARTIAL_REASON_PATTERNS, иначе "
        f"перебор учёток для этого отказа молча выключится."
    )


@pytest.mark.parametrize("engine,fragment,why", DEAD_CREDENTIAL_MESSAGES)
def test_dead_credential_hands_task_to_next_account(engine, fragment, why):
    reason = _classify_partial_reason(fragment, permanent=False)
    assert reason in RETRY_WITH_NEXT_ACCOUNT, (
        f"{why}: «{fragment}» → {reason!r}, а этого токена нет в таблице перебора "
        f"{sorted(RETRY_WITH_NEXT_ACCOUNT)} — значит вторая учётка не получит ни "
        f"одной попытки."
    )


@pytest.mark.parametrize("msg", NOT_AN_ACCOUNT_PROBLEM)
def test_our_own_outage_does_not_burn_the_pool(msg):
    reason = _classify_partial_reason(msg, permanent=False)
    assert reason not in RETRY_WITH_NEXT_ACCOUNT, (
        f"«{msg}» → {reason!r}: авария на нашей стороне не должна прогонять все "
        f"учётки сервиса."
    )


def test_permanent_flag_still_wins_when_nothing_matches():
    """Расширение паттернов не должно съесть прежний фоллбэк."""
    assert _classify_partial_reason("ничего узнаваемого", permanent=True) == "region"
    assert _classify_partial_reason("ничего узнаваемого", permanent=False) == "postprocess"


def test_word_protuh_is_only_ever_about_credentials():
    """Паттерн берёт «протух» целиком — это верно, только пока слово не начали
    употреблять про что-то ещё (кэш, ссылку, манифест)."""
    bad = []
    for path in sorted(_ENGINES.glob("*.py")):
        for i, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            # Только то, что может стать текстом в логе: комментарии и прозу
            # docstring'ов классификатор не видит и видеть не должен.
            if line.lstrip().startswith("#") or '"' not in line and "'" not in line:
                continue
            if re.search(r"протух", line, re.I) and not re.search(
                r"ARL|токен|token|cookie|сесси|подписк|аккаунт|учётк|авториз|"
                r"устройств|GetToken|bearer", line, re.I
            ):
                bad.append(f"{path.name}:{i}: {line.strip()[:90]}")
    assert not bad, (
        "«протух» появилось не про учётные данные — паттерн `session` теперь "
        "ловит лишнее:\n" + "\n".join(bad)
    )
