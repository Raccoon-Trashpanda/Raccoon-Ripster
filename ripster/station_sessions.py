"""Станция как СЕССИЯ: пачки, дозаказ и перестройка по тому, что уже сказали.

ЗАЧЕМ. До этого станция была ВЫСТРЕЛОМ: `GET /api/station` → 30 строк → очередь
→ тишина. Всё, что человек сделал с этими тридцатью треками (скипнул на пятой
секунде, дослушал до конца, скачал), на текущий эфир не влияло НИКАК — только
косвенно, на следующую станцию, когда `artist_stats` переварит четыре события
одного артиста. Исследование (research/stations_apk_research_2026-09-19.md,
§1.1–1.4, §2.1, §3.2) показало у всех трёх сервисов одно и то же: сессия живёт
минутами, трек отдаётся короткими пачками, а фидбек входит в СЛЕДУЮЩУЮ пачку
этого же эфира. У Яндекса — `radioSessionId` + `batchId` + `feedbacks`, у
Spotify — `prev_tracks`, у Apple — держать запас в 3 трека
(`MIN_ITEMS_TO_MAINTAIN`). Скопирована ИДЕЯ, а не их формат: у нас свой сервер и
свой плеер.

═══════════════════════════════════════════════════════════════════════════════
 API — ПО ЭТОМУ КОНТРАКТУ ПИШЕТСЯ ФРОНТ (стадия 2; файлы плеера сейчас заняты
 другим работником, здесь их нет и не будет)
═══════════════════════════════════════════════════════════════════════════════

1) `POST /api/station/session` — открыть станцию. Сеть ходит ТОЛЬКО здесь:
   опрос витрин и MusicBrainz, ответ — секунды.

   запрос:
     {"id": "techno",                  // обязательный id плитки из /api/stations
      "batch": 10,                     // необязательный, 1..25, по умолчанию 10
      "seed": 123456,                  // необязательный, зерно эфира; нет — по часам
      "services": ["deezer","qobuz","tidal"],
                                       // необязательный: сервисы, которые КЛИЕНТ
                                       // способен сыграть. Пул собирается только из
                                       // них — сервер не платит сетью за строки,
                                       // которые браузер выбросит как неиграбельные
      "knobs": {"diversity": "default", "energy": "all", "language": "any"}}
   ответ:
     {"ok": true,
      "session_id": "6a1f…",           // ДАЛЬНЕЙШИЕ СОБЫТИЯ ТОЛЬКО С ЭТИМ id
      "station_id": "techno", "title": "Techno",
      "batch_id": "6a1f…-1",           // шить его в каждое событие пачки
      "sequence": 1,
      "tracks": [ …те же карточки, что у старого /api/station… ],
      "reserve": 30,                   // сколько кандидатов осталось на сервере
      "reserve_min": 3,                // при каком остатке очереди звать next
      "exhausted": false,
      "suggest": "next_batch",          // | "new_session"
      "knobs": {…}, "knob_report": {…}, // см. пункт 5
      "sources": {…}, "artists": […], "took_ms": 1830}
   провал: {"ok": false, "station_id": "…", "tracks": [],
            "reason": "ни один источник не дал треков этого жанра"}
   // причина обязательна: экран обязан сказать правду, а не «проверьте связь».

2) `POST /api/station/session/{session_id}/next` — следующая пачка того же
   эфира. БЕСПЛАТНЫЙ по сети по построению: кандидаты лежат в сессии, витрины и
   MusicBrainz этот вызов не трогает вообще (ограничитель MusicBrainz «не чаще
   запроса в секунду» иначе встал бы посреди эфира). Миллисекунды, а не секунды,
   — поэтому дозаказ успевает начаться, когда у игрока остаётся ≤3 трека.

   запрос (всё необязательное):
     {"events": [ {…} ],               // что случилось с прошлой пачкой — тот же
                                        // словарь, что у POST /api/stations/event;
                                        // сервер сам подставит session_id и batch_id
      "knobs": {"diversity": "discover"},   // смена ручки = пересборка остатка
      "batch": 12}                          // размер следующей пачки
   ответ — как в пункте 1, без `sources`/`artists`/`took_ms`, плюс `repeats`
   (сколько строк пачки — повторы по кулдауну; врать о них незачем).
   сессии нет (перезапуск приложения, час простоя):
     {"ok": false, "stale": true, "tracks": [],
      "reason": "сессия не найдена…"}
   → плеер обязан отреагировать тихо: открыть новую сессию, а не показать
   ошибку. Обрыв эфира из-за истёкшего состояния хуже, чем повтор состава.

3) `POST /api/station/session/{session_id}/knobs` — ручки внутри живого эфира.
   Запрос `{"knobs": {…}}`; ответ — пункт 1 без `tracks`: меняется порядок
   ОСТАТКА пула, очередь у игрока досчитывается сама. Смена ручки пишется
   событием `settings_changed` — как у Яндекса, где настройка пересобирает
   очередь в той же сессии, а не «со следующего запуска».

4) `GET /api/station/session/{session_id}` — состояние для отладки: пункт 3
   плюс `handed` (сколько треков уже выдано) и `next_up` — ближайшие 8
   кандидатов в текущем порядке. По `next_up` и проверяют, что фидбек
   ПЕРЕСТАВИЛ очередь, а не «просто не упал».

5) `knob_report` — ЧЕСТЬ РУЧЕК. Каждое значение с полями `effect`
   ("active" | "no-op") и `why`:
     diversity — active при favorite/discover/popular (доля «своих» артистов и
                 вес популярности); no-op в `default` — это значение по умолчанию;
     energy    — ВСЕГДА no-op: признаков bpm/energy у станционных треков нет;
     language  — ВСЕГДА no-op: язык трека определить нечем.
   Фронт не имеет права показывать контрол, который ничего не меняет, как
   рабочий (скилл ripster-setting-with-no-wire): либо прячет такие ручки, либо
   рисует подпись из `why`.

6) ЧТО ДЕЛАТЬ ПЛЕЕРУ (контракт поведения, а не только адресов):
   * `session_id` и `batch_id` из ответа сервера — в каждое событие (в
     `POST /api/stations/event` или телом `events` запроса `next`). Поле
     `batch_id` до сих пор не шлёт никто, и сервер не знает, какая выдача
     сработала, а какая — нет;
   * осталось ≤3 трека в очереди — слать `next`, НЕ блокируя игру (ответ
     приходит за миллисекунды);
   * `exhausted` или `suggest: "new_session"` — открыть новую сессию, не дожидаясь
     тишины в колонках;
   * трек, выданный в пачке N, в пачку N+1 не вернётся: сервер знает всё
     выданное сам, поэтому хвост очереди (`queue`/`prev_tracks`) с клиента не
     нужен и параметром не заведён.

═══════════════════════════════════════════════════════════════════════════════

ГДЕ ЧТО ЖИВЁТ. Сбор кандидатов, оценка и отбор пачки — в `stations.py`
(`pool` / `rank` / `draw`); здесь только состояние между запросами. Фидбек
читается через `station_events.session_feedback()` — ту самую функцию, которая
была написана, покрыта тестом и НИКОГДА не вызывалась из рабочего кода; теперь
её зовёт `_hand_out()` на каждой пачке. Оценка — по глубине прослушивания
(`played_s/length_s`), а не по числу строк-скипов.

СОСТОЯНИЕ В ПАМЯТИ, И ЭТО ОСОЗНАНО. Сессия живёт минуты; переживший перезапуск
id означал бы, что человек попал в чужой эфир с чужим фидбеком. Умирает сессия
по `TTL` простоя или по числу открытых (`MAX_SESSIONS`, вытесняются самые
давнишние) — и отвечает `stale: true`, а не ошибкой. Гость получает сессию на тех
же правах: его события пишутся с `is_guest=1`, вкус владельца не двигают, а его
собственный эфир перестраивают нормально — `session_id` уникален на одного
слушателя.
"""
from __future__ import annotations

