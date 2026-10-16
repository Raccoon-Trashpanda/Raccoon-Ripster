"""Живая проверка скинов Neon / Console / OLED Noir на боевом Ripster.

Три части:

1) Новые скины × темы × вкладки на настоящих данных — скриншоты в live_shots,
   плюс аудит контраста WCAG и сверка, что скин реально лёг на карточку
   (border-radius .rel-card == ожидаемый из токенов).
2) Переключатель в настройках: ru/en, внутри каждого скина, и клавиатурный
   прогон ←/→ (saveSetting подменён сборщиком — провод доказываем, конфиг
   владельца не пишем).
3) Вид по умолчанию ДО/ПОСЛЕ. Сравнивается боевой main.css с тем же файлом,
   из которого вырезан добавленный слой. Сравнение идёт на ОДНОЙ странице:
   <link> на main.css на ходу переводится на blob с урезанным css и обратно —
   два отдельных прогона давали бы тысячи фальшивых отличий только потому,
   что счётчики сканера и пагинация релизов живут своей жизнью. Доказательств
   два: полный обход DOM с computed-стилями (включая ::before/::after) и
   геометрией каждого элемента, и попиксельная разница кадров. Третий кадр
   (боевой css против самого себя) задаёт уровень шума измерения: им же и
   отсекаются оседающие асинхронные бейджи. Картинки в этом проне заглушены
   1x1, а анимации заморожены — иначе сеть шумела бы сильнее сигнала.
   Красивые кадры классики снимаются отдельно, без заглушек.

app.py НЕ перезапускается и конфиг НЕ пишет: setSkin(..., persist=False), а
демо-очередь живёт только в памяти вкладки.
"""
import base64
import hashlib
import hmac
import itertools
import pathlib
import sys
import time

import yaml

from playwright.sync_api import sync_playwright
# reaper (Tracker #44): sweep orphan browsers of previously killed runs
import sys as _sys; _sys.path.insert(0, r'C:\devpple_music	ools')
from headless_reaper import reaper as _reaper
_reaper.sweep_stale(reap=True)


sys.stdout.reconfigure(encoding="utf-8", errors="replace")

REPO = pathlib.Path(r"C:\dev\apple_music")
BASE = "http://127.0.0.1:7799"
OUT = pathlib.Path(r"C:\dev\ripster-concepts\live_shots")
CHECK = OUT / "classic_check"
MARK = "/* ══ СКИНЫ (data-skin"

SKINS = ["neon", "console", "oled"]
THEMES = ["dark", "light"]
VIEWS = ["releases", "queue"]
# с ним сверяем, что скин действительно лег на живые карточки
EXPECT_RADIUS = {"classic": "10px", "neon": "20px", "console": "6px", "oled": "14px"}
# 1x1 прозрачный PNG — заглушка для всех картинок в детерминированном прогоне
PIX = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJ"
    "AAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg==")


def session_cookie():
    secret = (yaml.safe_load((REPO / "config.yaml").read_text(encoding="utf-8"))
              or {}).get("session-secret", "")
    i = int(time.time())
    return f"{i}." + hmac.new(secret.encode(), str(i).encode(), hashlib.sha256).hexdigest()


# ── аудит контраста на живых элементах (тот же приём, что в prototypes shots.py)
AUDIT = """
([selTitle, selMeta, selBtn]) => {
  const lum=(r,g,b)=>{const f=c=>{c/=255;return c<=.03928?c/12.92:((c+.055)/1.055)**2.4};
    return .2126*f(r)+.7152*f(g)+.0722*f(b)};
  const parse=s=>{if(!/[\\d.]/.test(s))return [0,0,0];
    const m=s.match(/[\\d.]+/g).slice(0,3).map(Number);
    return s.startsWith('color(srgb')?m.map(v=>Math.round(v*255)):m;};
  const effBg=el=>{let n=el;
    while(n&&n!==document.documentElement){const bg=getComputedStyle(n).backgroundColor;
      if(bg&&bg!=='rgba(0, 0, 0, 0)'){const a=+(bg.match(/[\\d.]+/g)||[])[3];
        if(!(bg.startsWith('rgba(')&&a<.97)) return parse(bg);} n=n.parentElement;}
    return parse(getComputedStyle(document.documentElement).backgroundColor);};
  const out=[];
  const check=(label,sel)=>document.querySelectorAll(sel).forEach(el=>{
    if(!el.offsetParent&&label!=='body')return;
    const c=parse(getComputedStyle(el).color), b=effBg(el);
    const [l1,l2]=[lum(...c),lum(...b)].sort((x,y)=>y-x);
    out.push({label, ratio:+(((l1+.05)/(l2+.05))).toFixed(2),
              size:parseFloat(getComputedStyle(el).fontSize)||12,
              text:(el.textContent||'').trim().slice(0,22)});});
  check('title',selTitle); check('meta',selMeta); check('btn',selBtn);
  return out;
}
"""

