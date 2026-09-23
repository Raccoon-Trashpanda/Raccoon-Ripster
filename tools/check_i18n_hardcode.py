r"""Поиск захардкоженных строк в интерфейсе — НАСТОЯЩИМ парсером, не регулярками.

Зачем отдельный инструмент. `check_i18n_keys.py` отвечает на вопрос «ключ
упомянут, а в таблице его нет?». Этот — на обратный: «текст на экране есть, а
ключа/атрибута нет?». Оба нужны: они ловят разные половины одной беды.

Почему парсер, а не греп. 10.08.2026 эвристика «считать <div> построчно» на
settings.html дала ЛОЖНОЕ «чисто»: pool-хинты Deezer/SoundCloud/Yandex прятались
в вложенных узлах, и их нашёл владелец глазами. Поэтому разметка разбирается
`html.parser` со стеком тегов, entity-ссылки (`&#1056;`) раскодываются, текст
ищется по дереву, а не по строкам файла.

Что ловит (каждый пункт — своя категория в отчёте):
  A  видимый текст с кириллицей без `data-i18n*` на узле и без него у предков;
  B  `placeholder=`/`title=`/`alt=`/`aria-label=` с кириллицей без
     `data-i18n-ph`/`data-i18n-title`/`data-i18n-aria` (переведённая подпись с
     непереведённым подсказчиком — это полпочинки);
  C  `data-i18n` (textContent) на узле, внутри которого есть вложенные теги:
     теги СОТРУТСЯ, нужен `data-i18n-html`;
  D  текст, который РОВНО совпадает со значением из таблицы `ru`/`en`, но
     атрибута нет — ключ существует и просто не приложен;
  E  в JS: кириллица в контексте вывода (textContent/innerHTML/toast/label:/
     title:/template с тегом) — «верный ключ, неверный момент», applyLang()
     до этого текста не дотягивается;
  F  в JS/HTML: латинское служебное слово-статус (род `DOWNLOADING`), которое
     есть в английской таблице как значение, но в код попало литералом;
  G  модульный `const`-массив, зовущий `t()` на загрузке: значение вычисляется
     один раз и застывает в языке загрузки (чинится геттером);
  P  python: кириллица в носителе сообщения (`detail=`, `"error":`,
     `HTTPException(`), а вокруг неё ни `imsg(` (±2 строки), ни `error_key`
     (±4) — на экран уйдёт русский. Консольные логи, print и докстринги НЕ
     считаем: их переводить не надо, а наивный греп по кириллице завышает в
     разы (см. правило про auth.py: 46 из 52 уже были переведены);
  R  python: справочная таблица верхнего уровня с русскими ИМЕНАМИ (род
     `_COUNTRY_TZ = {'NZ': 'Новая Зеландия'}`) — продукт берёт из неё подпись
     и печатает при любом языке. Чинится отдачей кода, а не имени.

Запуск:  python tools/check_i18n_hardcode.py [--quiet] [--all]
         python tools/check_i18n_hardcode.py --py       (только бэкенд)
         python tools/check_i18n_hardcode.py --gate     (гейт: падает, только
                                                        если стало ХУЖЕ планки)
         python tools/check_i18n_hardcode.py --selftest (самопроверка на
                                                        засеянном хардкоде)
Код возврата: 0 — чисто (в --gate: не хуже планки), 1 — есть находки категории
A/B/E/F/G/H/P, 2 — selftest не прошёл. Категория C считается предупреждением
(иногда вложенный текст — мусорный пробел), D — тоже, но чинится механически.

Пороги осознанные: НЕ считаем текстом символы, emoji, числа, версии, URL,
имена собственных сервисов (Apple Music, Qobuz, ...) и машинные коды — они
языконейтральны (см. скилл ripster-i18n, «Skip pure symbols/emoji/names»).
"""
from __future__ import annotations

import argparse
import html
import json
import os
import re
import sys
import tempfile
from html.parser import HTMLParser
from pathlib import Path

# Коробка для самопроверки гейта: путь можно подменить переменной окружения и
# прогнать сканер по копии дерева, не трогая рабочий репозиторий.
_REPO = Path(os.environ.get("RIPSTER_REPO") or Path(__file__).resolve().parent.parent)
_TREES = ("static", "github_setup/static")
_PY_TREES = ("ripster", "github_setup/ripster")
# Планка «известного долга»: гейт падает только когда находок СТАЛО БОЛЬШЕ.
BASELINE = _REPO / "tools" / "i18n_hardcode_baseline.json"


