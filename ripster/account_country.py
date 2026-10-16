"""Страна каждой УЧЁТКИ: единое чтение, честная причина, самопочинка.

Зачем (трекер #9005, 21.09.2026). Панель «страны аккаунтов» показывала по ОДНОЙ
стране на сервис и прятала её, если не считывался часовой пояс. Владелец видел
«страна не определена» там, где страна была известна (CA у Apple, NZ у Tidal),
и не видел главного: что шесть аккаунтов одного сервиса живут в шести странах,
а релиз приходит в Новую Зеландию на полсуток раньше, чем в Европу.

Данных для этого хватало и раньше — они лежали в кэшах самих сервисов
(`*_accounts.known()`), у Apple-слотов (`apple_accounts.all_slots`) и вprobe-
значениях конфига. Проблема была в том, что обзор умеет читать только три
сервиса из шести, а остальное додумывает.

Правила, которые здесь закреплены:

  • НИКОГДА не угадывать страну. Ни «us», ни локали машины. Пустая строка —
    это ответ «не известно», и у него есть причина (`reason`).
  • «нет страны» и «нет часового пояса» — РАЗНЫЕ факты. Одно не отменяет
    другое (см. routes/setup._country_info).
  • «мёртвый токен» — не «нет страны». Это нет доступа, и чинят его иначе.
  • Страна берётся ПОСЛЕДОВАТЕЛЬНО: свежее измерение → remembered → то, что
    вписал человек. `source` говорит, откуда именно взято значение, чтобы
    вписанную вручную витрину нельзя было выдать за измеренную: маршрутизатор
    решает по стране, есть ли релиз ВООБЩЕ, и выдуманная строка в конфиге
    стоит дороже честного «не знаю».
"""
from __future__ import annotations

import os
import threading
import time

#: Причины, по которой страны нет. Коды едут на клиент и переводятся там —
#: собранный на сервере русский текст не переводится на другой язык.
NEVER_PROBED = "never_probe"       # про эту учётку ещё никто не спрашивал
NO_FIELD     = "no_country_field"  # сервис не отдаёт страну аккаунта
DEAD_TOKEN   = "dead_token"        # токен мёртв: профиль не отвечал
PROBE_FAILED = "probe_failed"      # опрос упал — назови исключение
UNKNOWN      = "unknown"           # сервер не сообщил причин (старый кэш)

#: Как получено значение. `declared` = вписал человек, `measured` = замеры.
MEASURED = "measured"
DECLARED = "declared"

#: «Unknown» не должно прилипать навсегда: раньше панель один раз показала
#«не определено» и молчала, потому что перечитывала только свой же кэш.
_STALE_AFTER = 6 * 3600.0


def _label(*vals: str, fallback: str = "") -> str:
    """Человеческая метка слота, не секрет.

    В `wrapper-accounts[i].label` у владельца реально лежит 246-символьный
    blob Apple-авторизации (вставили не туда при добавлении аккаунта). Тащить
    его в JSON панели нельзя: маскируется здесь же, где показ, а не «потом в
    клиенте». Длинное и похожее на токен уходит на `fallback`.
    """
    for v in vals:
        s = str(v or "").strip()
        if not s:
            continue
        if len(s) > 60 or "+/" in s or s.startswith("0."):
            continue
        return s
    return fallback


def _clean(cc: str) -> str:
    """Нормализовать код страны. Всё, что не две буквы, — не страна."""
    cc = (cc or "").strip().upper()
    return cc if len(cc) == 2 and cc.isalpha() else ""


def result(country: str = "", reason: str = "", source: str = MEASURED,
           at: float = 0.0) -> dict:
    """Одна запись ответа: страна ИЛИ причина, но не молчание."""
    cc = _clean(country)
    return {"country": cc,
            "country_known": bool(cc),
            "country_reason": "" if cc else (reason or UNKNOWN),
            "country_source": source if cc else "",
            "checked_at": at or ""}


# ── Чтение по сервисам ───────────────────────────────────────────────────────
# Каждый источник — уже существующий кэш/проба; сети здесь нет ни в одном
# пути: обзор обязан оставаться дешёвым и не жечь слоты устройств.

