# -*- coding: utf-8 -*-
"""Самоизлечение склада радара: чужих однофамильцев — в карантин, не в мусор.

Жалоба владельца (живёт с 03.09.2026): в «Релизах» показывались чужие
артисты-однофамильцы («BOP», «Solomon Grey»). Привязку починили (`ripster/
artist_identity.py`: тождество подтверждается общими работами, а не именем), но
склады радара остались ИСПОРЧЕННЫМИ — карточки, положенные туда прежним
name-matching, никуда не делись и лезут в ленту со склада, даже когда источник
уже молчит.

Что решает инструмент (только по ДАННЫМ подписки, никогда по списку имён):
  • `foreign_stitch` — карточка пришла по name-сшивке (`via_xref`), и ни одна
    подписка не подтвердила этот id в этой витрине;
  • `name_claim` — карточка лейбловой ленты (`via_label`), у которой на подписку
    указывает ТОЛЬКО строка имени: id артиста пуст, подтвердить его нечем, а
    склад знает, что работой владеет другой id (или личность подписки так и не
    подтверждена). Так живёт «BOP» × «Rumination» Hospital Records в подписке на
    рэпера (21.09.2026);
  • `merged_page` — карточка лежит на склеенной Apple-странице подписки, а
    подтверждённый профиль личности её не опирает (другой человек с тем же
    именем).

Ничего не удаляется: карточка переезжает в `radar_store_quarantine.json` и
остаётся видимой. По умолчанию — СУХОЙ прогон: пишется отчёт
`radar_selfheal_report.json` со списками «удалил бы / оставил бы», и только
`--apply` действительно перекладывает.

Запуск:
    python tools/radar_selfheal.py                 # сухой прогон, отчёт на диск
    python tools/radar_selfheal.py --names BOP "Solomon Grey"
    python tools/radar_selfheal.py --apply         # после проверки отчёта
"""
from __future__ import annotations

import argparse
import json
import random
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# Консоль владельца на Windows — cp1251: «×» в сводке валил инструмент
# трассировкой уже ПОСЛЕ записи отчёта (21.09.2026). Русский там читается,
# поэтому кодировку не меняем — только глушим непереводимые знаки.
try:
    sys.stdout.reconfigure(errors="replace")
    sys.stderr.reconfigure(errors="replace")
except Exception:                                   # noqa: BLE001
    pass

sys.path.insert(0, str(ROOT))

from ripster import artist_identity as ident          # noqa: E402

WATCHLIST   = ROOT / "watchlist.json"
STORE       = ROOT / "radar_store.json"
QUARANTINE  = ROOT / "radar_store_quarantine.json"
REPORT      = ROOT / "radar_selfheal_report.json"

# Подписки, по которым владелец просил разбор, — всегда в отчёте первыми.
PINNED = ("BOP", "Solomon Grey")


def _load(path: Path, fallback):
    try:
        return json.loads(path.read_text(encoding="utf-8")) if path.exists() else fallback
    except Exception as e:                                 # noqa: BLE001
        print(f"[selfheal] не прочитан {path.name}: {e}", flush=True)
        return fallback


def _entries(wl) -> list:
    if isinstance(wl, dict):
        return wl.get("items") or []
    return wl or []


def _by_aid(entries: list) -> dict:
    out = {}
    for e in entries:
        aid = str(e.get("artist_id") or "")
        if aid and e.get("kind") != "label":
            out.setdefault(aid, e)
    return out


def verdict(card: dict, entries: list, by_aid: dict, judged: set,
            catalog: dict | None = None) -> tuple[str, str]:
    """('keep', '') | ('foreign_stitch'|'merged_page'|'name_claim', причина).

    `judged` — имена подписок, привязку которых ХОТЬ РАЗ пробовали. Для
    остальных «не подтверждён» означает «ещё не проверяли», а не «чужой»:
    решение «не знаю» карточку не трогает.
    """
    if not ident.stitch_shows(card, entries):
        nm = str(card.get("artist") or "").strip().casefold()
        if nm not in judged:
            return ("keep", "")
        return ("foreign_stitch",
                f"пришла по имени: {card.get('service')} artist_id="
                f"{card.get('artist_id')} не подтверждён ни одной подпиской")
    ok, why = ident.name_claim_shows(card, entries, catalog)
    if not ok:
        return ("name_claim", f"атрибуция по одному имени: {why}")
    if str(card.get("service") or "") == "apple":
        entry = by_aid.get(str(card.get("artist_id") or ""))
        if entry is not None:
            ok, why = ident.home_show(entry, card)
            if not ok:
                return ("merged_page", f"страница «{entry.get('name')}»: {why}")
    return ("keep", "")