def unique_counts(findings) -> dict[str, int]:
    """Счёт по категориям без дублей: зеркало github_setup/ — те же строки."""
    seen: dict[str, set] = {}
    for cat, name, _line, text, _note in findings:
        if cat not in HARD:
            continue
        rel = name[len("github_setup/"):] if name.startswith("github_setup/") else name
        seen.setdefault(cat, set()).add((rel, " ".join(text.split())))
    return {k: len(v) for k, v in seen.items()}

CYR = re.compile(r"[А-Яа-яёЁіІїЇєЄ]")
WORD = re.compile(r"[A-Za-zА-Яа-яёЁ]{2,}")
# Языконейтральное: одно «слово», похожее на имя сервиса/код/версию/число.
NEUTRAL = re.compile(
    r"""^(?:
        [A-Za-z0-9_.:+/\-]+                      # один латинский токен: Qobuz, v3.7
        | \d+(?:[.,:~\-]\d+)?                     # числа, скорости, диапазоны
        | \$\{[^}]*\}                             # интерполяция шаблона
        | https?://\S+                            # адрес
        | [A-Z][A-Z0-9_]{1,20}                    # MACHINE_CODE
      )$""",
    re.X,
)
# Файлы-исключения: таблица переводов и её зеркало (там русский и обязан лежать).
SKIP_JS_NAMES = {"i18n.js"}
# HTML: эти узлы — не пользовательский текст.
NON_VISIBLE = {"script", "style", "textarea", "code", "pre", "noscript", "template"}
# узел, текст которого закрывает не атрибут, а скрипт (см. sets_title ниже)
SCRIPT_OWNED = {"title"}
I18N_ATTRS = ("data-i18n", "data-i18n-html", "data-i18n-ph", "data-i18n-title")
ATTR_NEEDS = {  # атрибут → атрибут i18n, который его закрывает
    "placeholder": "data-i18n-ph",
    "title": "data-i18n-title",
    "alt": "data-i18n-alt",
    "aria-label": "data-i18n-aria",
}

# ── JS: контексты, где строка уходит на экран ───────────────────────────────
DISPLAY_JS = re.compile(
    r"""(?:
        \.(?:textContent|innerText|innerHTML|outerHTML)\s*(?:[+=]{1,2}|$)
      | insertAdjacentHTML\s*\(
      | setAttribute\s*\(\s*['"](?:title|placeholder|aria-label|alt|label|data-tip)
      | (?:toast|notify|alert|confirm|prompt|showToast|pushToast|say|err|fail)\s*\(
      | (?:label|title|hint|text|tip|tooltip|placeholder|errmsg|message|body|sub|caption|desc|name)\s*[:=]
      | \bhtml\s*[:=]
      | (?:label|title|hint|text|tip|tooltip|placeholder)\s*\+=
      | [>?]\s*$
    )""",
    re.X,
)
# Шаблонный литерал, внутри которого строят разметку, — текст уйдёт в DOM как есть.
HTML_IN_TEMPLATE = re.compile(r"<\s*(?:div|span|button|label|option|td|th|p|li|a|b|i|small|strong|em|code|h[1-6]|svg|input|select|details|summary)\b", re.I)
TOP_LEVEL_DECL = re.compile(r"^(?:const|let|var)\s+[A-Za-z_$][\w$]*\s*=")
KEY_CALL = re.compile(r"\b(?:t|ti)\s*\(")


def load_tables(i18n_path: Path) -> tuple[dict[str, str], dict[str, str], list[str]]:
    """Ключи ru/en из словаря продукта; сюда же подмешивается словарь панели
    (`static/panel/i18n.panel.js` дополняет LANG теми же ru/en): без него ключи
    `m.*` выглядели бы «несуществующими»."""
    tables, dupes = {}, []
    files = [i18n_path, i18n_path.parent.parent / "panel" / "i18n.panel.js"]
    for src_path in files:
        if not src_path.exists():
            continue
        src = src_path.read_text(encoding="utf-8", errors="replace")
        block_re = re.compile(r"^\s*(?:(?:var|const|let)\s+)?(ru|en|hi|ja|zh)\s*(?::|=)\s*\{", re.M)
        marks = [(m.group(1), m.start()) for m in block_re.finditer(src)]
        entry_re = re.compile(r"""['"]([a-zA-Z][a-zA-Z0-9_.]*)['"]\s*:\s*(['"])((?:\\.|(?!\2).)*)\2""")
        for m in entry_re.finditer(src):
            prior = [n for n, p in marks if p < m.start()]
            if not prior or prior[-1] not in ("ru", "en"):
                continue
            lang = prior[-1]
            key, value = m.group(1), html.unescape(m.group(3).replace("\\'", "'").replace('\\"', '"'))
            table = tables.setdefault(lang, {})
            if key in table and table[key] != value:
                dupes.append(f"{key}: {table[key]!r} vs {value!r}")
            table[key] = value
    return tables.get("ru", {}), tables.get("en", {}), dupes


