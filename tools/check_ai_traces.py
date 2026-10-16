# -*- coding: utf-8 -*-
"""Следы генеративных моделей в том, что УЕЗЖАЕТ ЛЮДЯМ.

Запуск:
    python tools/check_ai_traces.py            # проверка
    python tools/check_ai_traces.py --selftest # доказать, что проверка умеет краснеть
Код возврата: 0 — чисто, 1 — есть что чинить.

ЧТО ИМЕННО ИЩЕТ И ПОЧЕМУ ИМЕННО ЭТО
-----------------------------------
1. **Провенанс C2PA в картинках.** Генераторы (ChatGPT, Grok и прочие) вшивают в
   файл манифест: JPEG — сегмент APP11/JUMBF, PNG — чанк `caBX`, внутри поля
   `digitalSourceType` / `trainedAlgorithmicMedia`. Это не «водяной знак на
   картинке», это подпись в метаданных, и она переживает переименование и
   копирование. Вместе с ней там же нередко лежит время создания и служебные
   идентификаторы.

2. **Невидимые символы в тексте.** Zero-width, мягкий перенос, bidi-переключатели,
   теговые символы U+E0000–E007F. Они переживают копипаст, ломают поиск и
   сравнение строк, а в UI дают «строка выглядит одинаково, а не совпадает».

ЧЕГО ЭТА ПРОВЕРКА НАМЕРЕННО НЕ ДЕЛАЕТ
-------------------------------------
Не трогает `design/` как таковой: это рабочая мастерская, и метка в референсе,
который никуда не уезжает, никому не вредит. Красным становится ТОЛЬКО то, что
попадает в публичную сборку (`github_setup/`, `static/`, `installer/`), потому
что вредит именно отгрузка.

Не пытается «переписать текст, чтобы не определялся детектором»: статистические
метки живут в выборе слов по всему тексту, и такая переделка — это переписывание
чужой моделью с потерей смысла. Нам это не нужно и мы этого не делаем.
"""
from __future__ import annotations

import os
import sys

# Консоль Windows по умолчанию cp1251: имя файла с составным «й» (и + U+0306)
# роняло печать находки, и проверка падала ПОСЛЕ того, как нашла её. Сломанный
# прибор вместо вердикта — см. скилл ripster-honest-diagnostics.
for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Маркеры провенанса в сырых байтах. Смотрим байты, а не библиотеку разбора:
# так проверка не зависит от того, установлен ли Pillow/exiftool на машине.
IMG_MARKS = (b"jumb", b"c2pa", b"C2PA", b"caBX",
             b"digitalSourceType", b"trainedAlgorithmicMedia", b"algorithmicMedia")

# Невидимые символы. BOM (U+FEFF) в НАЧАЛЕ файла — законная подпись кодировки и
# здесь не считается: иначе проверка вечно краснела бы на index.html.
INVISIBLE = {
    # ВНИМАНИЕ: только escape-последовательности, никаких литералов. Детектор,
    # хранящий искомое литералом, находит сам себя и краснеет на своём же коде
    # (наступили на это сразу же, 16.08.2026).
    "\u200b": "ZERO WIDTH SPACE", "\u200c": "ZWNJ", "\u200d": "ZWJ",
    "\u00ad": "SOFT HYPHEN", "\u2060": "WORD JOINER",
    "\u202a": "LRE", "\u202b": "RLE", "\u202c": "PDF-bidi",
    "\u202d": "LRO", "\u202e": "RLO",
    "\u2066": "LRI", "\u2067": "RLI", "\u2068": "FSI", "\u2069": "PDI",
    "\u2028": "LINE SEPARATOR", "\u2029": "PARAGRAPH SEPARATOR",
}
TAG_LO, TAG_HI = 0xE0000, 0xE007F

IMG_EXT = (".png", ".jpg", ".jpeg", ".webp", ".gif", ".ico", ".svg")
TXT_EXT = (".md", ".html", ".js", ".css", ".py", ".json", ".yaml", ".yml", ".txt")

# Куда смотреть строго (это уезжает людям) и куда — только для сведения.
SHIPPED = ("github_setup", "static", "installer")
INFO_ONLY = ("design",)

SKIP_DIRS = {".git", "__pycache__", "node_modules", ".venv", "backups", "orpheus",
             "site-packages", "vendor", "dist", "build", "lucida", "dev_test6"}


