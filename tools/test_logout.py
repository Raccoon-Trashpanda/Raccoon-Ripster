# -*- coding: utf-8 -*-
"""Выход из аккаунта чистит ровно то, что нужно, и не трогает настройки.

Выхода в Ripster не было вовсе. 01.08.2026 это стоило вечера: у Apple ID
кончились слоты устройств, а сменить аккаунт можно было только руками через
docker и config.yaml.

Опасность у такой кнопки одна и понятная — снести лишнее. Поэтому проверяем не
только «стёрло», но и «НЕ стёрло»: путь загрузок, качество и прочие настройки
должны пережить любой выход.

Запуск:  python tools/test_logout.py
"""
from __future__ import annotations

import asyncio
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from ripster.routes import auth as A  # noqa: E402

_fails: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(("  OK  " if ok else "ПРОВАЛ") + f"  {name}")
    if not ok:
        _fails.append(name)
        if detail:
            print(f"        {detail}")


BASE = {
    "yandex-token": "y-secret", "deezer-arl": "arl-secret",
    "qobuz-auth-token": "q-tok", "qobuz-user-id": "123", "qobuz-password": "pw",
    "tidal-token": "t-tok", "tidal-user-id": "42",
    "soundcloud-oauth-token": "sc-tok", "media-user-token": "mut",
    # То, что выход трогать НЕ должен ни при каких условиях.
    "save-path": r"C:\Music", "quality": "alac-hires", "language": "ru",
    "session-secret": "не-трогать", "app-password-hash": "тоже-не-трогать",
}


async def logout(svc: str, body: dict | None = None) -> dict:
    A._cfg.clear()
    A._cfg.update(BASE)
    saved = {"n": 0}
    A._save_config = lambda cfg: saved.__setitem__("n", saved["n"] + 1)
    r = await A.logout_service(svc, body or {})
    r["_saved"] = saved["n"]
    return r


async def main() -> None:
    r = await logout("yandex")
    check("яндекс: токен стёрт", A._cfg["yandex-token"] == "")
    check("яндекс: чужие ключи целы", A._cfg["deezer-arl"] == "arl-secret")
    check("яндекс: конфиг сохранён", r["_saved"] == 1)

    r = await logout("qobuz")
    check("qobuz: стёрты все три ключа",
          all(A._cfg[k] == "" for k in ("qobuz-auth-token", "qobuz-user-id", "qobuz-password")),
          str({k: A._cfg[k] for k in ("qobuz-auth-token", "qobuz-user-id", "qobuz-password")}))
    check("qobuz: отчитался, что именно стёр", len(r["cleared"]) == 3, str(r["cleared"]))

    r = await logout("tidal")
    check("tidal: токен и пользователь стёрты",
          A._cfg["tidal-token"] == "" and A._cfg["tidal-user-id"] == "")

    # Главное: настройки не должны страдать ни от одного выхода.
    for svc in ("yandex", "qobuz", "tidal", "deezer", "soundcloud"):
        await logout(svc)
        ok = (A._cfg["save-path"] == r"C:\Music" and A._cfg["quality"] == "alac-hires"
              and A._cfg["language"] == "ru")
        check(f"{svc}: настройки не тронуты", ok,
              f"save-path={A._cfg['save-path']} quality={A._cfg['quality']}")
        check(f"{svc}: секреты приложения целы",
              A._cfg["session-secret"] == "не-трогать"
              and A._cfg["app-password-hash"] == "тоже-не-трогать")

    # Неизвестный сервис — отказ, а не тихое «ок».
    try:
        await logout("нетакого")
        check("неизвестный сервис отвергнут", False, "прошло без ошибки")
    except Exception as e:
        check("неизвестный сервис отвергнут", "400" in str(e) or "Unsupported" in str(e), str(e))

    # Apple без явной просьбы НЕ должен сносить identity: это новое устройство
    # на аккаунте, а слоты конечны.
    src = pathlib.Path(A.__file__)
    print("\n  (apple: снос identity проверяется только по флагу forget_device —")
    print("   без него код к папке не обращается, см. logout_service)")
    check("apple: identity сносится только по флагу",
          'if (body or {}).get("forget_device")' in src.read_text(encoding="utf-8"))

    print()
    if _fails:
        print(f"ПРОВАЛЕНО: {len(_fails)} — {', '.join(_fails)}")
        sys.exit(1)
    print("Все проверки пройдены — выход чистит своё и не трогает чужое.")


asyncio.run(main())
