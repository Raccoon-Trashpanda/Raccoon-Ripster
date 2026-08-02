"""«Раскопки» — сами находки. Всё строится на том, что УЖЕ лежит на диске.

Ни одного сетевого запроса: 1713 успешных загрузок, 3407 событий проигрывания,
500 записей истории и стор радара на 5761 артиста с 31825 релизами. Этого хватает,
чтобы найти пробелы, — поэтому вкладка открывается мгновенно и работает офлайн,
а не «загружает рекомендации».

ЧУЖИЕ ЗАГРУЗКИ — ГЛАВНАЯ ЛОВУШКА
--------------------------------
Статистика в этой базе НЕ ТОЛЬКО владельца: через бота и гостевые ссылки качают
другие люди, и их музыка лежит в тех же таблицах. Владелец слушает электронику,
а в профиле уверенно поднимался индийский и бенгальский пласт — это гости бота,
и без фильтра «Раскопки» советовали бы владельцу чужой вкус.

По `session_id` их не отделить: он пуст у 1928 из 2051 загрузок. Поэтому чужое
опознаётся тремя независимыми способами:

  * `tgbot/cache_index.json` — что бот скачал и раздал (артист + название);
  * `client_ip != 127.0.0.1` в проигрываниях — гость слушает не с этой машины;
  * ручной список `digs-exclude-artists` в конфиге — последнее слово за владельцем,
    потому что надёжно вывести принадлежность из данных нельзя, а ошибаться
    в профиле вкуса дороже, чем спросить.
"""
from __future__ import annotations

import json
import re
import sqlite3
import time
from pathlib import Path

from ripster.digs import (
    _BASE, _DB, _HISTORY, _STREAM_SPLIT, _is_noise, _norm, _split_credit,
    build_profile,
)

_BOT_CACHE = _BASE / "tgbot" / "cache_index.json"

# Издания схлопываются: делюкс и ремастер того же альбома — тот же альбом,
# второй раз качать незачем.
_ALBUM_NOISE = re.compile(
    r"\s*[\(\[](?:deluxe|remaster(?:ed)?|expanded|anniversary|edition|bonus|"
    r"explicit|clean|mono|stereo|live|reissue)[^)\]]*[\)\]]\s*", re.I)


def _norm_title(t: str) -> str:
    s = _ALBUM_NOISE.sub(" ", (t or "").lower())
    return re.sub(r"[^\w\s]+", " ", s, flags=re.U).strip()


def _titles_match(want: str, cand: str) -> bool:
    """Одно ли это издание. Просто вхождение подстроки ловит посторонние релизы
    («Insight» попадает в «Insight Out Reworked»), поэтому требуем близкой длины
    — тот же приём, что в discovery._titles_match."""
    if not want or not cand:
        return False
    if want == cand:
        return True
    if want in cand or cand in want:
        short, long_ = sorted((len(want), len(cand)))
        return short / long_ >= 0.7
    return False


# ── Чужое ─────────────────────────────────────────────────────────────────────

