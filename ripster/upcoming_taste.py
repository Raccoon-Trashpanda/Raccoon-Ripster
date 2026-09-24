"""«Исключительно интересное» в радаре грядущего: ранжирование, отзыв владельца,
одно уведомление в бота.

Замысел владельца (24.09.2026, второй заход): пресс-анонсы (Pitchfork, DJ Mag,
XLR8R…) интересны ровно до тех пор, пока дело не касается андеграунда — у
подпольных техно-лейблов в прессе НЕТ НИЧЕГО, они объявляют предзаказ на
Bandcamp, Beatport и в магазинах. Значит ленте грядущего нужен источник, который
это видит, и порядок, который ставит вперёд ТО ЕДИНСТВЕННОЕ, что владельцу
интересно, с объяснением почему.

## Из чего складывается интерес (в порядке силы)

    подписка          лейбл или артист вотчлиста — самое сильное: человек сам
                      сказал, за чем следит («лейбл Semantica в наблюдении»)
    семейство лейбла  артист, который уже выпускался на лейбле вотчлиста
    похожий артист    кэш `digs_similar_cache.json` (ListenBrainz/Lastfm/Deezer)
    твои загрузки     `ripster_stats.db` (status='done') — сыгранное и скачанное
    слово владельца   «интересно» / «не то» из `upcoming_feedback.json`

Причина показывается человеку ВСЕГДА, когда балл больше нуля: список без
объяснения — это чёрный ящик, а чёрный ящик в рекомендациях владелец выключает.

## Почему отзыв хранится отдельно от `owner_feedback.json`

Там вердикт про ВЫШЕДШИЙ релиз («это не мой артист» — фильтр однофамильцев),
здесь — про БУДУЩИЙ («не хочу это ждать»). Разные вопросы, разные подписи:
смешав их, мы начали бы глушить артиста в радаре из-за отказa от его анонса.

## Уведомление — одно на релиз, и только про высокое

Прайс-лист из 200 позиций в мессенджере — спам, а не радар. Порог
`NOTIFY_MIN_SCORE` отсекает всё спорное, `upcoming_notified.json` не даёт
повторять. Тот же образец, что `accounts_watch.py`: состояние на диске,
сообщение только на разницу.
"""
from __future__ import annotations

import json
import re
import time
from pathlib import Path

#: Балл, с которого анонс считается достойным одного сообщения в бота.
#: Выше — только подписки: «лейбл/артист в наблюдении» (см. WEIGHTS), чтобы
#: уведомление не превращалось в поток «может, понравится».
NOTIFY_MIN_SCORE = 100

WEIGHTS = {
    "watched_label":   120,
    "watched_artist":  120,
    "label_family":     60,
    "similar_artist":   40,
    "downloaded":       30,
    "played":           15,
    "owner_like":       80,
    "owner_dislike":  -200,
    "preorder":         10,   # предзаказ = анонс, а не слух: небольшой плюс
}

_BASE: Path = Path(".")
_fb: dict | None = None
_nb: dict | None = None


def configure(base_dir) -> None:
    global _BASE, _fb, _nb
    _BASE = Path(base_dir) if base_dir else Path(".")
    _fb = _nb = None


# ── файлы состояния ─────────────────────────────────────────────────────────

def _file(name: str) -> Path:
    return _BASE / name


def _load(name: str, default):
    try:
        d = json.loads(_file(name).read_text(encoding="utf-8"))
        return d if isinstance(d, type(default)) else default
    except Exception:
        return default


def _dump(name: str, data) -> None:
    try:
        _file(name).write_text(json.dumps(data, ensure_ascii=False, indent=1),
                               encoding="utf-8")
    except Exception as e:
        print(f"[taste] save {name}: {e}", flush=True)


def _feedback() -> dict:
    global _fb
    if _fb is None:
        _fb = _load("upcoming_feedback.json", {})
    return _fb


def _notified() -> dict:
    global _nb
    if _nb is None:
        _nb = _load("upcoming_notified.json", {})
    return _nb


def norm(s: str) -> str:
    return re.sub(r"[^a-z0-9а-я]+", "", str(s or "").lower())


# ── сигналы вкуса ────────────────────────────────────────────────────────────

def _watched(watchlist: list) -> tuple[set, set]:
    labels, artists = set(), set()
    for e in watchlist or []:
        n = norm(e.get("name"))
        if not n:
            continue
        (labels if e.get("kind") == "label" else artists).add(n)
    return labels, artists


def label_family() -> dict:
    """Артисты, которые уже выпускались на лейблах вотчлиста.

    Карта копится по ходу работы радара (`note_label_artists`), отдельного
    обхода каталов ради неё не заводим.
    """
    d = _load("upcoming_label_family.json", {})
    return d if isinstance(d, dict) else {}


