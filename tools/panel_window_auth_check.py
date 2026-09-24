"""Внешний плеер: проверка НАСТОЯЩИМ ОКНОМ (24.09.2026).

Чем этот стенд отличается от panel_relay_election_check.py — и зачем он вовсе.
Тот стенд мерил relay-мост ВКЛАДКАМИ, открытыми в одном браузере: вкладки
делят куку хозяина, поэтому /ws у них принимался и панель регистрировалась.
Реальное окно «Внешний плеер» — отдельный процесс pywebview/WebView2 со своим
профилем, куки в нём нет, и рукопожатие /ws отвергается (403). Так окно
оставалось мёртвым при зелёных вкладках. Здесь проверяется ровно то, что
вкладками не проверялось: НАСТОЯЩЕЕ `python -m ripster.player_window`.

Что доказывается (s0–s7):
  s1 корневая причина измерена: /ws без куки — отвергнут, с хозяйской — принят;
  s2 кнопка поднимает живой отдельный процесс окна;
  s3 сервер ВИДИТ панель в релее (relay-status: hosts/panels/active_host);
  s4 состояние дошло и показано: ack панели = заголовок играющего трека;
  s5 сама панель сообщает из окна: мост жив, баннера «нет связи» нет;
  s6 НАСТОЯЩЕЕ нажатие Space в настоящем окне ставит хост на паузу, второе —
     снимает (то есть мост работает в обе стороны из окна);
  s7 разовый пропуск не попал в логи окна и сервера; окно закрылось по просьбе
     и вошло заново — по новому пропуску, а не по остатку профиля.

Честные подмены стенда (названы и в отчёте):
  • пуск трека — инъекцией в вкладку-хост (Preview.queue + el.play() на
    silent-wav), как в election-стенде: стенд не должен зависеть от сети
    Deezer/Apple;
  • «что видит окно» берётся из ack самой панели (live/nolink/title): рисует
    тот же код panel.js, что и вкладка, но доказательство — от самого окна.

Почему не CDP внутри окна: WebView2 сюда аргумент --remote-debugging-port через
WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS не доходит (pywebview 6.2 задаёт
AdditionalBrowserArguments сам), а на машине в это время висят ЧУЖИЕ отладочные
порты — цепляться к ним означало бы кликать по настоящему браузеру владельца.
Поэтому нажатие настоящее, системное: окну отдаётся фокус, шлётся Space, фокус
возвращается прежнему окну. Если фокус взять не удалось, s6 падает честным
FAIL — подмены вкладкой нет.

Сторож reauth здесь не запускается: ui_spare_server подменяет lifespan целиком,
а задача сторожа живёт именно в lifespan app.py — провод проверяется статически
в tests/test_player_window_auth.py.

Окно на время прогона реально видно на экране — это ожидаемо, в finally оно
закрывается. Боевой сервер 7799 не трогается: всё на запасном порту.

    .venv\\Scripts\\python.exe tools/panel_window_auth_check.py
    .venv\\Scripts\\python.exe tools/panel_window_auth_check.py --port 7807
"""
from __future__ import annotations

import argparse
import ctypes
import json
import os
import shutil
import subprocess
import sys
import time
from ctypes import wintypes
from pathlib import Path

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(errors="replace")
    except Exception:
        pass

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))

import requests                                  # noqa: E402
import websocket                                 # noqa: E402
from headless_reaper.reaper import owned_browser, kill_tree  # noqa: E402

import panel_relay_election_check as el          # noqa: E402
from ripster import player_window as pw          # noqa: E402

PORT = int(os.environ.get("RIPSTER_PORT") or 7805)   # для Origin; правится в main
REPORT: list[tuple[str, bool, str]] = []
STEP = ["старт"]
TEMP = Path(os.environ.get("TEMP", "."))
SRV_LOG = TEMP / "panel_window_auth_server.log"
TRACK = "Ripster Real Window · Nightcall"