def foreign_artists(cfg: dict | None = None) -> set:
    """Артисты, которых качал НЕ владелец. Ключи нормализованы.

    Один и того же артиста могут качать И гости бота, И владелец — вычёркивать
    по одному лишь появлению в кэше бота нельзя. Так из профиля вылетел Volen
    Sentir: у владельца 32 загрузки и 115 прослушиваний, но кто-то через бота
    качал его же, и фильтр вымел артиста целиком (поймано 01.08.2026).

    Поэтому «чужим» считается только тот, у кого НЕТ независимых следов
    владельца: ни прослушиваний с этой машины, ни явного «это моё». Ручной
    список `digs-exclude-artists` — исключение: там владелец сказал прямо, и
    его слово выше любых улик.
    """
    cfg = cfg or {}
    bot: set = set()
    try:
        for v in json.loads(_BOT_CACHE.read_text(encoding="utf-8")).values():
            for a in _split_credit((v or {}).get("artist") or ""):
                k = _norm(a)
                if k:
                    bot.add(k)
    except Exception:
        pass

    mine: set = set()
    for nm, _st, _ts, _hits in owner_plays():          # слушал сам, с этой машины
        parts = _STREAM_SPLIT.split((nm or "").strip(), 1)
        if len(parts) == 2:
            for a in _split_credit(parts[0].strip()):
                k = _norm(a)
                if k:
                    mine.add(k)
    for a in (cfg.get("digs-favorite-artists") or []):  # объявил своим
        k = _norm(str(a))
        if k:
            mine.add(k)

    # Кэш бота знает не всё: часть чужих загрузок в него не попала, и в профиле
    # оставались Telugu и Worldwide. Добираем по гостевым сессиям статистики —
    # у гостя внешний адрес, у владельца 127.0.0.1.
    try:
        db = sqlite3.connect(f"file:{_DB}?mode=ro", uri=True)
        for (nm,) in db.execute(
                "SELECT DISTINCT stream_name FROM stream_events "
                "WHERE event='start' AND stream_name LIKE '% — %' "
                "AND client_ip NOT IN ('127.0.0.1','::1') AND client_ip IS NOT NULL "
                "AND client_ip != ''"):
            parts = _STREAM_SPLIT.split((nm or "").strip(), 1)
            if len(parts) == 2:
                for a in _split_credit(parts[0].strip()):
                    k = _norm(a)
                    if k:
                        bot.add(k)
        db.close()
    except sqlite3.Error:
        pass

    out = bot - mine
    for a in (cfg.get("digs-exclude-artists") or []):
        k = _norm(str(a))
        if k:
            out.add(k)
    return out


def owner_plays() -> list[tuple]:
    """Проигрывания ТОЛЬКО с этой машины. У гостей внешний адрес, и их
    прослушивания к вкусу владельца отношения не имеют."""
    if not _DB.exists():
        return []
    try:
        db = sqlite3.connect(f"file:{_DB}?mode=ro", uri=True)
        rows = db.execute(
            "SELECT stream_name, stream_type, MAX(ts) ts, COUNT(*) hits "
            "FROM stream_events WHERE event='start' AND stream_name LIKE '% — %' "
            "AND (client_ip IS NULL OR client_ip='' OR client_ip='127.0.0.1' OR client_ip='::1') "
            "GROUP BY stream_name ORDER BY hits DESC").fetchall()
        db.close()
        return rows
    except sqlite3.Error:
        return []


# ── Что уже есть ──────────────────────────────────────────────────────────────

def owned() -> dict:
    """Карта артист → набор нормализованных названий, которые уже на диске.

    Это половина всей идеи: чужие сервисы советуют, что послушать, потому что НЕ
    ЗНАЮТ твоей фонотеки. Мы знаем — и не предлагаем то, что уже лежит.
    """
    by_artist: dict = {}

    def add(artist: str, title: str) -> None:
        t = _norm_title(title)
        if not t:
            return
        for name in _split_credit(artist or ""):
            k = _norm(name)
            if k:
                by_artist.setdefault(k, set()).add(t)

    if _DB.exists():
        try:
            db = sqlite3.connect(f"file:{_DB}?mode=ro", uri=True)
            for artist, album, title in db.execute(
                    "SELECT artist, album, title FROM downloads WHERE status='done'"):
                add(artist, album or title)
            db.close()
        except sqlite3.Error:
            pass
    if _HISTORY.exists():
        try:
            for it in json.loads(_HISTORY.read_text(encoding="utf-8")):
                if isinstance(it, dict):
                    add(it.get("artist") or "", it.get("album") or it.get("title") or "")
        except Exception:
            pass
    return by_artist


def _has(by_artist: dict, key: str, title: str) -> bool:
    have = by_artist.get(key)
    if not have:
        return False
    t = _norm_title(title)
    return any(_titles_match(t, o) for o in have)


# ── Находки ───────────────────────────────────────────────────────────────────

