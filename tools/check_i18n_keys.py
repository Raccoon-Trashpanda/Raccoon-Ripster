r"""Проверка ключей i18n: и в JS (`t()`/`ti()`/обёртки), и в разметке
(`data-i18n*`), и на стороне сервера (кто ОТДАЁТ ключ клиенту).

Зачем. Если ключ упомянут, но в таблице его НЕТ, `applyLang()` не показывает
сырой ключ — он оставляет авторский текст, который у нас русский. То есть
пропущенный ключ выглядит РОВНО как захардкоженная строка, и владелец дважды
сообщал «опять хардкод», хотя разметка была правильная.

Отдельно проверяется английская таблица: ключ, который есть только в `ru`,
отдаёт в английском интерфейсе русскую строку (fallback locale → en → ru).
Это второй способ получить «хардкод», не имея хардкода.

Что закрыто после 23.09.2026 (три слепых пятна, найденных i18n-воркером):
  (a) ключ с дефисом (`setup.wvd-manual.desc`) — символ ключа теперь включает '-';
  (b) ключ, собранный динамически (`t('setup.'+c.key+'.tag')`, шаблонные
      литералы): конечный набор разворачивается, если переменная идёт по
      литеральному массиву/объектам в ЭТОМ ЖЕ файле; что развернуть нельзя —
      уходит в СПИСОК НЕРАЗРЕШИМЫХ как справка, падением не считается;
  (c) вызовы через обёртки (`tplural`/`errKeyText`/`_tOr` в JS) и серверные
      поставщики ключа в Python (`imsg("k", …)`, `error_key=`/`"msg_key":`) —
      ключ обязан быть и в ru, и в en.

Запуск:  python tools/check_i18n_keys.py
Код возврата: 0 — чисто, 1 — есть проблемы (годится как гейт перед сборкой).
"""
from __future__ import annotations

import ast
import re
import sys
from pathlib import Path

_REPO = Path(__file__).resolve().parent.parent
_TREES = ("static", "github_setup/static")
# Словари, которые надо сложить, прежде чем судить о ключах: продукт и панель.
_DICTS = ("js/i18n.js", "panel/i18n.panel.js")
# Где ищутся ключи. Панель живёт отдельным документом со своей разметкой, и
# пока её файл не в списке, пропущенная строка в мобильном UI не ловилась ничем.
_SRC_GLOBS = ("js/*.js", "panel/*.js")
_DICT_NAMES = {Path(p).name for p in _DICTS}

# Python — ОДИН источник правды (не зеркалится, как статика). Ключи, которые
# сервер отдаёт клиенту, резолвятся тем же продуктовым словарём static/js, поэтому
# проверяем питон ровно раз и против продуктовой таблицы.
_MAIN_TREE = "static"
_PY_GLOBS = ("ripster/**/*.py", "tgbot/**/*.py", "app.py")

_BLOCK = re.compile(r"^\s*(?:var\s+|const\s+|let\s+)?(ru|en|hi|ja|zh)\s*[:=]\s*\{", re.M)
# Символ ключа. Дефис обязателен: `setup.wvd-manual.desc`, `qual.*.alac-hires.sub`
# живут в таблице и достаются динамикой `t('setup.'+c.key+'.tag')` — без дефиса
# `_ENTRY` их не регистрировал, и динамический разбор врал бы «ключа нет».
_KEY = r"[a-zA-Z][a-zA-Z0-9_.\-]*"
# Ключи таблиц пишут и в '', и в "" (так живёт блок ga.*). Regex только с ''
# объявлял существующие двойные кавычки «отсутствующими ключами» — 54 ложных
# находки на ровном месте.
_ENTRY = re.compile(r"""['"](%s)['"]\s*:""" % _KEY)
# Кто резолвит ключ на клиенте: t/ti — ядро, tplural — множественное число,
# errKeyText/_tOr — обёртки «ключ → текст с фолбэком». Все берут литеральным
# ПЕРВЫМ аргументом ключ; у обёрток аргумент может быть переменной — тогда это
# динамический вызов (пункт b), а не литерал.
_JS_WRAPPERS = ("t", "ti", "tplural", "_tOr", "errKeyText")
_JS_ALT = "|".join(sorted((re.escape(w) for w in _JS_WRAPPERS), key=len, reverse=True))
# t('key') / ti('key', {...}) — только литералы; t('cc.' + x) намеренно не тут.
_CALL = re.compile(r"\b(?:%s)\(\s*'(%s)'\s*[,)]" % (_JS_ALT, _KEY))
_ATTR = re.compile(r'data-i18n(?:-html|-ph|-title)?="(%s)"' % _KEY)
# Динамика: вызов переводчика с нетерминальным первым аргументом (склеенные
# куски или шаблонный литерал). Сам разбор — ниже, здесь только точка входа.
_DYN_HEAD = re.compile(r"\b(?:%s)\(" % _JS_ALT)