def _tidal_slots(cfg: dict) -> list[dict]:
    from ripster import tidal_accounts as ta, tidal_pool as tp
    out = []
    for i, a in enumerate(tp.configured_accounts(cfg) or []):
        sec = ta.account_secret(a)
        info = ta.known(sec) or {}
        r = result(info.get("country"),
                   "" if info.get("country") else
                   (DEAD_TOKEN if info.get("alive") is False else NEVER_PROBED),
                   MEASURED, info.get("cached_at") or "")
        # В пуле у слота может быть вписанная вручную страна. Показываем её,
        # когда измерения нет, но НЕ выдаём за измеренную.
        if not r["country_known"]:
            declared = _clean(a.get("tidal-country") or a.get("country") or "")
            if declared:
                r = result(declared, source=DECLARED)
        out.append({"slot": i,
                    "label": _label(a.get("label"), fallback="primary" if i == 0 else f"slot{i}"),
                    "alive": info.get("alive"), "plan": info.get("plan") or "",
                    "sub_end": str(info.get("valid_until") or "")[:10],
                    "lossless": info.get("lossless"), **r})
    return out


def _apple_slots(cfg: dict) -> list[dict]:
    """Apple: витрина ПОДПИСКИ каждого слота, а не из ссылки и не из конфига.

    Документированная ловушка: `storefront` в конфиге — то, что человек вписал
    (у владельца было `us`, аккаунт оказался `ca`), а ссылка на альбом вообще
    говорит про чужую витрину. Решает только `/me/storefront` этого аккаунта —
    его и спрашивает контейнер слота (`apple_accounts.container_storefront`),
    а для остановленного слота помнит ответ на диске (`slot_countries.json`).
    """
    from ripster import apple_accounts as aa
    try:
        slots = aa.all_slots(include_stopped=True, config=cfg)
    except Exception:                                       # noqa: BLE001
        slots = []
    known = {s.get("container"): s for s in slots}
    out = []
    try:
        from ripster import wrapper_pool as wp
        accounts = wp._configured_accounts(cfg)
    except Exception:                                       # noqa: BLE001
        accounts = []
    for i, a in enumerate(accounts or [{"id": "", "label": "primary"}]):
        name = "amd-wrapper" if i == 0 else f"rip-wrapper-{i}"
        s = known.get(name) or {}
        cc = (s.get("country") or "").upper()
        out.append({"slot": i, "label": _label(a.get("label"), fallback=f"slot{i}"),
                    "container": name, "running": bool(s.get("running")),
                    **result(cc, "" if cc else NEVER_PROBED),
                    "plan": "", "sub_end": "", "lossless": None, "alive": None})
    return out


def _deezer_slots(cfg: dict) -> list[dict]:
    from ripster import deezer_accounts as da, deezer_pool as dp
    out = []
    for i, a in enumerate(dp._configured_accounts(cfg) or []):
        info = da.known(a["arl"]) or {}
        out.append({"slot": i, "label": _label(a.get("label"), fallback=f"account{i}"),
                    "alive": info.get("alive"), "plan": info.get("plan") or "",
                    # Deezer даты окончания не отдаёт: `expiration_timestamp` —
                    # срок годности набора опций, а не подписки. Пусто честнее.
                    "sub_end": "", "lossless": info.get("lossless"),
                    **result(info.get("country"),
                             "" if info.get("country") else
                             (DEAD_TOKEN if info.get("alive") is False else NEVER_PROBED))})
    return out


def _qobuz_slots(cfg: dict) -> list[dict]:
    from ripster import qobuz_accounts as qa, qobuz_pool as qp
    out = []
    for i, a in enumerate(qp._configured_accounts(cfg) or []):
        info = qa.known(qa.account_secret(a)) or {}
        out.append({"slot": i, "label": _label(a.get("label"), fallback=f"account{i}"),
                    "alive": info.get("alive"), "plan": info.get("plan") or "",
                    "sub_end": str(info.get("expires") or "")[:10],
                    "lossless": info.get("hires") or info.get("lossless"),
                    **result(info.get("country"),
                             "" if info.get("country") else
                             (DEAD_TOKEN if info.get("alive") is False else NEVER_PROBED))})
    return out