def played_not_owned(by_artist: dict, foreign: set, limit: int,
                     show_keys: set | None = None) -> list[dict]:
    """Включал, но не забрал. Самый честный сигнал: человек сам выбрал это
    послушать — значит уже одобрил, — а на диске этого нет.

    Радио-шоу отсюда исключены. Выпуск подкаста — не альбом: предлагать «забери
    Anjunadeep Edition 601» бессмысленно, его и качать-то нечем. Без этого
    фильтра весь список забивался эпизодами, потому что их слушают чаще всего.
    """
    show_keys = show_keys or set()
    out, seen = [], set()
    for nm, stype, ts, hits in owner_plays():
        parts = _STREAM_SPLIT.split((nm or "").strip(), 1)
        if len(parts) != 2:
            continue
        owner, title = parts[0].strip(), parts[1].strip()
        key = _norm(owner)
        if (_is_noise(owner) or key in foreign or key in show_keys
                or _has(by_artist, key, title)):
            continue
        dd = (key, _norm_title(title))
        if dd in seen:
            continue
        seen.add(dd)
        out.append({"kind": "played_not_owned", "artist": owner, "title": title,
                    "service": stype or "", "plays": int(hits or 0),
                    "last_ts": int(ts or 0),
                    # Ключ + числа: фразу собирает клиент на своём языке.
                    "reason_key": "digs.r_played", "reason_args": {"n": int(hits or 0)},
                    "reason": f"включал {hits}×, а на диске нет"})
        if len(out) >= limit:
            break
    return out


def missing_releases(profile_artists: list, by_artist: dict, foreign: set,
                     limit: int) -> list[dict]:
    """Релизы твоих же артистов, которых у тебя нет. 31825 релизов уже собраны
    прошлыми сканами радара — искать их заново не нужно."""
    p = _BASE / "spotify_artist_state.json"
    if not p.exists():
        return []
    try:
        artists = (json.loads(p.read_text(encoding="utf-8")).get("artists") or {})
    except Exception:
        return []

    # Склад НЕ-Spotify источников (Apple/BBC/SoundCloud) появился 02.08.2026.
    # Без него шаг «знакомое, но не в фонотеке» видел только четверть радара:
    # релиз, найденный Apple-источником, для копателя не существовал вовсе —
    # ровно та же слепота, что мы чинили в самом радаре. Приводим записи к виду
    # склада Spotify и складываем в общий котёл.
    extra: dict = {}
    try:
        rs = _BASE / "radar_store.json"
        if rs.exists():
            for bucket in (json.loads(rs.read_text(encoding="utf-8")) or {}).values():
                for rel in (bucket or {}).values():
                    nm = rel.get("artist") or ""
                    if not nm:
                        continue
                    slot = extra.setdefault(_norm(nm), {"name": nm, "releases": []})
                    slot["releases"].append(rel)
    except Exception:
        pass

    wanted = {_norm(a["name"]): a for a in profile_artists
              if not a.get("is_show") and _norm(a["name"]) not in foreign}
    out = []
    for rec in list(artists.values()) + list(extra.values()):
        key = _norm(rec.get("name", ""))
        prof = wanted.get(key)
        if not prof:
            continue
        for rel in (rec.get("releases") or []):
            title = rel.get("title", "")
            if not title or _has(by_artist, key, title):
                continue
            out.append({"kind": "missing_release", "artist": rec.get("name", ""),
                        "title": title, "date": rel.get("date", ""),
                        "cover": rel.get("cover", ""), "url": rel.get("url", ""),
                        "type": rel.get("type", ""), "_w": prof["score"],
                        "reason_key": "digs.r_missed",
                        "reason_args": {"n": int(prof["downloads"])},
                        "reason": f"качал {prof['downloads']} его релизов, этот пропустил"})
    out.sort(key=lambda r: (r["_w"], r.get("date", "")), reverse=True)
    for r in out:
        r.pop("_w", None)
    return out[:limit]