# Серверные поставщики ключа (Python). `imsg("k", ru, …)` собирает объект
# {key,params,msg}; поля вида error_key/warning_key/note_key/msg_key/label_key/
# info_key/why_key доезжают до клиента и резолвятся errKeyText()/ti() тем же
# словарём. Берём ТОЛЬКО литеральные значения (через ast) — докстринги-примеры
# и комментарии не порождают Call/Dict-узлов, ложных «ключа нет» не будет.
_PY_PRODUCERS = ("error_key", "warning_key", "note_key", "msg_key",
                 "label_key", "info_key", "why_key")


def tables(paths) -> dict[str, set[str]]:
    """Ключи по языковым блокам: {'ru': {...}, 'en': {...}, ...}.

    Файлов словаря может быть несколько: продукт (js/i18n.js) и мобильная
    панель (panel/i18n.panel.js) дополняют LANG одним Object.assign, поэтому и
    проверять их надо одним множеством — иначе ключи панели считались бы
    отсутствующими.
    """
    out: dict[str, set[str]] = {}
    for i18n in paths:
        src = i18n.read_text(encoding="utf-8")
        marks = [(m.group(1), m.start()) for m in _BLOCK.finditer(src)]
        for name, _ in marks:
            out.setdefault(name, set())
        for m in _ENTRY.finditer(src):
            prior = [n for n, p in marks if p < m.start()]
            if prior:
                out[prior[-1]].add(m.group(1))
    return out


# ─────────────────────────────  разбор динамики (JS)  ─────────────────────────
# Мини-сканер JS: нам не нужен парсер, достаточно корректно перескакивать
# строки/шаблоны/скобки, чтобы (1) разрезать аргумент вызова по верхнеуровневым
# запятым и «+», и (2) выделить тело литерального массива/объекта.

def _skip_string(s, i):
    """s[i] — открывающая ' или ". Вернуть индекс сразу за закрывающей."""
    q = s[i]
    i += 1
    n = len(s)
    while i < n:
        c = s[i]
        if c == "\\":
            i += 2
            continue
        if c == q:
            return i + 1
        i += 1
    return i


def _skip_template(s, i):
    """s[i] == '`'. Проходим шаблон, ныряя в ${...} сбалансированно."""
    i += 1
    n = len(s)
    while i < n:
        c = s[i]
        if c == "\\":
            i += 2
            continue
        if c == "`":
            return i + 1
        if c == "$" and i + 1 < n and s[i + 1] == "{":
            i = _skip_balanced(s, i + 1)
            continue
        i += 1
    return i


def _skip_balanced(s, i):
    """s[i] — открывающая ( [ {. Вернуть индекс сразу за парной закрывающей."""
    pairs = {"(": ")", "{": "}", "[": "]"}
    closers = set(pairs.values())
    stack = []
    n = len(s)
    while i < n:
        c = s[i]
        if c in "\"'":
            i = _skip_string(s, i)
            continue
        if c == "`":
            i = _skip_template(s, i)
            continue
        if c == "/" and i + 1 < n and s[i + 1] == "/":
            j = s.find("\n", i)
            i = j if j != -1 else n
            continue
        if c == "/" and i + 1 < n and s[i + 1] == "*":
            j = s.find("*/", i + 2)
            i = j + 2 if j != -1 else n
            continue
        if c in pairs:
            stack.append(pairs[c])
        elif c in closers:
            if stack and stack[-1] == c:
                stack.pop()
                if not stack:
                    return i + 1
        i += 1
    return i


