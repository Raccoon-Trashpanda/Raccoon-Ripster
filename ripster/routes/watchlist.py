"""
Watchlist routes — CRUD + background new-release checker + smart suggestions.

Install: watchlist.install(app, ctx)
"""
from __future__ import annotations

import asyncio
import json
import re
from datetime import datetime, timedelta
from pathlib import Path

import httpx
from fastapi import APIRouter, HTTPException, Query
from ripster.i18n_msg import imsg

from ripster import compilations as _comps
from ripster import watchlist_suggest as _wls


def _engine_quality_ids(engine: str) -> set:
    """Какие качества умеет движок. Пустое множество = выяснить не удалось.

    Реестр движков наполняется автодискавери при старте приложения, поэтому в
    отрыве от него (тест, ранний импорт) он пуст — и проверка молча ничего не
    проверяла бы. Наполняем сами, если пусто.
    """
    try:
        from ripster.engines.registry import REGISTRY, get_engine
        if not REGISTRY:
            import pkgutil
            import ripster.engines as _pkg
            for _m in pkgutil.iter_modules(_pkg.__path__):
                if _m.name in ("base", "registry", "__init__", "streamrip_utils"):
                    continue
                try:
                    __import__(f"ripster.engines.{_m.name}")
                except Exception:
                    pass
        # get_engine отдаёт ГОТОВЫЙ объект движка, а не класс — вызывать его
        # как конструктор нельзя (TypeError уходил в except, и проверка молча
        # ничего не проверяла).
        return {x.get("id") for x in (get_engine(engine).qualities() or [])}
    except Exception:
        return set()


def _make_task(url: str, entry_quality: str, cfg: dict, source: str, idx: int = 0) -> dict:
    """Собрать задачу очереди из ссылки — с сервисом, движком и его качеством.

    Вишлист ставил задачи, указывая только ссылку и качество, а качество брал из
    ГЛОБАЛЬНОГО умолчания (оно эпловское). В итоге 29.07.2026 в очереди повисли
    четыре Deezer-альбома с качеством `alac-hires`, без `service` и без `engine`.
    Без них раннер подставляет apple+zhaarey по умолчанию, то есть Deezer-ссылка
    ушла бы в Apple-загрузчик, а метаданные не резолвились вовсе — карточки так и
    висели «Fetching metadata…».

    Сервис берём из самой ссылки, движок — из сервиса, качество — качество этого
    сервиса, если запрошенное ему не подходит.
    """
    from ripster.service_layer import (normalize_url, detect_service,
                                       engine_for_svc, default_quality)
    url = normalize_url(url)
    svc = detect_service(url)
    engine = engine_for_svc(svc)
    q = (entry_quality or "").strip()
    valid = _engine_quality_ids(engine)
    if valid and q not in valid:      # качество из другого сервиса — не годится
        q = ""
    if not q:
        q = default_quality(svc) or cfg.get("quality", "alac")
    return {
        "id":       f"wl_{int(datetime.now().timestamp()*1000)}" + (f"_{idx}" if idx else ""),
        "url":      url,
        "service":  svc,
        "engine":   engine,
        "quality":  q,
        "status":   "queued", "progress": 0, "log": [],
        "source":   source,
    }


def _enrich_soon(task: dict) -> None:
    """Дозаполнить карточку названием, артистом и обложкой — в фоне.

    Задачи вишлиста попадали в очередь напрямую, поэтому в интерфейсе висели
    «album · 1027914702 — Fetching metadata…» без обложки, пока не скачаются.
    """
    fn = _s.get("enrich_meta")
    if not fn:
        return
    try:
        asyncio.create_task(fn(task))
    except Exception:
        pass


router = APIRouter()
_s: dict = {}  # items, save, broadcast, config, queue, queue_snapshot, detect_service


def install(app, ctx) -> None:
    _s.update({
        "items":          ctx.watchlist,
        "save":           ctx.save_watchlist,
        "broadcast":      ctx.broadcast,
        "config":         ctx.config,
        "queue":          ctx.queue,
        "queue_snapshot": ctx.queue_snapshot,
        "detect_service": ctx.detect_service,
        "base_dir":       ctx.base_dir,
        # Без него карточки задач вишлиста показывали голый id и не имели
        # обложки: обогащение метаданными вызывается в маршруте добавления, а
        # вишлист кладёт задачу в очередь напрямую, мимо него.
        "enrich_meta":    getattr(ctx, "enrich_meta", None),
    })
    app.include_router(router)


# ── Dismissed suggestions ─────────────────────────────────────────────────────
# Kept in a sidecar file so a rejected suggestion stays rejected across restarts
# without polluting watchlist.json with non-watched entries.

def _dismiss_file() -> Path:
    return Path(_s.get("base_dir", ".")) / "watchlist_dismissed.json"


def _load_dismissed() -> set[str]:
    try:
        f = _dismiss_file()
        if f.exists():
            return set(json.loads(f.read_text(encoding="utf-8")) or [])
    except Exception as e:
        print(f"[watchlist] dismissed load failed: {e}", flush=True)
    return set()


def _save_dismissed(keys: set[str]) -> None:
    try:
        _dismiss_file().write_text(json.dumps(sorted(keys), ensure_ascii=False, indent=1),
                                   encoding="utf-8")
    except Exception as e:
        print(f"[watchlist] dismissed save failed: {e}", flush=True)


@router.get("/api/watchlist")
async def api_watchlist_get():
    return {"items": _s["items"]}


async def _resolve_target(name: str, url: str, service: str, artist_id: str) -> dict:
    """Fill in whatever the background checker needs to actually poll this entry.

    The Apple branch of _check_watchlist() only looks at entries that have an
    `artist_id`, so an entry added by name alone would sit there forever without
    ever being checked. Resolve it up front instead: from the URL when the user
    pasted an artist link, otherwise via the public iTunes Search API.
    """
    out = {"name": name, "url": url, "service": service, "artist_id": artist_id}

    if service == "soundcloud":
        hint = _wls.sc_permalink_from_url(url) or _wls.sc_permalink_from_url(name)
        r = await _wls.resolve_sc_channel(name or hint, hint)
        if r:
            out["url"] = r["url"]
        return out

    # Для ВСЕХ остальных сервисов artist_id тоже нужен: следим мы всегда через
    # каталог Apple (единственный бесплатный полный источник по артисту), а
    # `service` говорит лишь КУДА качать — ровно как у подписки на лейбл.
    # Раньше здесь стоял ранний выход для service != "apple", и запись вида
    # «Praana / deezer» уходила в список с пустым artist_id: цикл проверки её
    # не видел, last_check навсегда оставался null, а add-ответ рапортовал
    # resolved=true. Тихая слепота, найдена 31.07.2026.
    if not out["artist_id"]:
        out["artist_id"] = _wls.apple_id_from_url(url)
    if not out["artist_id"] and name:
        r = await _wls.resolve_apple_artist(name)
        if r:
            out["artist_id"] = r["artist_id"]
            out["url"] = out["url"] or r.get("url", "")
            out["name"] = out["name"] or r.get("name", name)
    return out


