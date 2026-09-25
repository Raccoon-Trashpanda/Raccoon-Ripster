"""Слово владельца: «это не мой артист» — самое сильное доказательство из всех.

Якорь v5 (`ripster/owner_anchor.py`) выводит чужого по КОСВЕННЫМ делам: что
качал, что лайкнул, что звучало в станции. Одно нажатие на карточке —
единственное доказательство ПРЯМОЕ, и оно обязано перевешивать любое правило:

  • правило может молчать («нечем судить») — владелец не молчит никогда;
  • правило может ошибиться на данных, которых ещё не видело, — владелец эту
    ошибку уже увидел своими глазами (жалоба 24.09.2026 на Qobuz
    «639 Hz Inner Worth», прошедшего мимо якоря).

Хранится НЕ в `config.yaml` и не в `watchlist.json`, а своим файлом
`owner_feedback.json`: отзыв переживает перепривязку подписки, смену витрины,
перезапуск и переносится вместе с резервной копией настроек
(`/api/config/export` → ключ `owner_feedback`).

Обобщение — весь смысл этой функции. Владелец жмёт на ОДНУ карточку, а чужих
на витрине десятки. Поэтому вместе с релизом запоминается, ЧЕМ карточка выдала
себя: id артиста в ЧУЖОЙ витрине, лейбл, жанровая семья и функциональный узор
заголовка («… Hz …»). Следующая карточка с тем же id, тем же лейблом или тем же
узором прячется без нового нажатия — ровно так, как просит владелец: «нужно не
артиста лечить, а алгоритм и правила». Отдельно запомненный id НЕ должен быть
подтверждённым id самой подписки: на склеенной странице под одним id живут двое,
и прятать его целиком значило бы выгнать хозяина из дома.

Так же работает и обратный ход: «Это мой» на спрятанной автоматом карточке
возвращает её и обобщается на тот же id/лейбл — слово владельца выше правила в
обе стороны.
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Optional

from .artist_xref import norm

VERSION = 1

# Ограничения обязательны: без них файл растёт вечно, а один случайный тап по
# чужой витрине не должен перевешивать годы подтверждённой подписки.
_MAX_PER_KIND = 200          # записей на артиста (отдельно «не мой» и «это мой»)
_MAX_ARTISTS = 400

_NEGATIVE = "not_mine"
_POSITIVE = "mine"

_BASE: Optional[Path] = None
_MEM: dict = {"key": None, "data": None}


def configure(base_dir) -> None:
    """Каталог, где лежит `owner_feedback.json` (рядом со складом радара)."""
    global _BASE
    if base_dir is not None:
        _BASE = Path(base_dir)


def store_file() -> Path:
    return (_BASE or Path(".")) / "owner_feedback.json"


def _key() -> tuple:
    try:
        st = store_file().stat()
        return (st.st_mtime_ns, st.st_size)
    except OSError:
        return (0, 0)


def _blank() -> dict:
    return {"version": VERSION, "updated": 0, "artists": {}}


def load() -> dict:
    """Весь реестр с диска; перечитывается, когда файл тронули."""
    k = _key()
    if _MEM["key"] != k or _MEM["data"] is None:
        data = _blank()
        try:
            raw = json.loads(store_file().read_text(encoding="utf-8"))
            if isinstance(raw, dict) and isinstance(raw.get("artists"), dict):
                data = raw
        except FileNotFoundError:
            data = _blank()
        except Exception as e:                                     # noqa: BLE001
            print(f"[feedback] реестр не прочитан: {e}", flush=True)
        _MEM["data"], _MEM["key"] = data, k
    return _MEM["data"]


def save(data: dict) -> None:
    """Атомарно: полузаписанный реестр читался бы как «владелец передумал молча»."""
    f = store_file()
    data["version"] = VERSION
    data["updated"] = time.time()
    try:
        tmp = f.with_suffix(f.suffix + ".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1),
                       encoding="utf-8")
        tmp.replace(f)
    except Exception as e:                                         # noqa: BLE001
        print(f"[feedback] реестр не записан: {e}", flush=True)
        return
    _MEM["data"], _MEM["key"] = data, _key()


def invalidate() -> None:
    _MEM["key"] = None
    _MEM["data"] = None


def touched_after(ts: float) -> bool:
    """Сказано ли что-то в реестре позже момента `ts` (секунды).

    Нужен тем, кто держит у себя снимок ленты: перечитывать дверь личности на
    каждое открытие вкладки дорого, а проигнорировать свежее слово владельца —
    значит показать ему «нажатие не подействовало».
    """
    try:
        return store_file().stat().st_mtime > float(ts or 0)
    except (OSError, TypeError, ValueError):
        return False


# ── Форма одной записи ────────────────────────────────────────────────────────

def _tkey(title: str) -> str:
    from .artist_identity import tkey                # поздно: иначе цикл импортов
    return tkey(title)


def _sig(rel: dict, artist: str, verdict: str) -> dict:
    """ЧЕМ карточка выдала себя — то и запоминается для обобщения."""
    from . import owner_anchor
    from .artist_identity import label_key

    rel = rel or {}
    title = str(rel.get("title") or rel.get("name") or "")
    raw_lbl = str(rel.get("label") or "")
    return {
        "ts": time.time(),
        "verdict": verdict,
        "artist": str(artist or rel.get("artist") or ""),
        "service": str(rel.get("service") or ""),
        "artist_id": str(rel.get("artist_id") or ""),
        "label": label_key(raw_lbl, artist),
        "label_raw": raw_lbl,
        "genres": [str(g) for g in owner_anchor.genres_of(rel)],
        "families": sorted(owner_anchor.families_of(rel)),
        "title": title,
        "key": _tkey(title),
        # Функциональный узор заголовка («… Hz», «for sleep») — то, чем
        # медитативная однофамилица выдаёт себя, когда жанр — ведро «Ambient».
        "marker": owner_anchor.title_marker(title),
        "url": str(rel.get("url") or ""),
    }


def _newest(slot: dict) -> float:
    rows = (slot.get("negative") or []) + (slot.get("positive") or [])
    return max([float(r.get("ts") or 0) for r in rows] or [0.0])


def _slot(data: dict, artist: str, create: bool) -> Optional[dict]:
    name = norm(artist)
    if not name:
        return None
    arts = data.setdefault("artists", {})
    slot = arts.get(name)
    if slot is None:
        if not create:
            return None
        slot = arts[name] = {"name": str(artist or ""), "negative": [],
                             "positive": []}
        if len(arts) > _MAX_ARTISTS:
            arts.pop(min(arts.items(), key=lambda kv: _newest(kv[1]))[0], None)
    return slot


def record(artist: str, rel: dict, verdict: str) -> dict:
    """Запомнить слово владельца об этой карточке. Возвращает запись.

    `verdict` — "not_mine" («это не мой артист») или "mine" («это мой»).
    Повторный тап по той же карточке не удваивает запись, а обновляет её:
    владелец вправе передумать, и новое слово должно быть слышно.
    """
    v = _POSITIVE if str(verdict or "").lower() in (_POSITIVE, "is_mine", "yes") \
        else _NEGATIVE
    sig = _sig(rel, artist, v)
    data = load()
    slot = _slot(data, sig["artist"] or artist, create=True)
    if slot is None:
        return sig
    kind = "positive" if v == _POSITIVE else "negative"
    other = "negative" if v == _POSITIVE else "positive"
    rows = slot.setdefault(kind, [])
    rows[:] = [r for r in rows if not _same(r, sig)]
    rows.insert(0, sig)
    del rows[_MAX_PER_KIND:]
    # Слово в одну сторону отменяет прежнее слово по ТОЙ ЖЕ карточке: держать её
    # одновременно в «своих» и в «чужих» — значит позволить правилу выбрать
    # любое. Обобщённые записи другой стороны не трогаются: они про другие
    # карточки.
    slot[other] = [r for r in (slot.get(other) or []) if not _same(r, sig)]
    save(data)
    invalidate()
    return sig


def _same(a: dict, b: dict) -> bool:
    """Та же карточка: по ключу релиза, а при его отсутствии — по адресу."""
    if (a.get("key") or "") and a.get("key") == b.get("key"):
        return True
    return bool(a.get("url")) and a.get("url") == b.get("url")


def for_artist(artist: str) -> dict:
    return load().get("artists", {}).get(norm(artist)) or {}


# ── Вердикт по карточке ───────────────────────────────────────────────────────

def _sid_of(service: str, artist_id: str) -> str:
    return f"{service or ''}|{artist_id or ''}"


def label_of(rec: dict) -> str:
    """Лейбл записи в той форме, в какой его хранит реестр (`label_key`)."""
    return str((rec or {}).get("label") or "")


def _spread_backed(slot: dict, sid: str, aid: str, proven: set) -> bool:
    """Достаточно ли слов, чтобы по этому id прятать ВСЕ релизы витрины.

    Одно «не мой» по id, который подписка носит как СВОЙ, — ещё не приговор
    ветке: на склеенной странице под одним id живут двое, и 24.09 именно так
    законный альбом уехал из ленты. Приговором становятся два независимых
    подтверждения:

      • либо по ЭТОМУ ЖЕ (витрина, id) сказано «не мой» дважды — человек
        отверг не одну карточку, а ветку целиком;
      • либо имя уже уличено в подмене по лейблу (`proven`: один и тот же
        лейбл отвергнут на РАЗНЫХ витринах) и этот id среди записей с тем
        лейблом.
    """
    neg = slot.get("negative") or []
    if sum(1 for r in neg if r.get("artist_id")
           and _sid_of(str(r.get("service") or ""),
                       str(r.get("artist_id"))) == sid) >= 2:
        return True
    if not (proven and aid):
        return False
    return any(aid == str(r.get("artist_id") or "")
               and label_of(r) in proven for r in neg)


def _proven_labels(slot: dict) -> set:
    """Лейблы-крышки: отвергнуты на РАЗНЫХ витринах.

    Одна карточка на одном импринте — ещё не доказательство: у артиста может
    быть два релиза на одном лейбле, и тап по ошибке спрятал бы половину
    каталога. Две разные витрины с тем же лейблом — это уже не однофамилец, а
    чужая подборка, собранная под нашим именем (Solomon Grey / «Spaceman
    Recordings», 25.09: Qobuz ×4, Spotify ×2, Apple ×1).
    """
    by_lbl: dict[str, set] = {}
    for r in slot.get("negative") or []:
        k = label_of(r)
        if k:
            by_lbl.setdefault(k, set()).add(str(r.get("service") or ""))
    return {k for k, svc in by_lbl.items() if len({s for s in svc if s}) >= 2}


def label_rejected(artist: str, label: str) -> bool:
    """Отвергнут ли этот лейбл словом хозяина по этому имени.

    Нужен тем, кто проверяет «а не своё ли это»: профиль склеенной страницы
    носит в своих лейблах и настоящую витрину, и крышку однофамильца («Spaceman
    Recordings», 25.09) — считать совпадение с ним доказательством «своего»
    нельзя, иначе слово владельца пересиливает то, против чего его сказали.
    """
    slot = for_artist(artist)
    if not slot or not str(label or "").strip():
        return False
    from .artist_identity import label_key                # поздно: иначе цикл
    k = label_key(str(label), artist)
    if not k:
        return False
    if k in _proven_labels(slot):
        return True
    return any(str(r.get("label") or "") == k for r in (slot.get("negative") or []))


def decide(rel: dict, artist: str, own_ids=()) -> dict:
    """Слово владельца против этой карточки: {"dec", "why", "soft"}.

    "" — владелец по ней ничего не говорил: дальше решают якорь и профиль.
    Порядок обязателен: сначала точное слово об ЭТОЙ карточке (любой стороны),
    потом обобщённое «это мой», и только потом обобщённое «не мой» — прячущее
    правило не должно красть то, что человек уже вернул себе.

    `soft: True` — обобщение по id, который подписка носит как СВОЙ: такое слово
    сильнее автоматических правил, но обязано уступить ПОДТВЕРЖДЁННОЙ подписи —
    той же ветке, чью работу зовёт чистый каталог владельца. Жёсткое слово
    (`soft: False`) перекрывает всё: ни якорь, ни профиль к нему не применяются.
    """
    from . import owner_anchor

    slot = for_artist(artist)
    if not slot:
        return {"dec": "", "why": "", "soft": False}
    rel = rel or {}
    title = str(rel.get("title") or rel.get("name") or "")
    key = _tkey(title)
    aid = str(rel.get("artist_id") or "")
    sid = _sid_of(str(rel.get("service") or ""), aid) if aid else ""
    own = {str(x) for x in (own_ids or ()) if x and str(x).strip("|")}
    # Под защитой и «пара сервис|id», и ГОЛЫЙ id: подписка носит канонический
    # id на верхнем уровне без витрины (`artist_id`), а карточка приходит с
    # сервисом — сверять их по одному формату значило бы прятать своё.
    own_pairs = {o for o in own if "|" in o}
    own_bare = {o.split("|")[-1] for o in own}
    own_sid = bool(sid) and (sid in own_pairs or aid in own_bare)
    lbl = norm(str(rel.get("label") or ""))
    fams = owner_anchor.families_of(rel)
    marker = owner_anchor.title_marker(title)
    pos, neg = slot.get("positive") or [], slot.get("negative") or []
    proven = _proven_labels(slot)

    for recs, dec, word in ((pos, "show", "это мой"), (neg, "hide", "не мой")):
        if key and any(r.get("key") == key for r in recs):
            return {"dec": dec, "why": f"хозяин сказал: «{word}» об этом релизе",
                    "soft": False}
    # Обобщение «это мой» раньше обобщения «не мой»: вернуть человеку его
    # подписку важнее, чем догадаться спрятать ещё что-то.
    for r in pos:
        if sid and r.get("artist_id") and _sid_of(r.get("service"),
                                                  r.get("artist_id")) == sid:
            return {"dec": "show", "soft": False,
                    "why": (f"хозяин сказал «это мой» релизам артиста "
                            f"{r.get('service')}:{r.get('artist_id')}")}
        if lbl and r.get("label") and norm(r["label"]) == lbl:
            return {"dec": "show", "soft": False,
                    "why": f"хозяин сказал «это мой» лейблу {r.get('label_raw')}"}
    for r in neg:
        if sid and r.get("artist_id") and _sid_of(r.get("service"),
                                                  r.get("artist_id")) == sid:
            # Чужой id прячется по первому слову. Свой — только по подтверждённому
            # (повтор по этой же ветке или уличённый лейбл), и то МЯГКО: под этим
            # id склеенная страница носит и настоящего хозяина.
            if own_sid and not _spread_backed(slot, sid, aid, proven):
                continue
            return {"dec": "hide", "soft": own_sid,
                    "why": (f"хозяин сказал: «не мой» артист "
                            f"{r.get('service')}:{r.get('artist_id')}")}
        if lbl and r.get("label") and norm(r["label"]) == lbl:
            return {"dec": "hide", "soft": False,
                    "why": f"хозяин сказал: «не мой» лейблу {r.get('label_raw')}"}
        if lbl and lbl in proven:
            # Та же крышка, но подписанная формой, которой нет в записи
            # («Spaceman» против «Spaceman Recordings»): уличённый лейбл есть
            # у имени — прячем и её.
            return {"dec": "hide", "soft": False,
                    "why": f"хозяин сказал: «не мой» лейблу {rel.get('label')}"}
        if marker and r.get("marker") == marker and (
                not fams or not r.get("families")
                or fams & set(r["families"])):
            return {"dec": "hide", "soft": False,
                    "why": (f"хозяин сказал: «не мой» релизам с «{marker}» "
                            f"у этого артиста")}
    return {"dec": "", "why": "", "soft": False}


def gate(rel: dict, artist: str, own_ids=()) -> tuple[str, str]:
    """Слово владельца по карточке: ("show"|"hide"|"", почему).

    Значение `soft` из `decide` здесь теряется: этот срез нужен тем, кто не
    различает жёсткое слово и обобщение по своей витрине.
    """
    d = decide(rel, artist, own_ids)
    return d["dec"], d["why"]

# ── Отчёт и переносимость ─────────────────────────────────────────────────────

def counts() -> dict:
    arts = load().get("artists") or {}
    return {"artists": len(arts),
            "negative": sum(len(a.get("negative") or []) for a in arts.values()),
            "positive": sum(len(a.get("positive") or []) for a in arts.values())}


def describe(artist: str) -> dict:
    """Что знает реестр об артисте — для карточки «почему скрыто» и для /api/identity."""
    slot = for_artist(artist)
    if not slot:
        return {}
    fields = ("title", "service", "artist_id", "label_raw", "families",
              "marker", "ts", "leak")
    return {"name": slot.get("name") or artist,
            "negative": [{k: r.get(k) for k in fields}
                         for r in (slot.get("negative") or [])[:10]],
            "positive": [{k: r.get(k) for k in fields}
                         for r in (slot.get("positive") or [])[:10]]}


def export() -> dict:
    """Тело для резервной копии настроек (`/api/config/export`)."""
    return load()


def apply_export(payload: dict) -> int:
    """Слить реестр из резервной копии. Возвращает число привнесённых карточек.

    Слияние по записи, а не заменой файла: копия могла быть снята давно, и
    молча затирать ею свежие отзывы нельзя.
    """
    if not isinstance(payload, dict) or not isinstance(payload.get("artists"), dict):
        return 0
    data = load()
    added = 0
    for name, slot in payload["artists"].items():
        if not isinstance(slot, dict):
            continue
        target = _slot(data, slot.get("name") or name, create=True)
        if target is None:
            continue
        for kind in ("negative", "positive"):
            have = target.setdefault(kind, [])
            for rec in slot.get(kind) or []:
                if isinstance(rec, dict) and not any(_same(h, rec) for h in have):
                    have.insert(0, rec)
                    added += 1
            del have[_MAX_PER_KIND:]
    if added:
        save(data)
        invalidate()
    return added


# ── Метрика утечки ────────────────────────────────────────────────────────────

_LEAK_CAP = 500


def leaks() -> list:
    """Карточки, которые радар ПОКАЗАЛ, а владелец отверг.

    Это метрика качества алгоритма: если она растёт, правило пропускает целые
    классы однофамильцев, и рапортовать о «скрыто N» было бы нечестно.
    """
    out = []
    for slot in (load().get("artists") or {}).values():
        for r in slot.get("negative") or []:
            if r.get("leak"):
                out.append({"artist": r.get("artist"), "title": r.get("title"),
                            "service": r.get("service"), "ts": r.get("ts")})
    out.sort(key=lambda x: x.get("ts") or 0, reverse=True)
    return out[:_LEAK_CAP]


def mark_leak(artist: str, rel: dict) -> bool:
    """Отметить, что карточку показал радар и владелец её отверг. True, если отметка новая.

    Зовётся СРАЗУ после `record`: «не мой» по карточке, которую правило
    пропустило, — это промах правила, а по уже спрятанной — согласие с ним.
    """
    sig = _sig(rel, artist, _NEGATIVE)
    slot = for_artist(sig["artist"])
    for r in slot.get("negative") or []:
        if _same(r, sig):
            if r.get("leak"):
                return False
            r["leak"] = True
            save(load())
            invalidate()
            return True
    return False