def scan(entries: list, store: dict, sample: int = 5, seed: int = 20260921,
         catalog: dict | None = None):
    """Разобрать склад на памяти: (отчёт, что переложить, чистый склад).

    Свой аргумент не трогает — сухую проверку нельзя делать на живом файле.
    """
    catalog = catalog if catalog is not None else ident.load_catalog(ROOT)
    by_aid = _by_aid(entries)
    judged = {str(e.get("name") or "").strip().casefold()
              for e in entries if ident.identity_of(e).get("services")}
    rng = random.Random(seed)
    wanted = {n.casefold() for n in PINNED}
    # Случайные подписки — чтобы победа была над ВСЕМИ однофамильцами, а не над
    # двумя названными в жалобе.
    others = [e for e in entries
              if e.get("artist_id") and e.get("kind") != "label"
              and str(e.get("name") or "").strip().casefold() not in wanted]
    for e in (rng.sample(others, min(sample, len(others))) if others else []):
        wanted.add(str(e.get("name") or "").strip().casefold())

    cleaned: dict = {}
    drops: dict = {}
    report = {"quarantine_total": 0, "kept_total": 0,
              "sources": {}, "per_subscription": {}}
    for source, bucket in (store or {}).items():
        if not isinstance(bucket, dict):
            cleaned[source] = bucket
            continue
        keep_bucket, drop_bucket = {}, {}
        for uid, card in bucket.items():
            kind, why = verdict(card, entries, by_aid, judged, catalog)
            nm = str(card.get("artist") or card.get("alb_artist") or "")
            slot = None
            if nm.casefold() in wanted:
                slot = report["per_subscription"].setdefault(
                    nm or "?", {"quarantined": [], "kept": 0})
            if kind == "keep":
                keep_bucket[uid] = card
                report["kept_total"] += 1
                if slot is not None:
                    slot["kept"] += 1
                continue
            drop_bucket[uid] = dict(card, _reason=kind, _why=why)
            report["quarantine_total"] += 1
            if slot is not None:
                slot["quarantined"].append({
                    "title": card.get("title") or card.get("name") or "",
                    "date": card.get("date") or "", "reason": kind, "why": why,
                    "service": card.get("service") or source,
                    "url": card.get("url") or ""})
        cleaned[source] = keep_bucket
        report["sources"][source] = {"kept": len(keep_bucket),
                                     "quarantined": len(drop_bucket)}
        if drop_bucket:
            drops[source] = drop_bucket
    return report, drops, cleaned