def say(scenario: str, ok, detail: str = "") -> None:
    REPORT.append((scenario, bool(ok), detail))
    print(f"[{'OK  ' if ok else 'FAIL'}] {scenario} — {detail}", flush=True)


def start_server(port: int):
    """Запасный сервер стенда: настоящий app.py, фоновые циклы выключены."""
    env = dict(os.environ)
    env.update({
        "RIPSTER_PORT": str(port), "RIPSTER_HOST": "127.0.0.1",
        "RIPSTER_LAUNCHER_LOG": str(TEMP / "panel_window_auth_launcher.log"),
        "PYTHONUNBUFFERED": "1",
    })
    out = open(SRV_LOG, "w", encoding="utf-8", errors="replace")
    srv = subprocess.Popen([sys.executable, str(ROOT / "tools" / "ui_spare_server.py")],
                           cwd=str(ROOT), env=env, stdout=out, stderr=subprocess.STDOUT)
    return srv, out


def sess(cookie: str) -> requests.Session:
    """Сессия «как браузер»: с Origin. Без него прослойка CSRF считает запрос
    не-браузерным, а на коробке с включённым удалённым доступом такой POST
    отсекается (403 «Cross-site request blocked») — окно-то Origin шлёт."""
    s = requests.Session()
    s.cookies.set("ripster-session", cookie)
    s.headers["Origin"] = f"http://127.0.0.1:{PORT}"
    return s


def ws_open(base: str, cookie: str | None, timeout: float = 8.0) -> tuple[bool, str]:
    """Рукопожатие /ws как есть: без куки сервер обязан отказать (403),
    с хозяйской — пустить. Тот же путь, что у окна."""
    url = base.replace("http://", "ws://") + "/ws"
    hdr = {"Cookie": f"ripster-session={cookie}"} if cookie else None
    try:
        ws = websocket.create_connection(url, header=hdr, timeout=timeout)
    except Exception as e:
        return False, f"{type(e).__name__}: {str(e)[:120]}"
    try:
        first = json.loads(ws.recv())
    except Exception as e:
        ws.close()
        return False, f"без init-кадра: {type(e).__name__}"
    ws.close()
    return True, f"первый кадр type={first.get('type')}"


def relay_status(s: requests.Session, base: str) -> dict:
    r = s.get(base + "/api/player-window/relay-status", timeout=10)
    r.raise_for_status()
    return r.json()


def wait_panel(s: requests.Session, base: str, pred, timeout: float = 45.0):
    """Ждём от relay-status условия (регистрация панели — асинхронная)."""
    t0 = time.time()
    last = {}
    while time.time() - t0 < timeout:
        try:
            last = relay_status(s, base)
            if pred(last):
                return last, time.time() - t0
        except Exception as e:
            last = {"err": f"{type(e).__name__}: {e}"}
        time.sleep(0.5)
    return last, time.time() - t0


# ── настоящее нажатие в настоящем окне ───────────────────────────────────────
_EnumWindowsProc = ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)


def window_hwnd(pid: int) -> int | None:
    """Верхнее видимое окно процесса. Недра WebView2 — дочерние, а фокус
    берётся у верхнего: так же делает человек, кликая по окну."""
    user32 = ctypes.windll.user32
    hits: list[int] = []

    def cb(hwnd, _lparam):
        wpid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(wpid))
        if wpid.value == pid and user32.IsWindowVisible(hwnd):
            hits.append(hwnd)
        return True

    user32.EnumWindows(_EnumWindowsProc(cb), 0)
    return hits[0] if hits else None


