"""Грядущие релизы: лента того, что ЕЩЁ НЕ ВЫШЛО.

Требование владельца (21.08.2026), дословно: «главное это 100 процентное
исключение галлюцинаций… реддит с их "а я слышал" — нахер их, нас интересуют
авторитетные издания, потоковые сервисы, магазины и полные метаданные».

Отсюда всё устройство модуля. Запись в ленту не «проверяется на достоверность»
постфактум — она физически НЕ МОЖЕТ появиться без предъявляемого источника.

## Контракт записи

Четыре поля обязательны, без любого из них `make_record` возвращает None:

    src         ключ источника (не название — ключ реестра ниже)
    src_url     страница/ресурс, который мы РЕАЛЬНО открыли
    fetched_at  когда открыли
    date_raw    строка даты ДОСЛОВНО как её отдал источник
    ident       UPC / ISRC / id в каталоге источника

`date_raw` хранится ОТДЕЛЬНО от разобранной `date` намеренно. Разбор — это наша
интерпретация; когда она разойдётся с источником (а она разойдётся: «Q4 2026»,
«Sept 2026», локальные форматы), будет с чем сверить. Запись, у которой мы
придумали дату, отличима от записи, где источник её назвал.

`ident` — не удобство, а условие слияния. Без идентификатора две записи с
похожими названиями склеятся в один несуществующий релиз; ровно так 16.08 в
ленте лейбла Fabric оказался артист «Dominik Fabrici». Поэтому записи без
`ident` между источниками НЕ СЛИВАЮТСЯ никогда.

## Ярусы источников — здесь и лежит защита от выдумки

    1 каталог   потоковый сервис/магазин с API: есть id и дата в ответе
    2 витрина   магазин со страницей товара: есть артикул/id товара
    3 издание   редакционный текст: есть ссылка и дата публикации, НО НЕТ id

**Ярус 3 не имеет права СОЗДАТЬ запись в ленте.** Он может только дополнить уже
существующую (ссылкой на разбор, цитатой, обложкой). Причина простая: у текста
нет идентификатора, а значит нечем доказать, что он про ТОТ ЖЕ релиз. Издание
пишет «новый альбом Bicep осенью» — это не запись о релизе, это ожидание.

Именно это правило делает галлюцинацию структурно невозможной, а не старательной
проверкой post-factum: неоткуда взяться записи, за которой не стоит каталог.

## Чего здесь СОЗНАТЕЛЬНО нет

Нет ни одного парсера, написанного «по памяти». Из контейнера, где писался этот
модуль, сеть к музыкальным API закрыта, проверить форму ответа было нечем —
поэтому реализованы ТОЛЬКО источники, чей разбор уже живёт в Ripster и работает
на живых данных. Остальные объявлены в реестре со статусом `planned`: они видны
в диагностике как «объявлен, не реализован», а не притворяются рабочими.

Пустой источник и выдуманный источник выглядят одинаково ровно один раз — пока
кто-нибудь не сверит с реальностью.
"""
from __future__ import annotations

import json
import re
import time
from datetime import datetime, timedelta