def _bind_in_memory(entries: list, names: list, limit: int) -> int:
    """Привязать выбранные подписки, НЕ трогая watchlist.json.

    Приложение держит вишлист в памяти и перезаписывает файл целиком, поэтому
    наша пометка в файле просто потерялась бы. Здесь результат нужен одному
    отчёту — пусть живёт в памяти процесса.
    """
    import asyncio

    import httpx

    from ripster.config_service import load_config as _load_config
    cfg = _load_config(ROOT / "config.yaml", ROOT / "tokens")
    want = {n.casefold() for n in names}
    todo = [e for e in entries
            if e.get("artist_id") and e.get("kind") != "label"
            and (not want or str(e.get("name") or "").strip().casefold() in want)]
    todo = todo[:limit] if todo else []
    if not todo:
        return 0
    state = ident.load_spotify_state(ROOT)

    async def _run():
        async with httpx.AsyncClient(timeout=30) as client:
            for e in todo:
                try:
                    await ident.bind(e, client, cfg, spotify_state=state)
                except Exception as ex:                      # noqa: BLE001
                    print(f"[selfheal] bind «{e.get('name')}»: {ex}", flush=True)
    asyncio.run(_run())
    return len(todo)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--apply", action="store_true",
                    help="переложить в карантин (по умолчанию — только отчёт)")
    ap.add_argument("--names", nargs="*", default=[],
                    help="подписки, которые обязательно разобрать в отчёте")
    ap.add_argument("--sample", type=int, default=5,
                    help="сколько случайных подписок разобрать дополнительно")
    ap.add_argument("--bind", nargs="*", default=None,
                    help="привязать выбранные подписки (или все) ПЕРЕД разбором; "
                         "watchlist.json не трогается — разбор в памяти")
    args = ap.parse_args()

    global PINNED
    PINNED = tuple(list(PINNED) + list(args.names))

    entries = _entries(_load(WATCHLIST, []))
    store = _load(STORE, {})
    if not isinstance(store, dict) or not store:
        print("[selfheal] склад пуст или не читается — делать нечего", flush=True)
        return 1

    bound_now = 0
    if args.bind is not None:
        limit = len(args.bind) if args.bind else 40
        bound_now = _bind_in_memory(entries, args.bind, limit=limit)
        print(f"[selfheal] привязали {bound_now} подписок (в памяти, файл не тронут)",
              flush=True)

    bound = sum(1 for e in entries if ident.identity_of(e).get("services"))
    if not bound:
        print("[selfheal] на подписках НЕТ подтверждённых идентичностей: решение "
              "«не знаю» = оставляем как есть. Прогоните радар (привязка идёт там) "
              "или запустите с --bind — выдумывать здесь нечего.", flush=True)

    report, drops, cleaned = scan(entries, store, sample=args.sample)
    # Что радар НЕ решит сам: на склеенной странице лежат два человека, и какой
    # из них подписан — знает только владелец. Список идёт в отчёт, чтобы
    # «скрыто/показано» было чем объяснить.
    report["needs_owner"] = [
        {"name": e.get("name"), "artist_id": str(e.get("artist_id") or ""),
         "groups": (ident.identity_of(e).get("profile") or {}).get("groups") or {},
         "witnesses": (ident.identity_of(e).get("profile") or {}).get("witnesses") or {},
         "hide": (ident.identity_of(e).get("choice") or {}).get("hide") or []}
        # НЕ сохранённый флаг `identity["needs_owner"]`: он записан правилом той
        # версии, что была на момент привязки, и сам не поумнеет. Пересчёт по
        # сохранённым кластерам — чтобы ужесточение клейки было видно в отчёте
        # до новой сетевой привязки.
        for e in entries if ident.needs_owner(e)]
    report = dict(report, subscriptions_total=len(entries),
                  subscriptions_with_identity=bound, dry_run=not args.apply,
                  store_file=STORE.name)
    REPORT.write_text(json.dumps(report, ensure_ascii=False, indent=1),
                      encoding="utf-8")
    print(f"[selfheal] оставлено {report['kept_total']}, в карантин "
          f"{report['quarantine_total']}; отчёт: {REPORT.name}", flush=True)
    for nm, slot in sorted(report["per_subscription"].items()):
        print(f"  {nm}: законных {slot['kept']}, под вопросом "
              f"{len(slot['quarantined'])}")
        for c in slot["quarantined"][:12]:
            print(f"    − {c['date']} [{c['service']}] {c['title']} — {c['reason']}")

    if report["needs_owner"]:
        print(f"[selfheal] ждут решения владельца {len(report['needs_owner'])} "
              f"склеенных подписок:", flush=True)
        for i in report["needs_owner"][:12]:
            print(f"  ? {i['name']} (id {i['artist_id']}): "
                  f"{', '.join(f'{k} ×{v}' for k, v in i['groups'].items()) or 'групп нет'}",
                  flush=True)

    if not args.apply:
        print("[selfheal] СУХОЙ ПРОГОН: файлы не тронуты. Проверьте отчёт и "
              "запустите с --apply.", flush=True)
        return 0

    quar = _load(QUARANTINE, {})
    for source, bucket in drops.items():
        quar.setdefault(source, {}).update(bucket)
    QUARANTINE.write_text(json.dumps(quar, ensure_ascii=False), encoding="utf-8")
    tmp = STORE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(cleaned, ensure_ascii=False), encoding="utf-8")
    tmp.replace(STORE)
    print(f"[selfheal] переложили {sum(len(b) for b in drops.values())} карточек в "
          f"{QUARANTINE.name} (обратно — той же командой без --apply не отменить, "
          f"но файлы на месте)", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