@router.post("/api/watchlist")
async def api_watchlist_add(body: dict):
    name      = body.get("name", "").strip()
    url       = body.get("url", "").strip()
    service   = body.get("service", _s["detect_service"](url)) or "apple"
    artist_id = body.get("artist_id", "")
    kind      = (body.get("kind") or "artist").strip()
    if not name and not url:
        raise HTTPException(400, "name or url required")

    # ── Label subscription ───────────────────────────────────────────────────
    # A label has no artist id and no channel to resolve — the name IS the key.
    # Verify up front that the label actually returns releases, so a typo can't
    # create an entry that silently never fires.
    if kind == "label":
        if not name:
            raise HTTPException(400, imsg("err.label_name_required", "название лейбла обязательно"))
        # Со страницы релиза в поле label часто приходит строка копирайта
        # («℗ 2024 Erased Tapes Records Ltd.»), а не имя. Приводим к имени до
        # проверки И перед сохранением — иначе каждый обход снова ищет мусор.
        from ripster.routes import discovery as _disc
        name = _disc._clean_label(name) or name
        rels, lbl_info = await _label_releases_ex(name, 10)
        entry = {
            "id":           f"wl_{int(datetime.now().timestamp()*1000)}",
            "name":         name,
            "kind":         "label",
            "url":          url,
            "service":      service,          # где качать, не где следить
            "artist_id":    "",
            "quality":      body.get("quality", _s["config"].get("quality", "alac")),
            "added":        datetime.now().isoformat(timespec="seconds"),
            "last_check":   datetime.now().isoformat(timespec="seconds") if rels else None,
            # Baseline immediately: subscribing must not dump the back catalogue
            # into the queue on the first check.
            "last_release":      (rels[0].get("title", "") if rels else None),
            "last_release_date": (str(rels[0].get("date") or rels[0].get("year") or "")
                                  if rels else ""),
            "auto_download": body.get("auto_download", False),
        }
        _s["items"].append(entry)
        _s["save"](_s["items"])
        # Предупреждение говорит то, что ИЗМЕРЕНО, и на языке интерфейса.
        #
        # Было: «Лейбл «X» не найден в каталогах Spotify/Deezer — проверь
        # написание». Три неправды в одной строке. Deezer не спрашивали вовсе
        # (`_LABEL_NATIVE` — только Spotify). Лейбл, случалось, был НАЙДЕН, и
        # выдача пустела на шаге сверки. А совет указывал на опечатку человека,
        # хотя отказывала наша проверка. Плюс строка была русской в коде, то есть
        # английский интерфейс всё равно получал русский текст.
        warn_key, warn_args, warn_ru = "", {}, ""
        if not rels:
            if lbl_info.get("verify_failed"):
                n = int(lbl_info.get("candidates") or 0)
                warn_key, warn_args = "wl.label_unverified", {"name": name, "n": n}
                warn_ru = (f"Лейбл «{name}» найден ({n} релизов), но Spotify сейчас "
                           f"не даёт подтвердить лейбл — подписка заведена, проверка "
                           f"повторится при следующем обходе")
            else:
                warn_key, warn_args = "wl.label_not_found", {"name": name}
                warn_ru = (f"Лейбл «{name}» не найден в каталоге Spotify — "
                           f"проверь написание, иначе отслеживать нечего")
        return {"ok": True, "item": entry, "resolved": bool(rels),
                "found": len(rels),
                "verify_failed": bool(lbl_info.get("verify_failed")),
                "warning_key": warn_key, "warning_args": warn_args,
                "warning": warn_ru}

    resolved = await _resolve_target(name, url, service, artist_id)

    entry = {
        "id":           f"wl_{int(datetime.now().timestamp()*1000)}",
        "name":         resolved["name"] or name,
        "kind":         "artist",
        "url":          resolved["url"],
        "service":      service,
        "artist_id":    resolved["artist_id"],
        "quality":      body.get("quality", _s["config"].get("quality", "alac")),
        "added":        datetime.now().isoformat(timespec="seconds"),
        "last_check":   None,
        "last_release": None,
        "auto_download": body.get("auto_download", True),
    }
    _s["items"].append(entry)
    _s["save"](_s["items"])
    # `resolved` is reported so the UI can warn when an entry went in unpollable
    # (no Apple artist id / unresolvable SC channel) instead of failing silently.
    # SoundCloud резолвится в url, у остальных признак пригодности — artist_id.
    return {"ok": True, "item": entry,
            "resolved": bool(entry["url"] if service == "soundcloud"
                             else entry["artist_id"])}


# ── Smart suggestions (mined from the local stats DB) ────────────────────────

@router.get("/api/watchlist/suggestions")
async def api_watchlist_suggestions(limit: int = Query(12, ge=1, le=50)):
    dismissed = _load_dismissed()
    # 60ms of pure SQLite + string work — off the event loop anyway.
    res = await asyncio.to_thread(_wls.compute, _s["items"], limit + len(dismissed))
    if res.get("suggestions"):
        res["suggestions"] = [s for s in res["suggestions"]
                              if s["key"] not in dismissed][:limit]
    res["dismissed"] = len(dismissed)
    return res


@router.post("/api/watchlist/auto")
async def wl_set_auto(body: dict):
    """Включить или выключить автоскачивание — одной записи или сразу группе.

    Зачем маршрут вообще понадобился: галочка на карточке НИКОГДА ничего не
    меняла. `wlToggleAuto` во фронте показывал всплывающее сообщение и выходил
    («Update via re-add (simple)»), сервер об этом не узнавал. Поэтому у
    владельца накопились 187 записей из 257 с включённым автоскачиванием,
    выключить которые было физически нечем.

    `scope`: `all` — все записи, `artist` / `label` — только свой вид,
    пусто — одна запись по `id`. Вид берём из поля `kind`, а у старых записей
    его нет вовсе (7 штук у владельца): такие считаем артистами, потому что
    отслеживание по лейблу появилось позже и всегда пишет `kind`.
    """
    items = _s["items"]
    value = bool(body.get("auto_download"))
    scope = str(body.get("scope") or "").strip().lower()
    item_id = str(body.get("id") or "")

    def kind_of(e: dict) -> str:
        return str(e.get("kind") or "artist").lower()

    if scope in ("all", "artist", "label"):
        target = [e for e in items
                  if scope == "all" or kind_of(e) == scope]
    elif item_id:
        target = [e for e in items if str(e.get("id")) == item_id]
        if not target:
            raise HTTPException(status_code=404, detail="Запись не найдена")
    else:
        raise HTTPException(status_code=400,
                            detail="Нужен scope (all/artist/label) или id записи")

    changed = 0
    for e in target:
        if bool(e.get("auto_download")) != value:
            e["auto_download"] = value
            changed += 1
    if changed:
        _s["save"](items)
    return {"ok": True, "changed": changed, "matched": len(target),
            "auto_download": value}


@router.post("/api/watchlist/suggestions/dismiss")
async def api_watchlist_suggestion_dismiss(body: dict):
    key = (body.get("key") or "").strip()
    if not key:
        raise HTTPException(400, "key required")
    keys = _load_dismissed()
    keys.add(key)
    _save_dismissed(keys)
    return {"ok": True, "dismissed": len(keys)}


@router.post("/api/watchlist/suggestions/reset")
async def api_watchlist_suggestions_reset():
    _save_dismissed(set())
    return {"ok": True}