# ── подпись всего DOM: computed-стили + геометрия каждого элемента. Любое
#    оформляющее правило, дописанное слоем скинов, изменило бы хотя бы строку.
#    Поддерево #skin-picker пропускается: это разметка НОВОГО переключателя,
#    она присутствует в DOM обоих прогонов (фрагмент настроек-то один), но в
#    «до»-css для неё нет ни одного правила — сравнивать её не с чем.
SIGN = r"""
() => {
  const P = ['backgroundColor','color','borderTopColor','borderTopWidth','borderRadius',
             'boxShadow','paddingTop','paddingLeft','fontFamily','fontSize','fontWeight',
             'lineHeight','letterSpacing','opacity','backdropFilter','position','top',
             'display','gap','gridTemplateColumns','textTransform','textAlign','whiteSpace',
             'backgroundImage','borderBottomColor','borderBottomWidth','borderLeftColor',
             'borderLeftWidth','borderRightColor','borderRightWidth','outlineStyle',
             'outlineWidth','textShadow','filter','visibility','transform','zIndex',
             'alignItems','justifyContent','flexWrap','overflow','verticalAlign'];
  // Псевдоэлементы тоже в подпись: слой скинов мог бы что-то повесить на
  // ::before/::after, и по одному только computedStyle узла это не видно.
  const PP = ['content','color','backgroundColor','backgroundImage','width','height',
              'borderRadius','borderTopColor','borderTopWidth','opacity','display'];
  const pseudo = (el, why) => { const cs = getComputedStyle(el, why);
    return '{' + why + ':' + PP.map(p => p + '=' + cs[p]).join(';') + '}'; };
  const out = [], geo = [];
  let skipped = 0;
  (function walk(el, path){
    if (el.id === 'skin-picker') { skipped += el.querySelectorAll('*').length + 1; return; }
    const cls = (el.getAttribute('class') || '').trim().replace(/\s+/g, '.');
    const i = [...el.parentElement.children].indexOf(el) + 1;
    const p = path + '>' + el.tagName.toLowerCase() + (cls ? '.' + cls : '') + ':nth(' + i + ')';
    const cs = getComputedStyle(el), r = el.getBoundingClientRect();
    out.push(p + ' ~ ' + P.map(x => cs[x]).join('|') + pseudo(el, ':before') + pseudo(el, ':after'));
    geo.push(p + ' ~ ' + [r.x, r.y, r.width, r.height].map(v => Math.round(v)).join(','));
    [...el.children].forEach(c => walk(c, p));
  })(document.body, 'html');
  return {rows: out, geo, skipped};
}
"""

# Заморозка для детерминированного прогона: пульсации, переходы, спиннер и
# полоса «загружаю ещё» двигают пиксели между прогонами, не имея отношения к
# проверке. Одна и та же вставка на обеих страницах — сравнение честное.
FREEZE = """
*, *::before, *::after { animation: none !important; transition: none !important; }
#rel-status { display: none !important; }
"""

# Очередь на живом сервере пуста (реальных задач нет), поэтому для съёмки
# подставляются четыре задачи — теми же рендерерами приложения (S.queue +
# renderQueue), что и настоящие. Это подмена в памяти вкладки: на сервер не
# уходит ничего, очередь приложения не меняется.
QUEUE_DEMO = """
() => {
  S.queue = [
    {id:'sk1', service:'apple', engine:'amd_v2', status:'running', progress:42, got:5,
     url:'https://music.apple.com/album/random-access-memories/1584619935',
     meta:{title:'Random Access Memories', artist:'Daft Punk', year:'2013',
           label:'Columbia', type:'album', trackCount:14}},
    {id:'sk2', service:'spotify', engine:'spotip', status:'queued', progress:0,
     url:'https://open.spotify.com/album/2MRovwuGEwrBKKZw5FF0Tg',
     meta:{title:'Discovery', artist:'Daft Punk', year:'2001', label:'Virgin',
           type:'album', trackCount:10}},
    {id:'sk3', service:'soundcloud', engine:'soundcloud', status:'done', progress:100,
     url:'https://soundcloud.com/porterrobinson/image',
     meta:{title:'Image', artist:'Porter Robinson', year:'2020', type:'song'}},
    {id:'sk4', service:'qobuz', engine:'qobuz', status:'error', progress:17,
     url:'https://www.qobuz.com/us-en/interpreter/arcade-fire/reflektor',
     meta:{title:'Reflektor', artist:'Arcade Fire', year:'2013', label:'Merge',
           type:'album', trackCount:13}}
  ];
  renderQueue();
  return document.querySelectorAll('#queue-list .qi').length;
}
"""


