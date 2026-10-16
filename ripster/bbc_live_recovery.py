"""Восстановление повреждённых записей BBC live (24.09.2026).

Живой эфир пишется ``-c copy`` — если сеть/писатель подменили кусок потока,
в файле портится НАГРАНКА кадров при ровном таймлайне: длительность верная,
число кадров верное, а декодер на испорченных минутах сыпет «Reserved bit
set» / «Number of bands exceeds limit». Починить битые AAC-кадры нельзя —
можно только выбросить: промер декодом по окнам, склейка честных окон БЕЗ
перекодирования, финальный decode-check.

Порядок восстановления после прохода проверки качества (вердикт движка
«повреждено»): сначала спасение честных 320 из самого файла — on-demand
версия того же эфира это 96..102 кбит/с HE-AAC, она не замена, а подпись
под ней (см. HANDOFF_2026-09-19_qoder_session_MASTER.md §4).

Оригинал не перезаписывается НИКОГДА: salvaged-копия кладётся рядом с явной
меткой в имени.
"""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

# Новое окно декодера всегда даёт одну служебную строку («channel element
# 0.0 duplicate» на старте) — это не повреждение.
_TOLERANCE = 1
_COARSE   = 60.0     # первый проход — окна по минуте
_FINE     = 10.0     # границы честного куска уточняются десятисекундными окнами
_MIN_KEEP = 60.0     # короче минуты не спасаем: с таким куском файл только шумнее
_TIMEOUT  = 900      # запас на медленный диск; висящий прогон не вешает восстановление
# ``-ss`` у copy-реза стартует с ближайшего синка и может захватить кадр-два
# ПЕРЕД запрошенной позицией (24.09: рез с честной границы 3080.0 принёс 23
# строки мусора, с 3081.0 — ноль). Прозрачная запаска в начале каждого куска.
_CUT_MARGIN = 2.0
_MARK_SALVAGED = "восстановлено 320"
_MARK_BROKEN   = "повреждено"


def _ff(program: str) -> str:
    p = shutil.which(program)
    if not p:
        raise RuntimeError(f"{program} не найден в PATH")
    return p


def probe_duration(path: Path) -> float:
    """Длительность файла; 0.0 — не прочиталась (битый контейнер)."""
    try:
        r = subprocess.run(
            [_ff("ffprobe"), "-v", "error", "-show_entries", "format=duration",
             "-of", "csv=p=0", str(path)],
            capture_output=True, text=True, timeout=120)
        return float((r.stdout or "0").strip() or 0.0)
    except Exception:
        return 0.0


def decode_errors(path: Path) -> int:
    """Число стрек декодера — мера битости файла (0 = декодируется чисто)."""
    return _decode_errors_window(path, None, None)


def _decode_errors_window(path: Path, start: float | None, dur: float | None) -> int:
    cmd = [_ff("ffmpeg"), "-v", "error"]
    if start is not None:
        cmd += ["-ss", f"{start:.3f}"]
    cmd += ["-i", str(path)]
    if dur is not None:
        cmd += ["-t", f"{dur:.3f}"]
    cmd += ["-f", "null", "-"]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=_TIMEOUT)
    except Exception:
        return 10 ** 9          # промер не удался — окно считаем битым, не честным
    return sum(1 for ln in r.stderr.splitlines() if ln.strip())


def clean_regions(path: Path, *, duration: float | None = None) -> list[tuple[float, float]]:
    """Честные куски файла по времени: (start, end) в секундах, по возрастанию.

    Сначала разметка минутными окнами, потом границы каждого честного куска
    уточняются десятисекундными — полный промер по 10 с на двухчасовом файле
    стоил бы час лишнего. Окна с числом ошибок не больше ``_TOLERANCE``
    считаются честными (это те самые служебные строки старта декодера).
    """
    total = float(duration or probe_duration(path))
    if total <= 0:
        return []
    coarse: list[bool] = []
    t = 0.0
    while t < total:
        w = min(_COARSE, total - t)
        coarse.append(_decode_errors_window(path, t + 0.5 if t else 0.0,
                                            max(1.0, w - 1.0)) <= _TOLERANCE)
        t += _COARSE
    runs: list[list[float]] = []
    for i, good in enumerate(coarse):
        if not good:
            continue
        st, en = i * _COARSE, min((i + 1) * _COARSE, total)
        if runs and abs(runs[-1][1] - st) < 1e-6:
            runs[-1][1] = en
        else:
            runs.append([st, en])
    out: list[tuple[float, float]] = []
    for st, en in runs:
        # размотка границ внутрь соседних битых минут — не дальше _COARSE,
        # иначе на полностью битом файле мы «найдём» честный кусок ниоткуда
        while st - _FINE >= 0 and _window_ok(path, max(0.0, st - _FINE), _FINE):
            st = max(0.0, st - _FINE)
        while en + _FINE <= total and _window_ok(path, en, min(_FINE, total - en)):
            en = min(total, en + _FINE)
        if en - st >= _MIN_KEEP:
            out.append((st, en))
    return out


def _window_ok(path: Path, start: float, dur: float) -> bool:
    return _decode_errors_window(path, start + (0.5 if start else 0.0),
                                 max(1.0, dur - 1.0)) <= _TOLERANCE


