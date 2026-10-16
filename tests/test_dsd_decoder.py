"""DSD-декодер: собрать и прогнать самопроверку.

`dsd.h` платформенно-независим (только колбэк чтения), поэтому проверять его
на телефоне незачем — он собирается обычным компилятором и гоняется здесь.

Проверка не «запустилось без падения»: `dsd_selftest.cpp` синтезирует DSD64 с
ИЗВЕСТНЫМ тоном, декодирует его и меряет, что вышло — амплитуду на нужной
частоте, подавление на чужой, постоянную составляющую, оба канала, оба порядка
бит и отказ на сжатом DST.

Именно она поймала настоящую ошибку 06.09.2026: все смещения в чанке `fmt` у
DSF были сдвинуты на четыре байта, и КАЖДЫЙ .dsf отвергался с «not raw DSD».
DFF при этом играл безупречно — без отдельной проверки формата ошибка дожила
бы до первого живого файла у человека.
"""
import os
import shutil
import subprocess

import pytest

# Заголовок движка общий для обеих версий Рипстера, поэтому и лежит он в двух
# местах: в дереве мобильного приложения и в ПК-репозитории (native/dsd).
# Тест берёт то, что есть, — иначе в одном из репозиториев он молча пропускался
# бы всегда, а вечно пропускаемая проверка не проверяет ничего.
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_CANDIDATES = (
    os.path.join(_ROOT, "android_app", "app", "src", "main", "cpp"),
    os.path.join(_ROOT, "native", "dsd"),
)
CPP_DIR = next((d for d in _CANDIDATES
                if os.path.exists(os.path.join(d, "dsd_selftest.cpp"))), _CANDIDATES[0])
SRC = os.path.join(CPP_DIR, "dsd_selftest.cpp")


def _compiler():
    """g++ откуда угодно: PATH или MinGW из Strawberry Perl."""
    found = shutil.which("g++") or shutil.which("clang++")
    if found:
        return found
    fallback = r"C:\Strawberry\c\bin\g++.exe"
    return fallback if os.path.exists(fallback) else None


@pytest.mark.skipif(not os.path.exists(SRC), reason="нет исходника самопроверки")
def test_dsd_selftest_passes(tmp_path):
    cc = _compiler()
    if not cc:
        pytest.skip("нет C++ компилятора — проверку DSD пропускаем ЧЕСТНО, "
                    "а не считаем пройденной")
    exe = str(tmp_path / "dsd_selftest.exe")
    build = subprocess.run(
        [cc, "-O2", "-std=c++17", "-D_USE_MATH_DEFINES", "-I", CPP_DIR, "-o", exe, SRC],
        capture_output=True, text=True)
    assert build.returncode == 0, f"не собралось:\n{build.stderr[:2000]}"

    run = subprocess.run([exe], capture_output=True, text=True, timeout=180)
    # Вывод печатаем всегда: когда проверка падает, важно ВИДЕТЬ числа, а не
    # только факт падения — по ним и понятно, что именно поехало.
    print(run.stdout)
    assert run.returncode == 0, "самопроверка DSD не сошлась"
