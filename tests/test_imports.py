"""Import smoke test — the regression net.

Every module under the `ripster` package must import without error. This is the
exact class of failure that has taken the live service down before (a refactor
leaves a broken import; the app crashes on next start). Running this after any
change catches it in milliseconds instead of in production.
"""
import importlib
import pkgutil

import pytest

import ripster


def _all_modules():
    names = []
    for m in pkgutil.walk_packages(ripster.__path__, "ripster."):
        names.append(m.name)
    return sorted(names)


ALL_MODULES = _all_modules()


def test_package_has_modules():
    # Guard against the discovery itself silently returning nothing.
    assert len(ALL_MODULES) > 50, f"only found {len(ALL_MODULES)} ripster modules"


@pytest.mark.parametrize("modname", ALL_MODULES)
def test_module_imports(modname):
    importlib.import_module(modname)
