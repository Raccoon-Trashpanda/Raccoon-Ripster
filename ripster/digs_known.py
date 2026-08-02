"""Кого владелец УЖЕ знает — чтобы «Раскопки» копали, а не показывали своё.

ЗАЧЕМ
-----
Дерево похожих артистов отдавало верхушку рейтинга ListenBrainz как есть. Оно
устойчиво по построению — на один и тот же запрос приходит один и тот же список,
и в нём в первую очередь те, кого владелец давно слушает и на кого подписан.
Смысл копателя при этом исчезает: он показывает знакомое под видом находки
(жалоба 01.08.2026: «почему там одни и те же, включая тех, на кого я уже
подписался»).

ЧТО СЧИТАЕТСЯ «ЗНАКОМЫМ»
-----------------------
Четыре независимых списка, любой из них — уже знакомство:

* подписки Spotify — самое сильное свидетельство, их тысячи;
* вишлист — за этими следят намеренно;
* фонотека и история загрузок — этих уже качали;
* «любимые артисты», заданные руками на старте Раскопок.

СОЗНАТЕЛЬНО НЕ ФИЛЬТРУЕМ НАСМЕРТЬ
--------------------------------
Знакомые не выбрасываются молча, а помечаются `known`. Во-первых, у нишевого
артиста похожих может быть всего трое, и все знакомые — пустое дерево хуже
знакомого. Во-вторых, увидеть «а этот у меня уже есть» — тоже полезный ответ.
Решение, показывать их или нет, принимает вызывающий.
"""
from __future__ import annotations

import json
import time
from pathlib import Path

_BASE = Path(__file__).parent.parent

# Пересобирать на каждый пузырь незачем: списки меняются раз в день, а кликов по
# дереву — десятки подряд.
_TTL = 300
_cache: dict = {"ts": 0.0, "names": frozenset()}


def _norm(s: str) -> str:
    from ripster.digs import _norm as _n
    return _n(s)


def _from_spotify() -> list[str]:
    """Подписки Spotify — они же основа релиз-радара.

    Сначала из памяти работающего приложения, а если там пусто — с диска.
    Модульная переменная наполняется при установке роутера, поэтому в стороннем
    процессе (тест, разовый скрипт) она пуста, и без файла знакомых оказывалось
    полсотни вместо пяти тысяч.
    """
    try:
        from ripster.routes import spotify as _sp
        live = [a.get("name", "") for a in (_sp._sp_followed_cache.get("artists") or [])]
        if live:
            return live
    except Exception:
        pass
    try:
        data = json.loads((_BASE / "spotify_artist_state.json").read_text(encoding="utf-8"))
        return [a.get("name", "") for a in ((data.get("followed") or {}).get("artists") or [])]
    except Exception:
        return []


def _from_json_list(path: Path, keys: tuple) -> list[str]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return []
    items = data if isinstance(data, list) else (data.get("items") or data.get("artists") or [])
    out: list[str] = []
    for it in items:
        if isinstance(it, str):
            out.append(it)
        elif isinstance(it, dict):
            for k in keys:
                if it.get(k):
                    out.append(str(it[k]))
                    break
    return out


def _from_favorites() -> list[str]:
    """«Любимые артисты», заданные владельцем руками на старте Раскопок."""
    try:
        import yaml
        cfg = yaml.safe_load((_BASE / "config.yaml").read_text(encoding="utf-8")) or {}
        v = cfg.get("digs-favorite-artists") or []
        if isinstance(v, str):
            v = [x.strip() for x in v.split(",")]
        return [str(x) for x in v]
    except Exception:
        return []


def known_names(force: bool = False) -> frozenset:
    """Нормализованные имена всех, кого владелец уже знает.

    Любой источник может отвалиться (нет файла, не поднят модуль Spotify) — это
    не повод падать: тогда просто знакомых меньше, и в дерево попадёт лишнее.
    Пустой ответ здесь безопаснее исключения.
    """
    now = time.time()
    if not force and _cache["names"] and (now - _cache["ts"]) < _TTL:
        return _cache["names"]

    names: list[str] = []
    names += _from_spotify()
    names += _from_json_list(_BASE / "watchlist.json", ("artist", "name", "artist_name"))
    names += _from_json_list(_BASE / "download_history.json", ("artist", "artist_name"))
    names += _from_favorites()

    out = frozenset(n for n in (_norm(x) for x in names if x) if n)
    _cache["names"] = out
    _cache["ts"] = now
    return out


def mark(items: list[dict], key: str = "name") -> list[dict]:
    """Проставить каждому `known`. Список меняется на месте и возвращается."""
    kn = known_names()
    for it in items:
        it["known"] = _norm(it.get(key, "")) in kn
    return items
