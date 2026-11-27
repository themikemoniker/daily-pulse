"""Registry of source adapters, keyed by name."""
from __future__ import annotations

from daily_pulse.adapters.base import SourceAdapter

_ADAPTERS: dict[str, SourceAdapter] = {}


def register(adapter: SourceAdapter) -> SourceAdapter:
    _ADAPTERS[adapter.name] = adapter
    return adapter


def get(name: str) -> SourceAdapter | None:
    return _ADAPTERS.get(name)
