"""Ретранслятор сообщений мобильной панели поверх общего /ws (трекер #37).

Третий транспорт панели — «relay»: окно панели (pywebview или любая вкладка
с ?transport=relay) и главная страница Ripster сидят на одном сервере и
обмениваются rp-сообщениями ЧЕРЕЗ СЕРВЕР, а не через pywebview или postMessage.
Аудио при этом остаётся единственного владельца — главной страницы; окно
только контроллер. Это то, чего не мог дать «oswin»: он требовал лаунчера,
а владелец работает в обычной вкладке браузера, где OS-окно не создаётся.

ВЫБОРЫ ХОСТА (исправление 24.09.2026, «плеер живёт своей жизнью»). Раньше
панель командовала САМЫМ СВЕЖИМ хостом — но хостом регистрируется КАЖДАЯ
вкладка Ripster при каждом открытии сокета. Стоило открыть вторую вкладку
(или перегрузить любую), как мост переезжал на молчаливую вкладку, и окно
показывало тишину вместо играет-ли-что. Теперь адресат — вкладка, КОТОРАЯ
ИГРАЕТ (последний заигравший «прилипает», пока его не перехватит другой
заигравший хост). Хосты шлют лёгкий heartbeat {playing, ts}; релей выбирает
активного, ему — команды панели и от него — состояние. Остальные вкладки
молчат: их state релей выбрасывает.

Класс чистый: никаких WebSocket-вызовов, только маршрутизация. app.py
скармливает ему сообщения из приёмного цикла /ws и рассылает пары
(ws, payload) через свой брокер. Так логика живёт под юнит-тестом без
поднятого сервера.

Безопасность: роль регистрирует ТОЛЬКО не-гость и не-спаренный телефон
(проверка остаётся у вызывающего — сюда роль просто не попадает). Сообщения
без rp-метки выбрасываются, чужая роль — тишина.
"""
from __future__ import annotations

import time
from typing import Any

Role = str  # 'host' | 'panel'


def _rp(msg: dict) -> dict:
    return {"type": "rp", "msg": msg}