def boot(pg, hold=True):
    pg.goto(BASE + "/", wait_until="domcontentloaded")
    pg.wait_for_function(
        "() => typeof setSkin==='function' && typeof showView==='function'"
        " && !!document.getElementById('skin-picker')", timeout=45000)
    # Приложение переприменяет скин из конфига на каждом WS-init. Пока
    # whitelist не задействован (app.py не перезапускали), POST /api/config
    # молча выбрасывает ui-skin, и любой реконнект откатывал бы вкладку к
    # классике прямо во время съёмки. Держатель — только в этом тесте.
    pg.evaluate("""(hold) => {
      window.__hold = '';
      if (hold) { const real = getSkin; window.getSkin = () => window.__hold || real.call(null); }
      try { DLOrb && DLOrb.setEnabled && DLOrb.setEnabled(false); } catch (e) {}
    }""", hold)
    pg.wait_for_timeout(300)


def ensure_queue(pg, want=4):
    """Демо-очередь обязана пережить съёмку. WS-init с настоящей (пустой)
    очередью иногда успевает её затереть между вставкой и кадром."""
    for _ in range(4):
        if pg.evaluate("() => document.querySelectorAll('#view-queue .qi').length") == want:
            return want
        pg.evaluate(QUEUE_DEMO)
        pg.wait_for_timeout(300)
    return pg.evaluate("() => document.querySelectorAll('#view-queue .qi').length")


def show(pg, view, skin, theme):
    pg.evaluate("""([view, skin, theme]) => {
      window.__hold = skin;
      setTheme(theme);
      setSkin(skin, false);            // persist=false: конфиг владельца не трогаем
      showView(view, document.querySelector('.nav-item[data-view=' + view + ']'));
    }""", [view, skin, theme])
    if view == "releases":
        pg.wait_for_selector("#view-releases .rel-card", timeout=90000)
        pg.wait_for_timeout(1200)
    else:
        assert ensure_queue(pg) == 4, "демо-очередь не отрисовалась"
        pg.wait_for_timeout(700)
    assert skin_of(pg) == skin, f"скин слетел: {skin_of(pg)} != {skin}"


def shot(pg, name, where=OUT):
    path = where / name
    pg.screenshot(path=str(path))
    return path


def skin_of(pg):
    return pg.evaluate("() => document.documentElement.dataset.skin")


def card_radius(pg):
    return pg.evaluate("""() => { const el =
      document.querySelector('#view-releases .rel-card') || document.querySelector('#view-queue .qi');
      return el ? getComputedStyle(el).borderRadius : '(нет элемента)'; }""")


def audit(pg, view):
    if view == "releases":
        args = ["#view-releases .card-grid div[style*='font-weight:600']",
                "#view-releases .card-grid div[style*='font-size:10px']",
                "#view-releases .rel-dl-btn"]
    else:
        args = ["#view-queue .qi-title", "#view-queue .qi-artist, #view-queue .qi-count",
                "#view-queue .qi-badge"]
        # WS-init с живой (пустой) очередью иногда успевает затереть демо между
        # вставкой и замером — тогда аудит считает пустоту. Вставляем ещё раз.
        ensure_queue(pg)
    rows = pg.evaluate(AUDIT, args)
    bad = [(r["label"], r["ratio"], 3.0 if r["size"] >= 18.5 else 4.5, r["text"])
           for r in rows if r["ratio"] < (3.0 if r["size"] >= 18.5 else 4.5)]
    return rows, bad


def open_picker(pg):
    pg.evaluate("""() => {
      window.__hold = 'classic';
      showView('settings', document.querySelector('.nav-item[data-view=settings]'));
      setSkin('classic', false);
    }""")
    pg.wait_for_timeout(200)
    pg.evaluate("() => showStab('global', document.querySelector('[data-stab=global]'))")
    pg.wait_for_timeout(600)
    pg.locator("#skin-picker").scroll_into_view_if_needed()
    pg.wait_for_timeout(300)