def _strip_js_comments(s):
    """Заглушить // и /* */ в ПРОБЕЛЫ, не трогая содержимое строк и шаблонов.

    Длина и переводы строк сохраняются — номера строк в отчёте не съезжают.
    Обычный `re.sub("^\\s*//.*$")` снимает только строчные комментарии с начала
    строки, а `desc:'', // текст в i18n` внутри литерала массива живёт в КОНЦЕ
    строки: он разрывал разбор объекта на части.
    """
    out, i, n = [], 0, len(s)
    while i < n:
        c = s[i]
        if c in "\"'":
            j = _skip_string(s, i)
            out.append(s[i:j])
            i = j
            continue
        if c == "`":
            j = _skip_template(s, i)
            out.append(s[i:j])
            i = j
            continue
        if c == "/" and i + 1 < n and s[i + 1] == "/":
            j = s.find("\n", i)
            j = j if j != -1 else n
            out.append(" " * (j - i))
            i = j
            continue
        if c == "/" and i + 1 < n and s[i + 1] == "*":
            j = s.find("*/", i + 2)
            j = j + 2 if j != -1 else n
            out.append("".join(ch if ch == "\n" else " " for ch in s[i:j]))
            i = j
            continue
        out.append(c)
        i += 1
    return "".join(out)


def _split_top(s, sep):
    """Разрезать по `sep` верхнего уровня (не внутри строк/скобок/шаблонов)."""
    parts, cur, i, n = [], [], 0, len(s)
    while i < n:
        c = s[i]
        if c in "\"'":
            j = _skip_string(s, i)
            cur.append(s[i:j])
            i = j
            continue
        if c == "`":
            j = _skip_template(s, i)
            cur.append(s[i:j])
            i = j
            continue
        if c in "([{":
            j = _skip_balanced(s, i)
            cur.append(s[i:j])
            i = j
            continue
        if c == sep:
            parts.append("".join(cur))
            cur = []
            i += 1
            continue
        cur.append(c)
        i += 1
    parts.append("".join(cur))
    return parts


def _unquote(tok):
    t = tok.strip()
    if len(t) >= 2 and t[0] == t[-1] and t[0] in "\"'":
        return t[1:-1]
    return None


def _read_call_arg(s, lparen):
    """Текст ПЕРВОГО аргумента вызова, чья '(' стоит в s[lparen]."""
    i = lparen + 1
    n = len(s)
    depth = 0
    out = []
    while i < n:
        c = s[i]
        if depth == 0 and c == ",":
            return "".join(out), i
        if depth == 0 and c == ")":
            return "".join(out), i
        if c in "\"'":
            j = _skip_string(s, i)
            out.append(s[i:j])
            i = j
            continue
        if c == "`":
            j = _skip_template(s, i)
            out.append(s[i:j])
            i = j
            continue
        if c in "([{":
            depth += 1
            out.append(c)
            i += 1
            continue
        if c in ")]}":
            depth -= 1
            out.append(c)
            i += 1
            continue
        out.append(c)
        i += 1
    return "".join(out), n


def _parse_obj(s):
    """'{ key:"a", tag:true }' → {'key':'a', 'tag':'__TRUE__'}; None, если непонятно."""
    props = _split_top(s[1:-1], ",")
    o = {}
    for p in props:
        p = p.strip()
        if p == "":
            continue
        mm = re.match(r"^([A-Za-z_$][\w$]*)\s*:\s*(.+)$", p, re.S)
        if not mm:
            return None
        name, raw = mm.group(1), mm.group(2).strip()
        v = _unquote(raw)
        if v is not None:
            o[name] = v
        elif raw == "true":
            o[name] = "__TRUE__"
        elif raw == "false":
            o[name] = "__FALSE__"
        elif re.match(r"^\d", raw):
            o[name] = "__NUM__"
        else:
            o[name] = "__DYN__"
    return o


def _parse_array_body(body):
    items = [it.strip() for it in _split_top(body, ",")]
    parsed = []
    for s in items:
        if s == "":
            continue
        if s[0] == "{":
            o = _parse_obj(s)
            if o is None:
                return {"kind": "opaque"}
            parsed.append(("obj", o))
        elif s[0] in "\"'":
            v = _unquote(s)
            if v is None:
                return {"kind": "opaque"}
            parsed.append(("str", v))
        else:
            return {"kind": "opaque"}
    kinds = {k for k, _ in parsed}
    if kinds == {"str"}:
        return {"kind": "scalar", "values": [v for _, v in parsed]}
    if kinds == {"obj"}:
        return {"kind": "objects", "objects": [o for _, o in parsed]}
    return {"kind": "opaque"}


_ARR_DECL = re.compile(r"\b(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*=\s*(\[)")