def show_guests(shows: list, by_artist: dict, foreign: set, limit: int) -> list[dict]:
    """Гости твоих шоу, которых ты ни разу не качал. Ты слушал их сет целиком —
    это рекомендация, которую ты уже принял, просто не заметил."""
    out, seen = [], set()
    for sh in shows:
        for raw in (sh.get("guests") or []):
            # Разделять по « & » САМОСТОЯТЕЛЬНО НЕЛЬЗЯ: «Above & Beyond» —
            # это один коллектив, и наивный split выдал в находках отдельных
            # «Above» и «Beyond» (проверено, 01.08.2026). В проекте это уже
            # решено: _split_credit режет по « & » только если в строке ЕСТЬ
            # запятая, то есть она и так выглядит списком. Берём его, а не свой.
            # Хвосты вроде «Solomon Grey)» остаются от разбора скобок в названии
            # выпуска — их срезаем, иначе имя не совпадёт ни с чем.
            for g in _split_credit(raw):
                g = g.strip(" )(][.,-–—")
                key = _norm(g)
                if (not key or key in seen or key in by_artist or key in foreign
                        or _is_noise(g)):
                    continue
                seen.add(key)
                out.append({"kind": "show_guest", "artist": g, "title": "",
                            "reason_key": "digs.r_guest",
                            "reason_args": {"show": sh["name"]},
                            "reason": f"гость «{sh['name']}», а его релизов у тебя нет"})
                if len(out) >= limit:
                    return out
    return out


def forgotten(profile_artists: list, foreign: set, limit: int) -> list[dict]:
    """Качал много, давно не трогал. Не «новое», а забытое своё — то, чего чужая
    лента не покажет никогда, потому что не помнит твоей истории."""
    now = time.time()
    out = []
    for a in profile_artists:
        if a.get("is_show") or a["downloads"] < 4 or _norm(a["name"]) in foreign:
            continue
        days = (now - a["last_ts"]) / 86_400 if a["last_ts"] else 0
        # Порог 90 дней не переходил НИКТО: владелец качает регулярно, и раздел
        # стоял пустым. 45 дней — это уже «давно не возвращался», а не «забыл».
        if days < 45:
            continue
        # ЛЮБОВЬ × ОТСУТСТВИЕ, а не одна давность.
        #
        # Раньше список сортировался по `days`, и наверх лез тот, кого не трогали
        # дольше всех — даже если у него четыре релиза и он никогда толком не
        # нравился. «Забытый любимый» — это пересечение двух условий: его МНОГО
        # качали И к нему давно не возвращались. Одно без другого даёт либо
        # случайных знакомых, либо тех, кого и так слушаешь.
        #
        # Тот же принцип независимо нашли в Longplay (оценка × время с последнего
        # прослушивания) — он и считается сейчас лучшим для этой задачи.
        #
        # Корень у давности намеренно: без него полгода молчания перевешивают
        # любую любовь, и раздел снова вырождается в «самое старое».
        love = float(a["downloads"]) + float(a.get("plays") or 0) * 0.5
        score = love * (days ** 0.5)
        out.append({"kind": "forgotten", "artist": a["name"], "title": "",
                    "days": int(days), "cover": a.get("cover", ""), "_w": score,
                    "reason_key": "digs.r_forgot",
                    "reason_args": {"n": int(a["downloads"]), "days": int(days)},
                    "reason": f"{a['downloads']} релизов, но не трогал {int(days)} дн."})
    out.sort(key=lambda r: r["_w"], reverse=True)
    for r in out:
        r.pop("_w", None)
    return out[:limit]


def apply_favorites(prof: dict, cfg: dict | None) -> dict:
    """Наложить на профиль то, что человек объявил своим САМ.

    Зачем это нужно, хотя статистика уже есть:

    * ХОЛОДНЫЙ СТАРТ. У нового человека истории нет вовсе, и «Раскопки» ему
      показать нечего. Спросить один раз — единственный способ начать.
    * ЧУЖИЕ ЗАГРУЗКИ. В базе лежит и то, что качали гости бота. Явный список
      «моё» — единственная надёжная опора, вывести это из данных нельзя.
    * ЗЕРНИСТОСТЬ ЖАНРОВ. iTunes знает «Dance» и «Electronic», а человек живёт
      в «джангле», «ликвид фанке» и «балеарик трансе». Такие направления
      берутся только со слов владельца.

    Названные артисты получают вес наравне с хорошо скачанными: это прямое
    утверждение о вкусе, оно не слабее косвенного вывода из статистики.
    """
    cfg = cfg or {}
    favs = [str(x).strip() for x in (cfg.get("digs-favorite-artists") or []) if str(x).strip()]
    genres = [str(x).strip() for x in (cfg.get("digs-favorite-genres") or []) if str(x).strip()]
    if not favs and not genres:
        prof["seeded"] = False
        return prof

    by_key = {_norm(a["name"]): a for a in prof["artists"]}
    base = max((a["score"] for a in prof["artists"]), default=10.0) * 0.6
    for name in favs:
        k = _norm(name)
        if not k:
            continue
        cur = by_key.get(k)
        if cur:
            cur["score"] = round(cur["score"] + base, 2)
            cur["favorite"] = True
        else:
            rec = {"key": k, "name": name, "score": round(base, 2), "downloads": 0,
                   "plays": 0, "watched": False, "albums": 0, "last_ts": int(time.time()),
                   "services": [], "cover": "", "genre": "", "is_show": False,
                   "favorite": True}
            prof["artists"].append(rec)
            by_key[k] = rec
    prof["artists"].sort(key=lambda a: a["score"], reverse=True)
    prof["favorite_genres"] = genres
    prof["seeded"] = True
    return prof