def press_space(pid: int) -> str:
    """Отдать окну фокус, ударить Space, вернуть фокус прежней программе.
    Без доказанного фокуса клавиша не шлётся вовсе: печатать в чужое окно
    стенд не имеет права."""
    user32 = ctypes.windll.user32
    hwnd = window_hwnd(pid)
    if not hwnd:
        return f"нет HWND процесса {pid}"
    prev = user32.GetForegroundWindow()
    user32.ShowWindow(hwnd, 9)                      # SW_RESTORE
    user32.SetForegroundWindow(hwnd)
    t0 = time.time()
    while time.time() - t0 < 5 and user32.GetForegroundWindow() != hwnd:
        time.sleep(0.2)
    if user32.GetForegroundWindow() != hwnd:
        return f"фокус не взят (впереди {user32.GetForegroundWindow()})"
    time.sleep(0.4)                                 # чтобы WebView2 успел проснуться
    for flag in (0, 2):                             # keybd_event: нажатие, отпускание
        user32.keybd_event(0x20, 0x39, flag, 0)
        time.sleep(0.08)
    if prev and prev != hwnd:
        user32.SetForegroundWindow(prev)
    return "ok"


def cookie_dbs(profile: Path) -> list[Path]:
    """Где WebView2 хранит куки постоянного профиля: обычно
    <profile>/EBWebView/Default/Network/Cookies, у старых рантаймов — Default/Cookies."""
    out = []
    for rel in (("EBWebView", "Default", "Network", "Cookies"),
                ("EBWebView", "Default", "Cookies"),
                ("Default", "Network", "Cookies"),
                ("Default", "Cookies")):
        p = profile.joinpath(*rel)
        if p.exists():
            out.append(p)
    return out