# ── Реестр источников ────────────────────────────────────────────────────────
# tier: 1 каталог, 2 витрина, 3 издание.
# state: 'live' — разбор уже работает в Ripster; 'planned' — объявлен, не написан.
#
# Каталоги, помеченные live, разбираются кодом, который в проекте уже есть и уже
# отвечает на живых данных. Это не доверие к моей памяти об их API — это ссылка
# на работающие функции.
SOURCES: dict = {
    # ── ярус 1: каталоги ─────────────────────────────────────────────────────
    "apple":      {"tier": 1, "state": "live",
                   "note": "watchlist._apple_artist_collections — предзаказы уже приходят"},
    "spotify":    {"tier": 1, "state": "live",
                   "note": "discovery._artist_spotify — release_date будущей датой"},
    "label":      {"tier": 1, "state": "live",
                   "note": "watchlist._label_releases — лейблы вотчлиста"},
    "deezer":     {"tier": 1, "state": "planned", "note": "разбор есть, отбор по дате не написан"},
    "qobuz":      {"tier": 1, "state": "planned", "note": "то же"},
    "tidal":      {"tier": 1, "state": "planned"},
    "beatport":   {"tier": 1, "state": "planned", "note": "api.beatport.com/v4 требует ключ (401)"},
    "musicbrainz":{"tier": 1, "state": "planned", "note": "нужен User-Agent и rate-limit 1 rps"},
    # ── ярус 2: витрины ──────────────────────────────────────────────────────
    "juno":       {"tier": 2, "state": "planned",
                   "note": "отдельный ежедневный sitemap пре-ордеров, проверен 21.08"},
    "junodownload":{"tier": 2, "state": "planned", "note": "закрыт для краулера, нужен свой клиент"},
    "traxsource": {"tier": 2, "state": "planned", "note": "/genre/<id>/<name>/upcoming — 200"},
    "bandcamp":   {"tier": 2, "state": "planned", "note": "разрешены 4 эндпоинта /api/"},
    "deejayde":   {"tier": 2, "state": "planned"},
    "boomkat":    {"tier": 2, "state": "planned"},
    "bleep":      {"tier": 2, "state": "planned"},
    "hdtracks":   {"tier": 2, "state": "planned"},
    "7digital":   {"tier": 2, "state": "planned"},
    "phonica":    {"tier": 2, "state": "planned"},
    "redeye":     {"tier": 2, "state": "planned"},
    "decks":      {"tier": 2, "state": "planned"},
    # ── ярус 3: издания. СОЗДАВАТЬ записи не могут, только дополнять ─────────
    "ra":         {"tier": 3, "state": "planned", "note": "Resident Advisor"},
    "mixmag":     {"tier": 3, "state": "planned"},
    "djmag":      {"tier": 3, "state": "planned"},
    "pitchfork":  {"tier": 3, "state": "planned"},
    "quietus":    {"tier": 3, "state": "planned"},
    "xlr8r":      {"tier": 3, "state": "planned"},
    "attack":     {"tier": 3, "state": "planned"},
    "bandcampdaily": {"tier": 3, "state": "planned"},
    "junodaily":  {"tier": 3, "state": "planned"},
    "beatportal":  {"tier": 3, "state": "planned"},
    "bbc":        {"tier": 3, "state": "live",
                   "note": "уже в радаре; здесь только как ДОПОЛНЕНИЕ, записи не создаёт"},
}

CREATING_TIERS = (1, 2)     # кто имеет право ЗАВЕСТИ запись
_STORE_CAP = 3000
_HORIZON_DAYS = 400         # дальше этого срока анонс — не анонс, а слух


def source_report() -> dict:
    """Что есть в реестре и что из этого реально работает.

    Отдельная функция, потому что «20+ источников» без разбивки по состоянию —
    это обещание, а не факт. Диагностика должна показывать оба числа.
    """
    live = [k for k, v in SOURCES.items() if v.get("state") == "live"]
    planned = [k for k, v in SOURCES.items() if v.get("state") != "live"]
    by_tier: dict = {}
    for k, v in SOURCES.items():
        by_tier.setdefault(v["tier"], []).append(k)
    return {"total": len(SOURCES), "live": sorted(live), "planned": sorted(planned),
            "by_tier": {t: sorted(ks) for t, ks in sorted(by_tier.items())}}


# ── Контракт записи ──────────────────────────────────────────────────────────

_DATE_RE = re.compile(r"(\d{4})-(\d{2})-(\d{2})")


def parse_date(raw: str) -> str:
    """Дословную дату источника → ISO, или пусто.

    Пусто — это ответ, а не сбой. Запись без разобранной даты в ленту не
    попадёт, но `date_raw` у неё останется, и в диагностике будет видно, ЧТО
    именно не разобралось. Молча подставлять «сегодня» нельзя: получится
    утверждение о дате релиза, которого источник не делал.
    """
    s = str(raw or "").strip()
    m = _DATE_RE.search(s)
    if m:
        return m.group(0)
    # Голый год («2026») — источник назвал только год. Это НЕ 1 января:
    # выдавать точную дату там, где её не сказали, — ровно та самая выдумка.
    return ""


