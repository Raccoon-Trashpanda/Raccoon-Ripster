"""Shared classifier for cross-service download failures every engine hits.

Two classes the owner flagged as needing real, honest messaging (not a generic
"error") across Apple/Tidal/Spotify/Qobuz/Deezer/etc.:
  • REGION — the content exists but is geo-locked to another region (ALAC hitting
    the region wall, a Tidal album not in the account's country, …).
  • GONE   — a phantom/dead link: the URL still resolves but the service has
    REMOVED the release (common on Spotify — the link shows but the track is gone).

Engines call ``classify_download_error(log_text)`` in their is_finished fallback;
the message propagates to the bot/UI automatically (both render the task error).
"""
from __future__ import annotations

import re

_PATTERNS: list[tuple[str, "re.Pattern[str]", str]] = [
    ("region",
     re.compile(
         r"region[\s\-]?lock|geo[\s\-]?block|geo[\s\-]?restrict|region[\s\-]?restrict"
         r"|not available in (your|this) (country|region)"
         r"|unavailable in (your|this) (country|region)"
         r"|not available in your country", re.I),
     "недоступно в регионе твоего аккаунта (гео-блок) — попробуй другой сервис "
     "(Apple/Qobuz/Deezer/Tidal) или смени регион аккаунта в Настройках."),
    # Go-загрузчик Apple: каталог не отдал альбом. Ловим ДО общего «gone», потому
    # что причина здесь конкретнее: чаще всего релиз просто не существует в том
    # магазине, чей код стоит в ссылке. Живой случай 08.08.2026 — альбом Aathi:
    # ссылка была /gb/, а альбом есть ТОЛЬКО в индийском магазине (проверено
    # публичным iTunes API: in → 1 результат, gb/us/ca/nz → 0).
    #
    # Отдельная важность: сам загрузчик при этом выходит с кодом 0, поэтому без
    # этого правила человек получал «✗ Exit code 0» — сообщение, из которого
    # причину узнать невозможно (см. скилл ripster-honest-diagnostics).
    ("storefront",
     re.compile(r"Failed to (get album response|rip album)"
                r"|error getting album response", re.I),
     # Без слова «Apple» в начале: runner подставляет название сервиса сам,
     # иначе получается «Apple: Apple не отдал…».
     "каталог не отдал этот релиз. Обычно это значит, что его нет в том магазине, "
     "чей код стоит в ссылке (туда подставляется страна того, кто делился) — "
     "проверь, в какой стране релиз издан, и возьми ссылку оттуда."),
    ("gone",
     re.compile(
         r"\bnot found\b|no longer available|has been removed|been deleted"
         r"|does not exist|\b404\b|track (is )?not available|cannot be found"
         r"|removed from", re.I),
     "контент удалён из сервиса или ссылка фантомная (ведёт на уже отсутствующий "
     "релиз) — проверь ссылку или поищи тот же релиз на другом сервисе."),
]


# Магазины, по которым ищем релиз, когда его нет в нашем. Список короткий
# намеренно: цель — назвать человеку рабочую страну, а не составить полную
# карту. Порядок — по вероятности для нашей музыки.
_STOREFRONTS = ("us", "gb", "de", "fr", "nl", "ca", "au", "nz",
                "br", "jp", "ru", "in", "se", "pl")

_RE_APPLE_ID = re.compile(r"/(?:album|song)/[^/]*/(\d+)")


