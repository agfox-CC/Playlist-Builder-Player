"""Custom playlists: filter the catalog, rank by popularity, keep one copy per song.

`select_playlist` is pure: it takes tracks and a spec and returns the ordered pick.
"""
from __future__ import annotations

import random
import re
from dataclasses import dataclass, field
from typing import Optional

from collections import Counter

from .config import Config
from .identity import apply_song_identity, artist_parts, is_placeholder
from .models import Track
from .scoring import _ALT_VERSION, apply_song_years, derive_profile, era_fit, familiarity, genre_fit, song_copies_and_years
from .tags import load_genre_rules
from .textnorm import TRAILING_ARTICLE_RE, norm_artist, norm_basic, norm_title

ORDERS = ("rank", "energy", "year", "alpha", "random")

# Plain-language energy levels (label, low, high) on the 0-1 energy scale. Ranges overlap a little
# on purpose so a song near a boundary shows up under both neighbouring words.
ENERGY_LEVELS: list[tuple[str, Optional[float], Optional[float]]] = [
    ("Any", None, None),
    ("Mellow - background, dinner", 0.0, 0.38),
    ("Laid-back - easy listening", 0.30, 0.50),
    ("Upbeat - toe-tapping", 0.45, 0.65),
    ("High energy - party", 0.60, 0.80),
    ("Full-on - loud rock and dance", 0.72, 1.0),
]


def energy_level_names() -> list[str]:
    return [name for name, _, _ in ENERGY_LEVELS]


def parse_energy(text: Optional[str]) -> tuple[Optional[float], Optional[float]]:
    """Accepts a numeric range ('0.55-0.8') or a level word ('upbeat', 'high energy', 'mellow')."""
    if not text or not text.strip():
        return None, None
    s = text.strip().lower()
    for name, lo, hi in ENERGY_LEVELS:
        key = name.split(" - ")[0].lower()
        if s == key or s == name.lower() or s.replace("-", " ") == key.replace("-", " "):
            return lo, hi
    return parse_range(text)


DECADES = [1950, 1960, 1970, 1980, 1990, 2000, 2010, 2020]


def decade_label(decade: int) -> str:
    return f"{decade % 100:02d}s" if decade < 2000 else f"{decade}s"