def _soundcloud_slots(cfg: dict) -> list[dict]:
    from ripster import soundcloud_accounts as sa, soundcloud_pool as sp
    out = []
    for i, a in enumerate(sp._configured_accounts(cfg) or []):
        info = sa.known(a["token"]) or {}
        out.append({"slot": i, "label": _label(a.get("label"), fallback=f"account{i}"),
                    "alive": info.get("alive"), "plan": info.get("plan") or "",
                    "sub_end": "",       # SoundCloud срока не отдаёт
                    "account": info.get("login") or "",
                    "lossless": info.get("go_plus"),
                    **result(info.get("country"),
                             "" if info.get("country") else
                             (DEAD_TOKEN if info.get("alive") is False else NEVER_PROBED))})
    return out


def _spotify_slots(cfg: dict) -> list[dict]:
    """Spotify страну аккаунта не отдаёт. Слоты показываем СТРОКОЙ с честным
    «не определено»: без них панель выглядела бы так, будто одна страна годится
    для всех учёток."""
    from ripster import spotify_pool as sp
    out: list[dict] = []
    # Слот 0 — основная учётка (её blob живёт отдельно от `spotify-accounts`),
    # дальше пул. Нумерация сквозная: два слота с номером 0 панель склеила бы в
    # один и показала одну страну там, где их две.
    try:
        has_primary = bool(sp.live_blob().exists())
    except Exception:                                       # noqa: BLE001
        has_primary = False
    if has_primary:
        out.append({"slot": 0, "label": _label(cfg.get("spotify-account-label"),
                                               fallback="primary"),
                    "alive": None, "plan": "", "sub_end": "", "lossless": None,
                    **result("", NO_FIELD)})
    for i, a in enumerate(cfg.get("spotify-accounts") or [], start=len(out)):
        if not isinstance(a, dict):
            continue
        out.append({"slot": i, "label": _label(a.get("label"), fallback=f"account{i}"),
                    "alive": None, "plan": "", "sub_end": "", "lossless": None,
                    **result("", NO_FIELD)})
    return out


_READERS = {"tidal": _tidal_slots, "apple": _apple_slots, "deezer": _deezer_slots,
            "qobuz": _qobuz_slots, "soundcloud": _soundcloud_slots,
            "spotify": _spotify_slots}


def slots(cfg: dict, svc: str) -> list[dict]:
    """Страны по каждому аккаунту сервиса. Пустой список — «сервис не знает
    про себя таких учёток», не ошибка."""
    fn = _READERS.get(svc)
    if fn is None:
        return []
    try:
        return fn(cfg) or []
    except Exception as e:                                  # noqa: BLE001
        # Один недоступный источник не должен сносить весь обзор: пустой
        # список панель покажет как «не измерено», а exception — как 500.
        print(f"[account_country] {svc}: {type(e).__name__}: {e}", flush=True)
        return []


def breakdown(rows: list[dict]) -> dict:
    """Итог по аккаунтам: различаются ли страны и какие именно.

    Различие — это и есть смысл панели: релиз выходит в НЗ на полсуток раньше
    Европы, и пока панель показывает «одну страну на сервис», она это прячет.
    """
    ccs = sorted({r["country"] for r in rows if r.get("country_known")})
    return {"countries": ccs, "varies": len(ccs) > 1,
            "unknown": sum(1 for r in rows if not r.get("country_known"))}


def stale_slots(rows: list[dict], max_age: float = _STALE_AFTER,
                now: float | None = None) -> list[dict]:
    """Слоты, которые стоит переспросить: без страны или с замером старше
    `max_age`. Это и есть самопочинка — «не определено» не должно прилипать
    до следующей ручной пробы."""
    now = now if now is not None else time.time()
    out = []
    for r in rows:
        if not r.get("country_known"):
            out.append(r)
            continue
        at = r.get("checked_at")
        try:
            if at and now - float(at) > max_age:
                out.append(r)
        except (TypeError, ValueError):
            out.append(r)
    return out