def note_label_artists(label: str, artists: list) -> int:
    """Запомнить, кто выпускался на этом лейбле. Возвращает число НОВЫХ имён."""
    key = norm(label)
    if not key:
        return 0
    fam = label_family()
    cur = set(fam.get(key) or [])
    before = len(cur)
    for a in artists or []:
        n = norm(a)
        if n:
            cur.add(n)
    if len(cur) != before:
        fam[key] = sorted(cur)[:400]
        _dump("upcoming_label_family.json", fam)
    return len(cur) - before


def _similar_index() -> dict:
    """{норма имени: [похожие имена]} из кэша `digs_similar_cache.json`."""
    raw = _load("digs_similar_cache.json", {})
    out = {}
    if not isinstance(raw, dict):
        return out
    for artist, blob in raw.items():
        items = (blob or {}).get("items") if isinstance(blob, dict) else None
        names = [str(i.get("name") or "") for i in (items or []) if isinstance(i, dict)]
        n = norm(artist)
        if n and names:
            out[n] = [norm(x) for x in names if x]
    return out


def _history() -> tuple[set, set]:
    """({артисты, которые качали}, {артисты, которые слушали в станциях})."""
    import sqlite3

    downloaded: set = set()
    try:
        f = _file("ripster_stats.db")
        if f.exists():
            con = sqlite3.connect(f"file:{f.as_posix()}?mode=ro", uri=True)
            try:
                for (a,) in con.execute(
                        "SELECT DISTINCT artist FROM downloads "
                        "WHERE status='done' AND artist<>'' LIMIT 20000"):
                    downloaded.add(norm(a))
            finally:
                con.close()
    except Exception:
        pass
    played: set = set()
    try:
        f = _file("stations.db")
        if f.exists():
            con = sqlite3.connect(f"file:{f.as_posix()}?mode=ro", uri=True)
            try:
                for (a,) in con.execute(
                        "SELECT DISTINCT artist FROM station_events "
                        "WHERE event='track_started' AND artist<>'' LIMIT 5000"):
                    played.add(norm(a))
            finally:
                con.close()
    except Exception:
        pass
    return downloaded, played


class Taste:
    """Свёрнутые сигналы вкуса на один обход.

    Собирается ОДИН раз на проход: читать `ripster_stats.db` на каждую карточку
    — это сотни открытий базы вместо одного.
    """

    def __init__(self, watchlist: list | None = None):
        wl = watchlist or []
        self.labels, self.artists = _watched(wl)
        self.family = label_family()
        self.similar = _similar_index()
        self.downloaded, self.played = _history()
        self.feedback = _feedback()

    # ── одна карточка ────────────────────────────────────────────────────────
    def judge(self, rec: dict) -> tuple[int, list]:
        """(балл, причины). Причины — ключ i18n + подстановки, НЕ готовый текст:
        интерфейс двуязычный, и фраза, зашитая в код, доедет до англичанина
        по-русски."""
        artist = norm(rec.get("artist") or "")
        label = norm(rec.get("label") or "")
        first = norm((artist.split(",")[0] if artist else ""))
        score = 0
        why: list = []

        def add(key: str, weight: str, **args):
            nonlocal score
            score += WEIGHTS[weight]
            why.append({"key": key, "args": args, "weight": weight})

        if label and label in self.labels:
            add("up.why.watched_label", "watched_label",
                label=str(rec.get("label") or ""))
        elif label and label in self.family:
            add("up.why.watched_label", "watched_label",
                label=str(rec.get("label") or ""))
        if artist and (artist in self.artists or first in self.artists):
            add("up.why.watched_artist", "watched_artist",
                artist=str(rec.get("artist") or ""))
        # артист семейства лейбла: кто из лейблов вотчлиста его выпускал
        if artist and not (artist in self.artists or first in self.artists):
            for lbl, names in self.family.items():
                if lbl in self.labels and (artist in names or first in names):
                    add("up.why.label_family", "label_family",
                        artist=str(rec.get("artist") or ""), label=lbl)
                    break
        if artist or first:
            for watched_key, sims in self.similar.items():
                if watched_key not in self.artists and watched_key not in self.labels:
                    continue
                if artist in sims or first in sims:
                    add("up.why.similar_artist", "similar_artist",
                        artist=str(rec.get("artist") or ""),
                        from_artist=watched_key)
                    break
        if artist and artist in self.downloaded:
            add("up.why.downloaded", "downloaded", artist=str(rec.get("artist") or ""))
        elif artist and artist in self.played:
            add("up.why.played", "played", artist=str(rec.get("artist") or ""))
        if rec.get("preorder"):
            add("up.why.preorder", "preorder")
        fb = self.feedback.get(_fid(rec)) or {}
        if fb.get("verdict") == "interesting":
            add("up.why.owner_like", "owner_like")
        elif fb.get("verdict") == "not_it":
            add("up.why.owner_dislike", "owner_dislike")
        return score, why


def _fid(rec: dict) -> str:
    """Устойчивый ключ карточки для отзыва: идент источника, а не позиция в
    списке (список пересобирается каждый проход)."""
    return str(rec.get("ident") or rec.get("id") or
               f"{norm(rec.get('artist'))}|{norm(rec.get('title'))}")


