"""Engine registry — single place to look up engines by name."""
from __future__ import annotations
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .base import EngineBase

REGISTRY: dict[str, type] = {}

def register(cls):
    REGISTRY[cls.name] = cls
    return cls

def get_engine(name: str) -> "EngineBase":
    cls = REGISTRY.get(name)
    if cls is None:
        raise KeyError(f"Unknown engine: {name!r}. Available: {list(REGISTRY)}")
    return cls()