def anniversary(foreign: set, limit: int) -> list[dict]:
    """«В этот день» — что качалось ровно год, полгода, три месяца назад.

    ЗАЧЕМ. У конкурентов самой цепляющей механикой оказалась не «похожее на то,
    что ты слушал», а напоминание о собственном прошлом: узнавание сильнее
    рекомендации, и никакая чужая лента такого не покажет — она не помнит твоей
    истории. Внешних источников не нужно ни одного.

    ПОЧЕМУ НЕ ТОЛЬКО ГОДЫ. Первая версия смотрела ровно на год назад и на этой
    установке давала НОЛЬ: истории всего 76 дней (02.08.2026). Раздел, который
    пуст у нового человека и наполнится через год, бесполезен. Поэтому вехи
    начинаются с трёх месяцев и растут вместе с историей — сначала месяцы,
    потом годы.

    ПОЧЕМУ БАЗА, А НЕ history.json. Файл хранит последние 500 записей — у
    активного человека это недели. В базе статистики лежит вся история загрузок.

    Окно ±3 дня: качают не каждый день, и жёсткая дата оставляла бы раздел
    пустым почти всегда.
    """
    if not _DB.exists():
        return []
    # Кого владелец слушал сам — независимое подтверждение принадлежности.
    _mine = set()
    try:
        for nm, _st, _ts, _hits in owner_plays():
            parts = _STREAM_SPLIT.split((nm or "").strip(), 1)
            if len(parts) == 2:
                for a in _split_credit(parts[0].strip()):
                    k = _norm(a)
                    if k:
                        _mine.add(k)
    except Exception:
        pass
    now = int(time.time())
    DAY = 86_400
    # (сдвиг в днях, ключ подписи, число для подписи)
    # Начинаем с МЕСЯЦА: на свежей установке истории мало (здесь — 77 дней), и
    # даже трёхмесячная веха пуста. Месяц даёт находки сразу, а дальше вехи
    # включаются сами по мере накопления — раздел растёт вместе с человеком.
    marks = [(30, "digs.r_anniv_m", 1), (90, "digs.r_anniv_m", 3),
             (182, "digs.r_anniv_m", 6),
             (365, "digs.r_anniv_y", 1), (730, "digs.r_anniv_y", 2)]
    out, seen = [], set()
    try:
        db = sqlite3.connect(f"file:{_DB}?mode=ro", uri=True)
        for days, key, n in marks:
            lo, hi = now - (days + 3) * DAY, now - (days - 3) * DAY
            for artist, album, title, svc, url, ts in db.execute(
                    "SELECT artist, album, title, service, url, ts FROM downloads "
                    "WHERE status='done' AND ts BETWEEN ? AND ? ORDER BY ts DESC",
                    (lo, hi)):
                artist = (artist or "").strip()
                name = (album or title or "").strip()
                if not artist or _norm(artist) in foreign:
                    continue
                # Одного «не в списке чужих» здесь мало. Кэш бота знает не все
                # гостевые загрузки, и первый же прогон вынес в «твой этот день»
                # болливудские саундтреки гостей (02.08.2026). Для ЭТОГО раздела
                # требование строже: артист должен иметь независимый след
                # владельца — он есть в профиле вкуса. Раздел про твоё прошлое,
                # и чужому здесь не место даже ценой меньшего числа находок.
                if _norm(artist) not in _mine:
                    continue
                k = (_norm(artist), _norm_title(name))
                if k in seen:
                    continue
                seen.add(k)
                out.append({"kind": "anniversary", "artist": artist, "title": name,
                            "service": svc or "", "url": url or "",
                            "date": time.strftime("%Y-%m-%d", time.localtime(ts)),
                            "reason_key": key, "reason_args": {"n": n},
                            "reason": (f"качал ровно {n} мес. назад" if key.endswith("_m")
                                       else f"качал ровно {n} г. назад")})
                if len(out) >= limit:
                    db.close()
                    return out
        db.close()
    except sqlite3.Error:
        return out
    return out