# ── Самопочинка: «unknown» не должно прилипнуть ─────────────────────────────
#
# Кэш страны живёт в кэшах самих сервисов (`*_accounts.known`) и у Apple-слотов
# (`slot_countries.json`). Если слот там не измерен, обзор показывал «не
# определено» и больше никогда не переспрашивал: перечитывался тот же пустой
# кэш. Отсюда и ощущение «даты и страны пропали» — они не пропадали, один
# неудачный ответ закреплялся до ручного «Проверить».
#
# Чиним ровно тем, что уже умеет приложение: те же пробы, что запускает сторож
# здоровья (`credential_health.check_all_*`), но фоновой задачкой и сgeneral
# троттлингом. НИКАКИХ перелогиниваний и записи токенов — только GET профиля,
# на котором эти пробы и построены.

_HEAL_PERIOD = 3600.0          # не чаще раза в час на сервис
_heal_at: dict[str, float] = {}

#: сервис → функция сторожа, которая меряет ВСЕ его слоты и пишет в кэш.
_HEALERS = {
    "tidal":      ("credential_health", "check_all_tidal_accounts"),
    "deezer":     ("credential_health", "check_all_deezer_arls"),
    "qobuz":      ("credential_health", "check_all_qobuz_accounts"),
    "soundcloud": ("credential_health", "check_all_soundcloud_tokens"),
}


def needs_heal(svc: str, rows: list[dict], now: float | None = None) -> bool:
    """Есть ли смысл переспрашивать: unknown-слот и час с прошлого запроса."""
    if svc not in _HEALERS:
        return False
    if not stale_slots(rows or [], now=now):
        return False
    t = time.time() if now is None else now
    return (t - _heal_at.get(svc, 0.0)) >= _HEAL_PERIOD


def heal(svc: str) -> bool:
    """Опросить все слоты сервиса их же пробой. True — если реально пуляли.

    Только для фоновой задачки: сама она ничего не решает, она обновляет кэш,
    из которого следующий обзор прочитает страну.
    """
    import asyncio

    entry = _HEALERS.get(svc)
    if not entry:
        return False
    _heal_at[svc] = time.time()
    mod, fn = entry

    async def _run():
        from importlib import import_module
        m = import_module(f"ripster.{mod}")
        # Сторож умеет ещё и снимать учётки с маршрутизации по порогу неудач.
        # Для починки СТРАНЫ это не нужно и опасно: вызываем проверку в режиме
        # «только померить» — порог заведомо за пределом того, что успевает
        # случиться за один прогон.
        check = getattr(m, fn)
        try:
            return check(threshold=10 ** 6)
        except TypeError:
            return check()

    try:
        asyncio.run(_run())
        return True
    except Exception as e:                                  # noqa: BLE001
        # Неудача починки — это НЕ «страны нет»: кэш остаётся как был, и
        # следующий обзор попробует снова по своему интервалу.
        print(f"[account_country] heal {svc}: {type(e).__name__}: {e}", flush=True)
        return False


def forget_heal(svc: str = "") -> None:
    """Разрешить перепроверку СЕЙЧАС же: после перевхода учётки (или правки её
    страны вручную) старое «не определено» не должно переживать этот интервал —
    иначе панель врёт ровно час после того, как всё уже можно узнать."""
    if svc:
        _heal_at.pop(svc, None)
    else:
        _heal_at.clear()


def schedule_heal(svc: str, rows: list[dict]) -> bool:
    """Фоновый запрос на починку, если он назрел. Возвращает True, если task
    поставили — caller'у это не нужно, но тесту полезно.

    RIPSTER_COUNTRY_HEAL=0 выключает сетевую часть: обзор остаётся полезным на
    машине без доступа к API (тесты, CI, офлайн-проверка), а решение «что
    переспросить» проверяется отдельно через `needs_heal`.
    """
    if not needs_heal(svc, rows):
        return False
    if os.environ.get("RIPSTER_COUNTRY_HEAL", "1") == "0":
        return False

    def _bg():
        try:
            heal(svc)
        except Exception:                                   # noqa: BLE001
            pass

    # Отдельный поток, а не create_task: обзор живёт в event-loop FastAPI, а
    # пробы сторожа — синхронные `asyncio.run(...)`, в чужом цикле они падают.
    threading.Thread(target=_bg, daemon=True).start()
    return True
