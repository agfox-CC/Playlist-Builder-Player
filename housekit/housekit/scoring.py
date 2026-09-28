"""Gig profile derivation and per-track scoring against a slot."""
from __future__ import annotations

import math
import re
import statistics
from collections import Counter
from typing import Optional

from .charts import chart_popularity
from .config import Config
from .models import GigProfile, ScoreBreakdown, SlotSpec, Track
from .tags import GenreRules
from .textnorm import decade_of, norm_artist, norm_title

from .era import COMPILATION_RE as _COMPILATION  # one definition of "this album is a compilation"


def song_copies_and_years(tracks: list[Track]) -> tuple[dict[str, int], dict[str, int]]:
    """Per song_key: how many copies the library holds, and the earliest tagged year among them
    (a 2019 "Classic Rock Collection" copy of Hotel California is still a 1976 song)."""
    copies: dict[str, int] = {}
    years: dict[str, int] = {}
    for t in tracks:
        if t.missing or not t.song_key or t.song_key.startswith("|"):
            continue
        copies[t.song_key] = copies.get(t.song_key, 0) + 1
        if t.year:
            years[t.song_key] = min(years.get(t.song_key, t.year), t.year)
    return copies, years


def apply_song_years(tracks: list[Track], years: dict[str, int]) -> None:
    """Set each track's year to its song's earliest known year (in place; the catalog is untouched)."""
    for t in tracks:
        y = years.get(t.song_key)
        if y is not None and (t.year is None or y < t.year):
            t.year = y


def derive_profile(setlist_tracks: list[Track]) -> GigProfile:
    """What the band sounds like, from the setlist entries we matched to the library."""
    decades = Counter(d for d in (decade_of(t.year) for t in setlist_tracks) if d)
    genres = Counter(t.genre_norm or "Other" for t in setlist_tracks)
    energies = [t.energy for t in setlist_tracks if t.energy is not None]
    nd, ng = sum(decades.values()), sum(genres.values())
    return GigProfile(
        decades={d: c / nd for d, c in sorted(decades.items())} if nd else {},
        genres={g: c / ng for g, c in genres.most_common()} if ng else {},
        energy_mean=round(statistics.fmean(energies), 3) if energies else None,
        energy_std=round(statistics.pstdev(energies), 3) if len(energies) > 1 else None,
        setlist_artists=sorted({norm_artist(t.artist) for t in setlist_tracks if t.artist}),
        setlist_titles=sorted({norm_title(t.title) for t in setlist_tracks}),
        n_tracks=len(setlist_tracks),
    )


def energy_fit(energy: Optional[float], slot: SlotSpec, falloff: float) -> float:
    if energy is None:
        return 0.5
    if slot.energy_min <= energy <= slot.energy_max:
        return 1.0
    dist = slot.energy_min - energy if energy < slot.energy_min else energy - slot.energy_max
    return max(0.0, 1.0 - dist / max(falloff, 1e-6))


def era_fit(year: Optional[int], profile: GigProfile) -> float:
    if not profile.decades:
        return 0.5
    d = decade_of(year)
    if d is None:
        return 0.4
    s = profile.decades.get(d, 0.0) + 0.5 * (profile.decades.get(d - 10, 0.0) + profile.decades.get(d + 10, 0.0))
    # A setlist spread over five decades gives every decade a small share; rescale so the
    # best-represented decade scores 1.0 rather than 0.3.
    peak = max(profile.decades.values())
    peak_adj = peak + 0.5 * (profile.decades.get(max(profile.decades, key=profile.decades.get) - 10, 0.0) + profile.decades.get(max(profile.decades, key=profile.decades.get) + 10, 0.0))
    return min(1.0, s / peak_adj) if peak_adj > 0 else 0.5


def genre_fit(genre: str, profile: GigProfile, rules: GenreRules) -> float:
    if not profile.genres:
        return 0.5
    g = genre or "Other"
    s = sum(share * rules.similarity_of(g, pg) for pg, share in profile.genres.items())
    peak = max(profile.genres.values())
    return min(1.0, s / peak) if peak > 0 else 0.5


_ALT_VERSION = re.compile(r"acoustic|demo|remix|\bmix\b|cover|karaoke|instrumental|alternate|outtake|rehearsal|sped up|slowed|reprise|take \d", re.IGNORECASE)


def familiarity(track: Track, copies: int = 1) -> float:
    """How likely the room is to recognise this track.

    With popularity data loaded (Hot 100, Billboard 200, radio spins, year-end genre charts), a
    song scores 0.45..1.0 from its best source: a #1 that stayed half a year is 1.0, a one-week
    #100 is about 0.46. Without it, the library itself is the evidence: several copies of a song
    (album + greatest hits + compilation) or a compilation album title are hints. Unknown tracks
    start lower (0.35) than any charted one, so hits are preferred whenever chart data exists."""
    pop = track.popularity
    if pop is None and track.chart_peak is not None:
        pop = chart_popularity(track.chart_peak, track.chart_weeks)
    if pop is not None:
        return round(0.45 + 0.55 * max(0.0, min(1.0, pop)), 3)
    f = 0.35
    if copies >= 3:
        f += 0.3
    elif copies == 2:
        f += 0.15
    if track.album and _COMPILATION.search(track.album):
        f += 0.2
    if track.version and re.search(r"single|radio", track.version, re.IGNORECASE):
        f += 0.1
    return min(0.85, f)


def score_track(
    track: Track,
    profile: GigProfile,
    slot: SlotSpec,
    cfg: Config,
    genre_rules: GenreRules,
    jitter: float = 0.0,
    copies: int = 1,
) -> ScoreBreakdown:
    w = cfg.weights
    e = energy_fit(track.energy, slot, cfg.energy_falloff)
    era = era_fit(track.year, profile)
    g = genre_fit(track.genre_norm, profile, genre_rules)
    fam = familiarity(track, copies)
    penalty = 1.0
    if track.live:
        penalty *= cfg.penalties.live
    if track.instrumental:
        penalty *= cfg.penalties.instrumental
    if track.version and _ALT_VERSION.search(track.version):
        penalty *= cfg.penalties.alt_version
    if track.tag_confidence < 0.5:
        penalty *= cfg.penalties.low_tag_confidence
    if track.energy is None:
        penalty *= cfg.penalties.unanalyzed
    wsum = w.energy + w.era + w.genre + w.familiarity
    base = (w.energy * e + w.era * era + w.genre * g + w.familiarity * fam) / (wsum if wsum > 0 else 1.0)
    total = base * penalty + jitter
    return ScoreBreakdown(
        energy=round(e, 3), era=round(era, 3), genre=round(g, 3), familiarity=round(fam, 3),
        penalty=round(penalty, 3), jitter=round(jitter, 4), total=round(total, 4),
    )


def lufs_gain(lufs: Optional[float], target: float, peak_dbfs: Optional[float] = None, ceiling: float = -1.0) -> Optional[float]:
    """Gain (dB) to bring a track to the target loudness, capped so the peak stays under the ceiling."""
    if lufs is None or not math.isfinite(lufs):
        return None
    g = target - lufs
    if peak_dbfs is not None and math.isfinite(peak_dbfs):
        g = min(g, ceiling - peak_dbfs)
    return round(g, 1)
