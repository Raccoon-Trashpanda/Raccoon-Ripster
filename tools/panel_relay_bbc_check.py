"""Внешний плеер (трекер #37): relay-панель + BBC — проверка в двух вкладках.

Что доказывается. Хозяин сидит в обычном браузере, поэтому раньше панель жила
ДОКОМ внутри той же страницы. Здесь проверяется то, что должно быть вместо
дока: главная вкладка остаётся единственным владельцем звука, а панель —
отдельная страница (`?transport=relay`), которой сервер перекладывает сообщения
через общий `/ws`; на BBC панель показывает карточку эфира и её пауза
действительно глушит `<audio id="bbc-audio">` в главной вкладке.

Что здесь НАСТОЯЩЕЕ: сервер (`tools/ui_spare_server.py` — тот же app.py, его
маршруты, middleware авторизации, /ws и ретранслятор rp_relay), главная
страница, панель, мост `RP_CMD` → `previewToggle` → `bbcTogglePlay`, живой
`<audio id="bbc-audio">` с реальным воспроизведением, настоящая мышью нажатая
кнопка панели.

Что ПОДСТАВЛЕНО (и почему): карточка эфира — `BBC.pid/title/artist/art`
вручную вместо `/api/bbc/stream`, потому что проверка не должна зависеть от
того, идёт ли сейчас прямой эфир; флаг live — у элемента принудительно
`duration === Infinity`, ровно как его даёт HLS-эфир. При этом second-фаза
(он-деманд) проверяется на конечном `duration`: скриб, позиция, «нет бейджа
LIVE».

Порт 7805, живой 7799 не трогается. Браузер — только через общий сборщик
`tools/headless_reaper` (своё дерево, снос по маркеру, никогда не по имени
образа).

Запуск:  .venv\\Scripts\\python.exe tools/panel_relay_bbc_check.py
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import hmac
import io
import json
import os
import struct
import subprocess
import sys
import time
import wave
from pathlib import Path

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(errors="replace")
    except Exception:
        pass

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))

import requests                                        # noqa: E402
import websocket                                       # noqa: E402  pip: websocket-client
import yaml                                            # noqa: E402

from headless_reaper.reaper import owned_browser, kill_tree  # noqa: E402

TITLE = "Ripster Relay Check · Mix 24.09"
ARTIST = "DJ Verify"
ART = "https://example.com/relay-check-cover.jpg"
PID = "p0mrf1le-verify"

REPORT: list[tuple[str, bool, str]] = []
STEP = ["старт"]          # где именно встал прогон — нужно для честного отчёта
TEMP = Path(os.environ.get("TEMP", "."))
SRV_LOG = TEMP / "panel_relay_bbc_server.log"


def say(tag: str, ok, detail: str = "") -> None:
    REPORT.append((tag, bool(ok), detail))
    print(f"[{'OK  ' if ok else 'FAIL'}] {tag} — {detail}", flush=True)


# ── кука хозяина: тот же HMAC, что ставит приложение ─────────────────────────
def owner_cookie() -> str:
    cfg = yaml.safe_load((ROOT / "config.yaml").read_text(encoding="utf-8")) or {}
    secret = str(cfg.get("session-secret") or "")
    if len(secret) < 32:
        raise RuntimeError("в config.yaml нет session-secret — песочный сервер "
                           "придётся логинить паролем, а этого проверка не делает")
    issued = int(time.time())
    mac = hmac.new(secret.encode(), str(issued).encode(), hashlib.sha256).hexdigest()
    return f"{issued}.{mac}"


# ── тишина, которую реально играет элемент ───────────────────────────────────
def silent_wav_uri(seconds: float = 12.0, rate: int = 8000) -> str:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(struct.pack("<" + "h" * int(seconds * rate), *([0] * int(seconds * rate))))
    return "data:audio/wav;base64," + base64.b64encode(buf.getvalue()).decode()


# ── минимальный CDP ──────────────────────────────────────────────────────────
class CDP:
    def __init__(self, ws_url: str):
        self.ws = websocket.create_connection(ws_url, timeout=60, max_size=None)
        self.id = 0

    def send(self, method: str, params=None, timeout: float = 60):
        self.id += 1
        mid = self.id
        self.ws.send(json.dumps({"id": mid, "method": method, "params": params or {}}))
        t0 = time.time()
        while True:
            msg = json.loads(self.ws.recv())
            if msg.get("id") == mid:
                if "error" in msg:
                    raise RuntimeError(f"{method}: {json.dumps(msg['error'])[:200]}")
                return msg.get("result", {})
            if time.time() - t0 > timeout:
                raise TimeoutError(method)

    def ev(self, expr: str):
        r = self.send("Runtime.evaluate", {
            "expression": expr, "returnByValue": True, "awaitPromise": True})
        if r.get("exceptionDetails"):
            raise RuntimeError("JS: " + json.dumps(r["exceptionDetails"])[:400])
        return (r.get("result") or {}).get("value")

    def wait(self, expr: str, timeout: float = 40.0, every: float = 0.25):
        """Готовность определяем опросом, а не фиксированным сном (см. skill)."""
        t0 = time.time()
        while time.time() - t0 < timeout:
            try:
                v = self.ev(expr)
            except Exception:
                v = None
            if v:
                return v
            time.sleep(every)
        return None

    def hit_probe(self, selector: str) -> str:
        """Что реально стоит под центром элемента: клик «мимо» надо видеть, а не
        принимать за «команда не дошла»."""
        return self.ev(f"(() => {{const e=document.querySelector({json.dumps(selector)});"
                       f"if(!e)return 'нет элемента';const r=e.getBoundingClientRect();"
                       f"const x=r.left+r.width/2, y=r.top+r.height/2;"
                       f"const h=document.elementFromPoint(x,y);"
                       f"return ((h&&(h.id||h.className))||'—')+' @'+Math.round(x)+','+Math.round(y)"
                       f"+(h===e?' (сам)':' (не он)');}})()") or "?"

    def click(self, selector: str) -> bool:
        """Настоящий жест мышью: программный вызов onclick кое-что не переживает."""
        box = self.ev(f"(() => {{const e=document.querySelector({json.dumps(selector)});"
                      f"if(!e)return null;const r=e.getBoundingClientRect();"
                      f"return {{x:r.left+r.width/2,y:r.top+r.height/2}};}})()")
        if not box:
            return False
        for typ in ("mousePressed", "mouseReleased"):
            self.send("Input.dispatchMouseEvent",
                      {"type": typ, "x": box["x"], "y": box["y"], "button": "left",
                       "clickCount": 1})
        return True

    def close(self):
        try:
            self.ws.close()
        except Exception:
            pass


def diagnose(cdp_port: int) -> str:
    """Что живёт в браузере в момент падения: цели, их URL и тип."""
    try:
        pages = requests.get(f"http://127.0.0.1:{cdp_port}/json", timeout=5).json()
        return "; ".join(f"{p.get('type')}:{(p.get('url') or '')[:60]}" for p in pages) or "пусто"
    except Exception as exc:
        return f"список целей недоступен: {type(exc).__name__}: {exc}"


def open_tab(cdp_port: int, cookie: str, url: str, base: str) -> CDP:
    t = requests.put(f"http://127.0.0.1:{cdp_port}/json/new?about:blank", timeout=10).json()
    cdp = CDP(t["webSocketDebuggerUrl"])
    cdp.send("Page.enable")
    cdp.send("Runtime.enable")
    cdp.send("Network.enable")
    cdp.send("Network.setCookie", {"name": "ripster-session", "value": cookie, "url": base})
    cdp.send("Page.navigate", {"url": url})
    return cdp


# ── зонды ────────────────────────────────────────────────────────────────────
HOST_VIEW = r"""(() => {
  const el = document.getElementById('bbc-audio');
  return { ws_up: (typeof ws !== 'undefined' && ws && ws.readyState === 1) || false,
           transport: (typeof RP !== 'undefined' && RP.transport) || '',
           open: !!(typeof RP !== 'undefined' && RP.open),
           ready: !!(typeof RP !== 'undefined' && RP.ready),
           seq: (typeof RP !== 'undefined' && RP.seq) | 0,
           mode: (typeof Preview !== 'undefined' && Preview.mode) || '',
           paused: el ? !!el.paused : null, pos: el ? el.currentTime : null,
           volume: el ? el.volume : null,
           dur: (function(){ try { return isFinite(el.duration) ? el.duration : 'inf'; }
                              catch (e) { return 'nan'; } })(),
           pp_play: (document.getElementById('pp-play')||{}).textContent || '' };
})()"""

PANEL_VIEW = r"""(() => {
  const mt = document.getElementById('mini-title');
  return { RELAY: !!RELAY, live: !!B.live, env: (typeof S !== 'undefined' && S.env) || '',
           caps_relay: !!B.caps.relay, seq: (B.state||{}).seq|0,
           engine: (B.state||{}).engine || '',
           item: B.state && B.state.item ? {
             title: B.state.item.title, artist: B.state.item.artist,
             cover: B.state.item.cover, service: B.state.item.service,
             live: !!B.state.item.live, dur: B.state.item.duration } : null,
           paused: (B.state||{}).paused, pos: (B.state||{}).pos,
           mini_title: mt ? mt.textContent : '',
           mini_live: !!(mt && mt.querySelector('.live')),
           mini_cover: (document.getElementById('mini-cover')||{}).src || '',
           mini_play: (document.getElementById('mini-play')||{}).textContent || '',
           p_play: (document.getElementById('p-play')||{}).textContent || '',
           own_audio: (function(){ return {paused: !!T.el.paused, has_src: !!T.el.src}; })() };
})()"""

# Подмена эфира: карточка вручную (эфир мог не идти), элемент — настоящий.
INJECT_BBC = r"""((pid, title, artist, art, src, live) => {
  const el = document.getElementById('bbc-audio');
  if (!el) return {err: 'нет #bbc-audio (вьюха BBC не загрузилась)'};
  Preview.mode = 'bbc'; Preview.queue = []; Preview.idx = -1;
  BBC.pid = pid; BBC.title = title; BBC.artist = artist; BBC.art = art;
  BBC.duration = live ? 0 : 12;

  if (live) {
    // ровно то, что отдаёт HLS-эфир: duration === Infinity
    Object.defineProperty(el, 'duration', {get: () => Infinity, configurable: true});
    // currentTime НЕ подделываем: с фиктивной позицией play() после паузы ищет
    // 1234 с в 12-секундном файле и эфир не возвращается — это стенд, а не продукт.
    el.src = src;
    // НЕ muted: тихий трек Chrome считает «видео без звука» и в скрытой
    // вкладке глушит сам ради энергии — проверка тогда принимает это за
    // «команда из панели не дошла». Autoplay здесь разрешён флагом
    // --autoplay-policy=no-user-gesture-required.
    return el.play().then(() => ({ok: true})).catch(e => ({err: String(e)}));
  }
  try { delete el.duration; } catch (e) {}
  el.src = src; el.loop = true;
  return el.play().then(() => ({ok: true})).catch(e => ({err: String(e)}));
})"""


def host_open_bbc_view(cdp: CDP, base: str) -> bool:
    """Главная вкладка «как у хозяина»: страница приложения, вьюха BBC (из неё
    штатно приезжает views/bbc.html с <audio id="bbc-audio">) и открытый /ws."""
    if not cdp.wait("typeof rpState==='function' && typeof RP!=='undefined'", timeout=60):
        return False
    cdp.ev("showView('bbc', document.querySelector('.nav-item[data-view=bbc]'))")
    if not cdp.wait("!!document.getElementById('bbc-audio')", timeout=40):
        return False
    return bool(cdp.wait("(typeof ws!=='undefined' && ws && ws.readyState===1) || false", timeout=30))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=int(os.environ.get("RIPSTER_PORT") or 7805))
    ap.add_argument("--cdp-port", type=int, default=9433)
    args = ap.parse_args()

    base = f"http://127.0.0.1:{args.port}"
    cookie = owner_cookie()
    wav = silent_wav_uri()

    env = dict(os.environ)
    env.update({"RIPSTER_PORT": str(args.port), "RIPSTER_HOST": "127.0.0.1",
                "RIPSTER_LAUNCHER_LOG": str(TEMP / "panel_relay_bbc_launcher.log"),
                "PYTHONUNBUFFERED": "1"})
    out = open(SRV_LOG, "w", encoding="utf-8", errors="replace")
    srv = subprocess.Popen([sys.executable, str(ROOT / "tools" / "ui_spare_server.py")],
                           cwd=str(ROOT), env=env, stdout=out, stderr=subprocess.STDOUT)
    host = panel = None
    try:
        # ── сервер ────────────────────────────────────────────────────────────
        up = None
        t0 = time.time()
        while time.time() - t0 < 180:
            try:
                if requests.get(base + "/api/ping", timeout=2).status_code == 200:
                    up = time.time() - t0
                    break
            except Exception:
                pass
            if srv.poll() is not None:
                break
            time.sleep(0.2)
        say("стенд: сервер на запасном порту", up is not None,
            f"{base} за {up:.1f} с" if up else f"не поднялся, см. {SRV_LOG}")
        if up is None:
            return finish()

        with owned_browser(port=args.cdp_port, headless=True, profile_suffix="relay_bbc",
                           extra_args=["--remote-allow-origins=*", "--autoplay-policy=no-user-gesture-required",
                                       # фоновая вкладка headless-прогона — не «фоновая» для
                                       # экономии энергии: иначе Chrome сам глушит #bbc-audio
                                       # и проверка пишет «пауза не дошла», хотя глушил браузер
                                       "--disable-background-media-suspend",
                                       "--disable-backgrounding-occluded-windows",
                                       "--disable-background-timer-throttling",
                                       "--disable-renderer-backgrounding",
                                       "--window-size=1400,900"]) as br:
            pages = requests.get(f"http://127.0.0.1:{br['port']}/json", timeout=10).json()
            first = next(p for p in pages if p.get("type") == "page")
            host = CDP(first["webSocketDebuggerUrl"])
            host.send("Page.enable"); host.send("Runtime.enable"); host.send("Network.enable")
            host.send("Network.setCookie", {"name": "ripster-session", "value": cookie, "url": base})
            host.send("Page.navigate", {"url": base + "/"})
            say("главная вкладка открыта под хозяином", host_open_bbc_view(host, base),
                "app.js + panel_host.js + вьюха BBC + /ws")

            # вьюха BBC — штатным кликом по навигации: из неё приедет views/bbc.html
            hv = host.ev(HOST_VIEW)
            say("реальный <audio id=\"bbc-audio\"> из вьюхи BBC", hv["mode"] != "",
                "views/bbc.html подтянут, элемент на странице")
            say("главная вкладка держит /ws", hv["ws_up"], f"readyState=1, режим {hv['mode']}")

            STEP[0] = "фаза LIVE: инъекция эфира в главную вкладку"
            # ── фаза LIVE ─────────────────────────────────────────────────────
            r = host.ev(f"({INJECT_BBC})({json.dumps(PID)}, {json.dumps(TITLE)}, "
                        f"{json.dumps(ARTIST)}, {json.dumps(ART)}, {json.dumps(wav)}, true)")
            hv = host.ev(HOST_VIEW)
            say("BBC-эфир играет в главной вкладке", hv["paused"] is False and not r.get("err"),
                f"duration={hv['dur']}, позиция={hv['pos']}" + (f", ошибка={r.get('err')}" if r.get("err") else ""))

            STEP[0] = "открытие панели (?transport=relay) второй вкладкой"
            panel = open_tab(br["port"], cookie, base + "/static/panel/index.html?transport=relay", base)
            live_ok = bool(panel.wait("B.live && B.caps.relay && B.state && B.state.item "
                                      "&& B.state.item.title===%s" % json.dumps(TITLE), timeout=40))
            pv = panel.ev(PANEL_VIEW)
            hv = host.ev(HOST_VIEW)
            say("панель в relay-режиме и без хоста в-process", pv["RELAY"] and "relay" in pv["env"],
                f"env={pv['env']!r}, caps.relay={pv['caps_relay']}")
            say("хост принял панель через /ws (RP.transport=relay)",
                hv["transport"] == "relay" and hv["open"] and hv["ready"],
                f"transport={hv['transport']!r} open={hv['open']} ready={hv['ready']} seq={hv['seq']}")
            say("панель видит движок bbc", pv["engine"] == "bbc",
                f"engine={pv['engine']!r}, seq={pv['seq']}")
            it = pv["item"] or {}
            say("карточка эфира доехала целиком",
                it.get("title") == TITLE and it.get("artist") == ARTIST
                and it.get("service") == "bbc" and it.get("cover") == ART,
                f"item={json.dumps(it, ensure_ascii=False)[:180]}")
            say("LIVE-эфир: бейдж LIVE и заголовок в панели",
                pv["mini_live"] and TITLE in pv["mini_title"] and it.get("live") is True,
                f"mini-title={pv['mini_title']!r}")
            say("обложка эфира в панели", ART in pv["mini_cover"], f"src={pv['mini_cover'][:80]!r}")
            say("панель НЕ вторая колонка звука", pv["own_audio"]["paused"] and not pv["own_audio"]["has_src"],
                f"её собственный audio: paused={pv['own_audio']['paused']}, src={pv['own_audio']['has_src']}")

            STEP[0] = "пауза кликом по кнопке панели"
            # пауза НАСТОЯЩИМ кликом по кнопке панели
            panel.ev("openPlayer && openPlayer()")
            panel.wait("!!document.getElementById('p-play')", timeout=15)   # экран ещё открывается
            where = panel.hit_probe("#p-play")
            clicked = panel.click("#p-play")
            t_click = time.time()
            lag = None
            for _ in range(200):                      # 20 с: ждём не «когда успели», а «было ли вообще»
                hv = host.ev(HOST_VIEW)
                if hv["paused"]:
                    lag = time.time() - t_click
                    break
                time.sleep(0.1)
            pv = panel.ev(PANEL_VIEW)
            say("пауза из панели глушит #bbc-audio главной вкладки",
                clicked and hv["paused"] is True,
                # лаг измеряется опросом раз в ~100 мс плюс сам CDP-запрос,
                # поэтому честный результат — «не больше», а не точные миллисекунды;
                # если поймали первым же запросом, «0 мс» было бы хвастовством
                f"главная paused={hv['paused']}"
                + (f" уже к первому опросу ({lag*1000:.0f} мс — это цена самого CDP-запроса)"
                   if lag is not None and lag < 0.02
                   else f" не позже {lag*1000:.0f} мс" if lag is not None
                   else " и не в паузе за 20 с")
                + f"; клик по {where!r}; иконка панели {pv['p_play']!r}, state.paused={pv['paused']!r}")
            say("иконка панели отчиталась о паузе",
                bool(panel.wait("B.state && B.state.paused===true", timeout=15))
                and (pv := panel.ev(PANEL_VIEW))["p_play"] == "▶",
                f"state.paused={pv['paused']!r}, p-play={pv['p_play']!r}, mini-play={pv['mini_play']!r}")

            clicked = panel.click("#p-play")
            resumed = bool(panel.wait("B.state && B.state.paused===false", timeout=15))
            hv = host.ev(HOST_VIEW)
            el = host.ev("(() => {const a=document.getElementById('bbc-audio');"
                         "return {paused: a.paused, ended: a.ended, src: (a.currentSrc||'').slice(0,24),"
                         "readyState: a.readyState, networkState: a.networkState,"
                         "err: a.error ? a.error.code : 0, t: a.currentTime};})()")
            say("повторный клик вернул эфир", clicked and resumed and hv["paused"] is False,
                f"панель paused={panel.ev('B.state.paused')!r}, главная paused={hv['paused']}, "
                f"кнопка главной {hv['pp_play']!r}, элемент={json.dumps(el)}")

            # ── фаза ОН-ДЕМАНД: главная Перезагружена (чистый <audio>, конечная
            # длительность) — это же проверка того, что relay переживает перезагрузку
            # хоста: панель обязана принять нового хоста без перезагрузки себя.
            panel.wait("B.live && B.state && B.state.paused===false", timeout=15)
            STEP[0] = "перезагрузка главной вкладки (проверка живучести relay)"
            host.send("Page.navigate", {"url": base + "/"})
            reloaded = host_open_bbc_view(host, base)
            adopted = bool(panel.wait("B.live && B.caps.relay", timeout=40))
            hv = host.ev(HOST_VIEW)
            say("relay пережил перезагрузку главной: панель приняла нового хоста",
                reloaded and adopted and hv["transport"] == "relay" and hv["ready"],
                f"B.live={panel.ev('!!B.live')}, caps.relay={panel.ev('!!B.caps.relay')}, "
                f"transport={hv['transport']!r}, ready={hv['ready']}")

            r = host.ev(f"({INJECT_BBC})({json.dumps(PID)}, {json.dumps(TITLE)}, "
                        f"{json.dumps(ARTIST)}, {json.dumps(ART)}, {json.dumps(wav)}, false)")
            ondemand_ok = bool(panel.wait(
                "B.state && B.state.item && B.state.item.live===false "
                "&& B.state.item.duration>0 && B.state.pos>0", timeout=30))
            pv = panel.ev(PANEL_VIEW)
            hv = host.ev(HOST_VIEW)
            it = pv["item"] or {}
            say("он-деманд: конечная длительность дошла, бейджа LIVE нет",
                ondemand_ok and it.get("dur", 0) > 0 and not pv["mini_live"],
                f"dur={it.get('dur')}, live-бейдж={pv['mini_live']}, ошибка={r.get('err')}")
            pos0 = hv["pos"]
            time.sleep(1.2)
            pos1 = host.ev(HOST_VIEW)["pos"]
            say("позиция ticking: часы главной вкладки идут",
                isinstance(pos0, (int, float)) and isinstance(pos1, (int, float)) and pos1 > pos0,
                f"{pos0:.2f}с → {pos1:.2f}с (реальное воспроизведение)")

            panel.ev("rpSend({k:'cmd',cmd:'seek',arg:{sec:1.4}})")
            seeked = bool(host.wait("document.getElementById('bbc-audio').currentTime>1.2", timeout=15))
            say("скриб из панели переставил #bbc-audio", seeked,
                f"currentTime={host.ev('document.getElementById(\"bbc-audio\").currentTime'):.2f}с")

            panel.ev("rpSend({k:'cmd',cmd:'volume',arg:{v:0.25}})")
            vol_ok = bool(host.wait("Math.abs(document.getElementById('bbc-audio').volume-0.25)<0.01",
                                    timeout=15))
            say("громкость из панели дошла до bbc-audio", vol_ok,
                f"volume={host.ev('document.getElementById(\"bbc-audio\").volume')}")

            STEP[0] = "смерть хоста: панель должна сказать правду"
            # панель обязана говорить вслух, если хозяин исчез
            host.send("Page.navigate", {"url": "about:blank"})
            gone = bool(panel.wait("!B.live", timeout=25))
            pv = panel.ev(PANEL_VIEW)
            say("хост умер — панель не притворяется живой", gone and not pv["live"],
                f"B.live={pv['live']}")
    except Exception as exc:                     # прогон честен и в падении
        import traceback
        say("прогон прервался исключением", False,
            f"на шаге «{STEP[0]}»: {type(exc).__name__}: {exc}")
        print("     цели браузера:", diagnose(args.cdp_port), flush=True)
        traceback.print_exc()
    finally:
        for c in (panel, host):
            if c:
                try:
                    c.close()
                except Exception:
                    pass
        try:
            if srv.poll() is None:
                kill_tree(srv.pid)
        except Exception:
            pass
        out.close()
    return finish()


def finish() -> int:
    bad = [tag for tag, ok, _ in REPORT if not ok]
    print("\n" + "—" * 62)
    print(f"проверок: {len(REPORT)}, из них FAIL: {len(bad)}")
    for tag in bad:
        print("  FAIL:", tag)
    print("лог песочного сервера:", SRV_LOG)
    return 1 if bad or not REPORT else 0


if __name__ == "__main__":
    sys.exit(main())