import random
import threading
import time
import uuid
from collections import OrderedDict
from typing import Optional

BATCH = 10              # размер пачки: исследование §5.2 (8–10 треков)
BATCH_MAX = 25
RESERVE_MIN = 3         # Apple MIN_ITEMS_TO_MAINTAIN: дозаказ начинается за 3 трека
POOL_BATCHES = 4        # на сколько пачек вперёд держим пул — резерв с запасом
ARTIST_CAP = 2          # не больше двух треков одного артиста на пачку
ARTIST_GAP = 4          # и не ближе четырёх позиций друг к другу
TTL = 30 * 60           # полчаса простоя — и сессия никому не нужна
MAX_SESSIONS = 16       # столько человек слушают одновременно; дальше — LRU

_LOCK = threading.Lock()
_SESSIONS: "OrderedDict[str, dict]" = OrderedDict()


def _new_id() -> str:
    return uuid.uuid4().hex[:20]


def _touch(s: dict) -> None:
    """Сессия жива, пока её спрашивают. Держим не больше MAX_SESSIONS самых свежих.

    (Записи станции — в `station_events`, они и есть долгая память; здесь только
    очередь выдачи.)
    """
    now = time.time()
    s["seen"] = now
    _SESSIONS.move_to_end(s["id"])
    for sid in [k for k, v in _SESSIONS.items() if now - v["seen"] > TTL]:
        _SESSIONS.pop(sid, None)
    while len(_SESSIONS) > MAX_SESSIONS:
        _SESSIONS.popitem(last=False)