def neutral(text: str) -> bool:
    t = text.strip()
    if not t or not WORD.search(t):
        return True
    if NEUTRAL.match(t):
        return True
    return not CYR.search(t)


# ── HTML ─────────────────────────────────────────────────────────────────────
class HtmlWalker(HTMLParser):
    """Обход со стеком: узел «закрыт» переводом, если data-i18n* на нём или на предке."""

    def __init__(self, ru: dict[str, str], en: dict[str, str], skip: set[str] = frozenset()):
        super().__init__(convert_charrefs=True)
        self.ru, self.en = ru, en
        self.skip = NON_VISIBLE | skip
        self.stack: list[dict] = []
        self.findings: list[tuple[str, int, str, str]] = []  # (cat, line, text, note)
        self.nonvisible = 0

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        covered = tag in self.skip or (self.stack and self.stack[-1]["covered"])
        i18n_plain = a.get("data-i18n")
        for name, val in a.items():
            if name in I18N_ATTRS or name == "data-i18n-aria":
                if val not in self.ru and val not in self.en:
                    self.findings.append(("H", self.getpos()[0], val,
                                          f"{tag}[{name}]: ключа нет ни в ru, ни в en — "
                                          f"на экране останется авторский текст"))
                elif val not in self.en:
                    self.findings.append(("H", self.getpos()[0], val, f"{tag}[{name}]: только в ru"))
                elif val not in self.ru:
                    self.findings.append(("H", self.getpos()[0], val, f"{tag}[{name}]: только в en"))
        self.stack.append({
            "tag": tag, "attrs": a, "covered": covered or bool(i18n_plain)
            or bool(a.get("data-i18n-html")),
            "i18n_plain": i18n_plain, "open_line": self.getpos()[0],
            "kids_with_tags": False, "had_text": False,
        })
        if self.stack[-1]["covered"] and not a.get("data-i18n-html") and i18n_plain:
            # отметим родителя: вложенные теги при data-i18n сотрутся
            self.stack[-1]["nested_risk"] = True
        for prev in reversed(self.stack[:-1]):
            if prev.get("nested_risk"):
                prev["kids_with_tags"] = True
                break
        # атрибуты
        for attr, need in ATTR_NEEDS.items():
            val = a.get(attr)
            if val and CYR.search(val) and not a.get(need):
                self.findings.append(("B", self.getpos()[0], val, f"{tag}[{attr}]"))

    def handle_endtag(self, tag):
        for i in range(len(self.stack) - 1, -1, -1):
            if self.stack[i]["tag"] == tag:
                node = self.stack.pop(i)
                if node.get("nested_risk") and node["kids_with_tags"]:
                    self.findings.append(("C", node["open_line"], node["i18n_plain"] or "",
                                          f"внутри data-i18n=«{node['i18n_plain']}» есть вложенные теги — "
                                          f"нужен data-i18n-html"))
                if node.get("kids_with_tags") or node.get("had_text"):
                    for prev in reversed(self.stack):
                        if prev.get("nested_risk"):
                            prev["kids_with_tags"] = True
                            break
                return
        # парный хвост от кривой разметки — игнорируем

    def handle_data(self, data):
        text = " ".join(data.split())
        if not text:
            return
        top = self.stack[-1] if self.stack else None
        if top is not None:
            top["had_text"] = True
        if top is None or top["covered"]:
            return
        line = self.getpos()[0]
        if CYR.search(text):
            hit = [k for k, v in list(self.ru.items()) + list(self.en.items())
                   if " ".join(v.split()) == text]
            if hit:
                self.findings.append(("D", line, text,
                                      f"ключ есть в таблице ({hit[0]}), атрибута нет"))
            else:
                self.findings.append(("A", line, text, self._path()))
        elif self.en and text.upper() == text and len(WORD.findall(text)) == 1:
            hit = [k for k, v in self.en.items()
                   if " ".join(v.split()).upper() == text.upper()]
            if hit:
                self.findings.append(("F", line, text, f"англ. статус без t(): ключ {hit[0]}"))

    def _path(self) -> str:
        return "/".join(n["tag"] for n in self.stack[-3:]) or "root"


def scan_html(path: Path, ru, en, skip: set[str] = frozenset()) -> list[tuple[str, int, str, str]]:
    w = HtmlWalker(ru, en, skip)
    w.feed(path.read_text(encoding="utf-8", errors="replace"))
    return [(c, l, t, n) for c, l, t, n in w.findings]


