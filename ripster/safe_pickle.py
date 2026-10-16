"""
Restricted pickle loading for OrpheusDL's loginstorage.bin.

OrpheusDL (third-party, in orpheus/) persists its Tidal/Beatport sessions as a
plain pickle of nested dict/list/str/int/float/bool/None — we don't control
that on-disk format (its own login/refresh code writes it the same way), so we
can't switch it to JSON without breaking OrpheusDL itself. But `pickle.loads`
on an untrusted/tamperable file is a classic RCE-via-gadget-chain primitive:
if anything with local write access ever plants or swaps this file (a path-
traversal bug elsewhere, a compromised OrpheusDL dependency, a shared/multi-
user machine), a crafted pickle can execute arbitrary code the moment we read
it back.

`safe_loads` uses a restricted Unpickler that only allows the builtin
container/scalar types the real session files are made of — REDUCE (the
opcode that calls arbitrary callables, the actual RCE mechanism) never gets a
class it's willing to instantiate, so a malicious payload fails closed
instead of executing.
"""
from __future__ import annotations

import io
import pickle

_ALLOWED_BUILTINS = frozenset({
    "dict", "list", "tuple", "set", "frozenset", "str", "bytes", "bytearray",
    "int", "float", "bool", "complex", "NoneType",
})


class _RestrictedUnpickler(pickle.Unpickler):
    def find_class(self, module, name):
        if module == "builtins" and name in _ALLOWED_BUILTINS:
            import builtins
            return getattr(builtins, name)
        if module == "datetime" and name in ("datetime", "date", "timedelta", "timezone"):
            import datetime
            return getattr(datetime, name)
        raise pickle.UnpicklingError(f"safe_pickle: refusing to unpickle {module}.{name}")


def safe_loads(data: bytes):
    """Like pickle.loads(data), but raises instead of instantiating anything
    beyond plain containers/scalars and datetime — never executes callables."""
    return _RestrictedUnpickler(io.BytesIO(data)).load()