def finish() -> int:
    bad = [r for r in REPORT if not r[1]]
    print("\n===== ИТОГ =====", flush=True)
    for name, ok, detail in REPORT:
        print(f"{'PASS' if ok else 'FAIL'}  {name}  |  {detail}", flush=True)
    print(f"всего {len(REPORT)}, FAIL: {len(bad)}", flush=True)
    return 1 if bad else 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=int(os.environ.get("RIPSTER_PORT") or 7805))
    ap.add_argument("--cdp-port", type=int, default=9437)
    args = ap.parse_args()

    global PORT
    PORT = args.port
    base = f"http://127.0.0.1:{args.port}"
    cookie = el.owner_cookie()
    wav = el.silent_wav_uri()
    paths = pw.win_paths(ROOT)

    if pw.running_pid(paths):
        print("[стенд] живо ЧУЖОЕ окно плеера (боевой Рипстер) — прогон снят, "
              "чтобы не фокусить и не убирать окно владельца", flush=True)
        return 2

    # Профиль окна — данные стенда, и прошлый прогон оставляет в них и куку, и
    # восстановленную сессию WebView2. Детерминизм важнее: окно обязано войти
    # по разовому пропуску, а не по остатку с прошлого раза.
    shutil.rmtree(paths["profile"], ignore_errors=True)

    srv, out = start_server(args.port)
    tabs: list[el.CDP] = []
    try:
        up = el.wait_server(base, srv)
        say("s0 стенд: сервер на запасном порту", up is not None,
            f"{base} за {up:.1f} с" if up else f"не поднялся, см. {SRV_LOG}")
        if up is None:
            return finish()
        owner = sess(cookie)

        # ── s1: корневая причина измерена, а не заявлена ─────────────────────
        STEP[0] = "s1: /ws без куки"
        no_cookie, d_no = ws_open(base, None)
        with_cookie, d_yes = ws_open(base, cookie)
        say("s1 /ws БЕЗ куки отвергнут (это и убивал окно)", not no_cookie, d_no)
        say("s1 /ws С хозяйской кукой принят", with_cookie, d_yes)

        # ── играющая вкладка-хост ────────────────────────────────────────────
        STEP[0] = "s2: хост играет"
        with owned_browser(port=args.cdp_port, headless=True,
                           profile_suffix="winauth",
                           extra_args=["--remote-allow-origins=*",
                                       "--autoplay-policy=no-user-gesture-required",
                                       "--disable-background-media-suspend",
                                       "--disable-backgrounding-occluded-windows",
                                       "--disable-background-timer-throttling",
                                       "--disable-renderer-backgrounding",
                                       "--window-size=1400,900"]) as br:
            host = el.open_tab(br["port"], cookie, base + "/", base)
            tabs.append(host)
            say("s2 вкладка-хост: страница, /ws, panel_host", el.host_ready(host),
                f"tabId={host.ev(el.HOST_VIEW)['tabId']}")
            r = host.ev(f"({el.JS_PLAY})({json.dumps(wav)}, {json.dumps(TRACK)})")
            host.wait("document.getElementById('pp-audio') && "
                      "!document.getElementById('pp-audio').paused", timeout=15)
            say("s2 хост играет тестовый трек",
                host.ev(el.HOST_VIEW)["title"] == TRACK and not (r or {}).get("err"),
                f"title={host.ev(el.HOST_VIEW)['title']!r}"
                + (f" err={r.get('err')}" if (r or {}).get("err") else ""))

            # ── s2: кнопка поднимает НАСТОЯЩЕЕ окно ──────────────────────────
            STEP[0] = "s2: запуск окна"
            log_before = paths["log"].stat().st_size if paths["log"].exists() else 0
            t0 = time.time()
            resp = owner.post(base + "/api/player-window/open", json={"launcher": False},
                              timeout=30)
            body = resp.json() if resp.status_code == 200 else {}
            pid = pw.running_pid(paths)
            say("s2 POST /api/player-window/open поднял standalone-окно",
                resp.status_code == 200 and body.get("ok") and body.get("how") == "standalone"
                and pid is not None,
                f"{resp.status_code} {body} pid={pid} за {time.time() - t0:.1f} с")

            # ── s3/s4: сервер видит панель и её показ ────────────────────────
            STEP[0] = "s3: панель в релее"
            st, waited = wait_panel(owner, base, lambda d: d.get("panels", 0) >= 1)
            say("s3 панель зарегистрирована в rp_relay", st.get("panels", 0) >= 1,
                f"hosts={st.get('hosts')} panels={st.get('panels')} "
                f"panel_ids={st.get('panel_ids')} active={st.get('active_host')} "
                f"за {waited:.1f} с" + (f" err={st.get('err')}" if st.get("err") else ""))
            STEP[0] = "s4: ack панели"
            st, waited = wait_panel(owner, base,
                                    lambda d: (d.get("last_panel_ack") or {}).get("title") == TRACK)
            ack = st.get("last_panel_ack") or {}
            say("s4 панель подтвердила ПОКАЗ играющего трека",
                ack.get("title") == TRACK,
                f"title={ack.get('title')!r} artist={ack.get('artist')!r} "
                f"have={ack.get('have')} панель={ack.get('panel')} age={ack.get('age_s')} "
                f"за {waited:.1f} с")
            say("s4 активный хост — играющая вкладка",
                st.get("active_host") in (st.get("host_ids") or [None]) and st.get("playing") is True,
                f"active={st.get('active_host')} playing={st.get('playing')} "
                f"hosts={st.get('host_ids')}")

            # ── s5: о себе сообщает сама панель внутри окна ──────────────────
            STEP[0] = "s5: самоотчёт окна"
            say("s5 окно сообщает: мост жив, баннера «нет связи» нет",
                ack.get("live") is True and ack.get("nolink") is False,
                f"из ack панели: live={ack.get('live')} nolink={ack.get('nolink')} — "
                f"эти поля шлёт сам panel.js, который работает в окне")

            # ── s6: настоящее нажатие в настоящем окне ───────────────────────
            STEP[0] = "s6: Space в окне"
            hv_before = host.ev(el.HOST_VIEW)
            sent = press_space(pid) if pid else "нет pid окна"
            paused = bool(host.wait("document.getElementById('pp-audio').paused === true",
                                    timeout=12)) if sent == "ok" else False
            say("s6 Space В НАСТОЯЩЕМ ОКНЕ поставил играющую вкладку на паузу",
                paused, f"нажатие: {sent}; host.paused={host.ev(el.HOST_VIEW)['paused']} "
                        f"(было {hv_before['paused']})")
            sent2 = press_space(pid) if paused else "пропущено: пауза не получена"
            resumed = bool(host.wait("document.getElementById('pp-audio').paused === false",
                                     timeout=12)) if sent2 == "ok" else False
            st_after = relay_status(owner, base)
            say("s6 второй Space из окна возвращает игру (мост в обе стороны)",
                resumed, f"нажатие: {sent2}; host.paused={host.ev(el.HOST_VIEW)['paused']}; "
                         f"ack={(st_after.get('last_panel_ack') or {}).get('title')!r} "
                         f"panels={st_after.get('panels')}")

            # ── s7: токен не светится; окно закрывается и входит само ────────
            STEP[0] = "s7: лог и перезапуск"
            try:
                tail = paths["log"].read_text(encoding="utf-8", errors="replace")[log_before:]
            except Exception:
                tail = ""
            srv_tail = ""
            try:
                srv_tail = SRV_LOG.read_text(encoding="utf-8", errors="replace")
            except Exception:
                pass
            say("s7 разовый пропуск не попал ни в лог окна, ни в лог сервера",
                "launch=" not in tail and "launch=" not in srv_tail,
                f"лог окна +{len(tail)} зн., первая строка: "
                f"{(tail.splitlines() or [''])[0][:90]!r}")

            STEP[0] = "s7: закрытие и повторный запуск"
            owner.post(base + "/api/player-window/close", json={}, timeout=15)
            gone = time.time()
            while pw.running_pid(paths) and time.time() - gone < 20:
                time.sleep(0.3)
            say("s7 окно закрыто по просьбе панели", pw.running_pid(paths) is None,
                f"lock снят за {time.time() - gone:.1f} с")
            resp2 = owner.post(base + "/api/player-window/open", json={"launcher": False},
                               timeout=30)
            st2, waited2 = wait_panel(owner, base, lambda d: d.get("panels", 0) >= 1)
            st2, _w = wait_panel(owner, base,
                                 lambda d: (d.get("last_panel_ack") or {}).get("title") == TRACK,
                                 timeout=25)
            pid2 = pw.running_pid(paths)
            say("s7 новое окно вошло САМО: профиль чист, допуск — только пропуск",
                resp2.status_code == 200 and st2.get("panels", 0) >= 1
                and (st2.get("last_panel_ack") or {}).get("title") == TRACK,
                f"{resp2.status_code} {resp2.json().get('how')} pid={pid2} → "
                f"panels={st2.get('panels')} panel_ids={st2.get('panel_ids')} "
                f"ack={(st2.get('last_panel_ack') or {}).get('title')!r} за {waited2:.1f} с")
            dbs = cookie_dbs(paths["profile"])
            say("s7 профиль окна постоянный (кука на диске, а не в inPrivate)",
                bool(dbs), f"базы кук: {[pw.strip_launch(str(p)) for p in dbs] or 'нет'}")

            if pid2:
                owner_post_close(paths)

        # окно закрыто стендом ниже (в finally), вкладчики уже сняты reaper-ом
        return finish()
    finally:
        for t in tabs:
            t.close()
        if pw.running_pid(paths):
            try:
                owner_post_close(paths)
            except Exception:
                pass
        if srv.poll() is None:
            kill_tree(srv.pid)
        out.close()


def owner_post_close(paths) -> None:
    """Финальная страховка: окно всё ещё живо — попросить его закрыться."""
    if pw.running_pid(paths):
        paths["close"].parent.mkdir(parents=True, exist_ok=True)
        paths["close"].write_text(str(os.getpid()), encoding="utf-8")
        time.sleep(3.0)


if __name__ == "__main__":
    sys.exit(main())
