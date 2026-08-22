"""Периодический обход учёток: сам замечает, что что-то протухло.

Пробы живости в Ripster были для каждого сервиса, но запускались ТОЛЬКО по
нажатию кнопки в настройках. То есть обхода не существовало — существовала
ручная проверка. Цена этого видна на живом примере: 09.08.2026 у gamdl
кончилась подписка, и узнали мы об этом в момент падения загрузки, а не за две
недели, когда её ещё можно было продлить спокойно.

Что делает этот модуль:
  • раз в несколько часов обходит все настроенные учётки теми же пробами;
  • сравнивает с прошлым разом и сообщает владельцу ТОЛЬКО ИЗМЕНЕНИЯ;
  • отдельно предупреждает о подписках, которым осталось меньше двух недель.

Почему только изменения. Ежедневное «всё хорошо» люди перестают читать через
неделю, и настоящее «Deezer умер» тонет вместе с остальным. Сообщение приходит,
когда есть о чём сообщить.

Состояние лежит на диске: после перезапуска приложения молчание не должно
означать «ничего не менялось» — оно должно означать именно то, что было.
"""
from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path

_STATE = "dist/accounts_watch.json"
_PERIOD = 6 * 3600.0
_FIRST_DELAY = 180.0          # дать приложению подняться и не толкаться на старте
_EXPIRY_WARN_DAYS = 14


def _state_path(base_dir) -> Path:
    p = Path(base_dir) / _STATE
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def _load(base_dir) -> dict:
    try:
        return json.loads(_state_path(base_dir).read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save(base_dir, data: dict) -> None:
    try:
        _state_path(base_dir).write_text(json.dumps(data, ensure_ascii=False, indent=1),
                                         encoding="utf-8")
    except Exception:
        pass


async def _survey() -> list[dict]:
    """Тот же обход, что и у ручки /api/accounts/survey — намеренно."""
    from ripster.routes.auth import accounts_survey
    res = await accounts_survey()
    return res.get("accounts") or []


def _svc_name(svc: str) -> str:
    """Человеческое имя службы для сообщения в бот.

    Технический ключ в тексте владельцу — загадка вместо новости:
    «apple_wrapper» не говорит ни что сломалось, ни куда идти чинить. Сам текст
    лежит в реестре `ripster/i18n.py` (ключи `svc.*`), а не здесь: в Ripster
    видимые человеку строки по месту не пишутся. Ключа нет — отдаём как есть,
    это лучше пустоты.
    """
    try:
        from ripster import i18n as _i18n
        key = f"svc.{svc}"
        if key in _i18n.FALLBACK:
            return _i18n.tr(key)
    except Exception:
        pass
    return svc


def _diff(old: dict, rows: list[dict]) -> list[str]:
    """Только изменения статуса. Первый прогон изменениями не считается."""
    msgs = []
    for r in rows:
        svc, ok = r["service"], bool(r["ok"])
        was = old.get(svc)
        if was is None:
            continue                      # первое знакомство — не новость
        if was.get("ok") != ok:
            if ok:
                msgs.append(f"✅ {_svc_name(svc)}: снова отвечает")
            else:
                msgs.append(f"🔴 {_svc_name(svc)}: перестал отвечать — {r.get('error') or 'без причины'}")
    return msgs


async def _expiring(config: dict) -> list[str]:
    """Подписки, которым осталось меньше двух недель."""
    out = []
    try:
        from ripster import deezer_accounts as _dza
        for a in await _dza.survey(config):
            d = a.get("expires_date")
            if not (a.get("alive") and d):
                continue
            try:
                y, m, dd = (int(x) for x in d.split("-"))
                from datetime import date
                left = (date(y, m, dd) - date.today()).days
            except Exception:
                continue
            if 0 <= left <= _EXPIRY_WARN_DAYS:
                out.append(f"⏳ Deezer «{a['label']}»: подписка кончается {d} "
                           f"(осталось {left} дн.)")
    except Exception:
        pass
    return out


async def _default_notify(text: str) -> None:
    """Сообщить владельцу в его бот.

    Отдельная функция, а не переиспользование telemetry.notify_owner_bot: тот
    шлёт отчёт об ошибке вместе с архивом, здесь нужен просто текст. Токен
    берём из tgbot/config.json — ключ там `bot_token`, а не `token` (на этом
    уже спотыкались).
    """
    import json as _j
    import urllib.parse as _up
    import urllib.request as _ur
    from pathlib import Path as _P
    try:
        cfg = _j.loads(_P("tgbot/config.json").read_text(encoding="utf-8-sig"))
        data = _up.urlencode({"chat_id": str(cfg["owner_id"]), "text": text,
                              "disable_web_page_preview": "true"}).encode()
        await asyncio.to_thread(
            _ur.urlopen,
            f"https://api.telegram.org/bot{cfg['bot_token']}/sendMessage", data, 30)
    except Exception as e:
        print(f"[accounts-watch] notification not sent: {type(e).__name__}: {e}", flush=True)


async def run(config: dict, base_dir, notify=None) -> None:
    """Вечный цикл обхода. `notify(text)` — как сообщить владельцу.

    Исключения глушим намеренно и с записью: сторож, роняющий приложение при
    недоступности чужого API, хуже отсутствующего сторожа.
    """
    notify = notify or _default_notify
    await asyncio.sleep(_FIRST_DELAY)
    while True:
        try:
            rows = await _survey()
            st = _load(base_dir)
            old = st.get("services") or {}
            msgs = _diff(old, rows)

            # Про сроки напоминаем не чаще раза в сутки, иначе это шум.
            last_exp = float(st.get("expiry_notified_at") or 0)
            if time.time() - last_exp > 86400:
                exp = await _expiring(config)
                if exp:
                    msgs += exp
                    st["expiry_notified_at"] = time.time()

            st["services"] = {r["service"]: {"ok": bool(r["ok"]),
                                             "error": r.get("error", "")} for r in rows}
            st["checked_at"] = int(time.time())
            _save(base_dir, st)

            if msgs and notify:
                dead = [r["service"] for r in rows if not r["ok"]]
                tail = f"\n\nЖивых: {len(rows) - len(dead)} из {len(rows)}."
                await notify("🔎 Обход учёток\n\n" + "\n".join(msgs) + tail)
            print(f"[accounts-watch] survey: {len(rows)} accounts, "
                  f"changes {len(msgs)}", flush=True)
        except Exception as e:
            print(f"[accounts-watch] survey failed: {type(e).__name__}: {e}", flush=True)
        await asyncio.sleep(_PERIOD)
