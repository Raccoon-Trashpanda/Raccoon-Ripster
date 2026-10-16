"""Самоаудит однофамильцев: перебрать весь склад текущими правилами.

Дверь ленты (`artist_identity.feed_filter`) судит при ЧТЕНИИ: склад не
переписывается, а в ленту не выходит то, чью принадлежность нечем подтвердить.
Этого мало в двух случаях, и оба — живые жалобы владельца:

  • карточка, пропущенная ВЧЕРашним правилом, сегодня должна быть спрятана, но
    она уже ушла в ленту однажды и лежит в скрытом списке подписки как
    «показана» — никто не сверил её заново;
  • карточка, спрятанная ПРЕЖНИМ правилом по ошибке, после правки должна
    ВЕРНУТЬСЯ. Сама она не вернётся: метка «скрыта» переживает правку.

Поэтому раз в сутки (и на старте, если правила менялись) прогоняем ВСЁ, что
накопилось на складе, через текущие доказательства, и пишем короткий отчёт:
сколько спрятали, сколько вернули, сколько утечек добавил день. Утечка — это
карточка, которую правило пропустило, а владелец отверг словом «не мой»
(`owner_feedback.mark_leak`): пока она растёт, рапортовать о «скрыто N»
нечестно.

Здесь нет ни одного имени артиста: правило то же, что и в ленте.
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Optional

STATE_NAME = "namesake_audit.json"
LOG_NAME = "namesake_audit.log"

_INTERVAL_S = 24 * 3600
# Хвост журнала: владелец читает последние строки, а не архив.
_LOG_KEEP = 400

_BASE: Optional[Path] = None


def configure(base_dir) -> None:
    global _BASE
    if base_dir is not None:
        _BASE = Path(base_dir)


def _base() -> Path:
    return _BASE or Path(".")


def state_file() -> Path:
    return _base() / STATE_NAME


def log_file() -> Path:
    return _base() / LOG_NAME


def model_version() -> str:
    """Отпечаток ПРАВИЛ, по которым судится лента.

    «Правила менялись» — это не про git: аудит обязан пересобрать вердикты,
    когда изменился любой признак (семьи жанров, совместимости, версия двери)
    или когда владелец добавил слово в реестр отзывов. Отпечаток — из того, что
    реально участвует в решении, а не из времени запуска.
    """
    import hashlib

    from . import artist_identity as ai
    from . import owner_anchor as oa
    from . import owner_feedback as of

    try:
        parts = [str(ai.IDENTITY_RULE_VERSION),
                 repr(oa.GENRE_FAMILIES), repr(oa.FAMILY_COMPAT),
                 repr(oa.COARSE_GENRES), repr(oa.GENRE_IGNORE),
                 repr(sorted(oa.BUCKET_FAMILIES)), oa._FUNC_TITLE_RE.pattern,
                 oa._MIX_TITLE_RE.pattern, str(oa._MAX_PER_SOURCE)]
        # Файла отзывов может просто не быть (свежая установка) — это не сбой
        # отпечатка: «пусто» обязано даваться тем же хэшем, что и всегда.
        try:
            st = of.store_file().stat()
            parts.append(f"feedback:{st.st_mtime_ns}:{st.st_size}")
        except OSError:
            parts.append("feedback:0:0")
    except Exception as e:                                     # noqa: BLE001
        return f"unknown:{type(e).__name__}"
    return hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()[:16]


def _load_state() -> dict:
    try:
        d = json.loads(state_file().read_text(encoding="utf-8"))
        return d if isinstance(d, dict) else {}
    except Exception:                                          # noqa: BLE001
        return {}


def _save_state(st: dict) -> None:
    try:
        state_file().write_text(json.dumps(st, ensure_ascii=False, indent=1),
                                encoding="utf-8")
    except Exception as e:                                     # noqa: BLE001
        print(f"[namesake] состояние не записано: {e}", flush=True)


def last() -> dict:
    """Последний прогон: {ts, model, hidden, returned, kept, leaks, log}."""
    return _load_state()


def due(now: float = 0) -> bool:
    """Пора ли: сутки истекли или правила с тех пор изменились."""
    st = _load_state()
    if not st:
        return True
    if str(st.get("model") or "") != model_version():
        return True
    return (now or time.time()) - float(st.get("ts") or 0) >= _INTERVAL_S


def _log(line: str) -> None:
    try:
        with log_file().open("a", encoding="utf-8") as f:
            f.write(line + "\n")
        lines = log_file().read_text(encoding="utf-8").splitlines()
        if len(lines) > _LOG_KEEP:
            log_file().write_text("\n".join(lines[-_LOG_KEEP:]) + "\n",
                                  encoding="utf-8")
    except Exception as e:                                     # noqa: BLE001
        print(f"[namesake] журнал не записан: {e}", flush=True)


def collect_store() -> list:
    """Все карточки долгосрочного склада радара — одним списком.

    Склад лежит в `radar_store.json` как {источник: {ключ: карточка}}; берём
    содержимое как есть, ничего не пересобирая: аудит судит РОВНО те карточки,
    из которых потом читается лента.
    """
    f = _base() / "radar_store.json"
    try:
        d = json.loads(f.read_text(encoding="utf-8"))
    except Exception:                                          # noqa: BLE001
        return []
    out = []
    for bucket in (d or {}).values():
        if isinstance(bucket, dict):
            out.extend(v for v in bucket.values() if isinstance(v, dict))
    return out


def sweep(entries: list, *, save=None, cards: Optional[list] = None,
          reason: str = "") -> dict:
    """Перебрать склад текущими правилами; вернуть {hidden, returned, kept, leaks}.

    `hidden` — карточки, которые правило спрятало (они остаются на складе и
    видны в отчёте, это не удаление). `returned` — те, что были спрятаны по
    прежним правилам, а по нынешним проходят: их возвращаем в ленту и снимаем
    с подписки метку «скрыто». Проводка обязана быть идемпотентной: второй
    прогон подряд не меняет ни числа, ни файлов.
    """
    from . import artist_identity as ai
    from . import owner_feedback as of

    cards = collect_store() if cards is None else cards
    entries = entries or []
    if not cards or not entries:
        return {"hidden": 0, "returned": 0, "kept": len(cards), "leaks": len(of.leaks()),
                "skipped": "нечем судить: пустой склад или пустой вишлист"}

    hidden_before = {ai.card_key(r) for r in cards
                     if _marked_hidden(ai, entries, r)}
    kept = {ai.card_key(r) for r in ai.feed_filter(
        [dict(r) for r in cards], entries, _base(), save)}
    all_keys = {ai.card_key(r) for r in cards}
    now_hidden = all_keys - kept
    # Спрятано ПРАВИЛОМ сейчас и не спрятано РАНЬШЕ: то, что утёкши в ленту
    # однажды, сегодня обязано исчезнуть — это и есть работа аудита.
    newly_hidden = now_hidden - hidden_before
    # Отмечено «скрыто» на подписке, но по нынешним правилам проходит: вернуть.
    returned = hidden_before - now_hidden

    _unhide(ai, entries, cards, returned, save)
    st = _load_state()
    leaks = len(of.leaks())
    prev_leaks = int(st.get("leaks") or 0)
    res = {"ts": time.time(), "reason": reason, "model": model_version(),
           "hidden": len(now_hidden), "newly_hidden": len(newly_hidden),
           "returned": len(returned),
           "kept": len(kept), "leaks": leaks,
           "leaks_delta": leaks - prev_leaks,
           "cards": len(cards), "counters": None}
    try:
        from . import owner_anchor as oa
        res["counters"] = oa.counters()
    except Exception:                                          # noqa: BLE001
        pass
    _save_state(res)
    line = (f"{time.strftime('%Y-%m-%d %H:%M', time.localtime(res['ts']))} "
            f"однофамильцы ({reason or 'рукой'}): скрыто {res['hidden']} "
            f"(новых {res['newly_hidden']}), возвращено {res['returned']}, "
            f"показано {res['kept']} из {res['cards']}, утечек {leaks}"
            + (f" (+{res['leaks_delta']})" if res["leaks_delta"] > 0 else ""))
    _log(line)
    print(f"[namesake] {line}", flush=True)
    return res


def _marked_hidden(ai, entries: list, rel: dict) -> bool:
    """Отмечена ли карточка «скрытой» на какой-нибудь подписке.

    Метка хранится как `tkey(заголовок)` (`hidden_add`), а склад — как
    «сервис|id»: сверяем и по заголовку, и по паре (сервис, id), иначе
    «возвращено» считается по одному ключу, а «скрыто» по другому.
    """
    t = ai.tkey(str(rel.get("title") or rel.get("name") or ""))
    svc, rid = str(rel.get("service") or ""), str(rel.get("id") or "")
    for e in entries:
        if str(e.get("name") or "").strip().lower() != \
                str(rel.get("artist") or "").strip().lower():
            continue
        for h in (ai.identity_of(e).get("hidden") or []):
            if (h.get("key") and h["key"] == t) or \
                    (svc and rid and str(h.get("service") or "") == svc
                     and str(h.get("id") or "") == rid):
                return True
    return False


def _unhide(ai, entries: list, cards: list, returned: set, save) -> None:
    """Снять метку «скрыто» с вернувшихся карточек — иначе они не вернутся.

    Лента судится при чтении, но отчёт «скрыто N» читается с подписки, и
    без этой чистки число росло бы вечно, показывая то, что уже показано.
    """
    if not returned:
        return
    touch = [r for r in cards if ai.card_key(r) in returned]
    touched = False
    for e in entries:
        for rel in touch:
            before = len(ai.identity_of(e).get("hidden") or [])
            ai.hidden_clear(e, rel)
            if len(ai.identity_of(e).get("hidden") or []) != before:
                touched = True
    if touched and save:
        try:
            save(entries)
        except Exception as e:                                 # noqa: BLE001
            print(f"[namesake] вишлист не сохранён: {e}", flush=True)


def run_if_due(entries: list, *, save=None, force: bool = False,
               reason: str = "") -> Optional[dict]:
    """Прогон по расписанию: сутки или смена правил. Молча пропускает не время.

    Всё, что может испортить ленту, здесь перехвачено: аудит — отчёт, а не
    обязательная часть показа ленты.
    """
    try:
        if not (force or due()):
            return None
        return sweep(entries, save=save, reason=reason or "по расписанию")
    except Exception as e:                                     # noqa: BLE001
        print(f"[namesake] аудит не состоялся: {e}", flush=True)
        return None


async def run_loop(entries: list, save=None, base_dir=None, *,
                   first_delay: float = 25.0,
                   interval: float = 6 * 3600) -> None:
    """Часовой прогон: сутки истекли или правила менялись — пересобрать вердикты.

    Интервал вчетверо меньше суток: сама проверка (`due()`) читает два файла и
    стоит доли миллисекунды, а пропустить «ночь после правки правила» дороже —
    именно на это владелец и жалуется («лечите алгоритм, а не артиста»).
    Первый прогон — через полминуты после старта: если отпечаток правил
    поменялся с прошлого запуска, лента приведена в порядок до того, как
    владелец её открыл.

    Сам прогон — в executor: он перебирает тысячи карточек, и вешать этим
    событийный цикл нельзя.
    """
    import asyncio

    if base_dir is not None:
        configure(base_dir)
    loop = asyncio.get_event_loop()
    first = True
    while True:
        try:
            await asyncio.sleep(first_delay if first else interval)
        except asyncio.CancelledError:
            return
        first = False
        try:
            if not due():
                continue
            await loop.run_in_executor(
                None, lambda: sweep(entries, save=save, reason="ночной прогон"))
        except Exception as e:                                 # noqa: BLE001
            print(f"[namesake] прогон не состоялся: {type(e).__name__}: {e}",
                  flush=True)


def report() -> dict:
    """Для healthcheck'а и отчёта владельца: последний прогон + утечки."""
    st = _load_state()
    from . import owner_feedback as of
    out = dict(st)
    out["model_now"] = model_version()
    out["stale_model"] = bool(st) and str(st.get("model") or "") != out["model_now"]
    out["due"] = due()
    out["feedback"] = of.counts()
    out["leak_items"] = of.leaks()[:10]
    # Число утечек — ТЕКУЩЕЕ, а не из состояния последнего прогона: между
    # прогонами человек мог отвергнуть ещё карточку, и healthcheck обязан
    # увидеть это сегодня, а не «когда-нибудь ночью».
    out["leaks"] = len(of.leaks())
    # Полное число утечек: `leak_items` — только первые десять, и по ним
    # healthcheck рапортовал бы «утечек 10», сколько бы их ни было на самом деле.
    out["leak_count"] = len(of.leaks())
    return out
