"""
Матрица доступности релиза по сервисам.

«Релиз вышел» — бесполезное утверждение. Мировая дата релиза это анонс, а витрины
наполняются каждая сама, в своей зоне и в своём темпе, да ещё аккаунты владельца
живут в разных странах (Tidal новозеландский, Apple американский, Deezer
европейский). Значение имеет только одно: **в каком сервисе я могу скачать это
прямо сейчас**.

Поэтому храним не «есть/нет», а состояние ПО КАЖДОМУ сервису вместе с причиной —
она различает три совершенно разные ситуации:

  not_in_catalog_yet  витрина ещё не наполнилась     → перепроверить позже
  region_locked       в этом сторефронте не будет    → перепроверять бессмысленно
  no_token            наша недоработка, не витрины   → чинится настройками

Сверяем только точными идентификаторами — по названию нельзя: одноимённые синглы,
делюксы и ремастеры дают ложные совпадения. Штрихкод (UPC) для Apple и Deezer,
ISRC первого трека для Qobuz и Tidal.

Кэш живёт на диске, иначе он не переживает перезапуск и каждое утро всё
опрашивается заново.
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Optional

# Сервисы, у которых есть точный поиск по идентификатору. Spotify держим
# отдельно: он каталог, а не источник файлов.
SERVICES = ("apple", "deezer", "qobuz", "tidal", "beatport")

REASON_NOT_YET  = "not_in_catalog_yet"
REASON_REGION   = "region_locked"
REASON_NO_TOKEN = "no_token"
# Релиз В КАТАЛОГЕ есть, а скачать эта учётка его не может: прав на поток нет.
# Отдельно от region_locked намеренно — это разные ответы человеку и разные
# действия программы. Регион лечится другой страной аккаунта; отсутствие прав —
# ДРУГОЙ УЧЁТКОЙ ТОГО ЖЕ сервиса (см. фоллбэк по аккаунтам) или покупкой, а
# прокси не добавляет прав вообще. 14.08.2026 Beatport отдавал 403-на-потоке на
# релизах, у которых is_available_for_streaming=True — по витрине не отличить.
REASON_NO_RIGHTS = "no_entitlement"
# Спросить было НЕЧЕМ. У Qobuz и Tidal нет поиска по штрихкоду — им нужен ISRC, и
# без него ответ «ещё не появился» был бы враньём: мы туда вообще не ходили.
# Отдельная причина, потому что лечится она не ожиданием, а добычей ISRC.
REASON_NO_ID    = "no_identifier"

# Найденное больше не перепроверяем: релиз из витрины не исчезает.
_TTL_MISS   = 30 * 60          # ещё не подъехало — заглядываем каждые полчаса
_TTL_REGION = 7 * 24 * 3600    # региональный отказ — раз в неделю, не чаще
_TTL_TOKEN  = 10 * 60          # наша проблема: почини токен — увидим сразу
# Прав нет — но подписку могли продлить, а релиз мог выйти из эксклюзива. Сутки:
# чаще смысла нет (ответ детерминированный), реже — застрянем на «нельзя», когда
# уже можно.
_TTL_RIGHTS = 24 * 3600

_cfg: dict = {}
_base_dir: Path = Path(".")
_cache: dict = {}
_loaded = False


def configure(cfg: dict, base_dir: Path) -> None:
    global _cfg, _base_dir
    _cfg, _base_dir = cfg, Path(base_dir)


def _cache_path() -> Path:
    return _base_dir / "availability_cache.json"


def _load() -> None:
    global _cache, _loaded
    if _loaded:
        return
    try:
        _cache = json.loads(_cache_path().read_text(encoding="utf-8")) or {}
    except Exception:
        _cache = {}
    _loaded = True


def _save() -> None:
    try:
        _cache_path().write_text(json.dumps(_cache, ensure_ascii=False), encoding="utf-8")
    except Exception:
        pass


def _key(upc: str, isrc: str, title: str, artist: str) -> str:
    if upc:
        return "upc:" + "".join(ch for ch in upc if ch.isdigit())
    if isrc:
        return "isrc:" + isrc.upper()
    return "name:" + (title or "").lower().strip() + "|" + (artist or "").lower().strip()


def _fresh(rec: dict) -> bool:
    """Нужно ли доверять записи или пора перепроверять."""
    if rec.get("available"):
        return True                     # найденное не пропадает
    age = time.time() - float(rec.get("checked_ts") or 0)
    reason = rec.get("reason") or REASON_NOT_YET
    ttl = {REASON_REGION: _TTL_REGION, REASON_NO_TOKEN: _TTL_TOKEN,
           REASON_NO_RIGHTS: _TTL_RIGHTS}.get(reason, _TTL_MISS)
    return age < ttl


def _has_credentials(service: str) -> bool:
    """Есть ли чем спрашивать этот сервис. Без этого «не найдено» — враньё."""
    c = _cfg or {}
    if service == "apple":
        tok = str(c.get("authorization-token") or "").strip()
        return bool(tok) and tok != "your-authorization-token"
    if service == "deezer":
        return True                     # публичный поиск по штрихкоду, токен не нужен
    if service == "qobuz":
        return bool(str(c.get("qobuz-auth-token") or "").strip())
    if service == "tidal":
        return bool(str(c.get("tidal-token") or "").strip()) or True  # есть путь через OrpheusDL
    if service == "beatport":
        # Каталог Beatport закрыт целиком: без логина не отвечает даже поиск, так
        # что «не нашли» без учётки было бы неправдой.
        return bool(str(c.get("beatport-username") or "").strip()
                    and str(c.get("beatport-password") or "").strip())
    return False


def _apple_storefronts() -> list[str]:
    """Магазины Apple, из которых мы РЕАЛЬНО можем качать, в порядке проверки.

    `storefront` в конфиге — это то, что человек вписал руками; `apple-country`
    приходит от самой Apple при проверке учётки и потому вернее. Разойтись они
    могут запросто, поэтому спрашиваем оба.

    Учётки пула (`wrapper-accounts`) своей страны пока не хранят — как только
    начнут, их коды добавятся сюда же, и проверка станет по-настоящему
    поаккаунтной. Список короткий и без повторов: каждый лишний магазин это
    лишний сетевой запрос на КАЖДУЮ проверку доступности.
    """
    c = _cfg or {}
    out: list[str] = []
    for v in (c.get("apple-country"), c.get("storefront"),
              *(a.get("storefront") or a.get("country")
                for a in (c.get("wrapper-accounts") or []) if isinstance(a, dict))):
        s = str(v or "").strip().lower()
        if s and s not in out:
            out.append(s)
    return out or ["us"]


async def _probe_one(service: str, upc: str, isrc: str) -> dict:
    """Спросить один сервис. Возвращает запись матрицы."""
    now = time.time()
    if not _has_credentials(service):
        return {"available": False, "reason": REASON_NO_TOKEN, "checked_ts": now}
    try:
        from ripster.routes import discovery as _disc
        hit = None
        if service == "apple" and upc:
            # Права выдаются на УЧЁТКУ, а не на сервис: релиза может не быть в
            # магазине из конфига и быть в магазине второй учётки. Раньше
            # спрашивали ровно один storefront — и «нет в Apple» означало на
            # деле «нет в том магазине, который вписан руками» (08.08.2026:
            # в конфиге стояло US, а учётка оказалась CA).
            for sf in _apple_storefronts():
                hit = await _disc._find_by_upc(upc, service, sf)
                if hit:
                    hit = dict(hit, storefront=sf)
                    break
        elif service == "deezer" and upc:
            hit = await _disc._find_by_upc(upc, service,
                                           str((_cfg or {}).get("storefront", "us")))
        elif service == "beatport" and upc:
            # Beatport умеет ОБА точных ключа (проверено 14.08.2026 на живом API:
            # /catalog/releases/?upc= и /catalog/tracks/?isrc= оба отдают count=1),
            # поэтому штрихкод первым, ISRC — запасной ход ниже.
            hit = await _disc._find_by_upc(upc, service)
        if hit is None and isrc and service in ("qobuz", "tidal", "beatport"):
            # Список ISRC (несколько первых треков) — одного мало: его может не
            # быть в каталоге при том, что релиз там есть.
            for one in (isrc.split(",") if isinstance(isrc, str) else list(isrc)):
                one = one.strip()
                if not one:
                    continue
                hit = await _disc._find_by_isrc(one, service)
                if hit:
                    break
        if hit is None and service in ("qobuz", "tidal") and not isrc:
            return {"available": False, "reason": REASON_NO_ID, "checked_ts": now}
        if hit is None and service == "beatport" and not upc and not isrc:
            return {"available": False, "reason": REASON_NO_ID, "checked_ts": now}
        if hit:
            return {"available": True, "url": hit.get("url", ""),
                    "title": hit.get("title", ""), "artist": hit.get("artist", ""),
                    "cover": hit.get("cover", ""), "matched_by": hit.get("matched_by", ""),
                    # В КАКОМ магазине нашлось. Когда учёток несколько, «доступно
                    # в Apple» без этого не отвечает на главный вопрос — какой
                    # именно учёткой качать.
                    "storefront": hit.get("storefront", ""),
                    "checked_ts": now}
    except Exception as e:                                     # noqa: BLE001
        return {"available": False, "reason": REASON_NOT_YET,
                "error": str(e)[:120], "checked_ts": now}
    # «Ещё не появился» — это ВЫВОД ИЗ ПРОВЕРКИ, а не значение по умолчанию.
    # apple/deezer опрашиваются только `if upc` (см. выше); без штрихкода обе
    # ветки пропускались, и сюда падал вердикт «ещё не появился» про сервисы,
    # которых никто не спрашивал — ложь, неотличимая от настоящего результата
    # (в кэше это видно по одинаковым до микросекунды checked_ts у всех служб).
    # Говорим правду: идентификатора не было.
    if service in ("apple", "deezer") and not upc:
        return {"available": False, "reason": REASON_NO_ID, "checked_ts": now}
    return {"available": False, "reason": REASON_NOT_YET, "checked_ts": now}


async def _derive_isrc(service: str, hit: dict) -> str:
    """Достать ISRC первого трека из найденного релиза.

    Он и есть ключ к Qobuz и Tidal: штрихкода у них нет, а ISRC опознаёт запись
    однозначно — без него пришлось бы сверять по названию, а это ложные
    совпадения на делюксах, ремастерах и одноимённых синглах.
    """
    try:
        from ripster.routes import discovery as _disc
        url = str(hit.get("url") or "")
        aid = ""
        if service == "deezer":
            import re
            m = re.search(r"/album/(\d+)", url)
            aid = m.group(1) if m else ""
        if service == "beatport":
            # Beatport отдаёт ISRC прямо в треках релиза — это САМЫЙ дешёвый
            # источник идентификатора для Qobuz и Tidal, у которых своего ключа
            # нет. Без него релиз, найденный только в Beatport, оставлял их в
            # состоянии «не спрашивал, нет ISRC» навсегда.
            import re
            m = re.search(r"/release/[^/]*/(\d+)", url)
            rid = m.group(1) if m else str(hit.get("id") or "")
            if not rid:
                return ""
            from ripster.routes import beatport as _bp
            from ripster import http_client as _HTTP
            tok = await _bp._get_token()
            if not tok:
                return ""
            async with _HTTP.ashared() as c:
                r = await c.get(f"{_bp._BASE}/catalog/releases/{rid}/tracks/",
                                headers=_bp._auth_headers(tok))
            if r.status_code != 200:
                return ""
            vals = [str(t.get("isrc") or "").strip()
                    for t in ((r.json() or {}).get("results") or [])[:4]]
            return ",".join(v for v in vals if v)
        if not aid:
            return ""
        vals = await _disc._seed_isrcs({"id": aid, "service": service}) or []
        return ",".join(vals)
    except Exception:
        return ""


async def matrix(upc: str = "", isrc: str = "", title: str = "", artist: str = "",
                 services: Optional[tuple] = None, force: bool = False) -> dict:
    """Где этот релиз можно взять прямо сейчас.

    Кэш: найденное не перепроверяется никогда, ненайденное — по своему TTL,
    региональный отказ — раз в неделю (в этом сторефронте оно не появится).
    """
    _load()
    svcs = tuple(services or SERVICES)
    k = _key(upc, isrc, title, artist)
    rec = _cache.get(k) or {"services": {}}
    out = dict(rec.get("services") or {})

    # По штрихкоду умеют Apple, Deezer и Beatport, поэтому идём ими первыми: если
    # релиз там нашёлся, из него добываем ISRC первых треков — и тогда Qobuz с
    # Tidal можно спросить по-настоящему, а не отписаться «нечем спросить».
    # Beatport добавлен в эту голову (а не в хвост) именно ради ISRC: у клубного
    # релиза он часто ЕДИНСТВЕННЫЙ, кто уже знает про пластинку.
    _SEEDERS = ("deezer", "apple", "beatport")
    ordered = [s for s in _SEEDERS if s in svcs] + \
              [s for s in svcs if s not in _SEEDERS]

    # ISRC, добытый КОГДА-ТО, годится всегда: он свойство записи, а не сервиса.
    # Раньше он поднимался из кэша только при живом «доступен» у сервиса-донора —
    # и стоило донору стать недоступным (403 по правам, ушёл из витрины), как
    # Qobuz с Tidal снова получали «не спрашивал, нет ISRC», хотя нужный ISRC
    # лежал в этой же записи. Программа знала и выбрасывала.
    isrc = isrc or str(rec.get("isrc") or "")

    for svc in ordered:
        cur = out.get(svc)
        if cur and not force and _fresh(cur):
            continue
        out[svc] = await _probe_one(svc, upc, isrc)
        if (svc in _SEEDERS and out[svc].get("available") and not isrc):
            isrc = await _derive_isrc(svc, out[svc])

    # Запись ДОПОЛНЯЕТСЯ, а не переписывается. Один и тот же релиз спрашивают из
    # разных мест с разной полнотой данных: карточка — со штрихкодом и названием,
    # рантайм после загрузки — только со штрихкодом. Пока здесь стояла замена,
    # бедный вызов затирал ISRC, добытый богатым, и следующая проверка снова
    # отвечала «не спрашивал, нет ISRC» — про то, что уже знала.
    _cache[k] = {**rec, "services": out,
                 "upc": upc or rec.get("upc", ""),
                 "isrc": isrc or rec.get("isrc", ""),
                 "title": title or rec.get("title", ""),
                 "artist": artist or rec.get("artist", ""),
                 "ts": time.time()}
    _save()
    return {"key": k, "upc": upc, "isrc": isrc, "services": out,
            "available_in": [s for s, v in out.items() if v.get("available")]}


# Токены причин из `runner._classify_partial_reason` → состояния матрицы. Здесь
# только те, что ГОВОРЯТ О ДОСТУПНОСТИ. Сетевые и врапперные отказы
# (_RE_WRAPPER_DEAD, _RE_PATIENT, _RE_DECRYPT_DOWN, «postprocess») сюда не
# попадают намеренно: авария нашей стороны — не факт о витрине, и записать её как
# «нельзя скачать» значит на сутки соврать самим себе.
_OUTCOME_TO_REASON = {
    "entitlement": REASON_NO_RIGHTS,
    "region":      REASON_REGION,
}


def record_outcome(service: str, outcome: str, *, upc: str = "", isrc: str = "",
                   title: str = "", artist: str = "", account: str = "") -> bool:
    """Записать в матрицу РЕАЛЬНЫЙ итог загрузки, а не итог опроса витрины.

    Опрос отвечает «есть ли в каталоге», и для Beatport этого мало: релиз с
    is_available_for_streaming=True всё равно отдаёт 403 на потоке, если у учётки
    нет прав. Единственный источник правды здесь — попытка скачать, поэтому
    рантайм после каждой отдаёт вердикт сюда.

    `outcome` — токен из `runner._classify_partial_reason` либо "ok".
    Возвращает True, если запись изменилась (то есть вердикт был про доступность).
    """
    if not service:
        return False
    _load()
    k = _key(upc, isrc, title, artist)
    rec = _cache.get(k) or {"services": {}}
    svcs = dict(rec.get("services") or {})
    now = time.time()
    if outcome == "ok":
        cur = dict(svcs.get(service) or {})
        cur.update({"available": True, "checked_ts": now, "verified_by": "download"})
        if account:
            cur["account"] = account
        svcs[service] = cur
    else:
        reason = _OUTCOME_TO_REASON.get(outcome)
        if not reason:
            return False
        svcs[service] = {"available": False, "reason": reason, "checked_ts": now,
                         "verified_by": "download",
                         **({"account": account} if account else {})}
    _cache[k] = {**rec, "services": svcs, "upc": upc or rec.get("upc", ""),
                 "isrc": isrc or rec.get("isrc", ""),
                 "title": title or rec.get("title", ""),
                 "artist": artist or rec.get("artist", ""), "ts": now}
    _save()
    return True


def pick_source(matrix_services: dict, preference: Optional[list] = None) -> str:
    """Откуда качать: пересечение предпочтений владельца с тем, что реально есть.

    Порядок по умолчанию — от лучшего качества к худшему. Ждать «свой» сервис,
    когда релиз уже доступен в другом, значит потерять сутки.
    """
    # Beatport последним: он покупочный магазин, и именно у него чаще всего
    # каталог есть, а прав на скачивание нет — брать его раньше значит менять
    # рабочий источник на тот, что вероятнее упрётся в 403.
    pref = preference or list((_cfg or {}).get("availability-preference")
                              or ["apple", "qobuz", "deezer", "tidal", "beatport"])
    for svc in pref:
        if (matrix_services.get(svc) or {}).get("available"):
            return svc
    # Предпочтения — это ПОРЯДОК, а не белый список. В config.yaml лежит
    # `availability-preference: [tidal, qobuz, apple]` — трёх сервисов из пяти там
    # нет вовсе, и релиз, доступный ТОЛЬКО в deezer (или теперь в beatport),
    # получал пустой ответ: «доступно, но качать неоткуда». Ровно та жалоба, из-за
    # которой затевался фоллбэк. Дальше идём по всем оставшимся доступным в
    # порядке SERVICES — молча и без ошибки.
    for svc in SERVICES:
        if svc not in pref and (matrix_services.get(svc) or {}).get("available"):
            return svc
    return ""


def summary_ru(matrix_services: dict) -> str:
    """Строка для владельца: не «релиз доступен», а где именно."""
    ready = [s for s, v in matrix_services.items() if v.get("available")]
    waiting = [s for s, v in matrix_services.items()
               if not v.get("available") and v.get("reason") == REASON_NOT_YET]
    blocked = [s for s, v in matrix_services.items()
               if v.get("reason") == REASON_REGION]
    no_rights = [s for s, v in matrix_services.items()
                 if v.get("reason") == REASON_NO_RIGHTS]
    no_tok = [s for s, v in matrix_services.items()
              if v.get("reason") == REASON_NO_TOKEN]
    no_id = [s for s, v in matrix_services.items()
             if v.get("reason") == REASON_NO_ID]
    parts = []
    if ready:
        parts.append("✅ " + ", ".join(ready) + " — можно скачать")
    if waiting:
        parts.append("⏳ " + ", ".join(waiting) + " — ещё не появился")
    if blocked:
        parts.append("🚫 " + ", ".join(blocked) + " — нет в регионе аккаунта")
    if no_rights:
        parts.append("🔒 " + ", ".join(no_rights) + " — есть в каталоге, но у аккаунта нет прав")
    if no_tok:
        parts.append("🔑 " + ", ".join(no_tok) + " — нет токена")
    if no_id:
        parts.append("❔ " + ", ".join(no_id) + " — не спрашивал, нет ISRC")
    return " · ".join(parts) or "нигде не найден"