@router.post("/api/watchlist/suggestions/accept")
async def api_watchlist_suggestion_accept(body: dict):
    """Turn a suggestion into a real, pollable watchlist entry."""
    name    = (body.get("name") or "").strip()
    service = (body.get("service") or "apple").strip()
    if not name:
        raise HTTPException(400, "name required")

    resolved = await _resolve_target(
        name,
        body.get("url", "") or (f"https://soundcloud.com/{body['sc_permalink']}"
                                if body.get("sc_permalink") else ""),
        service,
        body.get("apple_id", ""),
    )

    # An SC-classified name that turns out to have no channel is often still a
    # real Apple artist (played as a mix channel, released on Apple) — fall back
    # rather than refusing a suggestion that is perfectly watchable elsewhere.
    if service == "soundcloud" and not resolved["url"]:
        alt = await _resolve_target(name, "", "apple", "")
        if alt["artist_id"]:
            service, resolved = "apple", alt

    # Refusing here beats adding an entry the checker would silently skip.
    if service == "apple" and not resolved["artist_id"]:
        return {"ok": False, "error": "apple_artist_not_found", "name": name}
    if service == "soundcloud" and not resolved["url"]:
        return {"ok": False, "error": "sc_channel_not_found", "name": name}

    entry = {
        "id":           f"wl_{int(datetime.now().timestamp()*1000)}",
        "name":         resolved["name"] or name,
        "kind":         "artist",
        "url":          resolved["url"],
        "service":      service,
        "artist_id":    resolved["artist_id"],
        "quality":      body.get("quality", _s["config"].get("quality", "alac")),
        "added":        datetime.now().isoformat(timespec="seconds"),
        "last_check":   None,
        "last_release": None,
        "auto_download": body.get("auto_download", True),
        "from_suggestion": body.get("key", ""),
    }
    _s["items"].append(entry)
    _s["save"](_s["items"])
    return {"ok": True, "item": entry}


@router.post("/api/watchlist/{item_id}/download-latest")
async def api_watchlist_download_latest(item_id: str, body: dict | None = None):
    """Queue the label's most recent release right now.

    Subscribing records a baseline (otherwise the whole back catalogue lands in
    the queue), so the checker legitimately waits for the NEXT release — which
    reads as "it says new release but downloads nothing". This is the explicit
    "I want the current one" action."""
    entry = next((e for e in _s["items"] if e.get("id") == item_id), None)
    if not entry:
        raise HTTPException(404, imsg("err.entry_not_found", "запись не найдена"))
    if entry.get("kind") != "label":
        raise HTTPException(400, imsg("err.labels_only", "только для лейблов"))

    how_many = int((body or {}).get("count") or 1)
    how_many = max(1, min(how_many, 5))
    rels = await _label_releases(entry["name"], 20)
    if not rels:
        return {"ok": False, "error_key": "err.label_no_releases", "error_args": {"name": entry["name"]}, "error": f"У лейбла «{entry['name']}» не нашлось релизов"}

    from ripster.routes import discovery as _disc
    cfg, queue, snapshot = _s["config"], _s["queue"], _s["queue_snapshot"]
    svc = entry.get("service", "apple")
    queued, skipped = [], []
    for rel in rels[:how_many]:
        url = rel.get("url", "")
        if (rel.get("service") or "") != svc:      # источник ≠ цель → переводим
            try:
                m = await _disc._match_seeds_in_service([rel], svc, entry["name"], 1,
                                                        cfg.get("storefront", "us"))
                url = (m[0].get("url") if m else "") or ""
            except Exception:
                url = ""
        if not url:
            skipped.append(rel.get("title", "?"))
            continue
        _t = _make_task(url, entry.get("quality", ""), cfg,
                        "watchlist-label", idx=len(queued) or 1)
        queue.append(_t)
        _enrich_soon(_t)
        queued.append(rel.get("title", "?"))
    if queued:
        await _s["broadcast"]({"type": "queue_update", "queue": snapshot()})
    return {"ok": bool(queued), "queued": queued, "skipped": skipped,
            "label": entry["name"],
            # error_key ставим только когда есть о чём сообщать: пустой ключ при
            # успехе заставил бы клиент показать пустую строку как ошибку.
            **({} if queued else {"error_key": "err.releases_not_found_in",
                                  "error_args": {"svc": svc}}),
            "error": ("" if queued else f"Не нашёл этих релизов в {svc}")}


# Разделители склейки соавторов в строке кредитов. Применяются ТОЛЬКО после того,
# как имя целиком не нашлось в каталоге, — иначе «Above & Beyond» распалось бы на
# двух несуществующих артистов. «x» намеренно НЕ разделитель: «Malcolm X» дороже,
# чем пойманная пара DJ-коллабораций.
_CREDIT_SEP = re.compile(r"\s*(?:,|&|×|\+|\bfeat\.?|\bft\.?|\bvs\.?)\s*", re.I)


@router.post("/api/watchlist/repair")
async def api_watchlist_repair():
    """Backfill missing artist_ids and drop duplicates.

    Entries added before the add path resolved ids (and the UI's own duplicate
    adds, which reused the same generated id) are dead weight: the checker skips
    anything without an artist_id, so those artists were never actually watched.
    """
    items = _s["items"]
    seen: set[tuple] = set()
    unique: list[dict] = []
    dropped = 0
    for it in items:
        # `kind` is part of the signature: a LABEL called "Mesh" and an ARTIST
        # called "Mesh" are different subscriptions and must not collapse.
        sig = (it.get("kind", "artist"), _wls._norm(it.get("name", "")),
               it.get("service", ""), it.get("url", ""))
        if sig in seen:
            dropped += 1
            continue
        seen.add(sig)
        unique.append(it)

    fixed = 0
    split_out: list[dict] = []   # записи, отпочкованные от склейки соавторов
    for it in unique:
        # Labels have no artist_id by design — never try to "repair" them into
        # an artist. Doing so rewrote the label's name/url to a same-named
        # artist and then the artist_id de-dup below deleted it as a duplicate,
        # which is exactly how label subscriptions vanished after a check.
        if it.get("kind") == "label":
            continue
        # SoundCloud опрашивается по permalink-у, а не по artist_id.
        if it.get("service") == "soundcloud" or it.get("artist_id"):
            continue
        aid = _wls.apple_id_from_url(it.get("url", ""))
        if not aid and it.get("name"):
            r = await _wls.resolve_apple_artist(it["name"])
            aid = r.get("artist_id", "")
            if aid and not it.get("url"):
                it["url"] = r.get("url", "")
            if aid and r.get("name"):
                it["name"] = r["name"]
        # Склейка соавторов в имени. Кнопка ♥ на карточке релиза передаёт ту
        # строку кредитов, что нарисована на карточке, а у совместного релиза
        # это «A, B, C» — как артиста iTunes такое не находит НИКОГДА, и запись
        # ложилась в список мёртвой навсегда (02.08.2026: «A Vision of Panorama,
        # Meora, Café del Mar», «Mystific, Talk Jungle»). Разбираем на людей:
        # первый живой становится этой записью, остальные — отдельными.
        # 07.08.2026: разделителем была ТОЛЬКО запятая, поэтому «Golan Zocher &
        # Moshic» (добавлен 04.08) чинился нулём при каждой проверке и вечно
        # висел предупреждением. Разбор безопасен именно как ФОЛБЭК: имя целиком
        # уже пробовали выше, так что «Above & Beyond» сюда не доходит, а часть,
        # которую iTunes не знает, отсеивается ниже по пустому pid.
        if not aid and _CREDIT_SEP.search(it.get("name") or ""):
            for part in [p.strip() for p in _CREDIT_SEP.split(it["name"]) if p.strip()]:
                r = await _wls.resolve_apple_artist(part)
                pid = r.get("artist_id", "")
                if not pid:
                    continue
                if not aid:
                    aid = pid
                    it["name"] = r.get("name") or part
                    it["url"] = r.get("url", "") or it.get("url", "")
                else:
                    split_out.append({**it,
                                      "id": f"wl_{int(datetime.now().timestamp()*1000)}_{pid}",
                                      "name": r.get("name") or part,
                                      "url": r.get("url", ""),
                                      "artist_id": pid,
                                      "last_check": None,
                                      "last_release": None})
        if aid:
            it["artist_id"] = aid
            fixed += 1

    # De-dup by resolved artist_id too — "max cooper" and a pasted Max Cooper
    # link are the same target once both have an id.
    # Ключ включает СЕРВИС: один и тот же артист, подписанный на Apple и на
    # Deezer, — это две разные подписки (разные источники скачивания), и после
    # того как artist_id стал появляться у всех сервисов, общий ключ молча
    # удалял бы одну из них.
    # Отпочкованные проходят тот же de-dup: соавтор вполне может быть уже подписан.
    unique.extend(split_out)
    by_id: set[tuple] = set()
    final: list[dict] = []
    for it in unique:
        if it.get("kind") == "label":       # labels never de-dup by artist_id
            final.append(it)
            continue
        aid = (it.get("service", "apple"), it.get("artist_id", ""))
        if aid[1] and aid in by_id:
            dropped += 1
            continue
        if aid[1]:
            by_id.add(aid)
        final.append(it)

    items[:] = final
    _s["save"](items)
    return {"ok": True, "fixed": fixed, "dropped": dropped, "total": len(items),
            "split": len(split_out)}


