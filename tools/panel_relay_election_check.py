"""Внешний плеер (трекер #37): выборы relay-хоста по активности — сценарии s1–s6.

Что доказывается (дефект 24.09 «внешний плеер не знает, что играет в Рипстере,
живёт своей жизнью»):

  s1  окно, открытое с играющей вкладки, показывает ЕЁ трек;
  s2  позже открытая МОЛЧАЛИВАЯ вкладка не перехватывает мост: окно по-прежнему
      показывает T1, и пауза из панели глушит именно T1;
  s3  перезагрузка T1 — окно живёт всё это время (переезжает на T2 и обратно),
      а после возобновления игры T1 окно следует за ним не позже 3 с;
  s4  рестарт сервера: все три стороны переподключаются сами, окно снова с T1;
  s5  окно открыто ДО загрузки играющей вкладки: без хостов панель честно
      показывает «Нет связи с Рипстером», а приехавший T1 сам начинает вещать
      (panel-present + host-active), окно восстанавливается;
  s6  переключение игры с T1 на T2 — окно следует за T2, T1 замолкает.

Что ЗДЕСЬ НАСТОЯЩЕЕ: сервер (tools/ui_spare_server.py — тот же app.py, его
маршруты, /ws и relay-ретранслятор rp_relay с новыми выборами), две главные
вкладки и панель (?transport=relay) в реальном headless-Chrome, настоящая
мышь по кнопке панели, живое <audio> с реальным воспроизведением.

Что ПОДСТАВЛЕНО (обе подмены названы): карточка очереди и пуск — прямой
записью в Preview.queue + el.play(), а не кликами по поиску (стенд не должен
зависеть от сети сервисов); «возобновление после перезагрузки» — повтор той
же инъекции (продукт помнит очередь, но авто-продолжение звука после reload
— не поведение стенда, а поведение пользователя «нажал снова»).

Порт 7805, живой 7799 не трогается. Браузер — только через tools/headless_reaper.

Запуск:  .venv\\Scripts\\python.exe tools/panel_relay_election_check.py
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

T1_TITLE = "Ripster Election · Track One"
T2_TITLE = "Ripster Election · Track Two"
ARTIST = "DJ Verify"

REPORT: list[tuple[str, bool, str]] = []
STEP = ["старт"]
TEMP = Path(os.environ.get("TEMP", "."))
SRV_LOG = TEMP / "panel_relay_election_server.log"


def say(tag: str, scenario: str, ok, detail: str = "") -> None:
    REPORT.append((f"{scenario} · {tag}", bool(ok), detail))
    print(f"[{'OK  ' if ok else 'FAIL'}] {scenario} · {tag} — {detail}", flush=True)


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


def silent_wav_uri(seconds: float = 12.0, rate: int = 8000) -> str:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(struct.pack("<" + "h" * int(seconds * rate), *([0] * int(seconds * rate))))
    return "data:audio/wav;base64," + base64.b64encode(buf.getvalue()).decode()


# ── минимальный CDP (тот же профиль, что в panel_relay_bbc_check) ────────────
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

    def click(self, selector: str) -> bool:
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
HOST_VIEW = r"""(() => ({
  ws_up: (typeof ws !== 'undefined' && ws && ws.readyState === 1) || false,
  transport: (typeof RP !== 'undefined' && RP.transport) || '',
  open: !!(typeof RP !== 'undefined' && RP.open),
  ready: !!(typeof RP !== 'undefined' && RP.ready),
  active: !!(typeof RP !== 'undefined' && RP.active),
  panelPresent: !!(typeof RP !== 'undefined' && RP.panelPresent),
  seq: (typeof RP !== 'undefined' && RP.seq) | 0,
  tabId: (typeof RP !== 'undefined' && RP.tabId) || '',
  paused: (document.getElementById('pp-audio')||{}).paused,
  title: (typeof Preview !== 'undefined' && Preview.queue && Preview.queue[Preview.idx|0]
          && Preview.queue[Preview.idx|0].title) || ''
}))()"""

PANEL_VIEW = r"""(() => ({
  RELAY: !!RELAY, live: !!B.live, nolink: !!B.nolink,
  banner_on: !!(document.getElementById('nolink')||{classList:{contains:()=>false}}).classList.contains('on'),
  banner_text: (document.getElementById('nolink')||{}).textContent || '',
  env: (typeof S !== 'undefined' && S.env) || '',
  seq: (B.state||{}).seq|0, engine: (B.state||{}).engine || '',
  have: (B.state||{}).have, paused: (B.state||{}).paused,
  title: (B.state && B.state.item && B.state.item.title) || '',
  mini_title: (document.getElementById('mini-title')||{}).textContent || ''
}))()"""

# Подмена пуска: карточка в очередь и el.play() — вместо поиска/кликов по
# сервису (стенд не должен зависеть от сети Apple/BBC).
JS_PLAY = r"""((url, title) => {
  const a = document.getElementById('pp-audio');
  if (!a) return {err: 'нет #pp-audio'};
  Preview.queue = [{service: 'local', local: true, id: 'verify-' + Date.now(),
                    title: title, artist: 'DJ Verify', cover: '', label: 'Verify',
                    duration: 12, url: url, full: true}];
  Preview.idx = 0;
  a.src = url; a.loop = true;
  return a.play().then(() => ({ok: true})).catch(e => ({err: String(e)}));
})"""

JS_PAUSE = r"""(() => { const a = document.getElementById('pp-audio'); if (a) a.pause(); })()"""


def host_ready(cdp: CDP) -> bool:
    return bool(cdp.wait("typeof rpState==='function' && typeof RP!=='undefined'"
                         " && (typeof ws!=='undefined' && ws && ws.readyState===1)", timeout=60))


def start_server(port: int):
    env = dict(os.environ)
    env.update({"RIPSTER_PORT": str(port), "RIPSTER_HOST": "127.0.0.1",
                "RIPSTER_LAUNCHER_LOG": str(TEMP / "panel_relay_election_launcher.log"),
                "PYTHONUNBUFFERED": "1"})
    out = open(SRV_LOG, "w", encoding="utf-8", errors="replace")
    srv = subprocess.Popen([sys.executable, str(ROOT / "tools" / "ui_spare_server.py")],
                           cwd=str(ROOT), env=env, stdout=out, stderr=subprocess.STDOUT)
    return srv, out


def wait_server(base: str, srv) -> float | None:
    t0 = time.time()
    while time.time() - t0 < 180:
        try:
            if requests.get(base + "/api/ping", timeout=2).status_code == 200:
                return time.time() - t0
        except Exception:
            pass
        if srv.poll() is not None:
            return None
        time.sleep(0.2)
    return None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=int(os.environ.get("RIPSTER_PORT") or 7805))
    ap.add_argument("--cdp-port", type=int, default=9434)
    args = ap.parse_args()

    base = f"http://127.0.0.1:{args.port}"
    cookie = owner_cookie()
    wav = silent_wav_uri()

    srv, out = start_server(args.port)
    tabs: list[CDP] = []
    try:
        up = wait_server(base, srv)
        say("стенд: сервер на запасном порту", "подготовка", up is not None,
            f"{base} за {up:.1f} с" if up else f"не поднялся, см. {SRV_LOG}")
        if up is None:
            return finish()

        with owned_browser(port=args.cdp_port, headless=True, profile_suffix="relay_elect",
                           extra_args=["--remote-allow-origins=*",
                                       "--autoplay-policy=no-user-gesture-required",
                                       "--disable-background-media-suspend",
                                       "--disable-backgrounding-occluded-windows",
                                       "--disable-background-timer-throttling",
                                       "--disable-renderer-backgrounding",
                                       "--window-size=1400,900"]) as br:
            pages = requests.get(f"http://127.0.0.1:{br['port']}/json", timeout=10).json()
            first = next(p for p in pages if p.get("type") == "page")
            t1 = CDP(first["webSocketDebuggerUrl"])
            t1.send("Page.enable"); t1.send("Runtime.enable"); t1.send("Network.enable")
            t1.send("Network.setCookie", {"name": "ripster-session", "value": cookie, "url": base})
            t1.send("Page.navigate", {"url": base + "/"})
            tabs.append(t1)

            STEP[0] = "T1 готова"
            say("T1: страница, /ws, panel_host", "подготовка", host_ready(t1),
                f"tabId={t1.ev(HOST_VIEW)['tabId']}")

            # ── s1: играет T1, окно открывается с него ────────────────────────
            STEP[0] = "s1: пуск на T1"
            r = t1.ev(f"({JS_PLAY})({json.dumps(wav)}, {json.dumps(T1_TITLE)})")
            t1.wait("document.getElementById('pp-audio') && !document.getElementById('pp-audio').paused",
                    timeout=15)
            STEP[0] = "s1: панель (?transport=relay)"
            panel = open_tab(br["port"], cookie, base + "/static/panel/index.html?transport=relay", base)
            tabs.append(panel)
            shown = panel.wait(f"B.live && B.state && B.state.item && B.state.item.title==={json.dumps(T1_TITLE)}",
                               timeout=40)
            pv, hv = panel.ev(PANEL_VIEW), t1.ev(HOST_VIEW)
            say("окно показывает трек играющей T1", "s1", bool(shown) and pv["title"] == T1_TITLE,
                f"title={pv['title']!r} engine={pv['engine']!r} live={pv['live']} host: open={hv['open']} ready={hv['ready']}"
                + (f", ошибка={r.get('err')}" if r.get("err") else ""))
            say("T1 приняла мост сама (hello панели)", "s1",
                hv["transport"] == "relay" and hv["open"] and hv["ready"],
                f"transport={hv['transport']!r} seq={hv['seq']}")

            # ── s2: позже открытая молчаливая вкладка НЕ перехватывает ────────
            STEP[0] = "s2: вторая вкладка (молчит)"
            t2 = open_tab(br["port"], cookie, base + "/", base)
            tabs.append(t2)
            say("T2: страница, /ws", "s2", host_ready(t2), f"tabId={t2.ev(HOST_VIEW)['tabId']}")
            time.sleep(3.0)                        # T2 успела зарегистрироваться и сходить hb
            pv, h2 = panel.ev(PANEL_VIEW), t2.ev(HOST_VIEW)
            say("окно по-прежнему показывает T1 (не пустоту T2)", "s2",
                pv["live"] and pv["title"] == T1_TITLE,
                f"title={pv['title']!r} seq={pv['seq']}")
            say("T2 узнала про окно, но НЕ стала мостом", "s2",
                h2["panelPresent"] is True and h2["active"] is False and h2["open"] is False,
                f"panelPresent={h2['panelPresent']} active={h2['active']} open={h2['open']}")
            STEP[0] = "s2: пауза кликом по панели"
            panel.ev("openPlayer && openPlayer()")
            panel.wait("!!document.getElementById('p-play')", timeout=15)
            clicked = panel.click("#p-play")
            paused_t1 = bool(t1.wait("document.getElementById('pp-audio').paused===true", timeout=15))
            h2b = t2.ev(HOST_VIEW)
            say("пауза из панели глушит T1, а не T2", "s2",
                clicked and paused_t1 and h2b["paused"] is not False,
                f"T1.paused={t1.ev(HOST_VIEW)['paused']} T2.paused={h2b['paused']}")
            panel.click("#p-play")
            resumed = bool(t1.wait("!document.getElementById('pp-audio').paused", timeout=15))
            say("второй клик вернул T1", "s2", resumed, f"T1.paused={t1.ev(HOST_VIEW)['paused']}")

            # ── s3: перезагрузка T1 ───────────────────────────────────────────
            STEP[0] = "s3: reload T1"
            t1.send("Page.navigate", {"url": base + "/"})
            # пока T1 перегружается, панель не должна зависать на вчерашнем
            # состоянии: мост переезжает на живую T2 — её состояние have=false
            # (ничего не играет) и растущий seq доказывают, что relay дышит.
            handed = bool(panel.wait("B.live && B.state && B.state.have===false", timeout=10))
            seq_a = panel.ev("(B.state||{}).seq|0")
            time.sleep(1.0)
            seq_b = panel.ev("(B.state||{}).seq|0")
            say("во время перезагрузки T1 панель жила: состояние шло от T2", "s3",
                handed and seq_b > seq_a,
                f"have=false от T2, seq {seq_a}→{seq_b}")
            t1_reloaded = host_ready(t1)
            t0 = time.time()
            t1.ev(f"({JS_PLAY})({json.dumps(wav)}, {json.dumps(T1_TITLE)})")   # «нажал снова»
            got = panel.wait(f"B.state && B.state.item && B.state.item.title==={json.dumps(T1_TITLE)}"
                             " && B.state.paused===false", timeout=15)
            t_recovered = (time.time() - t0) if got else None
            h1v, h2v = t1.ev(HOST_VIEW), t2.ev(HOST_VIEW)
            say("после перезагрузки T1 вернула мост и окно следует за ней", "s3",
                t1_reloaded and bool(got) and t_recovered is not None and t_recovered <= 3.0,
                f"восстановление за {t_recovered:.1f} с (норма ≤3 с)" if t_recovered is not None
                else "окно не вернулось за 15 с")
            say("T2 честно отдала мост и молчит", "s3",
                h2v["open"] is False and h1v["active"] is True,
                f"T2 open={h2v['open']} active={h2v['active']}; T1 active={h1v['active']}")

            # ── s4: рестарт сервера ───────────────────────────────────────────
            STEP[0] = "s4: рестарт сервера"
            kill_tree(srv.pid)
            time.sleep(1.0)
            out.close()
            srv, out = start_server(args.port)
            up2 = wait_server(base, srv)
            say("сервер поднялся заново", "s4", up2 is not None,
                f"за {up2:.1f} с" if up2 else f"не поднялся, см. {SRV_LOG}")
            got = panel.wait(f"B.live && B.state && B.state.item && B.state.item.title==={json.dumps(T1_TITLE)}",
                             timeout=60)
            # /ws у главной — на цикле автопереподключения раз в 2 с: ждём его,
            # а не замеряем одним мгновенным сном (иначе это гонка стенда).
            ws_back = bool(t1.wait("(typeof ws!=='undefined' && ws && ws.readyState===1)", timeout=30))
            pv, h1v = panel.ev(PANEL_VIEW), t1.ev(HOST_VIEW)
            say("после рестарта все три стороны переговорились и окно снова с T1", "s4",
                bool(got) and ws_back,
                f"B.live={pv['live']} title={pv['title']!r} seq={pv['seq']} host ws_up={h1v['ws_up']} active={h1v['active']}")

            # ── s5: окно открыто ДО загрузки играющей вкладки ─────────────────
            STEP[0] = "s5: окно раньше хоста"
            t1.send("Page.navigate", {"url": "about:blank"})
            t2.send("Page.navigate", {"url": "about:blank"})
            panel.send("Page.navigate", {"url": "about:blank"})
            time.sleep(1.5)
            panel2 = open_tab(br["port"], cookie, base + "/static/panel/index.html?transport=relay", base)
            tabs.append(panel2)
            nolink = bool(panel2.wait("B.nolink && "
                                      "document.getElementById('nolink').classList.contains('on')", timeout=15))
            pv2 = panel2.ev(PANEL_VIEW)
            txt_ok = panel2.ev("document.getElementById('nolink').textContent === t('m.host.nolink')")
            say("без хостов окно честно говорит «Нет связи с Рипстером»", "s5",
                nolink and bool(txt_ok),
                f"nolink={pv2['nolink']} баннер={pv2['banner_text']!r}")
            t1 = open_tab(br["port"], cookie, base + "/", base)
            tabs.append(t1)
            ready5 = host_ready(t1)
            t0 = time.time()
            # регистрация роли и ответ panel-present/host-active — один сетевой
            # round-trip; ждём его, а не снимаем мгновенный срез (иначе это
            # гонка стенда: проверка успевает прочитать до ответа сервера).
            adopted = bool(t1.wait("RP.panelPresent===true && RP.active===true && RP.open===true",
                                   timeout=10))
            dt5 = time.time() - t0
            h1v = t1.ev(HOST_VIEW)
            say("приехавшая вкладка САМА узнала про окно (panel-present) и стала мостом", "s5",
                ready5 and adopted and dt5 <= 3.0,
                f"за {dt5:.1f} с (норма ≤3): panelPresent={h1v['panelPresent']} "
                f"active={h1v['active']} open={h1v['open']} ready={h1v['ready']}")
            t1.ev(f"({JS_PLAY})({json.dumps(wav)}, {json.dumps(T1_TITLE)})")
            recovered = bool(panel2.wait(
                f"B.state && B.state.item && B.state.item.title==={json.dumps(T1_TITLE)}", timeout=15))
            pv2 = panel2.ev(PANEL_VIEW)
            say("как только T1 заиграла — окно показало её и сняла баннер", "s5",
                recovered and not pv2["nolink"] and not pv2["banner_on"],
                f"title={pv2['title']!r} nolink={pv2['nolink']} live={pv2['live']}")

            # ── s6: игра переезжает с T1 на T2 ────────────────────────────────
            STEP[0] = "s6: переезд игры на T2"
            t2 = open_tab(br["port"], cookie, base + "/", base)
            tabs.append(t2)
            say("T2 готова", "s6", host_ready(t2), f"tabId={t2.ev(HOST_VIEW)['tabId']}")
            t1.ev(JS_PAUSE)                          # пользователь переключился: T1 пауза
            t2.ev(f"({JS_PLAY})({json.dumps(wav)}, {json.dumps(T2_TITLE)})")
            t0 = time.time()
            got = panel2.wait(f"B.state && B.state.item && B.state.item.title==={json.dumps(T2_TITLE)}"
                              " && B.state.paused===false", timeout=10)
            dt = time.time() - t0 if got else None
            h1v, h2v = t1.ev(HOST_VIEW), t2.ev(HOST_VIEW)
            pv2 = panel2.ev(PANEL_VIEW)
            say("окно последовало за T2", "s6",
                bool(got) and dt is not None and dt <= 3.0,
                f"переезд за {dt:.1f} с (норма ≤3 с), title={pv2['title']!r}" if dt is not None
                else "окно не переехало за 10 с")
            say("T1 замолчала по-настоящему (deactivate, а не по своей воле)", "s6",
                h1v["open"] is False and h2v["open"] is True and h2v["active"] is True,
                f"T1 open={h1v['open']}; T2 open={h2v['open']} active={h2v['active']}")
    except Exception as exc:                     # прогон честен и в падении
        import traceback
        say("прогон прервался исключением", STEP[0], False,
            f"{type(exc).__name__}: {exc}")
        print("     цели браузера:", diagnose(args.cdp_port), flush=True)
        traceback.print_exc()
    finally:
        for c in tabs:
            try:
                c.close()
            except Exception:
                pass
        try:
            if srv.poll() is None:
                kill_tree(srv.pid)
        except Exception:
            pass
        try:
            out.close()
        except Exception:
            pass
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