# ── JS ───────────────────────────────────────────────────────────────────────
def js_string_literals(src: str):
    """(start_line, text, is_template, prefix) для каждого строкового литерала.

    Свой сканер, а не regex: нужно корректно проходить комментарии, шаблонные
    литералы с ${} и регулярки, в которых кавычки. Regex на таком расползается.
    """
    out = []
    i, line = 0, 1
    n = len(src)
    prev_sig = ""  # последний значимый символ вне строк/пробелов
    while i < n:
        c = src[i]
        if c == "\n":
            line += 1
            i += 1
            continue
        if c == "/" and i + 1 < n and src[i + 1] == "/":
            j = src.find("\n", i)
            i = n if j < 0 else j
            continue
        if c == "/" and i + 1 < n and src[i + 1] == "*":
            j = src.find("*/", i + 2)
            j = n if j < 0 else j + 2
            line += src.count("\n", i, j)
            i = j
            continue
        if c == "/" and prev_sig in "(,=:[!&|?{};+-*%<>~^\n" and i + 1 < n and src[i + 1] not in " /*=":
            # регулярка: [ ] внутри меняют правила
            j = i + 1
            cls = False
            while j < n:
                d = src[j]
                if d == "\\":
                    j += 2
                    continue
                if d == "[":
                    cls = True
                elif d == "]":
                    cls = False
                elif d == "/" and not cls:
                    break
                elif d == "\n":
                    break
                j += 1
            line += src.count("\n", i, min(j, n))
            i = j + 1
            prev_sig = "/"
            continue
        if c in "'\"`":
            quote = c
            start_line = line
            j = i + 1
            buf = []
            nested = 0
            while j < n:
                d = src[j]
                if d == "\\":
                    if j + 1 < n:
                        if src[j + 1] == "\n":
                            line += 1
                        buf.append(src[j + 1] if src[j + 1] != "\n" else "")
                    j += 2
                    continue
                if d == "\n":
                    if quote == "`":
                        line += 1
                        buf.append(d)
                        j += 1
                        continue
                    break
                if quote == "`" and d == "$" and j + 1 < n and src[j + 1] == "{":
                    nested += 1
                    buf.append("${")
                    j += 2
                    continue
                if quote == "`" and nested and d == "}":
                    nested -= 1
                    buf.append("}")
                    j += 1
                    continue
                if d == quote and nested == 0:
                    break
                buf.append(d)
                j += 1
            text = "".join(buf)
            prefix = src[max(0, i - 90):i]
            out.append((start_line, text, quote == "`", prefix))
            line += src.count("\n", i, min(j, n))
            prev_sig = quote
            i = j + 1
            continue
        if not c.isspace():
            prev_sig = c
        i += 1
    return out


# Как называют себя языки в собственном написании — «Русский» в списке языков
# переводить НЕЛЬЗЯ: это не строка интерфейса, а опознание языка для человека,
# который его читает. Без этой стоп-фразы сканер орёт на правильном коде
# (языковой селектор в app.js), а инструмент, который всегда красный, не читают.
ENDONYMS = {
    "Русский", "English", "हिन्दी", "日本語", "中文", "Español", "Português",
    "Deutsch", "Français", "Italiano", "Türkçe", "Українська", "Polski",
}
CSSISH = re.compile(r"[{};]")


def _displayable(text: str) -> str:
    """Текст литерала так, как его увидит глаз: без CSS-комментариев и флагов."""
    body = re.sub(r"/\*.*?\*/", " ", text, flags=re.S)
    body = re.sub(r"^\s*//.*$", " ", body, flags=re.M)
    if CSSISH.search(body) and "<" not in body:
        # шаблон с CSS внутри — это стили, а не текст на экране
        return ""
    return body


def _drop_endonyms(text: str) -> str:
    """Убирает самоназвания языков: «🇷🇺 Русский» в списке языков — не дефект,
    а остальной русский в том же литерале сканеру всё равно виден."""
    return re.sub(r"\b(" + "|".join(map(re.escape, ENDONYMS)) + r")\b", " ", text)


def _in_display_context(prefix: str, text: str) -> str:
    """Пусто — не видно на экране; иначе — короткий ярлык контекста."""
    tail = prefix[-70:]
    tail_nc = re.sub(r"/\*.*?\*/", "", tail, flags=re.S)
    if DISPLAY_JS.search(tail_nc):
        return tail_nc.strip().splitlines()[-1].strip()[-46:]
    if HTML_IN_TEMPLATE.search(text) and CYR.search(text):
        return "template-разметка"
    return ""