def parse_decade(text: str) -> int:
    """'50' / '50s' / '1950' / '1950s' -> 1950; '00' / '2000s' -> 2000; '10s' -> 2010."""
    s = str(text).strip().lower().rstrip("s").strip("'")
    if not s.isdigit():
        raise ValueError(f"bad decade {text!r}")
    n = int(s)
    if n < 100:
        n = 1900 + n if n >= 30 else 2000 + n
    return (n // 10) * 10


def year_in_era(year: Optional[int], decades: list[int], year_min: Optional[int], year_max: Optional[int]) -> bool:
    """Ticked decades OR the custom range; no era filter at all passes everything."""
    has_range = year_min is not None or year_max is not None
    if not decades and not has_range:
        return True
    if year is None:
        return False
    if decades and (year // 10) * 10 in decades:
        return True
    return has_range and (year_min is None or year >= year_min) and (year_max is None or year <= year_max)


@dataclass
class PlaylistSpec:
    name: str
    artists: list[str] = field(default_factory=list)  # substring match on artist / album artist
    genres: list[str] = field(default_factory=list)  # normalized genre names
    decades: list[int] = field(default_factory=list)  # 1950, 1960 ...; OR-ed with the year range
    year_min: Optional[int] = None
    year_max: Optional[int] = None
    energy_min: Optional[float] = None
    energy_max: Optional[float] = None
    top: int = 0  # chart peak at this position or better
    search: str = ""  # words that must all appear in title or album
    count: Optional[int] = 20
    minutes: Optional[float] = None  # alternative to count
    order: str = "rank"
    clean: bool = False
    allow_live: bool = False
    allow_holiday: bool = False
    dedupe: bool = True
    seed: Optional[int] = None
    fill: bool = False  # when an artist / search list comes up short, top up with similar songs by other artists


@dataclass
class PlaylistTrack:
    track: Track
    rank_score: float
    position: int = 0
    out_name: str = ""
    filled: bool = False  # added by the fill, not by the filters


@dataclass
class PlaylistResult:
    spec: PlaylistSpec
    tracks: list[PlaylistTrack]
    runtime: float
    candidates: int  # after filters, before dedupe/limit
    warnings: list[str] = field(default_factory=list)


def parse_range(text: Optional[str], as_int: bool = False) -> tuple[Optional[float], Optional[float]]:
    """'1975-1985' -> (1975, 1985); '1980' -> (1980, 1980); '-1985' -> (None, 1985); '0.3-0.6' -> (0.3, 0.6)."""
    if not text:
        return None, None
    s = text.strip()
    m = re.fullmatch(r"\s*([\d.]*)\s*(?:-|\.\.|to)\s*([\d.]*)\s*", s)
    conv = (lambda v: int(float(v))) if as_int else float
    if m:
        lo = conv(m.group(1)) if m.group(1) else None
        hi = conv(m.group(2)) if m.group(2) else None
        return lo, hi
    v = conv(s)
    return v, v


def lead_artist(t: Track) -> str:
    """The act a listener hears the song as: the first credited artist ("Santana feat. Rob Thomas" -> santana),
    the album artist when the track's artist is a placeholder."""
    a = t.artist if not is_placeholder(t.artist) else (t.album_artist or "")
    parts = artist_parts(a)
    return parts[0] if parts else norm_artist(a)


def _same_by(key):
    return lambda a, b: key(a) == key(b)


def _adjacent_clashes(items: list, same) -> int:
    return sum(1 for i in range(1, len(items)) if same(items[i - 1], items[i]))


def _clash_at(items: list, p: int, same) -> bool:
    return (p > 0 and same(items[p - 1], items[p])) or (p + 1 < len(items) and same(items[p], items[p + 1]))


def spacing_key(spec: PlaylistSpec):
    """The act a song counts as when keeping neighbours apart. An act the user asked for by name is
    exempt (a Steely Dan list is all Steely Dan, and that is the point)."""
    asked = [norm_basic(a) for a in spec.artists if a.strip()]

    def key(t: Track) -> str:
        if asked:
            hay = norm_basic(t.artist) + " | " + norm_basic(t.album_artist)
            if any(a in hay for a in asked):
                return f"#{t.id}"
        return lead_artist(t)

    return key


def space_in_order(items: list, key, same=None) -> list:
    """Keep the given order but never let two neighbours clash (`same(a, b)`, by default the same
    `key`): the nearest later song that fits is pulled forward; a song that fits nowhere later is
    slotted into the last earlier gap that takes it; only when nothing fits anywhere do two of a
    kind meet."""
    same = same or _same_by(key)
    out: list = []
    rest = list(items)
    while rest:
        j = next((i for i, t in enumerate(rest) if not out or not same(out[-1], t)), None)
        if j is not None:
            out.append(rest.pop(j))
            continue
        t = rest.pop(0)
        p = next((p for p in range(len(out) - 1, -1, -1) if not same(out[p], t) and (p == 0 or not same(out[p - 1], t))), None)
        if p is None:
            out.append(t)
        else:
            out.insert(p, t)
    return out


def _space_once(items: list, same) -> list:
    """One left-to-right pass: whenever a song clashes with the one before it, swap in the first
    later song that fits there without creating a clash at either end of the swap."""
    out = list(items)
    n = len(out)
    for i in range(1, n):
        if not same(out[i - 1], out[i]):
            continue
        for j in range(i + 1, n):
            out[i], out[j] = out[j], out[i]
            if not _clash_at(out, i, same) and not _clash_at(out, j, same):
                break
            out[i], out[j] = out[j], out[i]
    return out


def spread_artists(items: list, key, rng: random.Random, attempts: int = 30, same=None) -> list:
    """Reorder a shuffled list so no two neighbours clash (`same(a, b)`, by default the same `key`,
    the lead artist) whenever the mix allows it. The first pass keeps the given order where it can;
    if clashes remain, later passes start from fresh shuffles and the arrangement with the fewest
    clashes wins. A mix where one act has more than half the songs can never be fully spaced; those
    clashes are kept to a minimum."""
    same = same or _same_by(key)
    if len(items) < 3:
        return list(items)
    best = _space_once(items, same)
    best_clashes = _adjacent_clashes(best, same)
    order = list(items)
    for _ in range(attempts - 1):
        if best_clashes == 0:
            break
        rng.shuffle(order)
        cand = _space_once(order, same)
        c = _adjacent_clashes(cand, same)
        if c < best_clashes:
            best, best_clashes = cand, c
    return best


def _copy_quality(t: Track) -> tuple:
    """Higher is the better copy of a song: studio over live, plain over remix/demo, a named artist
    over "Various", the artist spelling the library uses most (the song group's representative)
    over a one-off ("Jimmy Buffet"), plain names over "Doobie Brothers, The", well tagged, lossless."""
    rep = t.song_key.split("|")[0] if t.song_key else ""
    return (
        not t.live,
        not (t.version and _ALT_VERSION.search(t.version)),
        not is_placeholder(t.artist),
        norm_artist(t.artist) == rep,
        not TRAILING_ARTICLE_RE.match(t.artist or ""),
        t.tag_confidence,
        t.codec.upper() in ("FLAC", "WAVE", "WAV", "AIFF"),
        -(len(t.version or "")),
    )


def _base_ok(t: Track, spec: PlaylistSpec, cfg: Config, genres: set[str]) -> bool:
    """The filters that describe the kind of song wanted (everything except artist and search words)."""
    if t.missing or t.duration is None or t.junk:
        return False
    if t.live and not spec.allow_live:
        return False
    if t.holiday and not spec.allow_holiday:
        return False
    if spec.clean and t.explicit is not False:
        trusted = (
            t.explicit is None
            and t.year is not None
            and cfg.clean_trust_before_year > 0
            and t.year < cfg.clean_trust_before_year
            and t.genre_norm != "Hip-Hop"
        )
        if not trusted:
            return False
    if genres and (t.genre_norm or "Other").lower() not in genres:
        return False
    if not year_in_era(t.year, spec.decades, spec.year_min, spec.year_max):
        return False
    if spec.energy_min is not None and (t.energy is None or t.energy < spec.energy_min):
        return False
    if spec.energy_max is not None and (t.energy is None or t.energy > spec.energy_max):
        return False
    if spec.top > 0 and (t.chart_peak is None or t.chart_peak > spec.top):
        return False
    return True


def _best_copies(pool: list[Track]) -> list[Track]:
    best: dict[str, Track] = {}
    for t in pool:
        cur = best.get(t.song_key)
        if cur is None or _copy_quality(t) > _copy_quality(cur):
            best[t.song_key] = t
    return list(best.values())


def fill_similar(tracks: list[Track], picked: list[Track], spec: PlaylistSpec, cfg: Config, copies: dict[str, int],
                 years: dict[str, int], need_count: Optional[int], need_seconds: Optional[float],
                 per_artist_cap: int = 2) -> list[Track]:
    """Songs by other artists that sit closest to the picked ones: same decades and genres, similar
    energy, and known where possible. Honours every filter except artist and search words, never
    repeats a song (or an act) already picked, and takes at most `per_artist_cap` per act so one
    neighbour doesn't take over."""
    if not picked or ((need_count or 0) <= 0 and (need_seconds or 0) <= 0):
        return []
    profile = derive_profile(picked)
    rules = load_genre_rules(str(cfg.genres_file))
    genres = {g.strip().lower() for g in spec.genres if g.strip()}
    have = {t.song_key for t in picked}
    picked_acts = {norm_artist(t.artist) for t in picked}
    cands = _best_copies([t for t in tracks if _base_ok(t, spec, cfg, genres) and t.song_key not in have
                          and norm_artist(t.artist) not in picked_acts])
    apply_song_years(cands, years)  # a compilation copy still counts as the year the song came out
    free_energy = spec.energy_min is None and spec.energy_max is None

    def similarity(t: Track) -> float:
        e = era_fit(t.year, profile)
        g = genre_fit(t.genre_norm, profile, rules)
        f = familiarity(t, copies.get(t.song_key, 1))
        en = 1.0
        if free_energy and profile.energy_mean is not None and t.energy is not None:
            en = max(0.0, 1.0 - abs(t.energy - profile.energy_mean) / 0.35)
        return 0.35 * e + 0.30 * g + 0.20 * f + 0.15 * en

    cands.sort(key=lambda t: (-round(similarity(t), 4), t.artist.lower(), t.title.lower()))
    out: list[Track] = []
    per_act: Counter = Counter()
    secs = 0.0
    for t in cands:
        if need_count is not None and len(out) >= need_count:
            break
        if need_seconds is not None and secs >= need_seconds:
            break
        act = norm_artist(t.artist)
        if per_act[act] >= per_artist_cap:
            continue
        out.append(t)
        per_act[act] += 1
        secs += t.duration or 0.0
    return out


def select_playlist(tracks: list[Track], spec: PlaylistSpec, cfg: Config, known_artists: Optional[set[str]] = None,
                    chart_title_acts: Optional[dict[str, set[str]]] = None) -> PlaylistResult:
    artists = [norm_basic(a) for a in spec.artists if a.strip()]
    genres = {g.strip().lower() for g in spec.genres if g.strip()}
    words = [norm_basic(w) for w in spec.search.split() if w.strip()]
    warnings: list[str] = []
    apply_song_identity(tracks, known_artists, chart_title_acts)  # one key per song however the artist is written (or mis-written)

    pool: list[Track] = []
    for t in tracks:
        if not _base_ok(t, spec, cfg, genres):
            continue
        if artists:
            hay = norm_basic(t.artist) + " | " + norm_basic(t.album_artist)
            if not any(a in hay for a in artists):
                continue
        if words:
            hay = norm_basic(t.title) + " | " + norm_basic(t.album)
            if not all(w in hay for w in words):
                continue
        pool.append(t)
    n_candidates = len(pool)

    copies, years = song_copies_and_years(tracks)
    apply_song_years(pool, years)

    if spec.dedupe:
        # Keys were just computed live (apply_song_identity), so copies indexed under older rules
        # or under another spelling of the artist still collapse into one song.
        pool = _best_copies(pool)

    # With popularity data loaded, anything a chart, radio or year-end source knows outranks
    # anything unknown, however many copies of the album track the library holds. Without it,
    # the library's own hints (copies, compilations) are all there is.
    have_charts = any(t.popularity is not None or t.chart_peak is not None for t in tracks)

    def rank_key(t: Track) -> tuple:
        fam = familiarity(t, copies.get(t.song_key, 1))
        known = t.popularity is not None or t.chart_peak is not None
        tier = 0 if (known or not have_charts) else 1
        return (tier, -fam, t.chart_peak if t.chart_peak is not None else 999, -(t.chart_weeks or 0), t.year or 9999, t.title.lower())

    ranked = sorted(pool, key=rank_key)

    picked: list[Track] = []
    runtime = 0.0
    if spec.minutes:
        for t in ranked:
            if runtime >= spec.minutes * 60:
                break
            picked.append(t)
            runtime += t.duration or 0.0
    else:
        n = spec.count if spec.count is not None else len(ranked)
        picked = ranked[:n]
        runtime = sum(t.duration or 0.0 for t in picked)

    # Short list? With `fill`, top up from other artists that sound like what matched (same era,
    # genres, similar energy, best known first). Only an artist or search filter can leave room:
    # any other filter set already admits everything that could fill it.
    n_matched, matched_secs = len(picked), runtime
    want = spec.count if spec.count and not spec.minutes else None
    need_count = want - n_matched if want and n_matched < want else None
    need_secs = spec.minutes * 60 - runtime if spec.minutes and runtime < spec.minutes * 60 else None
    filled: list[Track] = []
    if spec.fill and (artists or words) and (need_count or need_secs):
        filled = fill_similar(tracks, picked, spec, cfg, copies, years, need_count, need_secs)
        picked = picked + filled
        runtime += sum(t.duration or 0.0 for t in filled)
    filled_ids = {t.id for t in filled}

    if want and n_matched < want:
        msg = f"Only {n_matched} song(s) match the filters (asked for {want})"
        if filled:
            msg += f"; {len(filled)} added from similar artists and years, marked (similar)."
        elif spec.fill and (artists or words):
            msg += "; nothing similar enough was found to fill with."
        elif artists or words:
            msg += ". 'Fill with similar songs' (--fill) tops the list up from other artists."
        else:
            msg += "."
        warnings.append(msg)
    elif filled:
        warnings.append(f"Only {matched_secs / 60:.0f} min match the filters (asked for {spec.minutes:.0f}); "
                        f"{len(filled)} song(s) added from similar artists and years, marked (similar).")
    if spec.top and n_candidates == 0 and not any(t.chart_peak is not None for t in tracks):
        warnings.append("--top needs chart data: run `housekit charts import <csv>` first.")

    order = spec.order if spec.order in ORDERS else "rank"
    skey = spacing_key(spec)

    def same(a: Track, b: Track) -> bool:  # the same act, or the same song under another name
        return skey(a) == skey(b) or norm_title(a.title) == norm_title(b.title)

    if order == "energy":
        picked.sort(key=lambda t: (t.energy if t.energy is not None else 0.5, t.title))
    elif order == "year":
        picked.sort(key=lambda t: (t.year or 9999, t.title))
    elif order == "alpha":
        picked.sort(key=lambda t: (t.artist.lower(), t.title.lower()))
    elif order == "random":
        rng = random.Random(spec.seed)
        rng.shuffle(picked)
        picked = spread_artists(picked, skey, rng, same=same)  # never the same act twice in a row, where the mix allows
    if order in ("rank", "energy", "year") and len(picked) >= 3:
        picked = space_in_order(picked, skey, same)  # same rule for the ordered lists, keeping the order otherwise

    fam_of = {t.id: familiarity(t, copies.get(t.song_key, 1)) for t in picked}
    out = [PlaylistTrack(track=t, rank_score=round(fam_of[t.id], 3), position=i, filled=t.id in filled_ids)
           for i, t in enumerate(picked, 1)]
    return PlaylistResult(spec=spec, tracks=out, runtime=round(runtime, 1), candidates=n_candidates, warnings=warnings)
