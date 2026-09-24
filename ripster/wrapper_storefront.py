"""Разбор диалога Apple «Invalid store» из лога wrapper'а.

Сессия жива, порт открыт, ключ не выдан — и почти всегда на это отвечают
одинаково: «ключа нет». Но у «ключа нет» две противоположные причины:

  · релиз просто не издан в витрине аккаунта — лечится ссылкой из своей витрины
    или другой своей учёткой (это делает apple_router);
  · сам аккаунт Apple закреплён НЕ за той страной, где оформлена подписка.
    Wrapper авторизуется в витрине X, а покупать/расшифровывать этому аккаунту
    разрешено только в витрине Y. Это свойство аккаунта на стороне Apple: ни
    перелогин, ни `-I` (локаль), ни смена ссылки не помогают — и гонять по этому
    поводу лестницу слотов бессмысленно.

Apple выдаёт вторую причину отдельным диалогом в stderr wrapper'а:

    Invalid store: You are signed in with an Apple Account that is not valid
    for use in the Canadian Store. You may only purchase music from the
    U.K. Store

Модуль вытаскивает из этой строки обе витрины и сводит английское прилагательное
(«Canadian», «U.K.») к коду страны, чтобы человек и бот получили внятный текст, а
не общий «ключа нет». Только разбор — без побочных эффектов, чтобы это было чем
тестировать.
"""
from __future__ import annotations

import re

# Apple пишет названия витрин прилагательными («Canadian», «U.K.»). Ключ —
# нормализованная форма (нижний регистр, точки убраны), значение — ISO-код
# витрины из таблицы `apple_accounts._STOREFRONT_ID_TO_CC`.
_STORE_WORD_TO_CC = {
    "us": "us", "unitedstates": "us", "american": "us",
    "uk": "gb", "unitedkingdom": "gb", "greatbritain": "gb",
    "british": "gb", "england": "gb",
    "canadian": "ca", "canada": "ca",
    "japanese": "jp", "japan": "jp",
    "german": "de", "germany": "de",
    "french": "fr", "france": "fr",
    "russian": "ru", "russia": "ru",
    "australian": "au", "australia": "au",
    "italian": "it", "italy": "it",
    "spanish": "es", "spain": "es",
    "dutch": "nl", "netherlands": "nl",
    "brazilian": "br", "brazil": "br",
    "indian": "in", "india": "in",
    "mexican": "mx", "mexico": "mx",
    "korean": "kr", "korea": "kr",
    "chinese": "cn", "china": "cn",
    "swedish": "se", "sweden": "se",
    "norwegian": "no", "norway": "no",
}

# Название витрины — между «the» и «Store». Нестяжательно, до первой точки или
# конца, чтобы «U.K. Store» не склеился со следующим предложением.
_SIGNED_RE = re.compile(
    r"not valid for use in the\s+([A-Za-z][A-Za-z. ]*?)\s+Store", re.I)
_ALLOWED_RE = re.compile(
    r"may only purchase music from the\s+([A-Za-z][A-Za-z. ]*?)\s+Store", re.I)


def _norm(name: str) -> str:
    """«U.K.» → «uk», «Canadian» → «canadian» — для поиска по таблице."""
    return re.sub(r"[^a-z]", "", (name or "").lower())


def store_to_cc(name: str) -> str:
    """Английское название витрины → ISO-код страны ('' если не распознано)."""
    return _STORE_WORD_TO_CC.get(_norm(name), "")


def parse_invalid_store(text: str) -> dict | None:
    """Разобрать строку лога wrapper'а.

    Возвращает None, если это НЕ диалог «Invalid store». Иначе словарь:
        signed_in      — витрина, в которую авторизовался wrapper (где НЕЛЬЗЯ)
        signed_in_cc   — её ISO-код ('' если не узнали)
        allowed        — витрина, где аккаунту покупать разрешено
        allowed_cc     — её ISO-код
    """
    if not text:
        return None
    low = text.lower()
    if "invalid store" not in low and "not valid for use in the" not in low:
        return None
    sm = _SIGNED_RE.search(text)
    am = _ALLOWED_RE.search(text)
    if not sm and not am:
        return None
    signed = (sm.group(1).strip() if sm else "")
    allowed = (am.group(1).strip() if am else "")
    if not signed and not allowed:
        return None
    # Настоящий конфликт витрин — когда названы обе ИЛИ хотя бы одна, но строка
    # об этом говорит явно («Invalid store» / «not valid for use»). Одна витрина
    # без второй всё равно диагноз: аккаунт не в своей стране.
    return {
        "signed_in": signed,
        "signed_in_cc": store_to_cc(signed),
        "allowed": allowed,
        "allowed_cc": store_to_cc(allowed),
    }
