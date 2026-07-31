"""«Раскопки» (Digs) — шаг 1: профиль вкуса из собственной статистики.

Замысел (TASKS_NEXT п.8, 29.07.2026): потоки советуют ЧТО ПОСЛУШАТЬ, а Ripster
может советовать ЧТО ЗАБРАТЬ в lossless — и знает, чего у человека ещё НЕТ. Ни
один чужой сервис так не умеет: у него нет твоей фонотеки.

Здесь только ПЕРВЫЙ шаг: понять вкус по тому, что уже сделано. Порядок работ
заведён так сознательно — профиль показывается владельцу живьём и оценивается на
осмысленность ДО того, как на него навешивать чарты и рейтинги. Строить подбор
поверх непроверенного профиля значит потом гадать, что именно врёт.

Что считается сигналом и почему разный вес:

  СКАЧАЛ  — самый сильный сигнал, какой у нас есть. Скачивание это осознанное
            «хочу оставить у себя», в отличие от прослушивания, которое часто
            фоновое или случайное. Считается по УНИКАЛЬНЫМ (артист, альбом):
            десять перекачек одного альбома это одно решение, а не десять.
  СЛУШАЛ  — слабее, но зато массово и честно: что реально играло.
  ВИШЛИСТ — намерение на будущее, ещё не реализованное.

Жанра в статистике нет вообще (проверено: таблицы downloads / stream_events /
ws_sessions его не содержат), поэтому жанры добираются по артистам из iTunes
Search — бесплатно, без ключа, тем же путём, которым уже пользуются zhaarey и
tagger. Результат кэшируется на диск: список артистов между запусками почти не
меняется, а дёргать сеть на каждый показ незачем.
"""
from __future__ import annotations

import asyncio
import json
import re
import sqlite3
import time
from pathlib import Path

from ripster.watchlist_suggest import (
    _is_noise, _mine_title_credits, _norm, _recency_weight, _split_credit,
)

_BASE = Path(__file__).parent.parent
_DB = _BASE / "ripster_stats.db"
_HISTORY = _BASE / "history.json"
_GENRE_CACHE = _BASE / "digs_genre_cache.json"

# Вес сигналов друг относительно друга. Скачивание примерно втрое весомее
# прослушивания: это решение оставить у себя, а не просто включить.
_W_DOWNLOAD = 3.0
_W_PLAY     = 1.0
_W_WATCH    = 2.0

# «Artist — Title» в stream_events. Тире именно длинное: так их пишет
# сам Ripster (см. stats_collector), обычный дефис встречается внутри названий.
_STREAM_SPLIT = re.compile(r"\s+—\s+")

# ── Три искажения, найденные на живых данных 31.07.2026 ──────────────────────
# Без них профиль получался бессмысленным: 78% веса забирал один лейбл.
#
# 1. ОДНО ПРОСЛУШИВАНИЕ ≠ ОДНО СОБЫТИЕ. У одного выпуска 428 событий `start`:
#    перемотка, переподключение и повторный запуск пишут новое событие. Считаем
#    РАЗНЫЕ названия, а не события.
# 2. РАДИО-ШОУ И ЛЕЙБЛЫ — НЕ АРТИСТЫ. В SoundCloud «артист» это владелец канала,
#    поэтому Anjunadeep, Mixmag, Balance Music выглядели как самые любимые
#    исполнители. Слушать подкаст лейбла — это про жанр, а не про артиста.
# 3. МНОГО ПРОСЛУШИВАНИЙ ≠ СИЛЬНЕЕ ЛЮБИТ. 1500 фоновых включений не весомее
#    десяти осознанных скачиваний, поэтому прослушивания демпфируются корнем:
#    рост есть, но он не линейный.
_SHOW_MARKERS = (
    "podcast", "radio", "mixtape", "edition", "selections", "sessions",
    "episode", "group therapy", "lab", "takeover", "guest mix", "residency",
    "essential mix", "live set", "dj set", "chart", "showcase",
)
_LABEL_MARKERS = ("records", "recordings", "label", "music group", "collective")
# Медиа-бренды и площадки: у них нет нумерации выпусков и своё имя стоит не в
# каждом названии («The Cover Mix: NOTION»), поэтому признаки выше их не берут.
# Список ручной — ровно так же, как _STOP_ARTISTS у подсказок вишлиста: их
# немного, они не меняются, и это честнее, чем натягивать эвристику на один
# канал и ломать ею живых исполнителей.
_BRAND_CHANNELS = {
    "mixmag", "dj mag", "djmag", "resident advisor", "ra", "boiler room",
    "fact magazine", "cercle", "defected", "toolroom", "beatport",
    "hospital records", "drumandbassarena", "uKf", "ukf", "monstercat",
    "proximity", "trap nation", "nest hq", "majestic casual", "the lot radio",
}
# «… 596», «Vol. 12», «#245» — нумерация выпусков, главный признак сериальности.
_EPISODE_NUM_RE = re.compile(r"(?:\b|#)(\d{2,4})\b")
# Гость выпуска: «Edition 596 with PROFF». Это настоящий артист, и это ровно тот
# сигнал, ради которого шоу вообще слушают.
_GUEST_RE = re.compile(r"\bwith\s+([^|(\[\]/]{2,50}?)(?=\s*(?:[|(\[/,]|$))", re.I)