def make_record(*, src: str, src_url: str, date_raw: str, ident: str,
                title: str, artist: str, **extra) -> dict | None:
    """Запись ленты, либо None если контракт не выполнен.

    Возвращать None, а не «запись с пустыми полями» — принципиально: неполная
    запись выглядит как данные и доедет до экрана.
    """
    meta = SOURCES.get(src)
    if not meta:
        return None
    if not (src_url and str(ident).strip() and str(title).strip()):
        return None
    if meta["tier"] not in CREATING_TIERS:
        return None          # ярус 3 записей не заводит — только дополняет
    date = parse_date(date_raw)
    rec = {
        "src": src, "src_tier": meta["tier"],
        "src_url": src_url,
        "fetched_at": datetime.now().isoformat(timespec="seconds"),
        "date_raw": str(date_raw or ""),
        "date": date,
        "ident": str(ident).strip(),
        "title": str(title).strip(),
        "artist": str(artist or "").strip(),
        "confirmed_by": [src],
    }
    rec.update({k: v for k, v in extra.items() if k not in rec})
    return rec


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9а-я]+", "", str(s or "").lower())


def identity(rec: dict) -> str:
    """Ключ слияния. UPC и ISRC — точные; всё прочее — только в пределах
    ОДНОГО источника.

    Склеивать записи разных источников по «артист + название» запрещено: у
    релизов бывают делюксы, ремастеры и «Extended Mix», и такое слияние
    порождает релиз, которого нет ни в одном каталоге.
    """
    upc = str(rec.get("upc") or "").strip()
    if upc:
        return f"upc:{upc}"
    isrc = str(rec.get("isrc") or "").strip().upper()
    if isrc:
        return f"isrc:{isrc}"
    return f"{rec.get('src')}:{rec.get('ident')}"


def merge(records: list) -> list:
    """Слить записи разных источников. Совпадение по UPC/ISRC повышает доверие.

    `confirmed_by` — не украшение: релиз, названный тремя каталогами, и релиз,
    названный одним, — разные по надёжности вещи, и человек должен видеть
    разницу, а не получать усреднённое «источник: несколько».
    """
    out: dict = {}
    for r in records or []:
        if not r:
            continue
        k = identity(r)
        cur = out.get(k)
        if cur is None:
            out[k] = dict(r)
            continue
        for s in r.get("confirmed_by") or [r.get("src")]:
            if s and s not in cur["confirmed_by"]:
                cur["confirmed_by"].append(s)
        # Побеждает БОЛЕЕ РАННЯЯ известная дата: анонс уточняется приближением,
        # а не отдалением. Пустую дату не предпочитаем никогда.
        if r.get("date") and (not cur.get("date") or r["date"] < cur["date"]):
            cur["date"], cur["date_raw"] = r["date"], r["date_raw"]
            cur["src_url"], cur["src"] = r["src_url"], r["src"]
        for f in ("cover", "url", "label", "upc", "isrc", "tracks", "type"):
            if not cur.get(f) and r.get(f):
                cur[f] = r[f]
    return list(out.values())


def in_horizon(rec: dict, today: str = "") -> bool:
    """Строго в будущем и не дальше горизонта.

    Верхняя граница нужна: дата «2031-01-01» в каталоге почти всегда заглушка
    правообладателя, а не анонс. Показать её значит пообещать релиз, которого
    никто не обещал.
    """
    d = rec.get("date") or ""
    if not d:
        return False
    today = today or datetime.now().strftime("%Y-%m-%d")
    horizon = (datetime.now() + timedelta(days=_HORIZON_DAYS)).strftime("%Y-%m-%d")
    return today < d <= horizon


def enrich(records: list, notes: list) -> list:
    """Дополнить записи материалами яруса 3 (издания).

    Дополнение привязывается ТОЛЬКО по `ident`. Совпадение по названию здесь
    было бы дырой в главном правиле: текст без идентификатора не должен уметь
    прицепиться к релизу «на глазок».
    """
    by_ident = {}
    for r in records:
        by_ident.setdefault(str(r.get("ident") or ""), []).append(r)
    for n in notes or []:
        tgt = by_ident.get(str(n.get("ident") or ""))
        if not tgt:
            continue                     # не к чему прицепить — молча мимо
        for r in tgt:
            r.setdefault("press", []).append(
                {"src": n.get("src"), "url": n.get("url"), "title": n.get("title"),
                 "published": n.get("published")})
    return records


