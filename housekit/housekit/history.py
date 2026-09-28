"""Play history: what was built for which gig, so the next build can skip recent repeats."""
from __future__ import annotations

from .catalog import Catalog
from .models import BuildPlan, GigRecord


def log_build(catalog: Catalog, plan: BuildPlan, out_dir: str) -> int:
    rows = [
        (st.track.id, sp.spec.name, st.position, st.track.song_key, st.track.display)
        for sp in plan.slots
        for st in sp.tracks
    ]
    return catalog.log_gig(plan.name, plan.date, plan.venue, out_dir, rows)


def recent_exclusions(catalog: Catalog, last_n: int) -> set[str]:
    return catalog.recent_song_keys(last_n)


def list_gigs(catalog: Catalog) -> list[GigRecord]:
    return catalog.list_gigs()