_GENRE_TTL = 30 * 86_400        # жанр артиста не меняется — перечитывать раз в месяц
_GENRE_CONCURRENCY = 6          # iTunes не любит шквал, но 6 держит спокойно


# ── Сбор сырых сигналов ───────────────────────────────────────────────────────

def _bump(acc: dict, name: str, weight: float, ts: int, kind: str) -> None:
    """Добавить вес артисту. `acc` ключуется нормализованным именем, но наружу
    отдаётся тот вариант написания, который встретился чаще — иначе в выдаче
    появляется 'depeche mode' вместо 'Depeche Mode'."""
    if _is_noise(name):
        return
    key = _norm(name)
    if not key:
        return
    rec = acc.setdefault(key, {
        "display": {}, "score": 0.0, "downloads": 0, "plays": 0,
        "watch": 0, "last_ts": 0, "albums": set(), "services": {},
    })
    rec["display"][name] = rec["display"].get(name, 0) + 1
    rec["score"] += weight * _recency_weight(ts, int(time.time()))
    rec["last_ts"] = max(rec["last_ts"], ts)
    if kind in ("downloads", "plays", "watch"):
        rec[kind] += 1


def _collect_downloads(acc: dict) -> int:
    """Успешные загрузки. Дедуп по (артист, альбом): повторная качка того же
    альбома — то же самое решение, а не новое."""
    if not _DB.exists():
        return 0
    seen: set[tuple] = set()
    n = 0
    try:
        db = sqlite3.connect(f"file:{_DB}?mode=ro", uri=True)
        db.row_factory = sqlite3.Row
        rows = db.execute(
            "SELECT ts, service, artist, album, title FROM downloads "
            "WHERE status='done' AND artist IS NOT NULL AND artist!=''"
        ).fetchall()
        db.close()
    except sqlite3.Error:
        return 0
    for r in rows:
        credit = (r["artist"] or "").strip()
        album  = (r["album"] or r["title"] or "").strip()
        ts     = int(r["ts"] or 0)
        names  = _split_credit(credit) + _mine_title_credits(r["title"] or "")
        for name in names:
            k = (_norm(name), _norm(album))
            if k in seen:
                continue
            seen.add(k)
            _bump(acc, name, _W_DOWNLOAD, ts, "downloads")
            key = _norm(name)
            if key in acc:
                if album:
                    acc[key]["albums"].add(album)
                svc = (r["service"] or "").strip()
                if svc:
                    acc[key]["services"][svc] = acc[key]["services"].get(svc, 0) + 1
            n += 1
    return n


def _series_signature(lows: list[str]) -> tuple[float, int]:
    """(доля выпусков под самым частым заголовком, сколько у него разных номеров).

    Заголовок = название с вырезанными числами, первые четыре слова. У серии
    таких заголовков один на всех, а номера разные; у исполнителя наоборот —
    заголовки все разные, а числа случайные (даты, годы).
    """
    import collections
    stems: collections.Counter = collections.Counter()
    nums: dict[str, set] = {}
    for t in lows:
        m = _EPISODE_NUM_RE.search(t)
        if not m:
            continue          # без номера выпуска серии не бывает
        # Имя серии — это то, что стоит ДО номера: «Balance Selections 372:
        # Raw Main» → «balance selections». Брать первые слова всей строки
        # нельзя: после номера идёт имя гостя, и у каждого выпуска оно своё,
        # отчего один и тот же цикл рассыпался на два десятка «разных» серий.
        head = " ".join(re.sub(r"\W+", " ", t[:m.start()]).split()[:4])
        if not head:
            continue
        stems[head] += 1
        nums.setdefault(head, set()).add(m.group(1))
    if not stems:
        return 0.0, 0
    stem, cnt = stems.most_common(1)[0]
    return cnt / len(lows), len(nums.get(stem, ()))


