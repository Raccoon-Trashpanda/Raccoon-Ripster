"""Отложенная запись BBC-эфиров.

У выпусков BBC есть время начала эфира. Владелец ставит «отложенную запись» —
она живёт КАЛЕНДАРЁМ в ``bbc_scheduled.json`` рядом с конфигом и показывается на
вкладке BBC. В очередь загрузки план НЕ кладётся: висящая сутками карточка с 0 %
превращает очередь в доску объявлений, а качаться в этот момент нечего. Цикл
``run_loop()`` (поднимается в lifespan) ждёт наступления времени и лишь тогда
создаёт обычную задачу-загрузку и заводит ``process_queue`` — дальше её ведёт
обычный путь очереди, то есть история, манифест и доставка ничем не отличаются
от любой другой загрузки и ничего не знают, что когда-то это был план.

Наступившее во время простоя (выключенная машина, упавшее приложение) окно
разбирает ``fire()``: свежий эфир дописывается с опозданием, а слишком
опоздавший не пишется задним числом и не исчезает молча — план остаётся в
календаре с честным вердиктом «пропущен», который видно на вкладке BBC.

Время хранится в UTC. Файл пишется атомарно (tmp + os.replace), чтобы обрыв
посреди записи не оставил полфайла вместо всех планов.
"""
from __future__ import annotations

import asyncio
import json
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path

from ripster.i18n_msg import imsg
from ripster.task_state import TaskStatus