# ── Склад ────────────────────────────────────────────────────────────────────
# Свой файл, а не radar_store.json: у грядущего релиза ДРУГОЙ жизненный цикл.
# Он не «был и выпал из окна источника» — он однажды ВЫХОДИТ и перестаёт быть
# грядущим. Смешивать это с прошедшими релизами значит потерять момент выхода.

def load(path) -> dict:
    try:
        if path and path.exists():
            d = json.loads(path.read_text(encoding="utf-8"))
            return d if isinstance(d, dict) else {}
    except Exception as e:
        print(f"[upcoming] store load: {e}", flush=True)
    return {}


def save(path, store: dict) -> None:
    try:
        if path:
            path.write_text(json.dumps(store, ensure_ascii=False), encoding="utf-8")
    except Exception as e:
        print(f"[upcoming] store save: {e}", flush=True)


def promote_released(store: dict, today: str = "") -> list:
    """Перевести наступившие анонсы из «грядущих» в «вышедшие».

    Это и есть смысл отдельного склада. Запись не удаляется: «вышло то, чего мы
    ждали» — событие, ради которого подписка и заводилась. Возвращает список
    того, что сегодня наступило, — его можно показать человеку.
    """
    today = today or datetime.now().strftime("%Y-%m-%d")
    out = []
    for k, r in list(store.items()):
        if r.get("released"):
            continue
        if r.get("date") and r["date"] <= today:
            r["released"] = True
            r["released_at"] = today
            out.append(r)
    return out


def put(store: dict, records: list) -> int:
    """Влить свежие записи. Возвращает число НОВЫХ (не обновлённых).

    Число, а не «ок»: обход без счётчика неотличим от обхода вхолостую — урок
    вишлиста, 22.08.2026.
    """
    added = 0
    for r in records or []:
        k = identity(r)
        cur = store.get(k)
        if cur is None:
            store[k] = r
            added += 1
            continue
        merged = merge([cur, r])
        if merged:
            merged[0]["released"] = cur.get("released", False)
            store[k] = merged[0]
    if len(store) > _STORE_CAP:
        newest = sorted(store.items(),
                        key=lambda kv: kv[1].get("date", ""), reverse=True)
        store.clear()
        store.update(dict(newest[:_STORE_CAP]))
    return added


# ── Эвристика: на что подписаться, чего мы ещё не слушаем ────────────────────

def suggest(records: list, watched_ids: set, watched_names: set,
            profile_genres: list, min_genre_hits: int = 1) -> list:
    """Грядущие релизы артистов, которых в вотчлисте НЕТ, но жанр совпал.

    Возвращает ПРЕДЛОЖЕНИЯ, а не подписки. Автоподписка по совпадению жанра —
    это действие по догадке: жанр в каталогах размечен грубо, и «techno» у
    Apple и у Beatport означают разное. Цена ошибки несимметрична: лишняя
    подписка на лейбл — это десятки автозагрузок в месяц.

    Кто хочет автоматизма — включает его отдельным ключом и знает, на что
    подписался: у каждого предложения написано, ПОЧЕМУ оно предложено.
    """
    want = {str(g).lower() for g in (profile_genres or []) if g}
    out = []
    for r in records or []:
        aid = str(r.get("artist_id") or "")
        nm = _norm(r.get("artist"))
        if (aid and aid in watched_ids) or (nm and nm in watched_names):
            continue
        genres = [str(g).lower() for g in (r.get("genres") or []) if g]
        hits = [g for g in genres if g in want]
        if len(hits) < min_genre_hits:
            continue
        out.append({**r, "why": {"genres": hits, "matched": len(hits)}})
    out.sort(key=lambda x: (-x["why"]["matched"], x.get("date", "")))
    return out
