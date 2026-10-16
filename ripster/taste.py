"""Вкус владельца, собранный из того, что ПК уже знает.

Владелец 05.09.2026: «чтобы раскопки учитывались и чтобы учитывались подписки
споти». И то и другое на ПК давно лежит, но телефон об этом не знал: станции на
нём строились по истории прослушиваний В САМОМ ТЕЛЕФОНЕ, а это капля рядом с
фонотекой и подписками на компьютере.

Два источника, и они РАЗНЫЕ по природе — поэтому и вес у них разный, и путать
их нельзя:

  · Раскопки (`digs.build_profile`) дают ИЗМЕРЕННЫЙ вес: сколько человек этого
    артиста качал и слушал. Замер 05.09.2026 — 40 артистов, у верхнего вес 88,
    у нижнего 15. Это настоящая шкала, её и берём как есть;

  · подписки Spotify — сигнал ДВОИЧНЫЙ: подписан или нет, промежуточного не
    бывает. Замер: 5761 подписка. Придумывать им «вес» по популярности или по
    алфавиту значило бы выдать выдумку за измерение, поэтому у всех один и тот
    же небольшой вес [FOLLOW_WEIGHT]: подписка — это «мне интересен», а не
    «слушаю каждый день».

Что модуль НЕ делает: не решает, что играть. Он отвечает на один вопрос — «кого
этот человек любит и насколько» — и отдаёт признаки. Решение принимает тот, кто
спросил (на телефоне это StationRanker).
"""
from __future__ import annotations

import json
from pathlib import Path

# Вес одной подписки Spotify относительно измеренного веса Раскопок.
# Специально мал: подписок тысячи, и если дать им вес наравне с реально
# слушаемым, они забьют профиль числом.
FOLLOW_WEIGHT = 3.0

# Столько подписок отдаём телефону. Не «сколько влезет»: замер показал 5761
# имя, это ~120 КБ на каждый запрос профиля. Верхняя часть списка ничем не
# лучше нижней (порядок у Spotify свой), поэтому режем по объёму, а НЕ делаем
# вид, что выбрали лучших.
FOLLOW_LIMIT = 2000


def _spotify_follows(base_dir: Path) -> list[str]:
    """Имена артистов, на которых владелец подписан в Spotify."""
    p = base_dir / "spotify_artist_state.json"
    if not p.exists():
        return []
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return []
    arr = ((data.get("followed") or {}).get("artists")) or []
    out = []
    for a in arr:
        nm = str((a or {}).get("name") or "").strip()
        if nm:
            out.append(nm)
    return out


def merge(digs: dict | None, follows: list[str],
          follow_limit: int = FOLLOW_LIMIT) -> dict:
    """Свести два источника в один профиль.

    Возвращает `{"artists": [{name, weight, genre, source}], "genres": [...],
    "counts": {...}}`. У каждой строки указан ИСТОЧНИК — чтобы на той стороне
    было видно, откуда взялась цифра, а не только сама цифра.
    """
    by_name: dict[str, dict] = {}

    for a in ((digs or {}).get("artists") or []):
        nm = str(a.get("name") or "").strip()
        if not nm or a.get("is_show"):
            continue
        by_name[nm.lower()] = {
            "name": nm,
            "weight": round(float(a.get("score") or 0.0), 2),
            "genre": str(a.get("genre") or "") or None,
            "source": "digs",
        }

    added = 0
    for nm in follows:
        if added >= follow_limit:
            break
        k = nm.lower()
        if k in by_name:
            # Подписан И слушает — это сильнее, чем просто слушает.
            by_name[k]["weight"] = round(by_name[k]["weight"] + FOLLOW_WEIGHT, 2)
            by_name[k]["source"] = "digs+follow"
            continue
        by_name[k] = {"name": nm, "weight": FOLLOW_WEIGHT, "genre": None,
                      "source": "follow"}
        added += 1

    artists = sorted(by_name.values(), key=lambda a: -a["weight"])
    genres = [
        {"genre": g.get("genre"), "weight": g.get("score"), "share": g.get("share")}
        for g in ((digs or {}).get("genres") or [])
        if g.get("genre")
    ]
    return {
        "artists": artists,
        "genres": genres,
        "counts": {
            "digs": sum(1 for a in artists if a["source"].startswith("digs")),
            "follows": sum(1 for a in artists if "follow" in a["source"]),
            "total": len(artists),
        },
    }


async def build(base_dir: Path, limit: int = 40) -> dict:
    """Профиль вкуса. Отказ одного источника не отменяет второй.

    Раскопки считаются по фонотеке и могут не ответить (нет данных, ошибка);
    подписки читаются из файла и могут отсутствовать. Возвращаем то, что
    получилось, и говорим в `counts`, чего сколько, — пустой профиль без
    объяснения был бы неотличим от «человек ничего не любит».
    """
    digs = None
    try:
        from ripster import digs as _digs
        digs = await _digs.build_profile(limit=limit, with_genres=True)
    except Exception:
        digs = None
    return merge(digs, _spotify_follows(base_dir))