def _looks_like_show(name: str, titles: list[str]) -> bool:
    """Канал это радио-шоу/лейбл, а не исполнитель?

    Решается по НАЗВАНИЯМ выпусков, а не по имени канала: имя «Balance Music»
    само по себе двусмысленно, а вот двадцать выпусков «Balance Selections 372»,
    «Balance Croatia 023» не оставляют вопросов. Имя учитывается только как
    вторичный признак (слово «Records» в названии артиста почти всегда лейбл).
    """
    low_name = _norm(name)
    if low_name in _BRAND_CHANNELS:
        return True
    if any(m in low_name for m in _LABEL_MARKERS):
        return True
    if not titles:
        return False
    n = len(titles)
    lows = [t.lower() for t in titles]

    # Точный признак серии — ПОВТОРЯЮЩИЙСЯ ЗАГОЛОВОК С РАСТУЩИМ НОМЕРОМ:
    # «The Anjunadeep Edition 596 / 595 / 597», «Balance Selections 372 / 371»,
    # «Planeta Amulanga 035 / 034». Проверять просто «имя канала встречается в
    # названиях» нельзя — так под нож попадали живые исполнители, которые
    # выкладывают свои же сеты («Max Cooper — Max Cooper Live at Das Bett»,
    # «YOTTO — YOTTO All Night Long»): имя есть, а серии нет. Разница именно в
    # том, что у серии ОДИН заголовок и много номеров, а у артиста заголовки
    # всегда разные.
    share, distinct_nums = _series_signature(lows)
    if share >= 0.4 and distinct_nums >= 3:
        return True

    marked = sum(1 for t in lows if any(m in t for m in _SHOW_MARKERS))
    if n < 3:
        return False

    # Бренд без нумерации — «Mixmag Lab», «The Cover Mix». Номеров нет, но имя
    # канала стоит почти в каждом названии И почти каждое несёт слово-признак
    # рубрики. Оба условия обязательны вместе: одно самоназвание ловит живых
    # исполнителей, выкладывающих свои сеты, а одно слово «radio» — треки, у
    # которых оно просто в названии.
    words = [w for w in re.split(r"\W+", low_name) if len(w) > 3]
    if words:
        selfnamed = sum(1 for t in lows if words[0] in t)
        if selfnamed / n >= 0.6 and marked / n >= 0.5:
            return True

    numbered = sum(1 for t in lows if _EPISODE_NUM_RE.search(t))
    return marked / n >= 0.5 and numbered / n >= 0.5


def _collect_plays(acc: dict, shows: dict) -> int:
    """Прослушивания. Имя артиста лежит в stream_name как «Артист — Название»;
    у bbc и apple там идентификатор, и разобрать его нечем — такие пропускаем,
    молча и осознанно, а не пытаемся угадать.

    Каналы-шоу уходят в отдельный список `shows`: они формируют жанровую
    картину, но артистами-опорами не являются. Из их выпусков при этом
    вынимаются ГОСТИ — вот они настоящие артисты.
    """
    if not _DB.exists():
        return 0
    try:
        db = sqlite3.connect(f"file:{_DB}?mode=ro", uri=True)
        db.row_factory = sqlite3.Row
        # DISTINCT по названию: 428 событий `start` у одного выпуска — это
        # перемотки, а не 428 прослушиваний.
        rows = db.execute(
            "SELECT stream_name, MAX(ts) ts, COUNT(*) hits "
            "FROM stream_events WHERE event='start' AND stream_name IS NOT NULL "
            "GROUP BY stream_name"
        ).fetchall()
        db.close()
    except sqlite3.Error:
        return 0

    # Сгруппировать по владельцу канала, чтобы решать «шоу или артист» разом.
    by_owner: dict[str, dict] = {}
    for r in rows:
        parts = _STREAM_SPLIT.split((r["stream_name"] or "").strip(), 1)
        if len(parts) != 2:
            continue
        owner, title = parts[0].strip(), parts[1].strip()
        o = by_owner.setdefault(_norm(owner), {"display": owner, "titles": [],
                                               "ts": 0, "hits": 0})
        o["titles"].append(title)
        o["ts"] = max(o["ts"], int(r["ts"] or 0))
        o["hits"] += int(r["hits"] or 0)

    n = 0
    for key, o in by_owner.items():
        titles, ts = o["titles"], o["ts"]
        # Демпфирование: 81 выпуск даёт вес 9, а не 81. Фоновое слушание не
        # должно перевешивать осознанные скачивания.
        damped = len(titles) ** 0.5

        if _looks_like_show(o["display"], titles):
            shows[key] = {
                "name": o["display"], "episodes": len(titles),
                "plays": o["hits"], "last_ts": ts,
            }
            # Гости выпусков — настоящие артисты, и это лучший сигнал шоу.
            guests: dict[str, int] = {}
            for t in titles:
                for m in _GUEST_RE.finditer(t):
                    # «with Above & Beyond and Ashibah» — это ДВА гостя. ' and '
                    # здесь надёжный разделитель (в отличие от ' & ', который
                    # часто внутри одного имени: «Above & Beyond»).
                    for chunk in re.split(r"\s+and\s+", m.group(1).strip(), flags=re.I):
                        for g in _split_credit(chunk.strip()):
                            if not _is_noise(g):
                                guests[g] = guests.get(g, 0) + 1
            for g, cnt in guests.items():
                _bump(acc, g, _W_PLAY * (cnt ** 0.5), ts, "plays")
                n += 1
            shows[key]["guests"] = sorted(guests, key=guests.get, reverse=True)[:8]
            continue

        for name in _split_credit(o["display"]):
            _bump(acc, name, _W_PLAY * damped, ts, "plays")
            key2 = _norm(name)
            if key2 in acc:
                acc[key2]["plays"] = len(titles)
            n += 1
    return n