STORE_VERSION = 1
MAX_LIVE_MINUTES = 8 * 60          # физический предел: дольше марафона BBC не пишем
_GRACE_SECONDS = 600               # «уже поздно» — эфир начался слишком давно
_LATENESS_SECONDS = 10             # допуск на дрейф часов и паузу цикла
# Статус карточек, которые СТАРАЯ сборка клала в очередь загрузки. План больше
# не карточка, но queue_pending.json переживает правку — их надо убрать при
# старте (см. restore()).
_LEGACY_CARD = "scheduled"


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def parse_utc(value: str | int | float) -> datetime:
    """ISO-строка (с смещением или без — без смещения считаем UTC) или unix-time."""
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(float(value), tz=timezone.utc)
    raw = (value or "").strip()
    if not raw:
        raise ValueError("empty time")
    iso = raw[:-1] + "+00:00" if raw.endswith("Z") else raw
    dt = datetime.fromisoformat(iso)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def fmt_utc(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ── Хранилище ────────────────────────────────────────────────────────────────

class ScheduledStore:
    """Список запланированных записей в JSON-файле рядом с config.yaml."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self._items: list[dict] = []
        self.loaded = False

    # ── чтение/запись ──

    def load(self) -> list[dict]:
        self.loaded = True
        try:
            if not self.path.exists():
                self._items = []
                return self._items
            raw = json.loads(self.path.read_text(encoding="utf-8")) or []
            rows = raw.get("recordings") if isinstance(raw, dict) else raw
            self._items = [r for r in (rows or []) if isinstance(r, dict) and r.get("id")]
        except Exception as e:
            print(f"[bbc-schedule] load failed ({self.path}): {e}. Starting empty.",
                  flush=True)
            self._items = []
        return self._items

    def save(self) -> None:
        """Атомарно: пишем временный файл рядом, ставим, затем os.replace."""
        payload = {"version": STORE_VERSION, "recordings": self._items}
        tmp = self.path.with_name(f".{self.path.name}.{os.getpid()}.tmp")
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(payload, fh, ensure_ascii=False, indent=2)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, self.path)
        except Exception as e:
            print(f"[bbc-schedule] save failed: {e}", flush=True)
            try:
                tmp.unlink()
            except OSError:
                pass

    # ── доступ ──

    def _ensure(self) -> list[dict]:
        if not self.loaded:
            self.load()
        return self._items

    def all(self) -> list[dict]:
        return [dict(r) for r in self._ensure()]

    def get(self, sid: str) -> dict | None:
        return next((r for r in self._ensure() if r.get("id") == sid), None)

    def pending(self) -> list[dict]:
        return [r for r in self._ensure() if r.get("status") == "pending"]

    def mark(self, sid: str, **fields) -> None:
        row = self.get(sid)
        if row is not None:
            row.update(fields)
            self.save()

    # ── изменение ──

    def add(self, *, channel: str, start_utc: datetime, duration: int,
            title: str = "", subtitle: str = "", cover: str = "",
            task_id: str = "", pid: str = "", forecast: dict | None = None) -> dict:
        """Поставить план. Дубликат (тот же канал и то же время эфира, ещё не
        записанный) — ValueError: вторая запись того же эфира была бы просто
        вторым экземпляром файла."""
        rows = self._ensure()
        if not channel:
            raise ValueError(imsg("err.bbc_live_no_channel", "Не указан канал эфира"))
        duration = int(duration or 0)
        if duration <= 0:
            raise ValueError(imsg("err.bbc_live_bad_duration",
                                  "Нужна длительность эфира в секундах"))
        if duration > MAX_LIVE_MINUTES * 60:
            raise ValueError(imsg("err.bbc_live_too_long",
                                  f"Дольше {MAX_LIVE_MINUTES * 60 // 60} часов эфир не пишем"))
        start = start_utc.astimezone(timezone.utc)
        if (start - utcnow()).total_seconds() < -_GRACE_SECONDS:
            raise ValueError(imsg("err.bbc_live_past",
                                  "Время эфира уже прошло — записать можно только будущий эфир"))
        same = [r for r in rows
                if r.get("channel") == channel
                and r.get("status") == "pending"
                and abs((_dt(r) - start).total_seconds()) < 30]
        if same:
            raise ValueError(imsg("err.bbc_live_duplicate",
                                  "Запись этого эфира уже запланирована"))
        row = {
            "id":         uuid.uuid4().hex[:8],
            "channel":    channel,
            "start_utc":  fmt_utc(start),
            "duration":   duration,
            "title":      title,
            "subtitle":   subtitle,
            "cover":      cover,
            "pid":        pid,
            # Что мы ОБЕЩАЛИ человеку до нажатия «записать» (ripster/bbc_quality:
            # фактическая лестница live-потока, признак повтора, есть ли копия).
            # Храним серверная сторона и свой ответ, а не то, что принёс фронт,
            # иначе план обещал бы то, что нарисовал кривой клиент.
            "forecast":   forecast or {},
            "task_id":    task_id,
            "status":     "pending",
            "created_utc": fmt_utc(utcnow()),
        }
        rows.append(row)
        self.save()
        return dict(row)

    def remove(self, sid: str) -> bool:
        rows = self._ensure()
        keep = [r for r in rows if r.get("id") != sid]
        if len(keep) == len(rows):
            return False
        self._items = keep
        self.save()
        return True


def _dt(row: dict) -> datetime:
    try:
        return parse_utc(row.get("start_utc") or "")
    except Exception:
        return utcnow()


# ── Контекст приложения ──────────────────────────────────────────────────────

_store: ScheduledStore | None = None
_queue:    list = []
_qs        = None
_lock:     asyncio.Lock | None = None
_cfg:      dict = {}
_broadcast = None
_process_queue = None
_make_task = None
_queue_snapshot = None
save_state       = None      # callable — сброс планов на диск (тесты подменяют)


def install(*, store: ScheduledStore, queue: list, qs, config: dict,
            broadcast, process_queue, make_task, queue_snapshot) -> None:
    global _store, _queue, _qs, _lock, _cfg, _broadcast, _process_queue
    global _make_task, _queue_snapshot, save_state
    _store         = store
    _queue         = queue
    _qs            = qs
    _lock          = qs.lock
    _cfg           = config
    _broadcast     = broadcast
    _process_queue = process_queue
    _make_task     = make_task
    _queue_snapshot = queue_snapshot
    save_state     = store.save


def get_store() -> ScheduledStore:
    if _store is None:
        raise RuntimeError("bbc_schedule.install() has not run yet")
    return _store


def new_task(row: dict) -> dict:
    """Обычная задача-загрузка из плана. Ничем от ручной не отличается: тот же
    ``queued``, тот же путь очереди. url — псевдо-ссылка эфира, по ней движок
    понимает, какой канал писать."""
    from ripster import bbc_live_channels as _ch
    channel = row.get("channel") or ""
    task = _make_task(f"https://www.bbc.co.uk/sounds/live/{channel}",
                      "live320", "bbc_live", "bbc", source="bbc_schedule")
    # Id плана = id задачи: по нему ``settle_finished`` находит запись в очереди
    # и в истории, а отмена снятого ещё плана чистит след именно в очереди.
    task["id"]     = row["task_id"] = row["id"]
    task["schedule_id"] = row["id"]
    task["meta"] = {
        "service":  "bbc",
        "title":    row.get("title") or _ch.label(channel),
        "artist":   _ch.label(channel),
        "artworkUrl": row.get("cover") or "",
        "duration": int(row.get("duration") or 0),
        "channel":  channel,
        "pid":      row.get("pid") or "",
    }
    return task


def schedule_recording(*, channel: str, start_utc: datetime, duration: int,
                       title: str = "", subtitle: str = "", cover: str = "",
                       pid: str = "", forecast: dict | None = None) -> dict:
    """Создать план. Только план: в очереди загрузки до наступления эфира
    делать нечего (см. докстринг модуля)."""
    return get_store().add(channel=channel, start_utc=start_utc, duration=duration,
                           title=title, subtitle=subtitle, cover=cover,
                           pid=pid, forecast=forecast)


def _drop_card(sid: str) -> bool:
    """Убрать из очереди карточку плана старого образца с этим id."""
    gone = False
    for t in list(_queue):
        if (t.get("id") == sid and t.get("source") == "bbc_schedule"
                and t.get("status") == _LEGACY_CARD):
            _queue.remove(t)
            gone = True
    return gone


def cancel_recording(sid: str) -> bool:
    """Снять план. Pending-план в очереди ничего не оставил — карточку убираем
    только если её завела старая сборка. Исполненную запись (fired) отмена не
    трогает: она уже обычная загрузка, и отменяют её средствами очереди."""
    store = get_store()
    row = store.get(sid)
    if row is None:
        return False
    store.remove(sid)
    return _drop_card(sid) or row.get("status") == "pending"


async def restore() -> int:
    """После перезапуска: в очереди загрузки планов быть не может.

    Старые сборки держали там карточку «запланировано на …» с нулём прогресса, и
    ``queue_pending.json`` возвращает её и сейчас — такая карточка висела бы
    сутками и не давала очереди умереть. Убираем: план живёт в своём файле и
    дождётся своего часа сам. Наступившие за простой окна не трогаем — их
    разберёт ``run_loop`` (он же честно отметит слишком опоздавшие)."""
    n = sum(_drop_card(t.get("id", "")) for t in list(_queue)
            if t.get("source") == "bbc_schedule"
            and t.get("status") == _LEGACY_CARD)
    # queue_update — единственный путь, который пишет queue_pending.json
    # (app.py делает это в broadcast()): без него снятая карточка вернулась бы
    # на следующем старте.
    if n and _broadcast and _queue_snapshot:
        await _broadcast({"type": "queue_update", "queue": _queue_snapshot()})
    return n


# ── Цикл проверки ────────────────────────────────────────────────────────────

def due_rows(store: ScheduledStore, now: datetime | None = None) -> list[dict]:
    now = now or utcnow()
    return [r for r in store.pending() if (now - _dt(r)).total_seconds() >= -_LATENESS_SECONDS]


async def _miss(row: dict, late: float) -> bool:
    """Окно прошло, пока машина стояла. Писать задним числом нечего — но и
    молча исчезать плану нельзя: человек планировал запись и вправе увидеть,
    что её НЕ будет и почему. Вердикт «пропущен» остаётся в календаре."""
    store = get_store()
    minutes = int(late // 60)
    _drop_card(row["id"])
    store.mark(row["id"], status="missed",
               verdict={"state": "missed", "late_minutes": minutes,
                        "missed_utc": fmt_utc(utcnow())})
    print(f"[bbc-schedule] {row.get('id')}: эфир начался {minutes} мин назад — "
          f"пропущен, записан не был", flush=True)
    if _broadcast:
        await _broadcast({"type": "bbc_sched_update"})
    return False


async def fire(row: dict) -> bool:
    """Наступило время — план становится обычной задачей-загрузкой."""
    store = get_store()
    late = (utcnow() - _dt(row)).total_seconds()
    if late > _GRACE_SECONDS:
        return await _miss(row, late)
    _drop_card(row["id"])
    _queue.append(new_task(row))
    store.mark(row["id"], status="fired", fired_utc=fmt_utc(utcnow()))
    if _broadcast:
        await _broadcast({"type": "queue_update", "queue": _queue_snapshot()})
        await _broadcast({"type": "bbc_sched_update"})
    if _process_queue and _qs:
        async with _lock:
            should_start = _qs.start()
        if should_start:
            asyncio.create_task(_process_queue())
            await _broadcast({"type": "queue_started"})
    print(f"[bbc-schedule] пора писать эфир {row.get('channel')} ({row.get('id')})", flush=True)
    return True


# ── Сверка записанного файла с обещанием ─────────────────────────────────────

_AUDIO_EXT = (".m4a", ".mp3", ".aac", ".opus", ".ogg", ".wav", ".flac", ".alac")
# Между концом эфира и появлением записи в истории: буфер на хвост перепаковки
# и на то, что часы чуть расходятся.
_SETTLE_GRACE = 180
# Как часто переспрашивать историю, пока вердикта нет (сек). Цикл — 5 с, а
# перечитывать history.json каждые пять секунд два часа подряд — просто шум.
_SETTLE_POLL = 60
_next_check: dict[str, float] = {}    # sid → unix-время следующей пробы


def _history_path() -> Path | None:
    """history.json лежит в той же папке, что и bbc_scheduled.json (оба — рядом
    с config.yaml: см. BASE_DIR в app.py)."""
    store = get_store()
    p = store.path.parent / "history.json"
    return p if p.is_file() else None


def _finished_task(sid: str) -> dict | None:
    """Задача записи после конца эфира: ещё жива в очереди — берём оттуда,
    иначе читаем history.json (очередь чистят, история остаётся)."""
    for t in _queue:
        if t.get("id") == sid and t.get("status") in ("done", "error", "cancelled"):
            return t
    path = _history_path()
    if path is None:
        return None
    try:
        rows = json.loads(path.read_text(encoding="utf-8")) or []
    except Exception:
        return None
    if isinstance(rows, dict):
        rows = rows.get("history") or rows.get("items") or []
    return next((h for h in rows if isinstance(h, dict) and h.get("id") == sid), None)


def _newest_audio(save_dir) -> Path | None:
    if not save_dir:
        return None
    d = Path(save_dir)
    if d.is_file():
        return d if d.suffix.lower() in _AUDIO_EXT else None
    try:
        files = [f for f in d.iterdir()
                 if f.is_file() and f.suffix.lower() in _AUDIO_EXT]
    except OSError:
        return None
    return max(files, key=lambda f: f.stat().st_size) if files else None


def settle_result(row: dict, task: dict | None) -> dict:
    """Вердикт по записанному файлу — числами, без доверия к шильдику.

    Спрашиваем САМ файл (ffprobe), а не поле `quality` задачи: история и папка
    называются по ЗАПРОШЕННОМУ качеству (проверено на другом сервисе — 28.07.2026
    при запросе FLAC приехал AAC 268 kbps в папке «FLAC»). Обещание планировщика —
    320 кбит/с AAC-LC live-потока; живой поток тем и отличается от файла, что
    ступень может измениться в середине ночи, поэтому «live320» в названии плана
    ничего не гарантирует.

    Профиль и полоса берутся из `.measured` (см. ripster/bbc_quality.py): у copy-
    записи из варианта LC они ровно те, что в потоке, — перекодировки нет, так что
    за апконверт здесь отвечает не спектр, а запрет на него до кодирования.
    """
    promised = int((row.get("forecast") or {}).get("best_kbps") or 0) or 320
    status = (task or {}).get("status") or ""
    if status in ("error", "cancelled"):
        return {"state": "failed", "promised_kbps": promised,
                "reason": str((task or {}).get("error") or status)[:300],
                "measured_utc": fmt_utc(utcnow())}
    sd = (task or {}).get("_save_dir") or ""
    audio = _newest_audio(sd)
    if audio is None:
        return {"state": "no_file", "promised_kbps": promised,
                "reason": "save-dir пуст" if sd else "папка задачи не записана",
                "measured_utc": fmt_utc(utcnow())}
    try:
        from ripster import bbc_quality as _q
        v = _q.verdict(audio, promised)
    except Exception as e:
        return {"state": "unmeasured", "promised_kbps": promised,
                "reason": type(e).__name__, "measured_utc": fmt_utc(utcnow())}
    v["file"] = audio.name
    return v


def settle_finished(now: datetime | None = None) -> int:
    """Довести каждую запись «fired» до вердикта. Число новых вердиктов."""
    store = get_store()
    now = now or utcnow()
    n = 0
    for row in store.all():
        if row.get("status") != "fired" or row.get("verdict"):
            continue
        sid = row.get("id") or ""
        started = _dt(row)
        end = started.timestamp() + int(row.get("duration") or 0) + _SETTLE_GRACE
        if now.timestamp() < end:
            continue
        if now.timestamp() < _next_check.get(sid, 0):
            continue
        _next_check[sid] = now.timestamp() + _SETTLE_POLL
        task = _finished_task(sid)
        if task is None:
            # Задача ещё идёт (или пропала): ждём, не выдумываем вердикт.
            if now.timestamp() > end + 6 * 3600:
                # Сдались: вердикт «не померили» — тоже вердикт, и он закрывает
                # строку. Не засчитать его здесь значит соврать счётчику
                # «сколько планов получили ответ», который возвращает цикл.
                store.mark(sid, verdict={"state": "unmeasured", "reason": "no task",
                                         "measured_utc": fmt_utc(now)},
                           status="finished", finished_utc=fmt_utc(now))
                _next_check.pop(sid, None)
                n += 1
            continue
        v = settle_result(row, task)
        store.mark(sid, verdict=v, finished_utc=v.get("measured_utc") or fmt_utc(now),
                   status="recorded" if v.get("state") == "as_promised" else "finished")
        _next_check.pop(sid, None)
        n += 1
        print(f"[bbc-schedule] {sid}: эфир записан — {v.get('state')} "
              f"{v.get('kbps', '')}{' кбит/с' if v.get('kbps') else ''} "
              f"{v.get('codec') or v.get('reason') or ''}".strip(), flush=True)
    return n


async def run_loop(period: float = 5.0) -> None:
    store = get_store()
    tick = 0
    while True:
        try:
            for row in due_rows(store):
                await fire(row)
            # Раз в ~30 с: сверить законченные записи с обещанием. ffprobe и
            # чтение истории — блокирующие, уводим в поток, чтобы не тормозить
            # очередь и ведро соккетов.
            if tick % max(1, int(30 / period)) == 0:
                await asyncio.to_thread(settle_finished)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            print(f"[bbc-schedule] cycle error: {type(e).__name__}: {e}", flush=True)
        tick += 1
        await asyncio.sleep(period)