def keyboard_walk(pg):
    """←/→ по radiogroup: скин меняется сразу, выбор уходит на сохранение.

    Ожидание по записям НЕ равно числу нажатий: setSkin(persist=true)
    сознательно пропускает сохранение, когда выбранный скин и есть уже
    сохранённый (шаг, замыкающий круг на classic). Поэтому ожидаемый список
    выводится из того же правила, а не задан руками.
    """
    stored = pg.evaluate("() => (S.config && S.config['ui-skin']) || ''")
    pg.evaluate("""() => {
      window.__saved = [];
      window.__realSave = saveSetting;
      saveSetting = (k, v) => { window.__saved.push([k, String(v)]); return Promise.resolve(); };
      document.querySelector('#skin-picker .sk-opt').focus();
    }""")
    seen = []
    for _ in range(4):
        pg.keyboard.press("ArrowRight")
        pg.wait_for_timeout(250)
        seen.append(skin_of(pg))
    aria = pg.evaluate("() => document.querySelector('#skin-picker [aria-checked=true]')?.dataset.skin")
    aria_ok = pg.evaluate("""() => { const r = document.getElementById('skin-picker');
      if (!r || r.getAttribute('role') !== 'radiogroup') return false;
      return SKIN_ORDER.every(k => { const o = r.querySelector('[role=radio][data-skin=' + k + ']');
        return o && o.getAttribute('aria-checked')
             === (o.dataset.skin === document.documentElement.dataset.skin ? 'true' : 'false'); }); }""")
    saved = pg.evaluate("() => window.__saved")
    pg.evaluate("() => { saveSetting = window.__realSave; }")
    expect = [["ui-skin", v] for v in seen if v != stored]
    ok = (seen == ["neon", "console", "oled", "classic"] and aria == seen[-1] and aria_ok
          and saved == expect and len(expect) == 3)
    return {"ok": ok, "sequence": seen, "aria_checked": aria, "saved": saved,
            "expect": expect, "stored": stored, "aria_ok": aria_ok}


def new_ctx(b, css_old=None, stub_images=False):
    ctx = b.new_context(viewport={"width": 1440, "height": 900})
    ctx.add_cookies([{"name": "ripster-session", "value": session_cookie(), "url": BASE}])
    def handler(route):
        if stub_images and route.request.resource_type == "image":
            return route.fulfill(body=PIX, content_type="image/png")
        if css_old and "static/css/main.css" in route.request.url:
            return route.fulfill(body=css_old, content_type="text/css")
        return route.continue_()
    if css_old or stub_images:
        ctx.route("**/*", handler)
    return ctx


def pixel_diff(a, b, out):
    """Доля отличающихся пикселей (порог яркости 8/255) + карта различий."""
    from PIL import Image, ImageChops
    ia, ib = Image.open(a).convert("RGB"), Image.open(b).convert("RGB")
    if ia.size != ib.size:
        return None
    diff = ImageChops.difference(ia, ib).convert("L").point(lambda v: 255 if v > 8 else 0)
    px = list(diff.getdata())
    diff.save(out)
    return sum(1 for v in px if v) / len(px)


