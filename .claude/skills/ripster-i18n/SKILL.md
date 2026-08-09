---
name: ripster-i18n
description: >
  Ripster's frontend i18n workflow. READ AND FOLLOW THIS whenever you add or edit
  ANY user-facing string in the Ripster UI — a toast, a button, a label, a help
  text, an option, a modal, a status line — in static/js/*.js, static/views/*.html,
  or static/index.html. The rule: never hardcode a Russian (or English) UI string;
  wire it through i18n from the start so we never have to retro-translate later.
  Triggers: "add a button/toggle/setting", "new toast", "new panel/modal/view",
  "translate", "hardcoded Russian", "i18n", "data-i18n", any new UI copy.
---

# Ripster i18n — translate as you go

**Golden rule:** every NEW user-facing string ships with an i18n key + a Russian
value + an English value, in the SAME change. No hardcoded UI copy — ever. Retro-
translating a whole codebase (what happened once) is the pain this prevents.

## The mechanism (already loaded globally)

`static/js/i18n.js` defines `var LANG = { ru:{…}, en:{…}, hi:{…}, ja:{…}, zh:{…} }`.
`static/js/app.js` defines the lookup:

- `t('key')` → current-lang value, falling back **locale → en → ru → the key string**.
  So a key present only in ru+en still renders English for hi/ja/zh (never the raw key).
- `ti('key', {name: val})` → same, but fills `{name}` placeholders. Use for interpolation.
- `applyLang()` (runs on load + on `setLang`) fills the DOM from `data-i18n*` attributes.

### In JavaScript
```js
toast(t('t.copied'), 'var(--green)');          // was: toast('Скопировано!', …)
el.textContent = t('s.dl_folder');             // was: el.textContent = 'Папка загрузок'
el.innerHTML = ti('as.amd_clients', {n: 5});   // interpolated
```
Never write a Cyrillic literal inside `toast(…)`, `textContent = …`, `innerHTML = …`.

### In HTML (static/views/*.html, index.html)
```html
<div class="block-title" data-i18n="s.svc_colors">🎨 Цвета сервисов</div>
<div data-i18n-html="s.cookies_help">…text with <b>…</b> tags…</div>  <!-- has nested tags -->
<input data-i18n-ph="s.search_ph" placeholder="…">                    <!-- placeholder -->
<button data-i18n-title="s.recheck_t" title="…">↺</button>           <!-- tooltip -->
```
- `data-i18n` → sets `textContent` (plain text only; nested tags get wiped).
- `data-i18n-html` → sets `innerHTML` (use when the string contains `<code>/<b>/<a>`).
- `data-i18n-ph` → placeholder, `data-i18n-title` → title.
- The inline text you write is just the authored fallback; `applyLang()` overwrites it
  from the key. If a key is MISSING, `applyLang` keeps the inline text (it does not
  dump the raw key) — but still ALWAYS add the key.

## Adding the key (ru + en; hi/ja/zh optional, they fall back to en)

Add to `static/js/i18n.js` in BOTH the `ru:` block and the `en:` block:
```js
    's.dl_folder':'Папка загрузок',   // in ru block
    's.dl_folder':'Downloads folder', // in en block
```
Namespaces in use: `t.*` (toasts), `s.*` (settings), `cd.*` (coder), `card.*` (cards),
`p.*` (player), `b.*` (bbc), `sc.*/sc2.*` (soundcloud), `ck.*` (cookies), `setup.*`,
`as.*` (amd status), `dlg.*` (dialogs). Reuse an existing key if the exact string
already has one (grep i18n.js first) — don't create duplicates.

## After editing — every time

1. **Syntax check** the JS you touched: `node --check static/js/<file>.js` and `i18n.js`.
2. **Key-integrity check** — обязательный:
   ```bash
   python tools/check_i18n_keys.py     # 0 — чисто, 1 — есть пропуски
   ```
   Он смотрит **и `t()`/`ti()` в JS, и `data-i18n*` в разметке**, по обоим
   деревьям (`static/` и зеркало), и различает три беды:

   | находка | что увидит человек |
   |---|---|
   | ключа нет ни в `ru`, ни в `en` | авторский текст из разметки — **выглядит как хардкод** |
   | ключ только в `ru` | русская строка в английском интерфейсе |
   | ключ только в `en` | английская строка в русском интерфейсе |

   Первая строка таблицы — та самая ловушка: `applyLang()` при отсутствии ключа
   **не показывает сырой ключ**, он оставляет то, что написано в разметке. А там
   у нас русский. Поэтому пропущенный ключ и захардкоженная строка выглядят
   одинаково, и владелец трижды сообщал «опять хардкод», когда разметка была
   правильная (08.08.2026 — `s.default_search_svc_label/_sub`: атрибуты стояли,
   ключей не было).

   Старая самодельная проверка ловила только `t()` в JS и **не смотрела разметку
   вообще** — именно поэтому эти два ключа жили незамеченными.
3. **Bump the cache version** so browsers refetch:
   - Changed a `static/js/X.js` → bump `X.js?v=N` in `static/index.html`.
   - Changed a `static/views/Y.html` → bump the `?v=` in `static/js/views.js` (its fetch
     of `views/*.html?v=N`) AND `views.js?v=` in index.html.
   - Changed `static/css/main.css` → bump `main.css?v=` in index.html.
4. Ripster serves **root `static/`**, NOT `ripster/static/`. UI edits go only in root static.

## Gotchas from the retro-translation session (2026-07-04)

- **Batch scripts anchored on a line that had merged onto another** silently added
  nothing (0 replacements) while the code already referenced the keys → raw keys shown.
  ALWAYS run the integrity check after a batch; never trust the "added N" print alone.
- **Blind `>TEXT<` → `<span data-i18n>` wrapping** is safe for plain-text elements but
  BREAKS elements whose text has nested tags — use `data-i18n-html` there instead.
- Skip pure symbols/emoji/names/version placeholders/×speeds — they are language-neutral.
- Heredocs break on apostrophes/smart-quotes in content; write the script to a file and
  run it, or keep EN values apostrophe-free.

Related memory: [[feedback_github_mirror]] (mirror i18n edits to `github_setup/` too).

## Добавил ключ — подними `?v=` у скрипта (29.07.2026)

Владелец дважды сообщил «опять захардкожен русский» в английском интерфейсе.
Оба раза **ключ был на месте, в английской таблице.** Причина другая: в
`static/index.html` скрипты подключены с версией в адресе
(`i18n.js?v=10072`), и пока она не меняется, браузер отдаёт СТАРЫЙ файл —
без только что добавленных ключей.

Дальше срабатывает штатное поведение `applyLang()`: если `t(key)` вернул сам
ключ (перевода нет), элемент **оставляют с авторским текстом**. А авторский
текст в разметке у нас русский. Итог: устаревший кэш выглядит ровно как
захардкоженная строка, и чинить начинаешь не то.

**Правило:** правка `i18n.js` (или любого js) без смены `?v=` в `index.html`
не доходит до пользователя. Поднимать версию — часть правки, а не отдельный шаг.

**Как отличить кэш от настоящего хардкода за минуту:**
```bash
# ключ реально есть в АНГЛИЙСКОЙ таблице?
python -c "
import re,pathlib; s=pathlib.Path('static/js/i18n.js').read_text(encoding='utf-8')
b=[(m.group(1),m.start()) for m in re.finditer(r'^\s*(ru|en|hi|ja|zh)\s*:\s*\{', s, re.M)]
i=s.find(\"'КЛЮЧ'\")
print([n for n,p in b if p<i][-1])   # в каком блоке лежит
"
```
Если ключ в блоке `en` — это кэш, а не хардкод: поднимай `?v=`.

**И отдельно:** текст, который строится НА СЕРВЕРЕ, перевести на клиенте нельзя.
29.07 сводка доступности собиралась в питоне по-русски и уходила готовой строкой —
пришлось переносить сборку на клиент, оставив серверу только машинные причины
(`not_in_catalog_yet`, `region_locked`, …). Сервер отдаёт состояние, текст строит
интерфейс.

## Серверный СПРАВОЧНИК переводится ровно так же плохо (08.08.2026)

Повторилось на панели «Аккаунты — страны и часовые пояса», и в двух видах сразу.
Первый — знакомая готовая фраза:

```python
hint = (f"{flag} {label} ({country_name}) входит в новый день на {dh:.0f} ч "
        f"раньше, чем Apple — ранние релизы лови оттуда.")   # ← так нельзя
```

Второй — менее очевидный: **таблица названий на сервере**.

```python
_COUNTRY_TZ = {"NZ": ("Pacific/Auckland", "Новая Зеландия", "🇳🇿"), ...}
return {"country": cc, "country_name": name, ...}   # ← «Новая Зеландия» приедет
                                                    #    и в английский интерфейс
```

Справочник выглядит как данные, поэтому его пропускают. Но «Новая Зеландия» —
такой же UI-текст, как кнопка, и он точно так же не переводится на клиенте.

Правильно: сервер отдаёт **код и флаг**, клиент подставляет название по ключу и
падает обратно на код, если ключа нет.

```python
return {"country": cc, "flag": flag, "offset": off_h, "local_time": local}
```
```js
const cname = cc => (cc && t('cc.' + cc) !== 'cc.' + cc) ? t('cc.' + cc) : (cc || '');
el.innerHTML = ti('s.acc_hint', {flag: h.flag, label: h.label,
                                 country: cname(h.country), h: h.hours, vs: h.vs});
```

Проверка, которая ловит оба вида за секунду: **посмотреть на сам JSON ответа.**
Если в полезной нагрузке есть слово на любом естественном языке — это баг.

```bash
curl -s .../api/accounts/overview | grep -P '[А-Яа-я]' && echo "в ответе есть текст!"
```

Правило: **в ответе API не должно быть ни одного слова, которое человек прочтёт
как текст.** Коды, числа, флаги — да; названия и фразы — нет.

**Как это вообще заметили:** владелец переключил интерфейс на английский, и
панель стала смесью — переведённые строки по-английски, зашитые по-русски. Смесь
языков на одном экране — самый быстрый способ найти хардкод: **переключи язык и
посмотри, что не изменилось.**
