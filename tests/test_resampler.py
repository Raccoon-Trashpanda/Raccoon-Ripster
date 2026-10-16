"""Полифазный ресемплер: собрать и прогнать самопроверку.

Как и у DSD-декодера, `resampler.h` намеренно без Android — он собирается
обычным компилятором и проверяется здесь, а не на телефоне.

Проверка не «запустилось»: синтезируется тон известной частоты и амплитуды,
пересчитывается, и меряется что вышло. Главная из проверок — ТА ЖЕ ЗАПИСЬ,
поданная одним куском и мелкими блоками, обязана дать одинаковый выход. Именно
этим болел линейный ресемпл, который здесь стоял: он начинал позицию с нуля на
каждом блоке декодера, и на каждой границе был разрыв — «щелчки на границах»
из каталога чужих ошибок, только свои собственные.

Найденное этой проверкой 06.09.2026: сперва мой же учёт позиции между блоками
(тон вышел 0.012 вместо 0.5), затем ступенька квантования фазы — расхождение
5.5e-4, то есть около -65 дБ, что для 24-битного материала выше собственного
шума записи. Закрыто интерполяцией между соседними фазами: стало 2e-6.
"""
import os
import shutil
import subprocess

import pytest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_CANDIDATES = (
    os.path.join(_ROOT, "android_app", "app", "src", "main", "cpp"),
    os.path.join(_ROOT, "native", "dsd"),
)
CPP_DIR = next((d for d in _CANDIDATES
                if os.path.exists(os.path.join(d, "resampler_selftest.cpp"))), _CANDIDATES[0])
SRC = os.path.join(CPP_DIR, "resampler_selftest.cpp")


def _compiler():
    found = shutil.which("g++") or shutil.which("clang++")
    if found:
        return found
    fallback = r"C:\Strawberry\c\bin\g++.exe"
    return fallback if os.path.exists(fallback) else None


@pytest.mark.skipif(not os.path.exists(SRC), reason="нет исходника самопроверки")
def test_resampler_selftest_passes(tmp_path):
    cc = _compiler()
    if not cc:
        pytest.skip("нет C++ компилятора — проверку ресемплера пропускаем ЧЕСТНО, "
                    "а не считаем пройденной")
    exe = str(tmp_path / "resampler_selftest.exe")
    build = subprocess.run(
        [cc, "-O2", "-std=c++17", "-D_USE_MATH_DEFINES", "-I", CPP_DIR, "-o", exe, SRC],
        capture_output=True, text=True)
    assert build.returncode == 0, f"не собралось:\n{build.stderr[:2000]}"

    run = subprocess.run([exe], capture_output=True, text=True, timeout=300)
    print(run.stdout)
    assert run.returncode == 0, "самопроверка ресемплера не сошлась"