def _find_arrays(src):
    """Имя → разобранный литеральный массив (скаляры/объекты/непрозрачный)."""
    out = {}
    for m in _ARR_DECL.finditer(src):
        name = m.group(1)
        lb = m.end() - 1
        end = _skip_balanced(src, lb)
        body = src[lb + 1:end - 1]
        if name not in out:                      # первое объявление важнее переназначений
            out[name] = _parse_array_body(body)
    return out


_NAME = r"([A-Za-z_$][\w$]*)"
# Колбэк может быть с параметром в скобках (`map((c) =>`) и без (`map(c =>`) —
# скобки параметра необязательны, иначе SETUP_COMPONENTS.map(c => …) не вяжется.
_LOOP_M = re.compile(r"\b" + _NAME + r"\s*\.\s*(?:forEach|map|filter|some|every|find|findIndex)\s*\(\s*(?:function\s*)?\(?\s*" + _NAME + r"\b")
_LOOP_R = re.compile(r"\b" + _NAME + r"\s*\.\s*reduce\s*\(\s*(?:function\s*)?\(?\s*" + _NAME + r"\s*,\s*" + _NAME + r"\b")
_LOOP_OF = re.compile(r"\bfor\s*\(\s*(?:const|let|var)\s+" + _NAME + r"\s+of\s+" + _NAME + r"\s*\)")


def _find_loops(src, arrays):
    """Переменная цикла → множество ПОДХОДЯЩИХ имён разобранных массивов."""
    out: dict[str, set] = {}

    def add(var, arr):
        if arr in arrays and arrays[arr]["kind"] in ("scalar", "objects"):
            out.setdefault(var, set()).add(arr)

    for m in _LOOP_M.finditer(src):
        add(m.group(2), m.group(1))
    for m in _LOOP_R.finditer(src):
        add(m.group(3), m.group(1))
    for m in _LOOP_OF.finditer(src):
        add(m.group(1), m.group(2))
    return out


_VAR_PROP = re.compile(r"^([A-Za-z_$][\w$]*)\.([A-Za-z_$][\w$]*)$")
_VAR_ONLY = re.compile(r"^([A-Za-z_$][\w$]*)$")


def _resolve_expr(expr, loopmap, arrays, suffix):
    """Одно «окно» переменной → конечное множество строковых значений, либо None."""
    mm = _VAR_PROP.match(expr)
    if mm:
        lv, prop = mm.group(1), mm.group(2)
    else:
        mo = _VAR_ONLY.match(expr)
        if not mo:
            return None
        lv, prop = mo.group(1), None
    arrnames = loopmap.get(lv)
    if not arrnames or len(arrnames) != 1:       # неизвестно или неоднозначно
        return None
    arr = arrays[next(iter(arrnames))]
    if arr["kind"] == "scalar":
        return set(arr["values"]) if prop is None else None
    if arr["kind"] != "objects" or prop is None:
        return None
    objs = arr["objects"]
    with_prop = [o for o in objs if isinstance(o.get(prop), str)]
    if len(with_prop) != len(objs):              # какой-то элемент без литерального prop — неполный набор
        return None
    vals = {o[prop] for o in objs}
    # Охрана «показывать или нет»: если хвостовой сегмент — свойство,
    # присутствующее НЕ на всех объектах, разворачиваем только те, где оно есть
    # (иначе `t('setup.'+c.key+'.tag')` ложно потребовал бы .tag у компонентов
    # без тега — soundcloud/orpheus/beatport).
    seg = suffix.rsplit(".", 1)[-1] if "." in suffix else suffix
    have = [o for o in objs if seg in o]
    if 0 < len(have) < len(objs):
        vals = {o[prop] for o in have}
    return vals


def _concat_shape(a):
    parts = _split_top(a, "+")
    lits = [_unquote(p) is not None for p in parts]
    if all(lits):
        return ("static",)
    i = 0
    while i < len(parts) and lits[i]:
        i += 1
    j = len(parts) - 1
    while j >= i and lits[j]:
        j -= 1
    prefix = "".join(_unquote(p) for p in parts[:i])
    suffix = "".join(_unquote(p) for p in parts[j + 1:])
    middle = parts[i:j + 1]
    if len(middle) == 1:
        return ("dyn", prefix, suffix, middle[0].strip())
    return ("dyn_multi", (prefix + "*+*…" + suffix) if (prefix or suffix) else "*+*…")