def scan_js(path: Path, ru, en) -> list[tuple[str, int, str, str]]:
    src = path.read_text(encoding="utf-8", errors="replace")
    lines = src.splitlines()
    findings: list[tuple[str, int, str, str]] = []
    for start_line, text, is_tpl, prefix in js_string_literals(src):
        if not text.strip() or not WORD.search(text):
            continue
        flat = " ".join(text.split())
        # аргумент t('key')/ti('key', …) — это имя ключа, а не текст на экране
        if not is_tpl and re.search(r"\b(?:t|ti)\s*\(\s*$", prefix[-24:]):
            continue
        ctx = _in_display_context(prefix, text)
        body = _drop_endonyms(_displayable(flat))
        if body and CYR.search(body):
            if ctx:
                findings.append(("E", start_line, body.strip(), f"в обход applyLang: …{ctx}"))
            else:
                findings.append(("E?", start_line, body.strip(), "кириллица в JS — проверить, доезжает ли до экрана"))
        elif ctx and en and flat.upper() == flat and len(flat) > 3 and " " not in flat.strip():
            hit = [k for k, v in en.items() if " ".join(v.split()).upper() == flat.upper()]
            if hit:
                findings.append(("F", start_line, flat, f"латинский статус литералом: ключ {hit[0]} есть в en"))
    # G: модульный const с t(), вычисленным один раз при загрузке
    cur: list[str] = []
    start = 0
    depth = 0
    for idx, raw in enumerate(lines, start=1):
        if depth <= 0:
            if not TOP_LEVEL_DECL.match(raw) or not ("[" in raw or "{" in raw):
                continue
            start, cur = idx, [raw]
        else:
            cur.append(raw)
        depth += raw.count("[") + raw.count("{") - raw.count("]") - raw.count("}")
        if depth <= 0:
            body = "\n".join(cur)
            if KEY_CALL.search(body) and not re.search(r"\bget\s|\bfunction\b|=>", body):
                findings.append(("G", start, cur[0].strip()[:78],
                                 "значение t() фиксировано при загрузке — нужен геттер"))
            cur, depth = [], 0
    return sorted(findings, key=lambda f: f[1])


# ── бэкенд: что уходит в интерфейс ДОСЛОВНО ──────────────────────────────────
# Критерий взят из .claude/rules/api-messages-i18n.md и он же — урок: наивный
# греп по кириллице ЗАВЫШАЕТ в разы (в auth.py из 52 «русских» строк 46 уже
# имели error_key). Поэтому считаем находкой только строку-носитель сообщения,
# вокруг которой нет ни imsg(, ни error_key.
PY_CARRIER = re.compile(
    r"""detail\s*=|["']error["']\s*:|["']message["']\s*:|["']msg["']\s*:|"""
    r"""HTTPException\s*\(|["']hint["']\s*:|["']desc["']\s*:""", re.X)
PY_IGNORE_LINE = re.compile(r"""^\s*(#|import\b|from\b|logger|logging|log\.|print\()""")
PY_REF_TABLE = re.compile(r"""^_?[A-Z][A-Z0-9_]{2,}\s*(?::[^=]+)?=\s*[\{\[]""")
_PY_STR = re.compile(r"""['"]([^'"\n]{2,})['"]""")