@router.delete("/api/watchlist/{item_id}")
async def api_watchlist_delete(item_id: str):
    items = _s["items"]
    items[:] = [x for x in items if x.get("id") != item_id]
    _s["save"](items)
    return {"ok": True}


@router.post("/api/watchlist/check")
async def api_watchlist_check():
    asyncio.create_task(_check_watchlist())
    return {"ok": True, "msg": "Checking in background…"}


@router.get("/api/label/{name}")
async def api_label_detail(name: str, limit: int = Query(60, ge=1, le=200)):
    """Карточка лейбла: то же, что страница артиста, только ключ — имя.

    Форма ответа намеренно повторяет /api/artist/{service}/{id} ({artist|label,
    releases}), чтобы интерфейс рисовал дискографию лейбла ТЕМ ЖЕ рендером
    карточек, что и дискографию артиста, а не заводил второй.

    Источник один и тот же — `_label_releases`: Spotify и Deezer единственные
    отвечают на вопрос «что выпустил лейбл», и разводить второй источник ради
    страницы значило бы получить два разных ответа на один вопрос.
    """
    name = (name or "").strip()
    if not name:
        raise HTTPException(400, imsg("err.label_name_required", "название лейбла обязательно"))
    rels, lbl_info = await _label_releases_ex(name, limit)
    releases = [{
        "id":      str(r.get("id") or r.get("url") or ""),
        "title":   r.get("title", ""),
        "artist":  r.get("artist", ""),
        "type":    (r.get("type") or "album"),
        "date":    r.get("date", ""),
        "year":    str(r.get("year") or str(r.get("date") or "")[:4]),
        "cover":   r.get("cover", ""),
        "url":     r.get("url", ""),
        "tracks":  r.get("tracks"),
        "service": r.get("service") or "spotify",
        "label":   name,
    } for r in rels]
    watched = any(e.get("kind") == "label"
                  and (e.get("name") or "").strip().lower() == name.lower()
                  for e in (_s.get("items") or []))
    # Пустая страница лейбла обязана сказать, ПОЧЕМУ она пустая: «такого лейбла
    # нет» и «сверка сейчас не работает» выглядят одинаково, а значат разное.
    return {"ok": True,
            "label": {"name": name, "watched": watched,
                      "releases_total": len(releases),
                      "verify_failed": bool(lbl_info.get("verify_failed")),
                      "candidates": int(lbl_info.get("candidates") or 0),
                      "service": (releases[0]["service"] if releases else "spotify")},
            "releases": releases}


async def _apple_artist_collections(client, artist_id: str, storefront: str,
                                    with_comps: bool = True) -> list:
    """Every release an Apple artist is on — their own albums AND the
    compilations they merely have a track on.

    Two lookups, because one endpoint cannot answer both questions:

      entity=album  the artist's own releases (they are the album artist)
      entity=song   their tracks — and a track always carries its parent
                    `collection*` fields, which is the ONLY way a various-artists
                    compilation shows up. `entity=album` never returns one, so
                    without this half a Hospital/Forza-style label compilation is
                    invisible while its individual tracks are visible.

    See ripster/compilations.py — same blind spot as the Spotify radar.
    """
    out: dict[str, dict] = {}

    async def _lookup(params: dict) -> list:
        try:
            r = await client.get("https://itunes.apple.com/lookup", params=params)
            if r.status_code != 200:
                return []
            return (r.json() or {}).get("results") or []
        except Exception as e:
            print(f"[watchlist] apple lookup {artist_id} ({params.get('entity')}): {e}",
                  flush=True)
            return []

    for x in await _lookup({"id": artist_id, "entity": "album", "limit": 25,
                            "sort": "recent", "country": storefront}):
        if x.get("wrapperType") == "collection" and x.get("collectionId"):
            out[str(x["collectionId"])] = {
                "url":   x.get("collectionViewUrl", ""),
                "name":  x.get("collectionName", ""),
                "date":  (x.get("releaseDate", "") or "")[:10],
                "artist": x.get("artistName", ""),
                # 100px is what the API hands out; ask for a usable card size.
                "cover": (x.get("artworkUrl100", "") or "").replace("100x100", "400x400"),
                "compilation": _comps.is_compilation(
                    album_artist=x.get("artistName", ""), title=x.get("collectionName", "")),
            }

    if with_comps:
        for x in await _lookup({"id": artist_id, "entity": "song", "limit": 200,
                                "sort": "recent", "country": storefront}):
            cid = x.get("collectionId")
            if x.get("wrapperType") != "track" or not cid or str(cid) in out:
                continue
            alb_artist = x.get("collectionArtistName") or x.get("artistName", "")
            out[str(cid)] = {
                "url":   x.get("collectionViewUrl", ""),
                "name":  x.get("collectionName", ""),
                # A track's releaseDate is the collection's release date here.
                "date":  (x.get("releaseDate", "") or "")[:10],
                "artist": alb_artist,
                "cover": (x.get("artworkUrl100", "") or "").replace("100x100", "400x400"),
                "compilation": _comps.is_compilation(
                    album_artist=alb_artist, title=x.get("collectionName", ""),
                    track_artist=x.get("artistName", "")),
            }
    return list(out.values())


async def _apple_latest_album(client, artist_id: str, storefront: str = "us",
                              with_comps: bool = True) -> dict:
    """Newest already-released release for an Apple artist, compilations included.

    Replaces the old `itunes.apple.com/rss/artistnewreleases/id=…` feed, which
    Apple retired — it now answers 400 "Invalid RSS channel name" for every id,
    so the Apple half of the watchlist silently checked nothing. The public
    lookup API still serves this and needs no token.
    """
    rels = await _apple_artist_collections(client, artist_id, storefront, with_comps)
    if not rels:
        return {}
    # Pre-orders carry a future releaseDate — keep them out, they cannot be
    # downloaded yet. Fall back to the newest overall if everything is upcoming.
    today = datetime.now().strftime("%Y-%m-%d")
    rels.sort(key=lambda x: x.get("date", ""), reverse=True)
    released = [x for x in rels if x.get("date", "") <= today]
    return (released or rels)[0]


# ── New-Zealand release window ────────────────────────────────────────────────
# New music drops on Friday 00:00 LOCAL time in each territory, and New Zealand
# is among the first places on earth to get there — a release is out in Auckland
# roughly half a day before it is here. The checker used to run on a bare 6h
# grid, so how early you saw a Friday release depended on where that grid
# happened to land. Aligning one check to the Auckland Friday means the moment
# a release exists anywhere, we look.
#
# Computed by hand rather than via zoneinfo: on Windows zoneinfo needs the
# `tzdata` package, and adding a dependency to this interpreter is exactly what
# ripster-dependency-versions says not to do casually. NZ's rule is simple and
# stable: UTC+13 from the last Sunday of September to the first Sunday of April,
# UTC+12 otherwise.