def _template_shape(a):
    lits, exprs, cur, i, n = [], [], [], 1, len(a)
    while i < n:
        c = a[i]
        if c == "\\":
            cur.append(a[i:i + 2])
            i += 2
            continue
        if c == "`":
            break
        if c == "$" and i + 1 < n and a[i + 1] == "{":
            end = _skip_balanced(a, i + 1)
            lits.append("".join(cur))
            cur = []
            exprs.append(a[i + 2:end - 1].strip())
            i = end
            continue
        cur.append(c)
        i += 1
    lits.append("".join(cur))
    if len(exprs) == 0:
        return ("static",)
    if len(exprs) == 1:
        return ("dyn", lits[0], lits[1], exprs[0])
    return ("dyn_multi", (lits[0] + "*…*" + lits[-1]) if (lits[0] or lits[-1]) else "*…")


def _shape(arg):
    a = arg.strip()
    if not a:
        return None
    if a[0] == "`":
        if _skip_template(a, 0) == len(a):
            return _template_shape(a)
        return ("dyn_multi", "`…`")
    return _concat_shape(a)


def dynamic_calls(src, label):
    """→ (resolved:[(key,label,line)], unresolved:[(shape,label,line)])."""
    arrays = _find_arrays(src)
    loopmap = _find_loops(src, arrays)
    resolved, unresolved = [], []
    for m in _DYN_HEAD.finditer(src):
        arg, _ = _read_call_arg(src, m.end() - 1)
        sh = _shape(arg)
        if not sh or sh[0] == "static":
            continue
        line = src.count("\n", 0, m.start()) + 1
        if sh[0] == "dyn_multi":
            if sh[1].strip("*+…` "):
                unresolved.append((sh[1], label, line))
            continue
        _, prefix, suffix, expr = sh
        if not (prefix or suffix):
            continue                              # t(переменная) — ключ не «собран», не проверяем
        vals = _resolve_expr(expr, loopmap, arrays, suffix)
        if vals is None:
            unresolved.append((prefix + "*" + suffix, label, line))
            continue
        for v in sorted(vals):
            key = prefix + v + suffix
            if re.fullmatch(_KEY, key):
                resolved.append((key, label, line))
    return resolved, unresolved


# ────────────────────────────  серверные ключи (Python)  ──────────────────────

def _const_str(node):
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return None


def _iter_py(root):
    seen, files = set(), []
    for g in _PY_GLOBS:
        for p in root.glob(g):
            if p.is_file() and p not in seen and "__pycache__" not in p.parts:
                seen.add(p)
                files.append(p)
    return sorted(files)


def python_keys(root) -> dict[str, set]:
    """Ключи, которые Python-код ОТСЫЛАЕТ клиенту, через ast — потому что
    литералы внутри докстрингов/комментариев узлами Call/Dict не являются, и
    пример `imsg("err.track_gone", ...)` из докстринга не заведётся как «ключа нет»."""
    out: dict[str, set] = {}

    def note(key, where):
        out.setdefault(key, set()).add(where)

    for f in _iter_py(root):
        try:
            tree = ast.parse(f.read_text(encoding="utf-8", errors="replace"))
        except (SyntaxError, ValueError, OSError):
            continue
        rel = f.relative_to(root).as_posix()
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                fn = node.func
                fname = fn.id if isinstance(fn, ast.Name) else (
                    fn.attr if isinstance(fn, ast.Attribute) else None)
                if fname == "imsg" and node.args:
                    k = _const_str(node.args[0])
                    if k:
                        note(k, f"{rel}:{node.lineno}")
                for kw in node.keywords:
                    if kw.arg in _PY_PRODUCERS:
                        k = _const_str(kw.value)
                        if k:
                            note(k, f"{rel}:{node.lineno}")
            elif isinstance(node, ast.Dict):
                for kk, vv in zip(node.keys, node.values):
                    if _const_str(kk) in _PY_PRODUCERS:
                        k = _const_str(vv)
                        if k:
                            note(k, f"{rel}:{getattr(vv, 'lineno', node.lineno)}")
    return out