async def open_station(station_id: str, *, batch: int = BATCH,
                       seed: Optional[int] = None,
                       knobs: Optional[dict] = None,
                       services: Optional[list] = None,
                       is_guest: bool = False) -> dict:
    """Открыть станцию: собрать пул (здесь вся сеть) и выдать первую пачку.

    `is_guest` решает СЕРВЕР (по гостевой сессии, как в приёмнике событий):
    слушатель не выбирает, чей вкус двигать.

    `services` — какие сервисы у слушателя вообще можно играть. Без него пул
    собирается изо всего, что нашли, и тогда `reserve` врал: считал строки,
    которые браузер выбросит как неиграбельные, и дозаказ стартовал позже, чем
    надо.
    """
    from ripster import stations as _st
    want = max(1, min(BATCH_MAX * POOL_BATCHES, int(batch) * POOL_BATCHES))
    p = await _st.pool(station_id, want=want, knobs=knobs, services=services)
    if not p.get("ok"):
        return {"ok": False, "station_id": p.get("id", station_id),
                "title": p.get("title", ""), "tracks": [], "reason": p.get("reason", ""),
                "sources": p.get("sources") or {}}
    now = time.time()
    s = {"id": _new_id(), "station": p["id"], "title": p["title"],
         "seed": int(seed) if seed else random.randrange(1 << 30),
         "knobs": p["knobs"], "candidates": p["candidates"], "handed": set(),
         "sequence": 0, "batch_id": "", "batch": max(1, min(BATCH_MAX, int(batch))),
         "sources": p["sources"], "artists": p["artists"], "created": now, "seen": now,
         "guest": bool(is_guest)}
    with _LOCK:
        _SESSIONS[s["id"]] = s
        _touch(s)
        out = _hand_out(s, events=None)
    out.update({"station_id": s["station"], "title": s["title"],
                "sources": s["sources"], "artists": s["artists"],
                "took_ms": p.get("took_ms")})
    return out


def next_batch(session_id: str, *, events: Optional[list] = None,
               knobs: Optional[dict] = None, batch: Optional[int] = None) -> dict:
    """Следующая пачка в ту же сессию. Сети нет: пул лежит здесь.

    Порядок действий и есть весь смысл: сначала ПРИНИМАЕМ то, что человек сделал
    с прошлой пачкой, потом ПЕРЕСОБИРАЕМ остаток по этому фидбеку, и только
    потом раздаём. У Яндекса скип попадает в следующий `session/{id}/tracks` в
    том же `feedbacks` — адаптация внутри эфира, а не «завтра, если повезёт».
    """
    with _LOCK:
        s = _SESSIONS.get(session_id)
        if s is None:
            return _gone(session_id)
        _touch(s)
        if batch:
            s["batch"] = max(1, min(BATCH_MAX, int(batch)))
        return _hand_out(s, events=events, knobs=knobs)


def apply_knobs(session_id: str, knobs: Optional[dict]) -> dict:
    """Сменить ручки внутри живого эфира: остаток пула пересобирается сразу."""
    with _LOCK:
        s = _SESSIONS.get(session_id)
        if s is None:
            return _gone(session_id)
        _touch(s)
        _set_knobs(s, knobs, record_change=True)
        return _summary(s)


def state(session_id: str) -> dict:
    """Что известно об эфире: сколько выдано, сколько в резерве, что дальше."""
    from ripster import stations as _st
    with _LOCK:
        s = _SESSIONS.get(session_id)
        if s is None:
            return _gone(session_id)
        _touch(s)
        ranked = _st.rank(s["candidates"], bias=_st.taste_bias(),
                          session=_session_feedback(s), knobs=s["knobs"])
        out = _summary(s)
        out["next_up"] = [{"artist": e["cand"]["item"].get("artist", ""),
                           "title": e["cand"]["item"].get("title", ""),
                           "weight": round(e["weight"], 4)} for e in ranked[:8]]
        return out