def scan_py(path: Path) -> list[tuple[str, int, str, str]]:
    src = path.read_text(encoding="utf-8", errors="replace")
    lines = src.splitlines()
    findings: list[tuple[str, int, str, str]] = []
    in_doc = ""                      # "" или открытая тройная кавычка
    for i, raw in enumerate(lines):
        # строки внутри тройных кавычек — это докстринги и примеры, их на экран
        # продукт не печатает; без этого фильтра докстринг imsg() давал «находки»
        body = raw
        if in_doc:
            if in_doc not in raw:
                continue
            body = raw.split(in_doc, 1)[1]
            in_doc = ""
        for q in ('"""', "'''"):
            if body.count(q) % 2:
                in_doc = q
                body = body.split(q, 1)[0]
                break
        raw = body
        if not CYR.search(raw) or PY_IGNORE_LINE.match(raw.strip()) or raw.strip().startswith("#"):
            continue
        # кириллица только в комментарии — это не текст на экране
        code = raw.split("#", 1)[0] if "#" in raw else raw
        if not CYR.search(code):
            continue
        near = "\n".join(lines[max(0, i - 2):i + 3])
        if re.search(r"\bimsg\s*\(", near):
            continue
        near = "\n".join(lines[max(0, i - 4):i + 5])
        if re.search(r"""["']error_key["']|\berror_key\b""", near):
            continue
        own = PY_CARRIER.search(code)
        carrier = own or any(PY_CARRIER.search(lines[j])
                             for j in range(max(0, i - 3), min(len(lines), i + 1)))
        if not carrier:
            continue
        txt = " ".join(code.split())[:96]
        # строка без собственного носителя, висящая на предыдущей, — это хвост
        # одного сообщения (`"msg": "начало " "продолжение"`), а не второй дефект
        if not own and findings and findings[-1][0] == "P" and i + 1 - findings[-1][1] <= 3:
            cat, line, prev, note = findings[-1]
            findings[-1] = (cat, line, (prev + " …")[:150], note)
            continue
        findings.append(("P", i + 1, txt, "уходит в UI без ключа (нет imsg/error_key рядом)"))
    # R: справочные таблицы верхнего уровня — продукт берёт из них ИМЕНА вместо
    # кодов, и на экране они русские при любом языке (скилл: «ship codes, not names»)
    for i, raw in enumerate(lines):
        if not PY_REF_TABLE.match(raw):
            continue
        depth, body = 0, []
        for j in range(i, min(len(lines), i + 40)):
            body.append(lines[j])
            depth += lines[j].count("{") + lines[j].count("[") - lines[j].count("}") - lines[j].count("]")
            if depth <= 0:
                break
        blob = "\n".join(body)
        vals = [v for v in _PY_STR.findall(blob) if CYR.search(v)]
        if vals:
            findings.append(("R", i + 1, raw.split("=", 1)[0].strip()[:60],
                             f"справочная таблица с русскими именами: {len(vals)} знач., "
                             f"напр. «{'» / «'.join(vals[:2])}»"))
    return sorted(findings, key=lambda f: f[1])