def _last_sunday(year: int, month: int) -> "datetime":
    from calendar import monthrange
    d = datetime(year, month, monthrange(year, month)[1])
    return d - timedelta(days=(d.weekday() + 1) % 7)


def _first_sunday(year: int, month: int) -> "datetime":
    d = datetime(year, month, 1)
    return d + timedelta(days=(6 - d.weekday()) % 7)


def _nz_offset(utc_dt: "datetime") -> int:
    """Hours NZ is ahead of UTC at this instant (12 or 13)."""
    y = utc_dt.year
    dst_start = _last_sunday(y, 9).replace(hour=2)    # NZDT begins
    dst_end   = _first_sunday(y, 4).replace(hour=3)   # NZDT ends
    # Southern hemisphere: DST spans the new year, so it is the GAP that is
    # standard time, not the span.
    return 12 if (dst_end <= utc_dt < dst_start) else 13


def nz_now(utc_dt: "datetime | None" = None) -> "datetime":
    utc_dt = utc_dt or datetime.utcnow()
    return utc_dt + timedelta(hours=_nz_offset(utc_dt))


def seconds_to_nz_friday(utc_dt: "datetime | None" = None,
                         grace_min: int = 5) -> float:
    """Seconds until the next Friday 00:05 in Auckland."""
    utc_dt = utc_dt or datetime.utcnow()
    nz = nz_now(utc_dt)
    target = (nz.replace(hour=0, minute=grace_min, second=0, microsecond=0)
              + timedelta(days=(4 - nz.weekday()) % 7))
    if target <= nz:
        target += timedelta(days=7)
    return (target - nz).total_seconds()


def next_check_delay(cfg: dict | None = None, base: float = 6 * 3600) -> float:
    """How long the background loop should sleep before the next check.

    Normally the plain interval; but if the Auckland Friday lands sooner, wake
    then instead so a Friday release is seen at the first minute it exists.
    """
    cfg = cfg if cfg is not None else (_s.get("config") or {})
    if cfg.get("watchlist-nz-window", True) is False:
        return base
    return max(60.0, min(base, seconds_to_nz_friday()))


def seconds_since_last_pass(items: "list | None" = None) -> "float | None":
    """Seconds since the most recent entry check, or None if nothing was ever
    checked. `last_check` on the entries IS our persisted clock — we keep no
    separate timer file."""
    items = items if items is not None else (_s.get("items") or [])
    newest = None
    for e in items:
        raw = (e or {}).get("last_check")
        if not raw:
            continue
        try:
            t = datetime.fromisoformat(str(raw))
        except ValueError:
            continue
        if newest is None or t > newest:
            newest = t
    if newest is None:
        return None
    # last_check is written as naive local time; compare on the same clock.
    return max(0.0, (datetime.now() - newest).total_seconds())


def initial_check_delay(cfg: dict | None = None, base: float = 6 * 3600,
                        grace: float = 120.0) -> float:
    """How long to sleep before the FIRST check after a start-up.

    Found 2026-08-09: the loop slept the full interval before its first pass, so
    every restart put the 6h clock back to zero. On a day of active development
    the app restarts every ~10-15 min and the watchlist was therefore checked
    almost never — 146 entries carried one single pass from 25h earlier, and the
    only reason that one happened was a rare quiet gap. Nothing looked broken:
    the loop was alive and dutifully sleeping, just never reaching the end.

    So resume the clock instead of resetting it — the interval is measured from
    the last real pass (`last_check`), not from process start. `grace` keeps a
    due check from firing into the middle of boot.
    """
    interval = next_check_delay(cfg, base)
    since    = seconds_since_last_pass()
    if since is None:
        return grace                      # fresh list — check shortly after boot
    return max(grace, min(interval, interval - since))


def _notify_release(artist: str, release: str, compilation: bool, queued: bool,
                    rel: dict | None = None) -> None:
    """Native desktop toast for a watchlist hit. Best-effort and never fatal —
    a notification failing must not abort the check that found the release.

    `rel` — сама запись релиза: из неё берём обложку, год, лейбл и сервис. Без
    них уведомление было голой строкой «артист — название», по которой не
    понять ни что это за релиз, ни откуда его брать.
    """
    cfg = _s.get("config") or {}
    if cfg.get("notify-on-release", True) is False:
        return
    r = rel or {}
    try:
        from ripster import notify as _notify
        _notify.toast_new_release(
            artist, release, compilation=compilation, queued=queued,
            lang=cfg.get("language", "en"),
            cover=str(r.get("cover") or r.get("artwork") or r.get("artworkUrl") or ""),
            year=str(r.get("date") or r.get("year") or ""),
            label=str(r.get("label") or ""),
            service=str(r.get("service") or ""))
    except Exception as e:
        print(f"[watchlist] toast failed: {e}", flush=True)


def _sc_permalink(entry: dict) -> str:
    """Extract a SoundCloud channel permalink from the entry's url/name field —
    accepts either a bare handle or a full soundcloud.com/<handle> link."""
    raw = (entry.get("url") or entry.get("name") or "").strip().strip("/")
    if "soundcloud.com/" in raw:
        raw = raw.split("soundcloud.com/", 1)[1]
    return raw.split("/", 1)[0].split("?", 1)[0]


async def _check_soundcloud_targets(items: list, broadcast, save, cfg, queue, snapshot) -> int:
    """SC channels: newest upload via the same lookup the SC search tab uses
    (sc_user_tracks) — SC has no RSS feed like Apple, so this hits the API
    directly instead."""
    from ripster.routes.soundcloud import sc_user_tracks

    targets = [e for e in items if e.get("service") == "soundcloud"]
    new_found = 0
    for entry in targets:
        permalink = _sc_permalink(entry)
        if not permalink:
            continue
        try:
            r = await sc_user_tracks(permalink=permalink, limit=1)
        except Exception as e:
            print(f"[watchlist] sc:{permalink}: {e}", flush=True)
            continue
        if not r.get("ok") or not r.get("results"):
            continue
        latest = r["results"][0]
        track_id = latest.get("id")
        track_url = latest.get("url", "")
        prev = entry.get("last_release")
        entry["last_check"] = datetime.now().isoformat(timespec="seconds")
        if track_id and str(track_id) != str(prev) and track_url:
            entry["last_release"] = str(track_id)
            # Same baseline rule as the Apple branch: the first check of a newly
            # added channel records where we are, it is not a "new release".
            if prev is None:
                save(items)
                continue
            new_found += 1
            save(items)
            await broadcast({"type": "watchlist_new_release",
                             "artist": entry.get("name") or permalink,
                             "release": latest.get("title", ""),
                             "url": track_url})
            _notify_release(entry.get("name") or permalink,
                            latest.get("title", ""), False,
                            bool(entry.get("auto_download")), latest)
            if entry.get("auto_download"):
                task = _make_task(track_url, entry.get("quality", ""), cfg,
                                  "watchlist")
                _enrich_soon(task)
                queue.append(task)
                await broadcast({"type": "queue_update", "queue": snapshot()})
    return new_found


async def _label_releases(label: str, limit: int = 20) -> list[dict]:
    """Releases of a label, newest first.

    Reuses the label search built for /api/search: Spotify and Deezer are the
    only services that can answer "what did this label put out", so they are the
    monitoring source regardless of where the user wants the files from.
    Sorted by release date because those APIs return by relevance, and a
    relevance-ordered list would make an old album look like a new release."""
    rels, _ = await _label_releases_ex(label, limit)
    return rels