def _check(used: dict[str, set], ru: set, en: set, where_prefix: str) -> list[str]:
    """Общая сверка собранного множества ключей с таблицами ru/en."""
    problems = []
    for k, where in sorted(used.items()):
        src = ", ".join(sorted(where))
        if k not in ru and k not in en:
            problems.append(f"{where_prefix}: ключа '{k}' НЕТ в таблице ({src}) — "
                            f"в интерфейсе останется авторский текст")
        elif k not in en:
            problems.append(f"{where_prefix}: ключ '{k}' только в ru ({src}) — "
                            f"английский интерфейс получит русскую строку")
        elif k not in ru:
            problems.append(f"{where_prefix}: ключ '{k}' только в en ({src}) — "
                            f"русский интерфейс получит английскую строку")
    return problems


def scan(tree: Path):
    """(problems, unresolved) для одного дерева статики.

    problems — падают гейтом; unresolved — СПИСОК неразрешимой динамики, только
    informational: собрать ключ из переменной-не-цикла статически нельзя, и
    требовать этого значит разгонять ложняк.
    """
    dicts = [tree / rel for rel in _DICTS if (tree / rel).exists()]
    if not dicts:
        return [f"{tree}: нет js/i18n.js"], []
    tbl = tables(dicts)
    ru, en = tbl.get("ru", set()), tbl.get("en", set())

    used: dict[str, set] = {}
    unresolved: list[tuple] = []
    for pat in _SRC_GLOBS:
        for f in sorted(tree.glob(pat)):
            if f.name in _DICT_NAMES:
                continue
            src = f.read_text(encoding="utf-8", errors="replace")
            # Комментарии глушим ПРОБЕЛАМИ (см. _strip_js_comments): в них живут
            # ПРИМЕРЫ вида t('i18n.key'), и без этого сканер стабильно даёт ложную
            # находку, а инструмент, который всегда красный, перестают читать.
            # Хвостовые `// …` внутри литералов иначе рвали и разбор динамики.
            src = _strip_js_comments(src)
            label = f"{f.parent.name}/{f.name}"
            for k in _CALL.findall(src):
                used.setdefault(k, set()).add(label)
            resolved, dyn_unresolved = dynamic_calls(src, label)
            for k, _lab, line in resolved:
                used.setdefault(k, set()).add(f"{label}:{line}")
            unresolved += dyn_unresolved
    for f in sorted([tree / "index.html", *tree.glob("views/*.html"),
                     *tree.glob("panel/*.html")]):
        if not f.exists():
            continue
        for k in _ATTR.findall(f.read_text(encoding="utf-8", errors="replace")):
            used.setdefault(k, set()).add(f.name)

    problems = _check(used, ru, en, str(tree))
    return problems, unresolved


def scan_python(root: Path):
    """Ключи, которые сервер отдаёт клиенту, — против продуктовой таблицы."""
    dicts = [root / _MAIN_TREE / rel for rel in _DICTS if (root / _MAIN_TREE / rel).exists()]
    if not dicts:
        return [f"{root / _MAIN_TREE}: нет js/i18n.js — нечем проверять python"]
    tbl = tables(dicts)
    ru, en = tbl.get("ru", set()), tbl.get("en", set())
    return _check(python_keys(root), ru, en, "python")


def main() -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
    all_problems: list[str] = []
    all_unresolved: list[tuple] = []
    for rel in _TREES:
        p = _REPO / rel
        if not p.exists():
            print(f"пропуск: нет {rel}")
            continue
        found, unresolved = scan(p)
        print(f"{rel}: {'ПРОБЛЕМЫ (' + str(len(found)) + ')' if found else 'чисто'}")
        all_problems += found
        all_unresolved += unresolved

    py_problems = scan_python(_REPO)
    print(f"python: {'ПРОБЛЕМЫ (' + str(len(py_problems)) + ')' if py_problems else 'чисто'}")
    all_problems += py_problems

    for line in all_problems:
        print("  ✗ " + line)

    # Неразрешимая динамика — справка, не провал: ключ собирается из переменной,
    # конечный набор которой статически не вывести. Гейт по ним не краснеет.
    shapes: dict[str, set] = {}
    for shape, label, line in all_unresolved:
        shapes.setdefault(shape, set()).add(f"{label}:{line}")
    if shapes:
        print(f"\nнеразрешимая динамика (справка, не ошибка): {len(shapes)} шт.")
        for shape in sorted(shapes):
            print(f"  ? {shape}  ← {', '.join(sorted(shapes[shape]))}")

    if all_problems:
        print(f"\nвсего проблем: {len(all_problems)} — см. скилл ripster-i18n")
        return 1
    print("\nвсе упомянутые ключи есть и в ru, и в en")
    return 0


if __name__ == "__main__":
    sys.exit(main())