def rank(records: list, watchlist: list | None = None,
         taste: Taste | None = None) -> list:
    """Пройтись по карточкам: каждой — `score` и `why`, список — по интересу.

    Порядок: балл (спуск), затем близость даты (подъём). Равносильные анонсы
    показываются по свежести — человек ждёт в первую очередь то, что ближе.
    """
    t = taste or Taste(watchlist)
    for r in records or []:
        s, why = t.judge(r)
        r["score"] = s
        r["why"] = why
        r["interesting"] = s >= NOTIFY_MIN_SCORE
    return sorted(records or [],
                  key=lambda r: (-int(r.get("score") or 0),
                                 str(r.get("date") or "9999-99-99")))


# ── отзыв владельца ──────────────────────────────────────────────────────────

VERDICTS = ("interesting", "not_it")


def set_feedback(rec: dict, verdict: str) -> dict:
    if verdict not in VERDICTS:
        return {"ok": False}
    store = _feedback()
    k = _fid(rec or {})
    if not k:
        return {"ok": False}
    store[k] = {"verdict": verdict, "at": int(time.time()),
                "artist": str((rec or {}).get("artist") or ""),
                "title": str((rec or {}).get("title") or "")}
    # Поток «не то» должен жить годами, а не раздувать файл: режем самое старое.
    if len(store) > 4000:
        newest = sorted(store.items(), key=lambda kv: -int(kv[1].get("at") or 0))
        store = dict(newest[:4000])
    _dump("upcoming_feedback.json", store)
    global _fb
    _fb = store
    return {"ok": True, "verdict": verdict, "key": k}


def feedback_counts() -> dict:
    store = _feedback()
    out = {v: 0 for v in VERDICTS}
    for x in store.values():
        v = (x or {}).get("verdict")
        if v in out:
            out[v] += 1
    return out


# ── уведомление: один раз, высокое,digestом ──────────────────────────────────

def pending_digest(records: list) -> list:
    """Карточки, о которых ещё НЕ сообщали, и которые выше порога.

    Не отправляет ничего — только говорит, что созрело. Разделение нужное:
    тест проверяет отбор без сети, а сеть — это один вызов в боте.
    """
    done = _notified()
    fresh = [r for r in records or []
             if int(r.get("score") or 0) >= NOTIFY_MIN_SCORE
             and _fid(r) not in done]
    return fresh


def mark_notified(records: list) -> None:
    done = _notified()
    now = int(time.time())
    for r in records or []:
        done[_fid(r)] = now
    if len(done) > 3000:
        newest = sorted(done.items(), key=lambda kv: -int(kv[1] or 0))
        done = dict(newest[:3000])
    _dump("upcoming_notified.json", done)
    global _nb
    _nb = done


def format_digest(records: list) -> str:
    """Текст одного сообщения. Даты — в человеческом формате, ссылка — прямая.

    «Bandcamp · предзаказ · выходит 25.09» — та форма, которую просил владелец;
    ISO-дату в мессенджере читать тяжело.
    """
    lines = []
    for r in records[:10]:
        d = str(r.get("date") or "")
        human = f"{d[8:10]}.{d[5:7]}.{d[0:4]}" if len(d) == 10 else "?"
        src = {"bandcamp": "Bandcamp", "beatport": "Beatport",
               "apple": "Apple", "label": "каталог"}.get(r.get("src"), r.get("src"))
        pre = " · предзаказ" if r.get("preorder") else ""
        why = "; ".join(str(w.get("args", {}).get("label") or
                            w.get("args", {}).get("artist") or "")
                        for w in (r.get("why") or [])[:2]) or "—"
        lines.append(f"• {r.get('artist') or ''} — {r.get('title') or ''}"
                     f" ({r.get('label') or '?'}), {src}{pre}, выходит {human}"
                     + (f" · {why}" if why != "—" else "")
                     + (f" · {r.get('url')}" if r.get("url") else ""))
    return "\n".join(lines)


def notify_owner(text: str) -> bool:
    """Одно сообщение владельцу в Telegram. Без сети и без учётки — тихо false.

    Тот же путь, что `accounts_watch`: ключи из `tgbot/config.json`, никакого
    текста в логи (там могут быть ссылки, видимые только владельцу).
    """
    if not text.strip():
        return False
    try:
        cfg = json.loads(_file("tgbot/config.json").read_text(encoding="utf-8"))
        token = str(cfg.get("bot_token") or "").strip()
        chat = str(cfg.get("owner_id") or "").strip()
        if not token or not chat:
            return False
        import httpx
        r = httpx.post(f"https://api.telegram.org/bot{token}/sendMessage",
                       json={"chat_id": chat, "text": text, "disable_web_page_preview": True},
                       timeout=15.0)
        return bool(r.status_code == 200 and (r.json() or {}).get("ok"))
    except Exception as e:
        print(f"[taste] notify failed: {type(e).__name__}", flush=True)
        return False