def apple_album_by_storefront(url_or_id: str, timeout: float = 5.0) -> dict[str, tuple[str, str]]:
    """Где релиз есть и под КАКИМ там идентификатором: {страна: (id, название)}.

    🔴 Главное, ради чего функция переписана 08.08.2026. Сначала здесь была
    проверка «один и тот же ID по всем магазинам», и она соврала: альбом Apparat
    «A Hum Of Maybe» показал 404 в us/ca/jp, я сделал вывод «в Канаде релиза
    нет», а владелец возразил — и оказался прав. Apple выдаёт релизу СВОЙ
    идентификатор в разных витринах:

        ru, gb, de, …  →  1850997297
        ca, us         →  1852529754
        jp             →  1878218670

    Поиск по ID отвечает 404 просто потому, что в этой витрине у альбома другой
    номер. Вывод «недоступно в регионе» из такого 404 не следует вообще.

    Поэтому ищем ПО НАЗВАНИЮ и артисту. Название берём из первой витрины, где
    ID отозвался, — так не нужно угадывать его из ссылки.

    Спрашиваем открытый iTunes API, а не amp-api: последнему нужен dev_token,
    живущий пять минут (см. скилл ripster-apple-wrapper), — диагностика,
    протухающая сама по себе, хуже отсутствующей.

    Пустой ответ означает «не выяснили» (сеть, лимит, чужая ссылка), а НЕ
    «нигде нет» — путать эти два состояния и есть та самая ошибка выше.
    """
    import json as _json
    import urllib.parse as _up
    import urllib.request as _ur
    from concurrent.futures import ThreadPoolExecutor

    m = _RE_APPLE_ID.search(url_or_id or "")
    aid = m.group(1) if m else (url_or_id.strip() if (url_or_id or "").strip().isdigit() else "")
    if not aid:
        return {}

    def _get(u: str):
        try:
            with _ur.urlopen(u, timeout=timeout) as r:
                return _json.load(r) or {}
        except Exception:
            return {}

    # 1. Узнаём название и артиста — по ID в той витрине, где он вообще известен.
    name = artist = ""
    for cc in ("us", "gb", "ru", "de", "jp"):
        d = _get(f"https://itunes.apple.com/lookup?id={aid}&country={cc}")
        for r in d.get("results", []):
            if r.get("collectionName"):
                name, artist = r["collectionName"], r.get("artistName") or ""
                break
        if name:
            break
    if not name:
        return {}

    # 2. Ищем ЭТО НАЗВАНИЕ по витринам — у каждой свой номер альбома.
    term = _up.quote(f"{artist} {name}".strip())
    want = _norm_title(name)

    def _find(cc: str) -> tuple[str, tuple[str, str]] | None:
        d = _get(f"https://itunes.apple.com/search?term={term}&country={cc}"
                 f"&entity=album&limit=8")
        for r in d.get("results", []):
            if _norm_title(r.get("collectionName") or "") == want:
                return cc, (str(r.get("collectionId") or ""), r.get("collectionName") or "")
        return None

    out: dict[str, tuple[str, str]] = {}
    try:
        with ThreadPoolExecutor(max_workers=8) as ex:
            for got in ex.map(_find, _STOREFRONTS):
                if got:
                    out[got[0]] = got[1]
    except Exception:
        return out
    return out


def _norm_title(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", (s or "").lower())


_RE_TRACEBACK_HDR = re.compile(r"^Traceback \(most recent call last\):\s*$", re.M)


def extract_traceback_summary(log_text: str) -> str | None:
    """When *log_text* contains a real Python traceback (an unhandled exception
    in a CLI subprocess, e.g. the ``amz``/AppleMusicDecrypt/etc. tools), return
    the actual ``ExceptionType: message`` tail line instead of the useless
    generic header.

    Bug this fixes: engines that pick "the last line matching an error-ish
    regex, searching backwards" can land on the traceback HEADER line
    ("Traceback (most recent call last):") instead of the real exception
    line beneath it — the header contains the word "traceback" (matches a
    generic error regex) but the actual exception line often does NOT match
    (e.g. "ValueError: ..." has no `\\berror\\b` word boundary — "Error" is
    glued to "Value" with no separator, and doesn't contain "exception" as a
    literal substring either). Confirmed live: a guest's Amazon Music
    download crashed with an unhandled exception and the ONLY text that
    reached them was "Traceback (most recent call last):" — worse than no
    message at all, since it looks like a message but says nothing.

    Traceback frame lines ("File "...", line N, in <func>" and the source
    line under it) are indented; the actual exception line is the last
    NON-indented, non-empty line after the header — take that.
    """
    if not log_text:
        return None
    last_hdr = None
    for m in _RE_TRACEBACK_HDR.finditer(log_text):
        last_hdr = m
    if not last_hdr:
        return None
    tail = log_text[last_hdr.end():]
    exc_line = None
    for line in tail.splitlines():
        if line and not line[0].isspace():
            exc_line = line.strip()
    return exc_line[:300] if exc_line else None


def classify_download_error(log_text: str) -> tuple[str, str] | None:
    """Return ``(category, user_message)`` for a recognized cross-service failure,
    or ``None`` if nothing matched. Categories: ``'region'`` | ``'gone'``.

    REGION is checked before GONE: a geo-locked item often also says "not found"
    in the wrong region, but the actionable cause is the region wall.
    """
    text = log_text or ""
    # A bitrate-availability failure ("track not found at desired bitrate and no
    # alternative found") is NOT a dead/phantom link — the release exists, just not
    # in the requested quality. Don't let the greedy GONE "not found" pattern
    # mislabel it; the engine already surfaces the real bitrate reason.
    if re.search(r"desired bitrate", text, re.I):
        return None

    # Если в этом же прогоне треки СОХРАНЯЛИСЬ, вердикты «контента нет» и «нет в
    # этом магазине» заведомо ложны: несуществующий релиз не может отдать файлы.
    # 09.08.2026 альбом Apparat скачался на 10 треков из 11 и был объявлен
    # «контент удалён из сервиса или ссылка фантомная» — потому что в чужом
    # выводе где-то мелькнуло «not found». Совет из такого вердикта («проверь
    # ссылку, поищи на другом сервисе») уводит ровно в противоположную сторону
    # от настоящей причины.
    saved_any = re.search(r"Track \d+ of \d+ saved|SONG:\s*SAVED|Completed download of",
                          text, re.I)
    for cat, rx, msg in _PATTERNS:
        if rx.search(text):
            if saved_any and cat in ("gone", "storefront"):
                continue          # ищем причину дальше, эта опровергнута диском
            return cat, msg
    return None
