"""Registry of source adapters, keyed by name."""
from __future__ import annotations

from daily_pulse.adapters.base import SourceAdapter

_ADAPTERS: dict[str, SourceAdapter] = {}


def register(adapter: SourceAdapter) -> SourceAdapter:
    _ADAPTERS[adapter.name] = adapter
    return adapter


def get(name: str) -> SourceAdapter | None:
    return _ADAPTERS.get(name)


def register_builtin() -> None:
    """Register the shipped adapters that aren't registered yet (safe to call repeatedly)."""
    from daily_pulse.adapters.instagram import InstagramAdapter
    from daily_pulse.adapters.x import XAdapter
    from daily_pulse.adapters.youtube import YouTubeAdapter

    for adapter in (XAdapter(), InstagramAdapter(), YouTubeAdapter()):
        _ADAPTERS.setdefault(adapter.name, adapter)
