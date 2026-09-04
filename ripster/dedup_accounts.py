"""Разные учётные записи или один аккаунт под двумя ключами — и что с этим делать.

Повод (04.09.2026). У владельца в пуле SoundCloud лежали три токена, все живые,
все с Go+ — и выглядело это как три учётки. На деле два из трёх принадлежали
ОДНОМУ аккаунту (`goku`): пул давал две независимые учётки, а не три. Такое
незаметно по списку токенов, но прямо влияет на параллельность загрузок и на
перебор при отказе: «следующая учётка» оказывается той же самой, и вторая
попытка гарантированно повторяет первую.

Владелец: «дубли надо убирать… но с проверкой, а то ты можешь сам себя
обмануть». Поэтому здесь ровно одно основание для удаления — ОТВЕТ СЕРВИСА о
том, чей это аккаунт. Ни длина ключа, ни его похожесть, ни соседство в конфиге
основанием не являются.

Правила, которые модуль соблюдает и которые проверяются тестами:

1. Дубль — это совпадение `account_id`, полученного от сервиса. Пустой id
   дублем не считается никогда: «не знаю» ≠ «то же самое».
2. Обе записи должны быть измерены СВЕЖИМ запросом в этом же проходе и обе
   живы. Недоступность отменяет весь разбор: по молчанию сети выводов не
   делаем.
3. Из группы остаётся ровно одна запись. Основная (слот 0) не удаляется
   никогда — она лежит в главном поле конфига, и её потеря меняет поведение
   куда сильнее, чем удаление записи пула.
4. Перед удалением всё измеряется ЗАНОВО. Если что-то разошлось с планом —
   ничего не удаляется. План, составленный минуту назад, не считается
   доказательством.
5. Точные копии строки — отдельный случай: одинаковый ключ есть одна и та же
   запись по определению, тут сеть не нужна.

По умолчанию всё работает в режиме плана (`dry_run=True`) — удаление требует
явного согласия вызывающего.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field


@dataclass
class Entry:
    """Одна настроенная запись сервиса."""
    slot: int
    secret: str
    label: str
    primary: bool
    account_id: str = ""
    alive: bool | None = None
    unreachable: bool = False
    display: str = ""          # логин/имя — только для отчёта


@dataclass
class Duplicate:
    """Группа записей, ведущих в один аккаунт."""
    account_id: str
    display: str
    keep: Entry
    drop: list[Entry] = field(default_factory=list)
    exact_copy: bool = False   # совпали сами ключи, не только аккаунт


def _mask(secret: str) -> str:
    s = (secret or "").strip()
    return f"...{s[-6:]}" if len(s) > 10 else "???"


# ── описание сервисов ────────────────────────────────────────────────────────
# Каждый сервис умеет: перечислить записи, спросить у себя про одну запись,
# оценить пригодность и снять запись с маршрутизации.

def _deezer(config: dict):
    from ripster import deezer_accounts as da, deezer_pool as dp
    from ripster import credential_health as ch

    def entries() -> list[Entry]:
        return [Entry(slot=i, secret=a["arl"], label=a.get("label") or f"account{i}",
                      primary=(i == 0))
                for i, a in enumerate(dp._configured_accounts(config))]

    async def probe(e: Entry) -> dict:
        return await da.arl_info(e.secret, fresh=True)

    # Снятие у Deezer принимает ещё и признак «основная». Дубли основную не
    # трогают никогда (правило 3), поэтому здесь всегда False.
    def disable(e: Entry) -> None:
        ch.disable_deezer_arl(False, e.secret)

    return entries, probe, (lambda e: dp.health_rank(e.secret)), disable, "name"


def _qobuz(config: dict):
    from ripster import qobuz_accounts as qa, qobuz_pool as qp
    from ripster import credential_health as ch
    app_id = (config.get("qobuz-app-id") or "").strip()
    raw = qp._configured_accounts(config)

    def entries() -> list[Entry]:
        return [Entry(slot=i, secret=qa.account_secret(a), label=a.get("label") or f"account{i}",
                      primary=(i == 0))
                for i, a in enumerate(raw)]

    async def probe(e: Entry) -> dict:
        return await qa.account_info(raw[e.slot], app_id=app_id, fresh=True)

    def disable(e: Entry) -> None:
        ch.disable_qobuz_account(e.secret)

    return entries, probe, (lambda e: qp.health_rank(raw[e.slot])), disable, "plan"


def _soundcloud(config: dict):
    from ripster import soundcloud_accounts as sa, soundcloud_pool as sp
    from ripster import credential_health as ch

    def entries() -> list[Entry]:
        return [Entry(slot=i, secret=a["token"], label=a.get("label") or f"account{i}",
                      primary=(i == 0))
                for i, a in enumerate(sp._configured_accounts(config))]

    async def probe(e: Entry) -> dict:
        return await sa.account_info(e.secret, fresh=True)

    def disable(e: Entry) -> None:
        ch.disable_soundcloud_token(e.secret)

    return entries, probe, (lambda e: sp.health_rank(e.secret)), disable, "login"


SERVICES = {"deezer": _deezer, "qobuz": _qobuz, "soundcloud": _soundcloud}
RETIRE_KIND = {"deezer": "deezer_arl", "qobuz": "qobuz_account",
               "soundcloud": "soundcloud_token"}


async def _measure(entries: list[Entry], probe, display_key: str) -> None:
    """Спросить сервис про каждую запись. Пишем результат в сами записи."""
    for e in entries:
        try:
            info = await probe(e)
        except Exception as ex:                    # noqa: BLE001
            e.alive, e.unreachable = False, True
            e.display = f"ошибка проверки: {type(ex).__name__}"
            continue
        e.alive = bool(info.get("alive"))
        e.unreachable = bool(info.get("unreachable"))
        e.account_id = str(info.get("account_id") or "")
        e.display = str(info.get(display_key) or info.get("plan") or "")


def _group(entries: list[Entry], rank) -> list[Duplicate]:
    """Собрать группы одинаковых аккаунтов и выбрать, кого оставить."""
    dups: list[Duplicate] = []

    # 1. Точные копии ключа — доказательство само по себе, сеть не нужна.
    by_secret: dict[str, list[Entry]] = {}
    for e in entries:
        by_secret.setdefault(e.secret.strip(), []).append(e)
    copied = {id(e) for g in by_secret.values() if len(g) > 1 for e in g}
    for secret, g in by_secret.items():
        if len(g) < 2:
            continue
        g.sort(key=lambda e: (not e.primary, rank(e), e.slot))
        dups.append(Duplicate(account_id="", display="точная копия ключа",
                              keep=g[0], drop=g[1:], exact_copy=True))

    # 2. Разные ключи, один аккаунт — только по ответу сервиса и только у живых.
    by_acc: dict[str, list[Entry]] = {}
    for e in entries:
        if id(e) in copied:
            continue                                # уже разобрана как копия
        if not e.alive or e.unreachable or not e.account_id:
            continue                                # «не знаю» — не повод
        by_acc.setdefault(e.account_id, []).append(e)
    for acc, g in by_acc.items():
        if len(g) < 2:
            continue
        # Оставляем: основную — всегда; иначе самую способную; при равенстве —
        # ту, что раньше в конфиге (стабильность важнее вкусов).
        g.sort(key=lambda e: (not e.primary, rank(e), e.slot))
        dups.append(Duplicate(account_id=acc, display=g[0].display or acc,
                              keep=g[0], drop=g[1:]))
    return dups


async def plan(config: dict, service: str) -> tuple[list[Duplicate], list[str]]:
    """Что бы мы удалили — и почему. Ничего не меняет.

    Второй элемент — заметки для человека: причины, по которым разбор
    неполный (например, до сервиса не достучались).
    """
    make = SERVICES.get(service)
    if make is None:
        return [], [f"сервис {service} не поддержан"]
    entries_fn, probe, rank, _disable, display_key = make(config)
    entries = entries_fn()
    if len(entries) < 2:
        return [], []
    await _measure(entries, probe, display_key)

    notes: list[str] = []
    unreachable = [e for e in entries if e.unreachable]
    if unreachable:
        # Молчание сети — не «уникальная учётка». Пока хоть одна запись не
        # проверена, дубли среди ЖИВЫХ искать можно, а её саму — нельзя.
        notes.append(
            f"{len(unreachable)} из {len(entries)} записей проверить не удалось — "
            f"они в разборе не участвуют: " +
            ", ".join(f"{_mask(e.secret)} ({e.display})" for e in unreachable))
    return _group(entries, rank), notes


async def apply(config: dict, service: str, confirm: bool = False) -> list[str]:
    """Удалить лишние записи. Без `confirm=True` только рассказывает план.

    Перед удалением ВСЁ измеряется заново и сверяется с планом: если хоть одна
    пара разошлась — не удаляем ничего. План минутной давности доказательством
    не считается.
    """
    dups, notes = await plan(config, service)
    lines = list(notes)
    if not dups:
        return lines

    for d in dups:
        who = "точная копия ключа" if d.exact_copy else f"аккаунт {d.display}"
        lines.append(
            f"{service}: {who} — оставляю {_mask(d.keep.secret)} "
            f"(слот {d.keep.slot}{', основная' if d.keep.primary else ''}), "
            f"лишние: " + ", ".join(f"{_mask(e.secret)} (слот {e.slot})" for e in d.drop))
    if not confirm:
        lines.append("это план; удаление не выполнялось")
        return lines

    # ── повторная проверка ───────────────────────────────────────────────────
    recheck, _ = await plan(config, service)
    before = {(d.keep.secret, tuple(sorted(e.secret for e in d.drop))) for d in dups}
    after = {(d.keep.secret, tuple(sorted(e.secret for e in d.drop))) for d in recheck}
    if before != after:
        lines.append("⚠️ повторная проверка дала другой результат — не удаляю ничего")
        return lines

    _e, _p, _r, disable, _d = SERVICES[service](config)
    from ripster import credential_health as ch
    for d in dups:
        for e in d.drop:
            try:
                disable(e)
            except Exception as ex:                 # noqa: BLE001
                lines.append(f"✗ {_mask(e.secret)}: снять не удалось ({type(ex).__name__})")
                continue
            reason = ("точная копия ключа слота %d" % d.keep.slot) if d.exact_copy else \
                     f"дубль аккаунта {d.display} (оставлен слот {d.keep.slot})"
            # Снятие пишет в реестр СВОЮ причину («отверг сервис») — здесь она
            # была бы неправдой: ключ рабочий, он просто лишний. Перезаписываем
            # честной формулировкой, иначе владелец решит, что учётка умерла.
            from ripster import retired_credentials as _retired
            _retired.retire(RETIRE_KIND[service], e.secret, reason)
            ch._append_archive(f"{service}_dup", _mask(e.secret), "", reason)
            lines.append(f"💀 {service} {_mask(e.secret)} (слот {e.slot}) удалён: {reason}")
    return lines


def plan_sync(config: dict, service: str) -> tuple[list[Duplicate], list[str]]:
    """Синхронная обёртка для сторожа здоровья (он не в цикле событий)."""
    return asyncio.run(plan(config, service))