def _gone(session_id: str) -> dict:
    """Честный ответ на «сессии нет». Не ошибка экрана — сигнал открыть новую."""
    return {"ok": False, "stale": True, "tracks": [], "session_id": session_id,
            "reason": "сессия не найдена: приложение перезапускалось или эфир "
                      "простаивал — откройте новую сессию"}


def _summary(s: dict) -> dict:
    from ripster import stations as _st
    return {"ok": True, "session_id": s["id"], "station_id": s["station"],
            "title": s["title"], "sequence": s["sequence"], "batch_id": s["batch_id"],
            "tracks": [], "reserve": len(s["candidates"]), "handed": len(s["handed"]),
            "exhausted": not s["candidates"],
            "suggest": "next_batch" if len(s["candidates"]) >= s["batch"] else "new_session",
            "reserve_min": RESERVE_MIN, "knobs": s["knobs"],
            "knob_report": _st.knob_report(s["knobs"])}


def _set_knobs(s: dict, knobs: Optional[dict], *, record_change: bool = False) -> None:
    """Новые ручки в живом эфире. Незнакомое значение — «как было», не отказ."""
    from ripster import stations as _st
    if knobs is None:
        return
    before = dict(s["knobs"])
    s["knobs"] = _st.knobs_norm(knobs)
    if not record_change or s["knobs"] == before:
        return
    try:
        from ripster import station_events as _se
        _se.record({"event": "settings_changed", "session_id": s["id"],
                    "batch_id": s["batch_id"], "source": "station",
                    "extra": {"from": before, "to": s["knobs"]}})
    except Exception as e:                                     # noqa: BLE001
        print(f"[station-session] settings_changed не записан ({type(e).__name__})",
              flush=True)


def _session_feedback(s: dict) -> dict:
    """Что сказал ЭТОТ слушатель в ЭТОЙ сессии.

    `owner_only=False` — потому что `session_id` серверный и уникальный: все
    строки этой сессии про одного человека, и гостю перестраивать эфир по его же
    скипам так же честно, как владельцу — по своим. Вкуса владельца при этом
    гость не двигает: долгий профиль (`taste_bias` ← `artist_stats`) читается с
    барьером `is_guest = 0`.
    """
    try:
        from ripster import station_events as _se
        return _se.session_feedback(s["id"], owner_only=False)
    except Exception as e:                                     # noqa: BLE001
        print(f"[station-session] фидбек не прочитан ({type(e).__name__}) — пачка "
              f"соберётся без перестройки", flush=True)
        return {}


def _accept(s: dict, events: Optional[list]) -> None:
    """Принять фидбек пачки.

    Пишется в ту же базу тем же `record()`, что и `POST /api/stations/event`, —
    приём не дублируем; несохранённые события приходят телом следующего запроса
    (ровно как `feedbacks` у Яндекса). `session_id` ставит СЕРВЕР: клиенту
    нельзя решать, в какую сессию писать.
    """
    if not events:
        return
    from ripster import station_events as _se
    for ev in events[:200]:                      # зациклившийся клиент не разрастет базу
        if not isinstance(ev, dict):
            continue
        row = dict(ev)
        row["session_id"] = s["id"]              # клиент не выбирает, в какую сессию писать
        row["is_guest"] = s["guest"]             # и не выбирает, чей вкус двигать
        row.setdefault("batch_id", s["batch_id"])
        _se.record(row)


def _hand_out(s: dict, *, events: Optional[list], knobs: Optional[dict] = None) -> dict:
    """Фидбек → пересборка → пачка → запоминание выданного.

    Единственное место, где `session_feedback()` доходит до того, что человек
    услышит дальше.
    """
    from ripster import stations as _st
    _accept(s, events)
    _set_knobs(s, knobs, record_change=True)
    ranked = _st.rank(s["candidates"], bias=_st.taste_bias(),
                      session=_session_feedback(s), knobs=s["knobs"])
    rnd = random.Random((s["seed"] * 1000003 + s["sequence"] + 1) & 0x7FFFFFFF)
    picked = _st.draw(ranked, s["batch"], rnd, cap=ARTIST_CAP, gap=ARTIST_GAP)
    for e in picked:
        s["handed"].add(e["cand"]["key"])
    # Антиповтор между пачками: выданное уходит из пула, а не «может вернуться».
    s["candidates"] = [e["cand"] for e in ranked if e["cand"]["key"] not in s["handed"]]
    s["sequence"] += 1
    s["batch_id"] = f"{s['id']}-{s['sequence']}"
    out = _summary(s)
    out["tracks"] = [e["cand"]["item"] for e in picked]
    out["repeats"] = sum(1 for e in picked if not e["cand"]["fresh"])
    return out
