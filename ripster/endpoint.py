"""Где лежит приёмник отчётов: discovery-файл в открытом репозитории.

Адрес приёмника был вшит в сборку намертво. Туннель меняет имя — и все тестерские
сборки остаются стучаться в мёртвую дверь: починка требовала переустановки, то
 есть доставки исправления через тот же самый канал, который и сломался. Поэтому
клиент сначала смотрит в крошечный JSON у себя в открытом репозитории:

    {"ingest": "https://…", "v": 1}    report-endpoint.json, корень репозитория

Правила, которые держат эту механику честной:

  • СВОЙ адрес в конфиге (`telemetry-url`) главнее всегда —Discovery нужен там,
    где у пользователя ничего не настроено, а не вместо его настройки.
  • Файл читается раз в сутки (TTL_S). Чаще — бессмысленно: raw.githubusercontent
    держит свой кэш столько же, и только плодим запросы.
  • Любой отказ (сеть, не-JSON, мусорный адрес, неизвестная `v`) = вшитый
    по умолчанию адрес. Молча, без исключения, без «отправки нет»: discovery —
    уточнение, а не условие.
  • В этом файле НЕТ токена. Клиент обязан уметь долиться до приёмника, не
    имея при себе ничего, кроме адреса.
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import time
from pathlib import Path
from typing import Optional

# Схема файла: понимаем ровно одну. Неизвестная `v` игнорируется, а не трактуется
# «как умею»: иначе следующая incompatible-схема могла бы направить отчёты туда,
# где её неправильно поняли.
SUPPORTED_V = 1

# Pinned: discovery качается ТОЛЬКО по https и только с raw.githubusercontent.
REPO_RAW_URL = ("https://raw.githubusercontent.com/"
                "Raccoon-Trashpanda/Raccoon-Ripster/main/report-endpoint.json")
TTL_S = 24 * 3600
CACHE_NAME = "report_endpoint.json"
_MAX_URL_LEN = 200

# Origin без путей и запросов: host[:port][/path]. Хост-класс не допускает ни
# «user:pass@», ни «?», ни пробелов — то есть ничего из того, чем подделывают
# адрес в строке.
_ORIGIN = re.compile(r"^https://[A-Za-z0-9][A-Za-z0-9._-]*(:[0-9]{2,5})?(/[-A-Za-z0-9._~%+/]*)?$")


def sane_url(url) -> str:
    """Нормализованный адрес, если он годится; иначе ''. Никаких исключений."""
    u = str(url or "").strip().rstrip("/")
    if not u or len(u) > _MAX_URL_LEN or u.lower().startswith("https://."):
        return ""
    if not _ORIGIN.match(u) or "@" in u or ".." in u:
        return ""
    return u


def parse(text: str) -> str:
    """Тело discovery-файла → адрес, либо '' (файл непонятен — не верим)."""
    try:
        data = json.loads(text)
    except Exception:
        return ""
    if not isinstance(data, dict) or data.get("v") != SUPPORTED_V:
        return ""
    return sane_url(data.get("ingest"))


def cache_dir() -> Path:
    """Дом кэша — тот же, что у instance_id: профиль пользователя, а НЕ дерево
    установки (последнее зеркалится в публичный репозиторий)."""
    base = os.environ.get("LOCALAPPDATA") or os.environ.get("APPDATA") or ""
    return (Path(base) / "Ripster") if base else (Path(".") / "logs")


def cache_path() -> Path:
    return cache_dir() / CACHE_NAME


# memo: (адрес,wall-clock дедлайн) — record() дёргает ingest_url() на КАЖДУЮ
# строку консоли, читать ради этого файл нельзя.
_memo: tuple[str, float] = ("", 0.0)


def read_cache(path: Optional[Path] = None, now: Optional[float] = None) -> tuple[str, float]:
    """(адрес, когда протухнет); ('', 0) — кэша нет или он не годится."""
    t = time.time() if now is None else now
    try:
        raw = json.loads((path or cache_path()).read_text(encoding="utf-8"))
        if not isinstance(raw, dict) or raw.get("v") != SUPPORTED_V:
            return ("", 0.0)
        url = sane_url(raw.get("ingest"))
        ts = float(raw.get("fetched_at") or 0)
    except Exception:
        return ("", 0.0)
    expires = ts + TTL_S
    if not url or t >= expires:
        return ("", 0.0)
    return (url, expires)


def write_cache(url: str, path: Optional[Path] = None,
                now: Optional[float] = None) -> bool:
    """Сохранить адрес. Best-effort: не записалось — живём с вшитым."""
    good = sane_url(url)
    if not good:
        return False
    try:
        p = path or cache_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(".tmp")
        tmp.write_text(json.dumps({"v": SUPPORTED_V, "ingest": good,
                                  "fetched_at": time.time() if now is None else now}),
                       encoding="utf-8")
        tmp.replace(p)
        global _memo
        _memo = (good, (time.time() if now is None else now) + TTL_S)
        return True
    except Exception:
        return False


def resolve(fallback: str = "", now: Optional[float] = None) -> str:
    """Свежий discovery-адрес, иначе вшитый по умолчанию. Никогда не бросает."""
    global _memo
    t = time.time() if now is None else now
    if _memo[0] and t < _memo[1]:
        return _memo[0]
    url, expires = read_cache(now=t)
    _memo = (url, expires) if url else ("", 0.0)
    if url:
        return url
    return sane_url(fallback) or (fallback or "").strip().rstrip("/")


def forget() -> None:
    """Тестам: сбросить memo (кэш на диске не трогается)."""
    global _memo
    _memo = ("", 0.0)


async def refresh(fallback: str = "", url: str = REPO_RAW_URL,
                  client=None) -> str:
    """Скачать discovery-файл и законсервировать адрес; вернуть рабочий адрес.

    Любая неудача — тихий откат к тому, что уже есть (кэш → вшитый). Отправка
    диагностики не имеет права умереть из-за того, что GitHub не ответил.
    """
    own = client is None
    client_ip = None
    try:
        if own:
            import httpx
            client_ip = httpx.AsyncClient(timeout=10)
            client = client_ip
        r = await client.get(url)
        if getattr(r, "status_code", 0) == 200:
            got = parse(r.text)
            if got:
                write_cache(got)
    except Exception:
        pass
    finally:
        if client_ip is not None:
            try:
                await client_ip.aclose()
            except Exception:
                pass
    return resolve(fallback)


async def run_loop(fallback: str = "", url: str = REPO_RAW_URL) -> None:
    """Фоновый обход: раз в TTL_S обновить адрес. Никаких исключений наружу —
    этот цикл обязан пережить и отсутствие сети, и отсутствие диска."""
    while True:
        try:
            await refresh(fallback, url)
        except asyncio.CancelledError:
            return
        except Exception:
            pass
        try:
            await asyncio.sleep(TTL_S)
        except asyncio.CancelledError:
            return
        except Exception:
            return