async def _label_releases_ex(label: str, limit: int = 20) -> tuple[list[dict], dict]:
    """`_label_releases` + ПРИЧИНА, по которой список получился таким.

    Пустой список раньше означал ровно одно сообщение человеку: «лейбл не найден,
    проверь написание». Но пустота бывает двух совершенно разных сортов, и
    23.08.2026 сработала вторая: у «Balance Music» поиск Spotify отдал десять
    релизов лейбла, а подтвердить их не вышло — `/v1/albums?ids=` отвечает 403,
    `/v1/albums/{id}` отвечает 429. Существующий лейбл был объявлен опечаткой, и
    совет «проверь написание» указывал на человека, тогда как сломана была наша
    проверка. `info` даёт вызывающему различить эти случаи."""
    from ripster.routes import discovery as _disc
    # «℗ 2024 Erased Tapes Records Ltd.» и подобные строки копирайта прилетают
    # сюда как имя лейбла (со страницы релиза, из радара, из вотчлиста) — чистим,
    # иначе поиск `label:"…"` не находит ничего и лейбл выглядит опечаткой.
    label = _disc._clean_label(label) or label
    try:
        seeds, info = await _disc._label_seeds_ex(label, limit)
    except Exception as e:
        print(f"[watchlist] label «{label}» lookup failed: {e}", flush=True)
        return [], {"candidates": 0, "verify_failed": False, "error": str(e)}
    seeds = [s for s in seeds if (s.get("date") or s.get("year"))]
    seeds.sort(key=lambda s: str(s.get("date") or s.get("year") or ""), reverse=True)
    return seeds, info


async def _check_label_targets(items, broadcast, save, cfg, queue, snapshot) -> int:
    """Poll label subscriptions.

    Honest monitoring: we compare RELEASE DATES, not list position. A label puts
    out several records a month, so "the newest one changed" is not enough —
    everything dated after the last seen date counts as new, and the first check
    only records a baseline instead of queueing the whole back catalogue."""
    labels = [e for e in items if e.get("kind") == "label" and e.get("name")]
    if not labels:
        return 0
    from ripster.routes import discovery as _disc
    found = 0
    for entry in labels:
        name = entry["name"]
        await broadcast({"type": "watchlist_check_progress", "artist": f"🏷 {name}"})
        try:
            rels = await _label_releases(name, 20)
            entry["last_check"] = datetime.now().isoformat(timespec="seconds")
            if not rels:
                continue
            seen_date = entry.get("last_release_date") or ""
            newest = str(rels[0].get("date") or rels[0].get("year") or "")
            if not seen_date:
                entry["last_release_date"] = newest        # baseline only
                entry["last_release"] = rels[0].get("title", "")
                save(items)
                continue
            fresh = [r for r in rels
                     if str(r.get("date") or r.get("year") or "") > seen_date]
            if not fresh:
                continue
            entry["last_release_date"] = newest
            entry["last_release"] = fresh[0].get("title", "")
            found += len(fresh)
            save(items)

            svc = entry.get("service", "apple")
            for rel in fresh[:5]:            # guard against a catalogue dump
                title = rel.get("title", "")
                await broadcast({"type": "watchlist_new_release",
                                 "artist": f"{rel.get('artist','')} · {name}",
                                 "release": title, "label": name,
                                 "url": rel.get("url", "")})
                _notify_release(f"{name} · {rel.get('artist','')}", title,
                                False, bool(entry.get("auto_download")), rel)
                if not entry.get("auto_download"):
                    continue
                # The monitoring source is not necessarily the download target,
                # so translate whenever they differ. Testing `svc not in
                # (spotify, deezer)` was wrong: a label watched with
                # service="deezer" still gets its releases from SPOTIFY (the
                # first catalogue queried), and that branch queued the raw
                # Spotify link — which Ripster then tried to fetch as an Apple
                # ALAC album and hung on "Fetching metadata…".
                # Куда стрелять — решает не подписка, а реальная доступность.
                # Раньше мы упирались в ОДИН сервис: не нашли там — пропустили
                # релиз совсем, хотя он мог лежать в трёх других. А витрины
                # наполняются вразнобой, и аккаунты у нас в разных странах.
                url = await _pick_download_url(rel, svc, name, cfg, title)
                if not url:
                    continue
                _t = _make_task(url, entry.get("quality", ""), cfg,
                                "watchlist-label")
                queue.append(_t)
                _enrich_soon(_t)
                await broadcast({"type": "queue_update", "queue": snapshot()})
        except Exception as e:
            print(f"[watchlist] label {name}: {e}", flush=True)
    save(items)
    return found



async def _pick_download_url(rel: dict, want_svc: str, label: str, cfg: dict,
                             title: str) -> str:
    """Ссылка на релиз в том сервисе, где его РЕАЛЬНО можно взять сейчас.

    Предпочтение — сначала сервис подписки, дальше порядок владельца по
    качеству. Если нигде нет, это не ошибка: витрина просто ещё не наполнилась,
    и писать такое в лог ошибкой значит приучить владельца не читать логи.
    """
    from ripster.routes import discovery as _disc
    from ripster import availability as _av

    # «auto» с фронта = нет жёсткого предпочтения: берём порядок качества владельца
    # и матрица сама выберет первый ДОСТУПНЫЙ (где релиз уже отдаётся раньше всех).
    _avail = cfg.get("availability-preference") or ["apple", "qobuz", "deezer", "tidal", "beatport"]
    if want_svc in ("", "auto", None):
        want_svc = _avail[0] if _avail else "apple"

    # Уже в нужном сервисе — ничего выяснять не надо.
    if (rel.get("service") or "") == want_svc and rel.get("url"):
        return rel.get("url", "")

    upc = ""
    try:
        upc = await _disc._seed_upc(rel)
    except Exception:
        upc = ""

    if upc:
        pref = [want_svc] + [x for x in (cfg.get("availability-preference")
                                         or ["apple", "qobuz", "deezer", "tidal", "beatport"])
                             if x != want_svc]
        m = await _av.matrix(upc=upc, title=title, artist=rel.get("artist", ""))
        best = _av.pick_source(m["services"], pref)
        if best:
            u = (m["services"][best] or {}).get("url", "")
            if u:
                if best != want_svc:
                    print(f"[watchlist] '{title}': not in {want_svc} yet, taking from {best}",
                          flush=True)
                return u
        print(f"[watchlist] '{title}' not available anywhere yet "
              f"({_av.summary_ru(m['services'])}) - will check later", flush=True)
        return ""

    # Штрихкода нет — старый путь: поиск в целевом сервисе по названию.
    try:
        mm = await _disc._match_seeds_in_service([rel], want_svc, label, 1,
                                                 cfg.get("storefront", "us"))
        u = (mm[0].get("url") if mm else "") or ""
    except Exception:
        u = ""
    if not u:
        print(f"[watchlist] '{title}' not found in {want_svc} (no barcode) - skipping",
              flush=True)
    return u


# ── Ранние витрины ────────────────────────────────────────────────────────────
# Наблюдение шло ТОЛЬКО через каталог Apple. Пока релиз не появлялся там, вишлист
# о нём не знал вовсе — даже если файл уже полсуток отдавался в другой витрине.
#
# Разбор 29.07.2026 (Sultan + Shepard — «Centuries»): релиз лежал в Tidal NZ с
# датой 30.07 и `streamReady=True`, в Apple его не было, артист в вишлисте записан
# со службой apple — и вишлист честно молчал. Владелец скачал релиз руками за
# сутки до даты. Ровно ради этих полусуток и заведён новозеландский аккаунт.
#
# Поэтому опрашиваем ещё и ранние витрины: артист ищется в их каталогах по имени
# (`ripster.artist_xref`, сверка только по точному совпадению), а найденный релиз
# идёт общим путём — уведомление и, если включено авто-скачивание, очередь.