def rebuild(src: Path, dst: Path, regions: list[tuple[float, float]]) -> bool:
    """Собрать dst из честных кусков src без единого битрейт-потерянного прохода
    (везде ``-c copy``). True — dst лежит и декодируется чисто."""
    if not regions:
        return False
    tmp = dst.with_name(f".{dst.stem}.tmp")
    tmp.mkdir(parents=True, exist_ok=True)
    try:
        parts: list[Path] = []
        for i, (st, en) in enumerate(regions):
            p = tmp / f"part_{i:03d}.m4a"
            cut_st, cut_dur = st + _CUT_MARGIN, en - st - _CUT_MARGIN
            if cut_dur < 10:
                continue
            r = subprocess.run(
                [_ff("ffmpeg"), "-hide_banner", "-v", "error", "-y",
                 "-ss", f"{cut_st:.3f}", "-t", f"{cut_dur:.3f}", "-i", str(src),
                 "-c", "copy", str(p)],
                capture_output=True, text=True, timeout=_TIMEOUT)
            if r.returncode != 0 or not p.exists() or p.stat().st_size < 1024:
                return False
            parts.append(p)
        if len(parts) == 1:
            cmd = [_ff("ffmpeg"), "-hide_banner", "-v", "error", "-y",
                   "-i", str(parts[0]), "-c", "copy",
                   "-movflags", "+faststart", str(dst)]
        else:
            listing = tmp / "list.txt"
            listing.write_text("".join(f"file '{p.as_posix()}'\n" for p in parts),
                               encoding="utf-8")
            cmd = [_ff("ffmpeg"), "-hide_banner", "-v", "error", "-y",
                   "-f", "concat", "-safe", "0", "-i", str(listing),
                   "-c", "copy", "-movflags", "+faststart", str(dst)]
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=_TIMEOUT)
        if r.returncode != 0 or not dst.exists():
            return False
        return decode_errors(dst) <= _TOLERANCE
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def measure_kbps(path: Path) -> int:
    """Измеренный битрейт аудиопотока файла, кбит/с (0 — не прочитан).
    Именем запасной копии должно говорить ЧИСЛО, а не обещание движка."""
    for entries in ("stream=bit_rate", "format=bit_rate"):
        try:
            r = subprocess.run(
                [_ff("ffprobe"), "-v", "error", "-select_streams", "a:0",
                 "-show_entries", entries, "-of", "csv=p=0", str(path)],
                capture_output=True, text=True, timeout=120)
            v = int((r.stdout or "0").strip() or 0)
            if v > 0:
                return round(v / 1000)
        except Exception:
            continue
    return 0


def find_episode(title: str) -> dict | None:
    """On-demand копия того же выпуска в BBC Sounds: RMS-поиск по названию,
    первый playable_item с совпавшим заголовком. {"pid","title","url"} или None.

    Поле `pid` здесь — настоящая передача из urn:bbc:radio:episode:… (id
    записи = vpid), ровно так же, как это делает поиск во вкладке BBC
    (ripster/routes/bbc.py::search_bbc).
    """
    q = (title or "").strip()
    if not q:
        return None
    try:
        import httpx
        r = httpx.get("https://rms.api.bbc.co.uk/v2/experience/inline/search",
                      params={"q": q}, headers={"Accept": "application/json"},
                      timeout=20)
        if r.status_code != 200:
            return None
        data = r.json() or {}
    except Exception:
        return None
    ql = q.lower()
    for block in data.get("data", []):
        for ep in block.get("data", []):
            if ep.get("type") != "playable_item":
                continue
            t = str((ep.get("titles") or {}).get("primary")
                    or ep.get("title") or "")
            urn = str(ep.get("urn") or "")
            pid = urn.split(":")[-1] if urn else str(ep.get("id") or "")
            if not pid or ql not in t.lower():
                continue
            return {"pid": pid, "title": t or q,
                    "url": f"https://www.bbc.co.uk/sounds/play/{pid}"}
    return None


def salvage(src: Path, out_name: str | None = None) -> dict:
    """Спасание одного файла. Возвращает честный отчёт:

        {"ok", "out", "clean_s", "damaged_s", "total_s", "reason"}

    ``out_name`` — имя файла копии (по умолчанию «<имя> (восстановлено 320).m4a»);
    оригинал не меняется никогда.
    """
    src = Path(src)
    report: dict = {"ok": False, "out": None, "clean_s": 0.0,
                    "damaged_s": 0.0, "total_s": 0.0, "reason": ""}
    total = probe_duration(src)
    report["total_s"] = total
    if total <= 0:
        report["reason"] = "файл не читается (нет длительности)"
        return report
    regions = clean_regions(src, duration=total)
    clean = sum(en - st for st, en in regions)
    report["clean_s"] = round(clean, 1)
    report["damaged_s"] = round(total - clean, 1)
    if not regions:
        report["reason"] = "честных минут не найдено — восстанавливать нечего"
        return report
    name = out_name or f"{src.stem} ({_MARK_SALVAGED}).{src.suffix.lstrip('.')}"
    dst = src.with_name(name)
    if dst.exists():                       # не трём прошлое восстановление
        k = 2
        while (dst := src.with_name(f"{src.stem} ({_MARK_SALVAGED} {k}).{src.suffix.lstrip('.')}")).exists():
            k += 1
    if not rebuild(src, dst, regions):
        dst.unlink(missing_ok=True)
        report["reason"] = "пересборка не прошла финальный decode-check"
        return report
    report["ok"] = True
    report["out"] = str(dst)
    return report
