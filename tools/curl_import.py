# -*- coding: utf-8 -*-
r"""Превратить «Copy as cURL (bash)» из DevTools в готовый запрос Ripster.

Зачем: у части сервисов нет публичного API, и единственный честный путь к
СВОИМ данным — повторить тот же запрос, что делает их собственный сайт под
твоей сессией. DevTools умеет отдать его целиком одной строкой; этот скрипт
разбирает её и складывает в `tokens/<имя>.json`, откуда клиент берёт адрес,
заголовки и куки.

Почему отдельный шаг, а не «вставь заголовки руками»: в cURL из браузера
десятки заголовков, часть из них одноразовые (`sec-fetch-*`, `priority`), а
один — `cookie` — секрет. Руками это переносится с ошибками, а ошибка здесь
выглядит как «сервис не отвечает».

Использование:
    python tools/curl_import.py featurefm --file curl.txt
    curl.txt — то, что дал DevTools → Copy → Copy as cURL (bash)

Проверить сохранённое, ничего не отправляя:
    python tools/curl_import.py featurefm --show
"""
from __future__ import annotations

import argparse
import json
import re
import shlex
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TOKENS = ROOT / "tokens"

#: Заголовки, которые не переносим: они относятся к КОНКРЕТНОМУ клику в
#: браузере, а не к запросу. Оставить их — значит повторять чужой контекст и
#: получать отказы там, где их быть не должно.
_DROP = {
    "accept-encoding", "content-length", "connection", "host",
    "sec-ch-ua", "sec-ch-ua-mobile", "sec-ch-ua-platform",
    "sec-fetch-dest", "sec-fetch-mode", "sec-fetch-site", "sec-fetch-user",
    "priority", "pragma", "cache-control", "upgrade-insecure-requests",
}


def parse_curl(text: str) -> dict:
    """Разобрать команду cURL. Возвращает {method,url,headers,cookies,body}."""
    # DevTools переносит строки обратным слэшем — склеиваем перед разбором.
    text = re.sub(r"\\\s*\n", " ", text.strip())
    if not text.lstrip().startswith("curl"):
        raise SystemExit("это не команда curl — нужен вывод «Copy as cURL (bash)»")
    parts = shlex.split(text)

    url = ""
    method = ""
    headers: dict[str, str] = {}
    cookies: dict[str, str] = {}
    body = ""

    i = 1
    while i < len(parts):
        p = parts[i]
        if p in ("-X", "--request"):
            i += 1
            method = parts[i].upper()
        elif p in ("-H", "--header"):
            i += 1
            k, _, v = parts[i].partition(":")
            k, v = k.strip(), v.strip()
            if k.lower() == "cookie":
                for chunk in v.split(";"):
                    name, _, val = chunk.strip().partition("=")
                    if name:
                        cookies[name] = val
            elif k.lower() not in _DROP:
                headers[k] = v
        elif p in ("-b", "--cookie"):
            i += 1
            for chunk in parts[i].split(";"):
                name, _, val = chunk.strip().partition("=")
                if name:
                    cookies[name] = val
        elif p in ("--data", "--data-raw", "--data-binary", "-d"):
            i += 1
            body = parts[i]
        elif p.startswith("http"):
            url = p
        i += 1

    if not url:
        raise SystemExit("в команде нет адреса запроса")
    if not method:
        method = "POST" if body else "GET"
    return {"method": method, "url": url, "headers": headers,
            "cookies": cookies, "body": body}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("name", help="как назвать: featurefm, …")
    ap.add_argument("--file", help="файл с командой cURL")
    ap.add_argument("--show", action="store_true", help="показать сохранённое")
    a = ap.parse_args()

    dst = TOKENS / f"{a.name}_request.json"
    if a.show:
        if not dst.is_file():
            raise SystemExit(f"нет сохранённого запроса: {dst}")
        d = json.loads(dst.read_text(encoding="utf-8"))
        print(f"{d['method']} {d['url']}")
        print(f"заголовков: {len(d['headers'])}, кук: {len(d['cookies'])}")
        print("тело:", (d.get("body") or "")[:200] or "(пусто)")
        return

    raw = Path(a.file).read_text(encoding="utf-8") if a.file else sys.stdin.read()
    parsed = parse_curl(raw)
    TOKENS.mkdir(parents=True, exist_ok=True)
    dst.write_text(json.dumps(parsed, ensure_ascii=False, indent=1), encoding="utf-8")
    # Значения не печатаем: среди них сессия владельца.
    print(f"сохранено: {dst}")
    print(f"{parsed['method']} {parsed['url']}")
    print(f"заголовков: {len(parsed['headers'])}, кук: {len(parsed['cookies'])}")


if __name__ == "__main__":
    main()
