"""X/Twitter: deferred. No usable read route exists yet (see README "Deferred sources")."""
from __future__ import annotations

from daily_pulse.adapters.base import AdapterContext, SourceAdapter
from daily_pulse.models import Item

DEFERRED = "deferred: no usable read route"


class XAdapter(SourceAdapter):
    name = "x"

    def status(self, ctx: AdapterContext) -> str | None:
        return DEFERRED  # the seed handles stay in [sources.x].handles for later

    def list_new(self, ctx: AdapterContext) -> list[Item]:
        return []

    def fetch_details(self, item: Item, ctx: AdapterContext) -> Item:
        return item