def _walk(base: str):
    for dp, dns, fns in os.walk(base):
        dns[:] = [d for d in dns if d not in SKIP_DIRS]
        for fn in fns:
            yield os.path.join(dp, fn)


def scan_images(base: str) -> list[tuple[str, list[str]]]:
    out = []
    for p in _walk(base):
        if not p.lower().endswith(IMG_EXT):
            continue
        try:
            # Манифест лежит в начале файла; 512 КБ хватает и не читает гигабайты.
            head = open(p, "rb").read(512_000)
        except OSError:
            continue
        found = sorted({m.decode() for m in IMG_MARKS if m in head})
        if found:
            out.append((os.path.relpath(p, ROOT), found))
    return out


def scan_text(base: str) -> list[tuple[str, dict]]:
    out = []
    for p in _walk(base):
        if not p.lower().endswith(TXT_EXT):
            continue
        try:
            s = open(p, encoding="utf-8").read()
        except (OSError, UnicodeDecodeError):
            continue
        if s.startswith("\ufeff"):
            s = s[1:]                      # законный BOM — не улика
        counts = {}
        for ch, name in INVISIBLE.items():
            n = s.count(ch)
            if n:
                counts[name] = n
        tags = sum(1 for ch in s if TAG_LO <= ord(ch) <= TAG_HI)
        if tags:
            counts["TAG CHARS"] = tags
        if counts:
            out.append((os.path.relpath(p, ROOT), counts))
    return out


def selftest() -> int:
    """Доказать, что проверка МОЖЕТ покраснеть. Без этого «зелено» ничего не значит."""
    import tempfile
    ok = True
    with tempfile.TemporaryDirectory() as d:
        img = os.path.join(d, "fake.png")
        open(img, "wb").write(b"\x89PNG\r\n\x1a\n" + b"\x00" * 20 + b"caBX"
                              + b"digitalSourceType trainedAlgorithmicMedia")
        hits = scan_images(d)
        print("  подложил картинку с меткой →", "НАЙДЕНО" if hits else "ПРОПУЩЕНО (плохо)")
        ok &= bool(hits)

        txt = os.path.join(d, "fake.md")
        open(txt, "w", encoding="utf-8").write("обычный текст\u200bс нулевой шириной")
        hits = scan_text(d)
        print("  подложил текст с zero-width →", "НАЙДЕНО" if hits else "ПРОПУЩЕНО (плохо)")
        ok &= bool(hits)

        clean = os.path.join(d, "clean.md")
        open(clean, "w", encoding="utf-8").write("\ufeffсовершенно обычный текст\n")
        hits = scan_text(clean if os.path.isdir(clean) else d)
        only_fake = all(h[0].endswith("fake.md") for h in hits)
        print("  файл с одним BOM в начале →",
              "не считается уликой" if only_fake else "ЛОЖНАЯ ТРЕВОГА (плохо)")
        ok &= only_fake
    return 0 if ok else 1


def main() -> int:
    if "--selftest" in sys.argv:
        print("Самопроверка (может ли проверка покраснеть):")
        return selftest()

    problems = 0

    print("=== ОТГРУЖАЕМОЕ (строго) ===")
    for sub in SHIPPED:
        base = os.path.join(ROOT, sub)
        if not os.path.isdir(base):
            continue
        imgs = scan_images(base)
        txts = scan_text(base)
        for p, marks in imgs:
            print(f"  ✗ {p} — метка провенанса: {', '.join(marks)}")
        for p, c in txts:
            print(f"  ✗ {p} — невидимые символы: {c}")
        problems += len(imgs) + len(txts)
    if not problems:
        print("  чисто")

    print("\n=== МАСТЕРСКАЯ design/ (к сведению, не ошибка) ===")
    info = 0
    for sub in INFO_ONLY:
        base = os.path.join(ROOT, sub)
        if not os.path.isdir(base):
            continue
        imgs = scan_images(base)
        info += len(imgs)
        for p, marks in imgs[:8]:
            print(f"  · {p} — {', '.join(marks)}")
        if len(imgs) > 8:
            print(f"  · … и ещё {len(imgs) - 8}")
    print(f"  помеченных картинок: {info} — вредит только отгрузка, здесь они безопасны")

    print(f"\nвсего проблем: {problems}")
    if problems:
        print("Чинить: метаданные картинки снимаются БЕЗ потери пикселей "
              "(см. скилл ai-traces-hygiene), невидимые символы — вычистить в тексте.")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