class RpRelay:
    def __init__(self) -> None:
        # dict держим в порядке регистрации: «самый свежий host» — запасной
        # адресат, когда ни одна вкладка не играет.
        self.roles: dict[Any, Role] = {}
        # ws -> {'playing': bool, 'ts': int}: heartbeat-картотека для выборов.
        self.hosts: dict[Any, dict] = {}
        self.elected: Any | None = None      # активный хост (адресат панели)
        self._active_sent: dict[Any, bool] = {}  # какой host-active уже слал
        # Метка каждого клиента ('h1', 'p1', …) — наружу в диагностике показываем
        # её, а не id объекта сокета (тот можно перепутать с чем угодно).
        self.ids: dict[Any, str] = {}
        self._seq: dict[Role, int] = {"host": 0, "panel": 0}
        # Последний «ack» от панели: что она РЕАЛЬНО показывает. Единственное
        # доказательство, что состояние до окна дошло, а не только уехало.
        self.last_ack: dict | None = None

    # ── регистрация ────────────────────────────────────────────────────────
    def register(self, ws: Any, role: Any) -> bool:
        if role not in ("host", "panel"):
            return False
        self.roles.pop(ws, None)          # перерегистрация = свежая очередь
        self.roles[ws] = role
        if role == "host":
            self.hosts[ws] = {"playing": False, "ts": 0}
            self._active_sent.pop(ws, None)
        else:
            self.hosts.pop(ws, None)
        if ws not in self.ids:
            self._seq[role] += 1
            self.ids[ws] = ("h" if role == "host" else "p") + str(self._seq[role])
        self._elect()
        return True


    def on_register(self, ws: Any) -> list[tuple[Any, dict]]:
        """Сразу после успешной регистрации ХОСТА (зовёт app.py): если панель
        уже есть — сообщить об этом новому хосту и подтвердить ЕМУ, активен ли
        он. Без этого вкладка, подключившаяся к живому окну, молчала бы, пока
        панель сама не поздоровается. Раздачу host-active остальным вкладкам
        делает первый же heartbeat (_on_hb) — трогать чужие сокеты при регистрации
        незачем."""
        if self.roles.get(ws) != "host" or not self._panels():
            return []
        on = ws == self.elected
        self._active_sent[ws] = on
        return [(ws, _rp({"rp": 1, "k": "panel-present"})),
                (ws, _rp({"rp": 1, "k": "host-active", "on": 1 if on else 0}))]

    def role(self, ws: Any) -> Role | None:
        return self.roles.get(ws)

    def elected_host(self) -> Any | None:
        return self.elected

    def unregister(self, ws: Any) -> Role | None:
        role = self.roles.pop(ws, None)
        self.hosts.pop(ws, None)
        self._active_sent.pop(ws, None)
        self.ids.pop(ws, None)
        if self.elected is ws:
            self.elected = None
        return role

    # ── маршрутизация ───────────────────────────────────────────────────────
    def route(self, ws: Any, msg: Any) -> list[tuple[Any, dict]]:
        """Сообщение от клиента → куда разослать.

        Панель говорит с ОДНИМ активным хостом (тем, что играет). Хост-heartbeat
        («hb») релей потребляет сам: обновляет картотеку, пересчитывает выборы и
        отвечает host-active только тем, у кого статус сменился. Состояние
        пропускаем на панели ТОЛЬКО от активного хоста — иначе молчаливая вкладка
        затопчет играющую своей пустотой."""
        if not isinstance(msg, dict) or msg.get("rp") != 1:
            return []
        role = self.roles.get(ws)
        if role == "panel":
            if msg.get("k") == "ack":
                # Подтверждение панели — не команда хосту, а отчёт серверу:
                # «мне дошло состояние, вот что на экране». Хосту не нужен.
                return self._on_ack(ws, msg)
            host = self._elect()
            return [(host, _rp(msg))] if host else []
        if role == "host":
            if msg.get("k") == "hb":
                return self._on_hb(ws, msg)
            if ws != self.elected:
                return []                    # неактивный хост — тишина
            return [(p, _rp(msg)) for p in self._panels()]
        return []

    def drop(self, ws: Any) -> list[tuple[Any, dict]]:
        """Клиент отвалился — предупредить тех, кто на него смотрел:
        панель умерла → хостам 'bye' (перестают слать состояние);
        хост умер:
          • умер НЕ активный — ничего не меняется, активный и так хозяин,
            панель ничего не замечает (без ложного host-gone и переезда моста);
          • умер АКТИВНЫЙ, но есть другой — молча передаём мост выжившему
            (panel-present + host-active), а не врём панели про «хост ушёл»;
          • хостов не осталось — панелям 'host-gone' (гасим мост честно)."""
        was_elected = ws == self.elected
        role = self.unregister(ws)
        if role == "panel":
            self.last_ack = None       # подтверждать больше нечего: панели нет
            return [(h, _rp({"rp": 1, "k": "bye"}))
                    for h in self._hosts()]
        if role == "host":
            if not was_elected:
                return []
            self._elect()
            panels = self._panels()
            out: list[tuple[Any, dict]] = []
            if self.elected and panels:
                out.append((self.elected, _rp({"rp": 1, "k": "panel-present"})))
            elif not self.elected and panels:
                out.extend((p, _rp({"rp": 1, "k": "host-gone"})) for p in panels)
            out.extend(self._sync_active())
            return out
        return []

    # ── выборы активного хоста ───────────────────────────────────────────────
    def _on_ack(self, ws: Any, msg: dict) -> list[tuple[Any, dict]]:
        """Панель подтвердила, что видит состояние: запоминаем заголовок и срок.
        title/artist — строки, наружу отдаём только их и обрезанными;
        live/nolink — как панель сама видит свой мост (баннер «нет связи»
        снаружи не увидеть ни откуда, кроме самой панели)."""
        self.last_ack = {
            "panel": self.ids.get(ws, "?"),
            "title": str(msg.get("title") or "")[:200],
            "artist": str(msg.get("artist") or "")[:200],
            "have": bool(msg.get("have")),
            "live": bool(msg.get("live")),
            "nolink": bool(msg.get("nolink")),
            "at": time.monotonic(),
        }
        return []

    def status(self) -> dict:
        """Срез для GET /api/player-window/relay-status: жив ли мост с точки
        зрения сервера. last_panel_ack.age_s — сколько секунд назад панель
        в последний раз подтверждала показ (None — если не подтверждала)."""
        ack = dict(self.last_ack) if self.last_ack else None
        if ack:
            ack["age_s"] = round(time.monotonic() - ack.pop("at"), 1)
        return {
            "hosts": len(self._hosts()),
            "panels": len(self._panels()),
            "host_ids": [self.ids.get(h, "?") for h in self._hosts()],
            "panel_ids": [self.ids.get(p, "?") for p in self._panels()],
            "active_host": self.ids.get(self.elected) if self.elected else None,
            "playing": bool(self.hosts.get(self.elected, {}).get("playing"))
                       if self.elected else False,
            "last_panel_ack": ack,
        }

    def _on_hb(self, ws: Any, msg: dict) -> list[tuple[Any, dict]]:
        info = self.hosts.setdefault(ws, {"playing": False, "ts": 0})
        info["playing"] = bool(msg.get("playing"))
        ts = msg.get("ts")
        if isinstance(ts, (int, float)):
            info["ts"] = int(ts)
        self._elect()
        return self._sync_active()

    def _elect(self) -> Any | None:
        """Липкие выборы: играет новый хост — перехватывает; иначе держим
        последнего игравшего (даже на паузе); некому — самая свежая вкладка."""
        hosts = self._hosts()
        if not hosts:
            self.elected = None
            return None
        playing = [h for h in hosts if self.hosts.get(h, {}).get("playing")]
        if playing:
            self.elected = max(playing, key=lambda h: self.hosts[h].get("ts", 0))
        elif self.elected in hosts:
            pass                                       # прилипли к последнему игравшему
        else:
            self.elected = hosts[-1]                   # никто не играл — свежайшая вкладка
        return self.elected

    def _sync_active(self) -> list[tuple[Any, dict]]:
        """host-active только тем вкладкам, у кого статус сменился — чтобы они
        заводили/гасили цикл рассылки состояния (не сам heartbeat)."""
        out: list[tuple[Any, dict]] = []
        for h in self._hosts():
            on = h == self.elected
            if self._active_sent.get(h) != on:
                self._active_sent[h] = on
                out.append((h, _rp({"rp": 1, "k": "host-active", "on": 1 if on else 0})))
        return out

    def _hosts(self) -> list[Any]:
        return [h for h, r in self.roles.items() if r == "host"]

    def _panels(self) -> list[Any]:
        return [p for p, r in self.roles.items() if r == "panel"]