_EARLY_WINDOW_DAYS = 14    # проверка раз в 6ч; окна с запасом хватает
_SEEN_CAP = 80             # столько ключей релизов помним на артиста

# Витрина одного релиза называет его по-разному: Apple любит «- Single», Tidal
# пишет голое название. Сравнивать надо то, что осталось после этого мусора,
# иначе один и тот же релиз уведомит дважды — сегодня из Tidal, завтра из Apple.
import re as _re


def _rel_key(title: str) -> str:
    from ripster.artist_xref import norm
    t = _re.sub(r"\s*[-–—]\s*(single|ep)\s*$", "", str(title or ""), flags=_re.I)
    t = _re.sub(r"\s*\((single|ep)\)\s*$", "", t, flags=_re.I)
    return norm(t)


def _seen_add(entry: dict, title: str) -> None:
    seen = entry.get("seen")
    if not isinstance(seen, list):
        seen = []
    k = _rel_key(title)
    if k and k not in seen:
        seen.insert(0, k)
    entry["seen"] = seen[:_SEEN_CAP]


def _seen_has(entry: dict, title: str) -> bool:
    return _rel_key(title) in (entry.get("seen") or [])


# ── «Объявили, но взять не смогли» ──────────────────────────────────────────────
# `seen` защищает от ПОВТОРНОГО УВЕДОМЛЕНИЯ — и правильно делает: один релиз не
# должен звенеть дважды, сегодня из Tidal, завтра из Apple. Но пометка ставится
# ДО попытки скачивания, поэтому релиз, который объявили, а скачать было неоткуда
# (витрина ещё не наполнилась, `_pick_download_url` вернул пусто), навсегда
# считался обработанным. Через сутки он появлялся в Apple — и не происходило
# НИЧЕГО: ни уведомления (правильно), ни загрузки (а вот это потеря).
#
# Держим такие релизы отдельным списком. Следующие проходы молча пробуют их
# добрать — без нового уведомления, как и просил владелец. Список с пределом и
# сроком: релиз, не подъехавший за неделю, перестаём дёргать, иначе получим
# вечный опрос витрин по накопленному хвосту.
_PENDING_CAP  = 40
_PENDING_TTL  = 7 * 24 * 3600


def _pending_add(entry: dict, title: str, rel: dict) -> None:
    k = _rel_key(title)
    if not k:
        return
    import time as _t
    lst = [p for p in (entry.get("pending") or [])
           if isinstance(p, dict) and p.get("key") != k]
    lst.insert(0, {"key": k, "title": title, "ts": _t.time(),
                   "rel": {x: rel.get(x) for x in ("title", "artist", "url",
                                                   "service", "id", "upc")}})
    entry["pending"] = lst[:_PENDING_CAP]


def _pending_drop(entry: dict, key: str) -> None:
    entry["pending"] = [p for p in (entry.get("pending") or [])
                        if isinstance(p, dict) and p.get("key") != key]


def _pending_live(entry: dict) -> list:
    """Ещё не протухшие записи. Протухшие вычищаются здесь же — отдельного
    сборщика заводить не за чем."""
    import time as _t
    now = _t.time()
    live = [p for p in (entry.get("pending") or [])
            if isinstance(p, dict) and (now - float(p.get("ts") or 0)) < _PENDING_TTL]
    if len(live) != len(entry.get("pending") or []):
        entry["pending"] = live
    return live


def _early_services(cfg: dict) -> list:
    """Какие витрины опрашивать раньше Apple.

    По умолчанию Tidal — единственный аккаунт, живущий в зоне, которая входит в
    пятницу раньше всех. Остальные не запрещены, но и не навязаны: каждый лишний
    сервис — это ещё один проход по всем артистам каждые 6 часов.
    """
    from ripster import artist_xref as _xref
    raw = cfg.get("watchlist-early-services")
    if raw is None:
        raw = "tidal"
    names = [s.strip() for s in str(raw).split(",") if s.strip()]
    return [s for s in names if s in _xref.SERVICES and _xref.has_credentials(s)]


async def _early_artist_releases(client, service: str, artist: dict,
                                 cutoff: str, cfg: dict) -> list:
    """Свежие релизы артиста в одной витрине — теми же функциями, что и радар."""
    from ripster.routes import releases as _rel
    sem = asyncio.Semaphore(1)
    if service == "tidal":
        hdr = {"Authorization": f"Bearer {str(cfg.get('tidal-token') or '').strip()}"}
        cc = str(cfg.get("tidal-country") or "US").strip().upper() or "US"
        return await _rel._tidal_fetch_artist(sem, client, artist, cc, hdr, cutoff)
    if service == "qobuz":
        app_id = str(cfg.get("qobuz-app-id") or "").strip() or "312369995"
        hdr = {"X-User-Auth-Token": str(cfg.get("qobuz-auth-token") or "").strip(),
               "X-App-Id": app_id}
        return await _rel._qobuz_fetch_artist(sem, client, artist, app_id, hdr, cutoff)
    if service == "deezer":
        return await _rel._deezer_fetch_artist(sem, client, artist, cutoff)
    return []