def _collect_history(acc: dict) -> int:
    """history.json — те же загрузки, но с обложками; берём ради обложки
    артиста, вес не начисляем повторно (иначе одно решение считается дважды)."""
    covers: dict[str, str] = {}
    if not _HISTORY.exists():
        return 0
    try:
        items = json.loads(_HISTORY.read_text(encoding="utf-8"))
    except Exception:
        return 0
    if not isinstance(items, list):
        return 0
    for it in items:
        if not isinstance(it, dict):
            continue
        art = (it.get("artist") or "").strip()
        cov = (it.get("artworkUrl") or "").strip()
        if not art or not cov:
            continue
        for name in _split_credit(art):
            covers.setdefault(_norm(name), cov)
    for key, cov in covers.items():
        if key in acc:
            acc[key].setdefault("cover", cov)
    return len(covers)


def _collect_watchlist(acc: dict) -> int:
    wl = _BASE / "watchlist.json"
    if not wl.exists():
        return 0
    try:
        data = json.loads(wl.read_text(encoding="utf-8"))
    except Exception:
        return 0
    items = data if isinstance(data, list) else (data.get("items") or [])
    now = int(time.time())
    n = 0
    for it in items:
        if not isinstance(it, dict):
            continue
        name = (it.get("name") or it.get("artist") or "").strip()
        if not name:
            continue
        _bump(acc, name, _W_WATCH, now, "watch")
        n += 1
    return n


# ── Жанры (в статистике их нет — добираем из iTunes) ──────────────────────────

def _load_genre_cache() -> dict:
    if not _GENRE_CACHE.exists():
        return {}
    try:
        d = json.loads(_GENRE_CACHE.read_text(encoding="utf-8"))
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def _save_genre_cache(cache: dict) -> None:
    try:
        _GENRE_CACHE.write_text(json.dumps(cache, ensure_ascii=False, indent=1),
                                encoding="utf-8")
    except OSError:
        pass


async def _fetch_genre(client, name: str) -> str:
    """primaryGenreName артиста из iTunes Search. Пусто — если не нашёлся;
    это НЕ ошибка, просто у артиста не будет жанра.

    Имя в ответе СВЕРЯЕТСЯ с запросом. iTunes всегда возвращает что-нибудь: на
    «Balance Music» он отдал христианский коллектив, и профиль всерьёз считал,
    что владелец слушает Christian. Лучше не знать жанр, чем знать неверный —
    неверный потом молча утечёт в подбор и объяснит рекомендацию тем, чего нет.
    """
    try:
        r = await client.get("https://itunes.apple.com/search", params={
            "term": name, "entity": "musicArtist", "limit": 3,
        }, timeout=10)
        if r.status_code != 200:
            return ""
        for res in ((r.json() or {}).get("results") or []):
            if _norm(res.get("artistName") or "") == _norm(name):
                return (res.get("primaryGenreName") or "").strip()
        return ""
    except Exception:
        return ""