async def find_all(cfg: dict | None = None, per_kind: int = 12) -> dict:
    prof = await build_profile(limit=60, with_genres=True)
    foreign = foreign_artists(cfg)
    # Профиль тоже чистим от чужого — иначе жанры считаются по гостям бота.
    prof["artists"] = [a for a in prof["artists"] if _norm(a["name"]) not in foreign]
    prof = apply_favorites(prof, cfg)
    prof["genres"] = _regenre(prof["artists"])
    by_artist = owned()
    show_keys = {_norm(s.get("name", "")) for s in prof.get("shows", [])}

    # Фильтр по сервисам: у человека может не быть Qobuz или Tidal, и предлагать
    # оттуда — значит показывать то, что он всё равно не заберёт. Пусто = все.
    want_svc = {str(x).strip().lower() for x in ((cfg or {}).get("digs-services") or []) if str(x).strip()}

    def keep(items: list) -> list:
        if not want_svc:
            return items
        out = []
        for it in items:
            svc = (it.get("service") or "").lower()
            url = (it.get("url") or "").lower()
            if not svc and url:
                # У находок из стора радара сервис в поле не лежит — он в ссылке.
                for name in ("spotify", "deezer", "tidal", "qobuz", "soundcloud", "apple"):
                    if name in url:
                        svc = name
                        break
            if not svc or svc in want_svc:
                out.append(it)
        return out

    return {
        "profile": prof,
        "foreign_filtered": len(foreign),
        "digs": {
            # «Год назад» стоит ПЕРВЫМ по смыслу: узнавание сильнее рекомендации.
            "anniversary":      keep(anniversary(foreign, per_kind * 2))[:per_kind],
            "played_not_owned": keep(played_not_owned(by_artist, foreign, per_kind * 2, show_keys))[:per_kind],
            "missing_release":  keep(missing_releases(prof["artists"], by_artist, foreign, per_kind * 3))[:per_kind],
            "show_guest":       show_guests(prof["shows"], by_artist, foreign, per_kind),
            "forgotten":        forgotten(prof["artists"], foreign, per_kind),
        },
        # Оформление отдаём вместе с данными, чтобы вкладка рисовалась сразу в
        # выбранном виде, а не мигала дефолтом и потом перекрашивалась.
        "look": {
            "shape":    (cfg or {}).get("digs-shape", "circle"),
            "size":     int((cfg or {}).get("digs-size", 44) or 44),
            "bg":       (cfg or {}).get("digs-bg", "earth"),
            "services": sorted(want_svc),
        },
        "generated_ts": int(time.time()),
    }


def _regenre(artists: list) -> list:
    w: dict = {}
    for a in artists:
        g = (a.get("genre") or "").strip()
        if not g or a.get("is_show"):
            continue
        rec = w.setdefault(g, {"genre": g, "score": 0.0, "artists": []})
        rec["score"] += a["score"]
        rec["artists"].append(a["name"])
    out = sorted(w.values(), key=lambda x: x["score"], reverse=True)
    total = sum(x["score"] for x in out) or 1.0
    for x in out:
        x["share"] = round(100.0 * x["score"] / total, 1)
        x["score"] = round(x["score"], 2)
        x["artists"] = x["artists"][:6]
    return out