# ── прогон ───────────────────────────────────────────────────────────────────
CATS = {
    "A": "видимый русский в разметке без data-i18n",
    "B": "placeholder/title/alt по-русски без data-i18n-ph/-title",
    "C": "data-i18n там, где нужны теги (data-i18n-html)",
    "D": "ключ в таблице есть, атрибут не поставлен",
    "E": "русский в JS мимо applyLang() (верный ключ, неверный момент)",
    "E?": "кириллица в JS — на триаж",
    "F": "латинский статус литералом вместо t()",
    "G": "модульный const застыл в языке загрузки",
    "H": "ключ в атрибуте data-i18n* отсутствует в таблице",
    "P": "python: сообщение уходит в UI без i18n-ключа",
    "R": "python: справочная таблица с русскими именами",
}
HARD = {"A", "B", "E", "F", "G", "H", "P"}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--quiet", action="store_true")
    ap.add_argument("--selftest", action="store_true", help="проверить сам сканер на засеянном хардкоде")
    ap.add_argument("--all", action="store_true", help="показывать и триаж (E?)")
    ap.add_argument("--py", action="store_true", help="только бэкенд (ripster/**.py)")
    ap.add_argument("--gate", action="store_true",
                    help="не краснеть на известный долг: падать, только если находок СТАЛО БОЛЬШЕ "
                         f", чем в {BASELINE.name}")
    ap.add_argument("--write-baseline", action="store_true",
                    help="записать текущие counts как планку")
    args = ap.parse_args()
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")
        except Exception:
            pass
    if args.selftest:
        return selftest()

    all_findings: list[tuple[str, str, int, str, str]] = []
    if not args.py:
        for rel in _TREES:
            root = _REPO / rel
            if not root.exists():
                print(f"пропуск: нет {rel}")
                continue
            ru, en, dupes = load_tables(root / "js" / "i18n.js")
            if dupes:
                print(f"⚠ {rel}: ключи с разным значением в одной таблице: {len(dupes)}")
            findings: list[tuple[str, str, int, str, str]] = []
            targets = [(root / "index.html", "html")]
            targets += [(p, "html") for p in sorted((root / "views").glob("*.html"))]
            targets += [(p, "html") for p in sorted((root / "panel").glob("*.html"))]
            js_files = [p for p in sorted((root / "js").glob("*.js")) if p.name not in SKIP_JS_NAMES]
            js_files += sorted((root / "panel").glob("*.js"))
            # <title> документа переводит сам скрипт (`document.title = t(...)`),
            # поэтому авторский текст в разметке — не хардкод.
            sets_title = any("document.title" in p.read_text(encoding="utf-8", errors="replace")
                             for p in js_files)
            targets += [(p, "js") for p in js_files]
            for f, kind in targets:
                if not f.exists():
                    continue
                relname = f.relative_to(_REPO).as_posix()
                raw = (scan_html(f, ru, en, SCRIPT_OWNED if sets_title else frozenset())
                       if kind == "html" else scan_js(f, ru, en))
                for cat, line, text, note in raw:
                    findings.append((cat, relname, line, text, note))
            hard = [x for x in findings if x[0] in HARD]
            soft = [x for x in findings if x[0] not in HARD]
            all_findings += findings
            print(f"── {rel}: жёстких {len(hard)}, на триаж {len(soft)}")
            if not args.quiet:
                for cat, name, line, text, note in sorted(hard + soft, key=lambda x: (x[0], x[1], x[2])):
                    if cat == "E?" and not args.all:
                        continue
                    t = text if len(text) <= 96 else text[:93] + "…"
                    print(f"  {cat:<2} {name}:{line}\n      «{t}»\n      {CATS[cat]}{' — ' + note if note else ''}")
    for rel in _PY_TREES:
        root = _REPO / rel
        if not root.exists():
            continue
        pys = [p for p in sorted(root.rglob("*.py")) if "__pycache__" not in p.parts]
        findings = []
        for f in pys:
            for cat, line, text, note in scan_py(f):
                findings.append((cat, f.relative_to(_REPO).as_posix(), line, text, note))
        hard = [x for x in findings if x[0] in HARD]
        soft = [x for x in findings if x[0] not in HARD]
        all_findings += findings
        print(f"── {rel}: жёстких {len(hard)} (P), справочных таблиц {len(soft)} (R)")
        for cat, name, line, text, note in sorted(hard + soft, key=lambda x: (x[0], x[1], x[2])):
            if args.quiet or (cat == "R" and not args.all):
                continue
            t = text if len(text) <= 96 else text[:93] + "…"
            print(f"  {cat:<2} {name}:{line}\n      «{t}»\n      {CATS[cat]}{' — ' + note if note else ''}")

    uniq = unique_counts(all_findings)
    total = sum(uniq.values())
    print(f"\nжёстких находок: {total} без зеркала, по категориям: "
          + ", ".join(f"{k}={v}" for k, v in sorted(uniq.items())))
    if args.write_baseline:
        BASELINE.write_text(json.dumps(uniq, ensure_ascii=False, indent=1, sort_keys=True),
                            encoding="utf-8")
        print(f"планка записана в {BASELINE.relative_to(_REPO).as_posix()}")
        return 0
    if args.gate:
        if not BASELINE.exists():
            print(f"✗ нет планки {BASELINE.name} — запиши её: --write-baseline")
            return 1
        base = json.loads(BASELINE.read_text(encoding="utf-8"))
        worse = {k: (base.get(k, 0), v) for k, v in uniq.items() if v > base.get(k, 0)}
        if worse:
            print("✗ СТАЛО ХУЖЕ (категория: было → стало):")
            for k, (b, v) in sorted(worse.items()):
                print(f"    {k}: {b} → {v}")
            print("\nСм. скилл ripster-i18n; отчёт печатает `python tools/check_i18n_hardcode.py`.")
            return 1
        # НЕ base.items(): там «стало» печаталось значением самой планки, и
        # строка врала про результат починки (B: 14→14 вместо 14→13).
        better = {k: (base[k], uniq.get(k, 0)) for k in base if base[k] > uniq.get(k, 0)}
        if better:
            print("○ находок стало меньше — снизь планку: --write-baseline  ("
                  + ", ".join(f"{k}: {b}→{v}" for k, (b, v) in sorted(better.items())) + ")")
        print("✓ новых хардкодов нет")
        return 0
    return 1 if total else 0


