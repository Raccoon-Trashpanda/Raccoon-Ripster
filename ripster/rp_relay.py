"""Ретранслятор сообщений мобильной панели поверх общего /ws (трекер #37).

Третий транспорт панели — «relay»: окно панели (pywebview или любая вкладка
с ?transport=relay) и главная страница Ripster сидят на одном сервере и
обмениваются rp-сообщениями ЧЕРЕЗ СЕРВЕР, а не через pywebview или postMessage.
Аудио при этом остаётся единственного владельца — главной страницы; окно
только контроллер. Это то, чего не мог дать «oswin»: он требовал лаунчера,
а владелец работает в обычной вкладке браузера, где OS-окно не создаётся.

Протокол сообщений НЕ меняется — ровно тот же {rp:1, k:…, cmd:…}, что
уже общий у iframe-дока и OS-окна (см. static/js/panel_host.js).

Класс чистый: никаких WebSocket-вызовов, только маршрутизация. app.py
скармливает ему сообщения из приёмного цикла /ws и рассылает пары
(ws, payload) через свой брокер. Так логика живёт под юнит-тестом без
поднятого сервера.

Безопасность: роль регистрирует ТОЛЬКО не-гость и не-спаренный телефон
(проверка остаётся у вызывающего — сюда роль просто не попадает). Сообщения
без rp-метки выбрасываются, чужая роль — тишина.
"""
from __future__ import annotations

from typing import Any

Role = str  # 'host' | 'panel'


class RpRelay:
    def __init__(self) -> None:
        # dict держим в порядке регистрации: «самый свежий host» — адресат
        # команд панели, когда хостов вдруг несколько (две вкладки).
        self.roles: dict[Any, Role] = {}

    # ── регистрация ────────────────────────────────────────────────────────
    def register(self, ws: Any, role: Any) -> bool:
        if role not in ("host", "panel"):
            return False
        self.roles.pop(ws, None)          # перерегистрация = свежая очередь
        self.roles[ws] = role
        return True

    def role(self, ws: Any) -> Role | None:
        return self.roles.get(ws)

    def unregister(self, ws: Any) -> Role | None:
        return self.roles.pop(ws, None)

    # ── маршрутизация ───────────────────────────────────────────────────────
    def route(self, ws: Any, msg: Any) -> list[tuple[Any, dict]]:
        """Сообщение от клиента → куда разослать. Панель говорит с ОДНИМ
        самым свежим хостом (иначе «дальше» из двух вкладок перематывает два
        трека), хост вещает всем панелям."""
        if not isinstance(msg, dict) or msg.get("rp") != 1:
            return []
        role = self.roles.get(ws)
        if role == "panel":
            host = self._latest_host()
            return [(host, {"type": "rp", "msg": msg})] if host else []
        if role == "host":
            return [(p, {"type": "rp", "msg": msg})
                    for p, r in self.roles.items() if r == "panel"]
        return []

    def drop(self, ws: Any) -> list[tuple[Any, dict]]:
        """Клиент отвалился — предупредить тех, кто на него смотрел:
        панель умерла → хосту 'bye' (перестаёт слать состояние);
        хост умер → панелям 'host-gone' (гасим «мост» честно)."""
        role = self.unregister(ws)
        if role == "panel":
            hosts = [h for h, r in self.roles.items() if r == "host"]
            return [(h, {"type": "rp", "msg": {"rp": 1, "k": "bye"}}) for h in hosts]
        if role == "host":
            panels = [p for p, r in self.roles.items() if r == "panel"]
            payload = {"type": "rp", "msg": {"rp": 1, "k": "host-gone"}}
            return [(p, dict(payload)) for p in panels]
        return []

    def _latest_host(self) -> Any | None:
        hosts = [h for h, r in self.roles.items() if r == "host"]
        return hosts[-1] if hosts else None