async def enrich_genres(names: list[str]) -> dict:
    """Жанр для каждого имени. Кэш на диске: между показами список артистов
    почти не меняется, а сеть дёргать на каждый показ незачем."""
    import httpx

    cache = _load_genre_cache()
    now = int(time.time())
    todo = [n for n in names
            if (now - int((cache.get(_norm(n)) or {}).get("ts", 0))) > _GENRE_TTL]

    if todo:
        sem = asyncio.Semaphore(_GENRE_CONCURRENCY)
        async with httpx.AsyncClient() as client:
            async def one(nm: str) -> None:
                async with sem:
                    g = await _fetch_genre(client, nm)
                    cache[_norm(nm)] = {"genre": g, "ts": now}
            await asyncio.gather(*(one(n) for n in todo), return_exceptions=True)
        _save_genre_cache(cache)

    return {n: (cache.get(_norm(n)) or {}).get("genre", "") for n in names}


# ── Профиль ───────────────────────────────────────────────────────────────────

async def build_profile(limit: int = 40, with_genres: bool = True) -> dict:
    """Профиль вкуса: артисты-опоры по весу + жанры по весу.

    Ничего не рекомендует и никуда не ходит за чартами — это намеренно. Сначала
    надо убедиться, что профиль вообще похож на правду.
    """
    acc: dict = {}
    shows: dict = {}
    n_dl = _collect_downloads(acc)
    n_pl = _collect_plays(acc, shows)
    n_wl = _collect_watchlist(acc)
    _collect_history(acc)

    artists = []
    for key, rec in acc.items():
        display = max(rec["display"].items(), key=lambda kv: kv[1])[0]
        artists.append({
            "key":       key,
            # Лейбл/шоу может попасть сюда и через ЗАГРУЗКИ: у сборников в поле
            # артиста стоит лейбл («Anjunadeep», «Balance Music»). Помечаем, но
            # не выбрасываем — качали их по-настоящему. Из жанровой картины
            # такие исключаются: «жанр лейбла» ничего не значит, а iTunes на
            # «Balance Music» отдаёт христианский коллектив, и профиль всерьёз
            # считал, что владелец слушает Christian.
            "is_show":   key in shows,
            "name":      display,
            "score":     round(rec["score"], 2),
            "downloads": rec["downloads"],
            "plays":     rec["plays"],
            "watched":   bool(rec["watch"]),
            "albums":    len(rec["albums"]),
            "last_ts":   rec["last_ts"],
            "services":  sorted(rec["services"], key=rec["services"].get, reverse=True)[:3],
            "cover":     rec.get("cover", ""),
        })
    artists.sort(key=lambda a: a["score"], reverse=True)
    top = artists[:limit]

    genres_by_artist: dict = {}
    if with_genres and top:
        genres_by_artist = await enrich_genres([a["name"] for a in top])
        for a in top:
            a["genre"] = genres_by_artist.get(a["name"], "")

    # Жанр весит столько, сколько весят стоящие за ним артисты — иначе один
    # часто качаемый артист и десять разовых считались бы одинаково.
    genre_weight: dict = {}
    for a in top:
        g = (a.get("genre") or "").strip()
        if not g or a.get("is_show"):
            continue
        gw = genre_weight.setdefault(g, {"genre": g, "score": 0.0, "artists": []})
        gw["score"] += a["score"]
        gw["artists"].append(a["name"])
    genres = sorted(genre_weight.values(), key=lambda g: g["score"], reverse=True)
    total = sum(g["score"] for g in genres) or 1.0
    for g in genres:
        g["share"] = round(100.0 * g["score"] / total, 1)
        g["score"] = round(g["score"], 2)
        g["artists"] = g["artists"][:6]

    show_list = sorted(shows.values(), key=lambda s: s["episodes"], reverse=True)[:12]

    return {
        "artists": top,
        "genres":  genres,
        # Радио-шоу и лейблы держим ОТДЕЛЬНО, а не выбрасываем: подписка на
        # подкаст лейбла — сильный жанровый сигнал и источник имён гостей,
        # просто это не «любимый исполнитель».
        "shows":   show_list,
        "totals": {
            "artists_known":   len(artists),
            "download_events": n_dl,
            "play_events":     n_pl,
            "watchlist":       n_wl,
            "shows":           len(shows),
        },
        "generated_ts": int(time.time()),
    }