def _diff(rows_a, rows_b):
    """path -> «чем отличается» для строк, где разошлось хоть что-то.

    Ключ — путь в DOM, а не вся строка: иначе добавление одного элемента
    сдвинуло бы счётчик «различий» на весь список потомков.
    """
    ma = {r.split(" ~ ", 1)[0]: r for r in rows_a}
    mb = {r.split(" ~ ", 1)[0]: r for r in rows_b}
    out = {}
    for p in set(ma) | set(mb):
        if ma.get(p) != mb.get(p):
            out[p] = (f"ПОСЛЕ: {(ma.get(p) or '—')[len(p):][:150]}  "
                      f"ДО: {(mb.get(p) or '—')[len(p):][:150]}")
    return out


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    CHECK.mkdir(parents=True, exist_ok=True)
    css_new = (REPO / "static/css/main.css").read_text(encoding="utf-8")
    if MARK not in css_new:
        print("НЕТ слоя скинов в main.css — нечего проверять"); return 1
    css_old = css_new.split(MARK)[0].rstrip() + "\n"
    assert "data-skin" not in css_old, "в «до»-css просочился слой скинов"
    print(f"main.css: {len(css_new)} байт, до слоя: {len(css_old)} "
          f"(добавлено {len(css_new) - len(css_old)})")

    fails, notes = [], []
    # В конфиге владельца сверяем ТОЛЬКО свою строку: приложение само обновляет
    # *_checked-at, и сравнивать файл целиком бессмысленно.
    def skin_line():
        for ln in (REPO / "config.yaml").read_text(encoding="utf-8").splitlines():
            if ln.startswith("ui-skin"):
                return ln
        return "(нет)"
    cfg_before = skin_line()

    with sync_playwright() as p:
        b = p.chromium.launch()

        # ══ 1+2: живые данные, красивые кадры ══════════════════════════════
        pg = new_ctx(b).new_page()
        pg.on("pageerror", lambda e: notes.append("pageerror: " + str(e)[:160]))
        pg.on("console", lambda m: notes.append("console." + m.type + ": " + m.text[:160])
              if m.type == "error" else None)
        boot(pg)

        for skin, theme in itertools.product(SKINS, THEMES):
            for view in VIEWS:
                show(pg, view, skin, theme)
                shot(pg, f"{view}__{skin}__{theme}.png")
        for skin in SKINS:
            show(pg, "releases", skin, "dark")
            rows, bad = audit(pg, "releases")
            r_exp = card_radius(pg)
            show(pg, "queue", skin, "dark")
            rows2, bad2 = audit(pg, "queue")
            worst = min([r["ratio"] for r in rows + rows2] or [99])
            applied = r_exp == EXPECT_RADIUS[skin]
            print(f"{skin:8s} r(.rel-card)={r_exp:5s} "
                  f"{'применён' if applied else 'НЕ ПРИМЕНЁН, ждали ' + EXPECT_RADIUS[skin]}  "
                  f"строка аудита: релизы={len(rows)} очередь={len(rows2)}  "
                  f"min-контраст={worst:5.2f} {'OK' if not (bad or bad2) else 'LOW'}")
            if not applied:
                fails.append(("skin-not-applied", skin, r_exp, EXPECT_RADIUS[skin]))
            if not (rows and rows2):
                fails.append(("audit-empty", skin, str(len(rows)), str(len(rows2))))
            for x in (bad + bad2)[:4]:
                print(f"   LOW: {x}")
                fails.append((skin, "dark", *x))
            open_picker(pg)
            pg.locator("#skin-picker").screenshot(path=str(OUT / f"picker__inside_{skin}.png"))

        open_picker(pg)
        shot(pg, "picker__settings_dark_ru.png")
        pg.evaluate("() => { S.lang='en'; applyLang(); }")   # без setLang: не пишем язык в конфиг
        pg.wait_for_timeout(400)
        shot(pg, "picker__settings_dark_en.png")
        pg.evaluate("() => { S.lang='ru'; applyLang(); }")
        pg.wait_for_timeout(300)
        kb = keyboard_walk(pg)
        print(f"\nклавиатура ←/→: {kb['sequence']} | aria-checked={kb['aria_checked']} "
              f"(матрица aria: {'OK' if kb['aria_ok'] else 'ЛОМЬ'})")
        print(f"   на сохранение ушло {kb['saved']} | ожидалось {kb['expect']} "
              f"(в конфиге уже лежит {kb['stored']!r}) → "
              f"{'OK' if kb['ok'] else 'РАСХОЖДЕНИЕ'}")
        if not kb["ok"]:
            fails.append(("keyboard", str(kb), "", ""))

        # красивые кадры классики (deliverable «до/после» для владельца)
        for theme in THEMES:
            for view in VIEWS:
                show(pg, view, "classic", theme)
                shot(pg, f"{view}__classic__{theme}_AFTER.png")
        pg.close()

        # ══ 3: неизменность вида по умолчанию ══════════════════════════════
        # ONE page, ONE set of live data: боевой main.css подменяется в полёте
        # (ссылка на <link> переводится на blob с урезанным css). Два захода на
        # две разные страницы давали расхождение в 3000+ элементов просто
        # потому, что счётчики сканера и пагинация релизов меняются между
        # прогонами. Подмена на месте сравнивает ровно одно — каскад стилей.
        print("\nКлассика: боевой main.css против того же css без слоя скинов"
              " (одна страница, те же данные):")
        c2 = new_ctx(b, stub_images=True)
        q = c2.new_page()
        boot(q)
        q.add_style_tag(content=FREEZE)   # после boot: goto сбрасывает вставленный стиль
        for theme in THEMES:
            for view in VIEWS:
                key = f"{view}:{theme}"
                cap = {}
                for tag, css in (("AFTER", None), ("BEFORE", css_old), ("AFTER2", None)):
                    if css is None:
                        q.evaluate("""() => { const l = document.querySelector('link[data-skinprobe]');
                          if (l) l.href = window.__realHref; }""")
                    else:
                        q.evaluate("""(css) => {
                          let l = document.querySelector('link[data-skinprobe]');
                          if (!l) { l = document.querySelector('link[href*="main.css"]');
                            l.setAttribute('data-skinprobe','1'); window.__realHref = l.href; }
                          if (window.__blob) URL.revokeObjectURL(window.__blob);
                          window.__blob = URL.createObjectURL(new Blob([css], {type:'text/css'}));
                          l.href = window.__blob;
                        }""", css)
                    show(q, view, "classic", theme)
                    q.wait_for_timeout(400)
                    cap[tag] = (q.evaluate(SIGN),
                                shot(q, f"{view}__classic__{theme}_{tag}.png", CHECK))
                # AFTER и AFTER2 — один и тот же боевой css дважды: это шум
                # измерения (анимации, догрузка), он не должен быть нулевым, но
                # обязан не превышать разницу BEFORE/AFTER.
                a_rows, a_geo = cap["AFTER"][0]["rows"], cap["AFTER"][0]["geo"]
                b_rows, b_geo = cap["BEFORE"][0]["rows"], cap["BEFORE"][0]["geo"]
                n_rows, n_geo = cap["AFTER2"][0]["rows"], cap["AFTER2"][0]["geo"]
                d_style_ab = _diff(a_rows, b_rows)
                d_style_nn = _diff(a_rows, n_rows)
                d_geo_ab = _diff(a_geo, b_geo)
                d_geo_nn = _diff(a_geo, n_geo)
                px_ab = pixel_diff(cap["BEFORE"][1], cap["AFTER"][1],
                                   CHECK / f"diff_{key.replace(':', '_')}.png")
                px_nn = pixel_diff(cap["AFTER2"][1], cap["AFTER"][1],
                                   CHECK / f"noise_{key.replace(':', '_')}.png")
                skipped = cap["AFTER"][0]["skipped"]
                # Отличия, которые боевой css показывает и против самого себя
                # (AFTER ↔ AFTER2), — оседание асинхронных бейджей, а не стиль.
                sty = {k: v for k, v in d_style_ab.items() if k not in d_style_nn}
                geo = {k: v for k, v in d_geo_ab.items() if k not in d_geo_nn}
                print(f"  {key:16s} элементов {len(a_rows)} (переключатель опущен {skipped}) | "
                      f"стили ДО↔ПОСЛЕ: {len(d_style_ab)} (из них необъяснённых шумом: {len(sty)}) | "
                      f"геометрия: {len(d_geo_ab)} (необъяснённых: {len(geo)}) | "
                      f"пиксели: " + ("?" if px_ab is None else f"{px_ab*100:.4f}%") +
                      f"  шуп: " + ("?" if px_nn is None else f"{px_nn*100:.4f}%"))
                for x in list(sty.values())[:4]:
                    print("    РАЗЛИЧИЕ СТИЛЯ:", x[:220])
                for x in list(geo.values())[:4]:
                    print("    РАЗЛИЧИЕ ГЕОМЕТРИИ:", x[:220])
                # жёсткий гейт: стили и геометрия совпали (с точностью до шума
                # измерения), а попиксельная разница не больше этого же шума
                if sty or geo:
                    fails.append(("classic-changed", key, len(sty), len(geo)))
                if px_ab is None or (px_nn is not None and px_ab > max(px_nn * 2, 0.002)):
                    fails.append(("classic-pixels", key, str(px_ab), f"шум {px_nn}"))
        c2.close()
        b.close()

    cfg_after = skin_line()
    print(f"\nconfig.yaml · ui-skin: ДО {cfg_before!r} → ПОСЛЕ {cfg_after!r}"
          + (" — НЕ ТРОНУТ" if cfg_before == cfg_after else " — ИЗМЕНЁН!"))
    if cfg_before != cfg_after:
        fails.append(("config-written", cfg_before, cfg_after, ""))

    for n in notes[:15]:
        print("ЗАМЕТКА:", n)
    if fails:
        print("\n=== ПРОВАЛЫ ===")
        for f in fails:
            print("FAIL:", *f)
    print(f"\nскриншоты: {OUT}  и  {CHECK}")
    return 1 if fails else 0


sys.exit(main())
