# -*- coding: utf-8 -*-
"""Кого пускать в сэмпл-точный gapless: решает ДЛИНА, а не сервис.

Движок Web Audio даёт стык без единого зазора, но требует скачать и
раскодировать трек целиком до первого сэмпла. У часового DJ-микса это минуты
ожидания — поэтому SoundCloud и Deezer были исключены. Но исключены ЦЕЛИКОМ,
вместе с обычными четырёхминутными треками, которые декодируются мгновенно: то
есть большая часть прослушивания шла через <audio> с резким стыком, хотя могла
идти без зазора вовсе (02.08.2026).

Разведка по Spotify и Apple Music в тот же день подтвердила направление: «пинок»
между треками — это задача GAPLESS, а не кроссфейда. Apple вообще не делает
кроссфейд для lossless, а gapless у Spotify включён по умолчанию.

Здесь проверяется само правило отбора — по тексту функции, без браузера.

Запуск:  python tools/test_gapless_gate.py
"""
from __future__ import annotations

import pathlib
import re
import sys

BASE = pathlib.Path(__file__).resolve().parent.parent
_fails: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(("  OK  " if ok else "ПРОВАЛ") + f"  {name}")
    if not ok:
        _fails.append(name)
        if detail:
            print(f"        {detail}")


src = (BASE / "static" / "js" / "player.js").read_text(encoding="utf-8")
fn = src.split("function _waCanPlay", 1)[1].split("\n}", 1)[0]
code = "\n".join(l for l in fn.splitlines() if not l.strip().startswith("//"))

check("сервис больше не отсекается целиком",
      "if (_svc === 'deezer') return false;" not in code
      and "if (_svc === 'soundcloud') return false;" not in code,
      "иначе обычные короткие треки снова теряют gapless")

check("решение принимается по длительности",
      "duration" in code and "_WA_MAX_SEC" in code)

m = re.search(r"_WA_MAX_SEC\s*=\s*(\d+)\s*\*\s*60", code)
check("порог задан в минутах и разумен",
      bool(m) and 5 <= int(m.group(1)) <= 30,
      f"порог: {m.group(1) if m else '?'} мин")

check("тяжёлые сервисы всё ещё под ограничением",
      "soundcloud" in code and "deezer" in code,
      "иначе часовой микс снова будет декодироваться целиком перед стартом")

check("неизвестная длительность считается длинной",
      "!dur ||" in code or "!dur||" in code,
      "лучше потерять gapless на одном треке, чем заставить ждать минуту")

check("проверка источника осталась",
      "location.origin" in code,
      "чужой origin ломает decodeAudioData из-за CORS")

print()
if _fails:
    print(f"ПРОВАЛЕНО: {len(_fails)} — {', '.join(_fails)}")
    sys.exit(1)
print("Все проверки пройдены — короткое играет без зазора, длинное потоком.")