async def _check_early_targets(targets: list, broadcast, save, cfg, queue,
                               snapshot) -> int:
    """Опросить ранние витрины по всем артистам вишлиста.

    Первый проход для записи — только baseline: помечаем всё, что видно в окне,
    и молчим. Иначе добавление источника выглядело бы как «вышло 14 релизов
    сразу» и, при включённом авто-скачивании, обрушило бы в очередь чужой
    бэк-каталог. Ровно та же осторожность, что и у Apple-ветки с `prev is None`.
    """
    from ripster import artist_xref as _xref

    services = _early_services(cfg)
    if not services:
        return 0

    cutoff = (datetime.now() - timedelta(days=_EARLY_WINDOW_DAYS)).strftime("%Y-%m-%d")
    early_dl = cfg.get("watchlist-early-download") is not False
    found = 0

    async with httpx.AsyncClient(timeout=20) as client:
        for service in services:
            names = [str(e.get("name") or "").strip() for e in targets
                     if str(e.get("name") or "").strip()]
            xref = await _xref.resolve_many(names, service, client)
            if not xref:
                continue
            print(f"[watchlist] {service}: polling {len(xref)} of {len(names)} artists",
                  flush=True)

            for entry in targets:
                nm = str(entry.get("name") or "").strip()
                hit = xref.get(nm)
                if not hit:
                    continue
                try:
                    rels = await _early_artist_releases(
                        client, service, {"id": hit["id"], "name": nm}, cutoff, cfg)
                except Exception as e:                              # noqa: BLE001
                    print(f"[watchlist] {service} {nm}: {e}", flush=True)
                    continue

                # Дата у Tidal бывает завтрашней при уже отдающемся файле — это и
                # есть опережение, резать будущее по дате нельзя. Зато анонс без
                # `streamReady` брать нельзя тем более: качать там нечего.
                rels = [r for r in rels if r.get("stream_ready", True)]

                baseline = not isinstance(entry.get("seen"), list)
                if baseline:
                    entry["seen"] = []
                    for r in rels:
                        _seen_add(entry, r.get("title", ""))
                    continue

                # Сначала — молчаливая дозагрузка того, что уже объявляли, но
                # взять было неоткуда. Ни тоста, ни события: человек про этот
                # релиз уже знает, ему нужен файл, а не второе уведомление.
                if entry.get("auto_download") and early_dl:
                    for p in list(_pending_live(entry)):
                        _rel = dict(p.get("rel") or {})
                        _ttl = _rel.get("title") or p.get("title") or ""
                        if not _ttl:
                            _pending_drop(entry, p.get("key", ""))
                            continue
                        _u = await _pick_download_url(
                            _rel, entry.get("service", "apple"), "", cfg, _ttl)
                        if not _u:
                            continue
                        _tsk = _make_task(_u, entry.get("quality", ""), cfg,
                                          "watchlist-early")
                        _enrich_soon(_tsk)
                        queue.append(_tsk)
                        _pending_drop(entry, p.get("key", ""))
                        await broadcast({"type": "queue_update", "queue": snapshot()})
                        print(f"[watchlist] ⤵ добрал '{_ttl}' ({nm}) — "
                              f"появился там, где можно взять", flush=True)

                for r in rels:
                    title = r.get("title", "")
                    if not title or _seen_has(entry, title):
                        continue
                    _seen_add(entry, title)
                    found += 1
                    await broadcast({"type": "watchlist_new_release",
                                     "artist": nm, "release": title,
                                     "compilation": False,
                                     "early": service,
                                     "url": r.get("url", "")})
                    _notify_release(nm, f"{title} (уже доступен в {service})",
                                    False, bool(entry.get("auto_download")), r)
                    print(f"[watchlist] ⚡ '{title}' ({nm}) already in {service} - "
                          f"not in Apple yet", flush=True)

                    if not (entry.get("auto_download") and early_dl):
                        continue
                    # Куда стрелять — решает доступность, а не подписка. Если в
                    # любимом сервисе релиза ещё нет, берём ту витрину, где он
                    # уже лежит: потерять полсуток хуже, чем скачать не оттуда.
                    dl_url = await _pick_download_url(
                        r, entry.get("service", "apple"), "", cfg, title
                    ) or r.get("url", "")
                    if not dl_url:
                        # Нигде ещё не отдаётся. Раньше здесь релиз просто
                        # терялся: в `seen` он уже лежит, значит больше его никто
                        # не увидит. Откладываем — доберём молча, когда появится.
                        _pending_add(entry, title, r)
                        continue
                    task = _make_task(dl_url, entry.get("quality", ""), cfg,
                                      "watchlist-early")
                    _enrich_soon(task)
                    queue.append(task)
                    await broadcast({"type": "queue_update", "queue": snapshot()})

    # Сохраняем всегда: baseline первого прохода тоже надо пережить перезапуск,
    # иначе следующий запуск снова примет весь бэк-каталог за новинки.
    save(_s["items"])
    return found


async def _check_watchlist():
    items      = _s["items"]
    broadcast  = _s["broadcast"]
    save       = _s["save"]
    cfg        = _s["config"]
    queue      = _s["queue"]
    snapshot   = _s["queue_snapshot"]

    # Следим через Apple-каталог независимо от того, откуда потом качаем:
    # у подписки на Deezer/Qobuz/Tidal сервис — это адрес доставки, а не
    # источник наблюдения. Пока здесь стояло `service == "apple"`, такие
    # записи не попадали НИ в один список и не проверялись вообще.
    targets = [e for e in items
               if e.get("service") != "soundcloud" and e.get("artist_id")
               and e.get("kind") != "label"]
    sc_count = len([e for e in items
                    if e.get("service") == "soundcloud" and e.get("kind") != "label"])
    label_count = len([e for e in items if e.get("kind") == "label"])
    total = len(targets) + sc_count + label_count
    if total == 0:
        return

    new_found = 0
    await broadcast({"type": "watchlist_check_start", "total": total})

    if label_count:
        new_found += await _check_label_targets(items, broadcast, save, cfg, queue, snapshot)

    if sc_count:
        new_found += await _check_soundcloud_targets(items, broadcast, save, cfg, queue, snapshot)

    # Ранние витрины опрашиваем ДО Apple: в них релиз появляется раньше, и смысл
    # прохода именно в том, чтобы узнать первым.
    try:
        new_found += await _check_early_targets(targets, broadcast, save, cfg,
                                                queue, snapshot)
    except Exception as e:                                          # noqa: BLE001
        print(f"[watchlist] early storefronts: {e}", flush=True)

    storefront = cfg.get("storefront", "us") or "us"
    # Compilations cost one extra lookup per artist; on by default because a
    # label compilation is exactly the release people miss.
    want_comps = cfg.get("watchlist-compilations", True) is not False
    async with httpx.AsyncClient(timeout=15) as client:
        for i, entry in enumerate(targets):
            artist_id = entry["artist_id"]
            await broadcast({
                "type":    "watchlist_check_progress",
                "current": sc_count + i + 1,
                "total":   total,
                "artist":  entry.get("name", "?"),
            })
            try:
                latest = await _apple_latest_album(client, artist_id, storefront,
                                                   with_comps=want_comps)
                # last_check is stamped even when the lookup yields nothing, so
                # "never checked" in the UI means exactly that.
                entry["last_check"] = datetime.now().isoformat(timespec="seconds")
                if not latest:
                    continue
                release_url  = latest["url"]
                release_name = latest["name"]
                prev = entry.get("last_release")
                # Тот же релиз мог прийти раньше из ранней витрины под другой
                # ссылкой — уведомлять о нём второй раз нельзя. `last_release`
                # для этого не годится: он хранит URL, а URL у каждой витрины свой.
                if release_url and release_url != prev and _seen_has(entry, release_name):
                    entry["last_release"] = release_url
                    save(items)
                    continue
                if release_url and release_url != prev:
                    entry["last_release"] = release_url
                    if isinstance(entry.get("seen"), list):
                        _seen_add(entry, release_name)
                    # First ever check just records a baseline: otherwise adding
                    # an artist would instantly "discover" their whole current
                    # back catalogue and queue it.
                    if prev is None:
                        save(items)
                        continue
                    new_found += 1
                    save(items)
                    await broadcast({"type":     "watchlist_new_release",
                                     "artist":   entry["name"],
                                     "release":  release_name,
                                     "compilation": bool(latest.get("compilation")),
                                     "url":      release_url})
                    _notify_release(entry["name"], release_name,
                                    bool(latest.get("compilation")),
                                    bool(entry.get("auto_download")), latest)
                    if entry.get("auto_download") and release_url:
                        # Нашли в Apple — но качать надо туда, куда подписан
                        # владелец. Для service="apple" это короткое замыкание
                        # внутри _pick_download_url и ровно прежнее поведение.
                        # Если в целевом сервисе релиза ещё нет, берём Apple-
                        # ссылку, которая у нас уже на руках: потерять релиз
                        # хуже, чем скачать его не из любимого сервиса.
                        dl_url = await _pick_download_url(
                            {"service": "apple", "url": release_url,
                             "title": release_name,
                             "artist": latest.get("artist", ""),
                             "date": latest.get("date", "")},
                            entry.get("service", "apple"), "", cfg, release_name,
                        ) or release_url
                        task = _make_task(dl_url, entry.get("quality", ""),
                                          cfg, "watchlist")
                        _enrich_soon(task)
                        queue.append(task)
                        await broadcast({"type": "queue_update", "queue": snapshot()})
            except Exception as e:
                print(f"[watchlist] {entry['name']}: {e}", flush=True)

    # Persist the last_check stamps of entries that had no news — without this
    # a quiet run leaves every entry looking like it was never checked.
    save(items)

    await broadcast({
        "type":    "watchlist_check_done",
        "checked": total,
        "new":     new_found,
    })