# ── самопроверка: сканер обязан ловить засеянное ────────────────────────────
FIXTURE_HTML = """<!doctype html><html><body>
<div class="ok" data-i18n="s.some">Всё правильно</div>
<div class="hint"><span class="lbl">Срок пула истёк — обновите cookies</span></div>
<input type="text" placeholder="Вставьте ссылку">
<div data-i18n="s.with_tags">См. <code>config.yaml</code> ниже</div>
<button title="Пересчитать">↺</button>
<div class="mirror">Скачать</div>
<div data-i18n="s.never_added">Тут должен быть перевод</div>
<div>DOWNLOADING</div>
<p>Qobuz</p><p>3.7.0</p><p>12 ×</p><p>🎧</p>
</body></html>
"""
FIXTURE_JS = """const LIST = [{value:'yandex', label: 'Жёлтый ' + t('s.yandex')}];
const OK = [{value:'x', get label(){ return t('s.yandex'); }}];
function paint(){ el.textContent = 'Распаковка невозможна'; }
toast('Готово', 'ok');
const RE = /["']/;
const MACHINE = 'DOWNLOADING';
el.innerHTML = `<div class="k">Загрузка файлов</div>`;
sel.innerHTML = `<option value="ru">🇷🇺 Русский</option><option value="en">🇬🇧 English</option>`;
css.textContent = `#x{color:red} /* Подсветка активной строки */ .on{font-weight:700}`;
"""
FIXTURE_I18N = """var LANG = {
 ru:{ 's.some':'Всё правильно','s.with_tags':'См. <code>config.yaml</code> ниже','s.yandex':'Жёлтый',
      'dlmeta.running':'Скачивается','dlmirror':'Скачать','ga.q':'Распаковка невозможна'},
 en:{ 's.some':'All right','s.with_tags':'See <code>config.yaml</code>','s.yandex':'Yellow',
      'dlmeta.running':'Downloading','dlmirror':'Download'},
};
"""
FIXTURE_PY = '''
from fastapi import HTTPException
from ripster.i18n_msg import imsg

_COUNTRY_TZ = {"NZ": "Новая Зеландия", "US": "США"}

def bad1(req):
    raise HTTPException(400, "Аккаунт уже привязан к другому пользователю")

def bad2(row):
    return {"ok": False, "error": "Нет доступа к файлу выгрузки"}

def good1(req):                      # уже переведён — обязан молчать
    raise HTTPException(400, imsg("err.bound", "Аккаунт уже привязан"))

def good2(row):
    return {"ok": False, "error_key": "err.no_file", "error": "Нет доступа к файлу выгрузки"}

def good3(name):                     # консольный лог — не текст на экране
    log.info("Загрузка завершена для %s", name)
    print("Сохраняю очередь")
'''
# (категория, подстрока-маркер, в каком файле ждать)
SEEDS = [
    ("A", "Срок пула истёк", "index.html"),
    ("B", "Вставьте ссылку", "index.html"),
    ("B", "Пересчитать", "index.html"),
    ("C", "s.with_tags", "index.html"),
    ("D", "Скачать", "index.html"),
    ("F", "DOWNLOADING", "index.html"),
    ("E", "Распаковка невозможна", "app.js"),
    ("E", "Готово", "app.js"),
    ("E", "Загрузка файлов", "app.js"),
    ("G", "LIST", "app.js"),
    ("H", "s.never_added", "index.html"),
    ("P", "Аккаунт уже привязан", "routes.py"),
    ("P", "Нет доступа к файлу выгрузки", "routes.py"),
    ("R", "COUNTRY_TZ", "routes.py"),
]
NEGATIVES = ["s.some", "OK =", "MACHINE", "Qobuz", "3.7.0", "🎧", "Русский", "Подсветка активной строки",
             "imsg", "error_key", "Загрузка завершена для", "Сохраняю очередь"]


def selftest() -> int:
    with tempfile.TemporaryDirectory() as td:
        root = Path(td) / "static"
        (root / "js").mkdir(parents=True)
        (root / "views").mkdir()
        (root / "index.html").write_text(FIXTURE_HTML, encoding="utf-8")
        (root / "js" / "app.js").write_text(FIXTURE_JS, encoding="utf-8")
        (root / "js" / "i18n.js").write_text(FIXTURE_I18N, encoding="utf-8")
        py = root / "routes.py"
        py.write_text(FIXTURE_PY, encoding="utf-8")
        ru, en, _ = load_tables(root / "js" / "i18n.js")
        found = []
        for cat, line, text, note in scan_html(root / "index.html", ru, en):
            found.append((cat, text, note, "index.html"))
        for cat, line, text, note in scan_js(root / "js" / "app.js", ru, en):
            found.append((cat, text, note, "app.js"))
        for cat, line, text, note in scan_py(py):
            found.append((cat, text, note, "routes.py"))
        blob = "\n".join(f"{c}|{t}|{n}|{f}" for c, t, n, f in found)
        bad = 0
        print("самопроверка сканера (засеянный хардкод обязан быть найден):")
        for cat, marker, fname in SEEDS:
            hit = any(c == cat and marker in txt and f == fname
                      for c, txt, _note, f in found)
            print(f"  {'✓' if hit else '✗ НЕ НАЙДЕНО'}  {cat} {marker!r} в {fname}")
            bad += 0 if hit else 1
        print("ложные срабатывания (нейтральное и правильно закрытое обязано молчать):")
        for neg in NEGATIVES:
            noise = [x for x in found if neg in x[1]]
            print(f"  {'✓' if not noise else '✗ ШУМ'}  {neg!r}")
            bad += 0 if not noise else 1
    print("\nСАМОПРОВЕРКА ПРОВАЛЕНА — сканеру нельзя верить" if bad
          else "\nсамопроверка пройдена: засевается, ложных нет")
    return 2 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
