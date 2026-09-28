"""Exclusions, candidate pool, and duration packing. Every hard rule about what may be
played and how many per artist/album lives here and nowhere else."""
from __future__ import annotations

import random
from datetime import date, datetime
from typing import Optional

from rapidfuzz import fuzz, process

from .config import Config
from .identity import apply_song_identity
from .models import BuildPlan, ExclusionSummary, GigProfile, ScoreBreakdown, SetlistParse, SlotPlan, SlotSpec, SlotTrack, Track
from .ordering import order_slot
from .scoring import apply_song_years, derive_profile, lufs_gain, score_track, song_copies_and_years
from .setlist import excluded_titles
from .tags import GenreRules, load_genre_rules
from .textnorm import norm_artist, norm_basic, norm_title

FUZZY_TITLE_EXCLUDE = 92  # rapidfuzz ratio at which a library title counts as "the same song"


def _title_exclusion_set(tracks: list[Track], setlist_titles: set[str]) -> set[str]:
    """Normalized library titles that match any setlist title exactly or near-exactly."""
    lib_titles = sorted({norm_title(t.title) for t in tracks if t.title})
    out: set[str] = set()
    for st in setlist_titles:
        if st in lib_titles:
            out.add(st)
        for choice, score, _ in process.extract(st, lib_titles, scorer=fuzz.ratio, limit=10, score_cutoff=FUZZY_TITLE_EXCLUDE):
            out.add(choice)
    return out


def eligible_pool(
    tracks: list[Track],
    parse: SetlistParse,
    cfg: Config,
    clean: bool,
    allow_holiday: bool,
    history_keys: set[str],
    today: Optional[date] = None,
    top: int = 0,
) -> tuple[list[Track], ExclusionSummary, set[str]]:
    """Apply every hard exclusion. Returns (pool, summary, excluded normalized titles).
    `top` > 0 keeps only songs whose chart peak reached that position (needs imported chart data)."""
    today = today or date.today()
    holiday_ok = allow_holiday or (today.month in cfg.holiday_months)
    setlist_titles = excluded_titles(parse)
    excl_titles = _title_exclusion_set(tracks, setlist_titles)
    matched_keys = {m.track_id for m in parse.matched if m.track_id is not None}
    excluded_song_keys: set[str] = set()
    for t in tracks:
        if t.id in matched_keys or norm_title(t.title) in excl_titles:
            excluded_song_keys.add(t.song_key)
            if t.dupe_group:
                excluded_song_keys.add(t.dupe_group)

    s = ExclusionSummary(total=len(tracks))
    pool: list[Track] = []
    max_len = cfg.max_track_minutes * 60.0
    for t in tracks:
        if t.missing:
            s.missing_file += 1
            continue
        if t.duration is None:
            s.no_duration += 1
            continue
        nt = norm_title(t.title)
        if t.id in matched_keys or nt in excl_titles:
            s.setlist += 1
            continue
        if t.song_key in excluded_song_keys or (t.dupe_group and t.dupe_group in excluded_song_keys):
            s.dupe_of_excluded += 1
            continue
        if t.junk:
            s.junk += 1
            continue
        if t.duration < cfg.min_track_seconds:
            s.too_short += 1
            continue
        if t.duration > max_len:
            s.too_long += 1
            continue
        if t.holiday and not holiday_ok:
            s.holiday += 1
            continue
        if clean:
            if t.explicit is True:
                s.explicit += 1
                continue
            if t.explicit is None:
                # Unknown rating: trust old non-hip-hop material (tag years only ever err *later*,
                # which keeps this conservative); everything else unknown is out.
                trusted = (
                    cfg.clean_trust_before_year > 0
                    and t.year is not None
                    and t.year < cfg.clean_trust_before_year
                    and t.genre_norm != "Hip-Hop"
                )
                if not trusted:
                    s.explicit_unknown += 1
                    continue
        if t.song_key in history_keys:
            s.history += 1
            continue
        if top > 0 and (t.chart_peak is None or t.chart_peak > top):
            s.not_top += 1
            continue
        pool.append(t)
    s.eligible = len(pool)
    return pool, s, excl_titles


def _artist_key(t: Track) -> str:
    return norm_artist(t.artist) or norm_artist(t.album_artist) or f"#{t.id}"


def _album_key(t: Track) -> Optional[str]:
    a = norm_basic(t.album)
    return a or None


def _fill_one(
    spec: SlotSpec,
    scored: list[tuple[float, Track, ScoreBreakdown]],
    cfg: Config,
    night_artist_count: dict[str, int],
    night_song_keys: set[str],
) -> tuple[list[tuple[Track, ScoreBreakdown]], float, int]:
    """Greedy by score, then a swap pass to land inside the tolerance window."""
    target, tol = spec.target_seconds, cfg.tolerance_seconds
    lo, hi = target - tol, target + tol
    chosen: list[tuple[Track, ScoreBreakdown]] = []
    runtime = 0.0
    slot_artists: dict[str, int] = {}
    slot_albums: dict[str, int] = {}

    def ok(t: Track, ignore: Optional[Track] = None) -> bool:
        ak, alk = _artist_key(t), _album_key(t)
        sa = slot_artists.get(ak, 0) - (1 if ignore is not None and _artist_key(ignore) == ak else 0)
        na = night_artist_count.get(ak, 0) - (1 if ignore is not None and _artist_key(ignore) == ak else 0)
        if sa >= cfg.artist_cap_slot or na >= cfg.artist_cap_night:
            return False
        if alk is not None:
            sal = slot_albums.get(alk, 0) - (1 if ignore is not None and _album_key(ignore) == alk else 0)
            if sal >= cfg.album_cap_slot:
                return False
        if t.song_key in night_song_keys and (ignore is None or ignore.song_key != t.song_key):
            return False
        return True

    def add(t: Track, sb: ScoreBreakdown) -> None:
        nonlocal runtime
        chosen.append((t, sb))
        runtime += t.duration or 0.0
        slot_artists[_artist_key(t)] = slot_artists.get(_artist_key(t), 0) + 1
        alk = _album_key(t)
        if alk:
            slot_albums[alk] = slot_albums.get(alk, 0) + 1
        night_artist_count[_artist_key(t)] = night_artist_count.get(_artist_key(t), 0) + 1
        night_song_keys.add(t.song_key)

    def remove(t: Track) -> None:
        nonlocal runtime
        for i, (ct, _) in enumerate(chosen):
            if ct.id == t.id:
                chosen.pop(i)
                break
        runtime -= t.duration or 0.0
        slot_artists[_artist_key(t)] -= 1
        alk = _album_key(t)
        if alk:
            slot_albums[alk] -= 1
        night_artist_count[_artist_key(t)] -= 1
        night_song_keys.discard(t.song_key)

    chosen_ids: set[int] = set()
    for total, t, sb in scored:
        if runtime >= lo:
            break
        if runtime + (t.duration or 0.0) > hi:
            continue
        if not ok(t):
            continue
        add(t, sb)
        chosen_ids.add(t.id)

    if not (lo <= runtime <= hi) and chosen:
        # Swap pass: replace one chosen track with an unchosen one whose length closes the gap.
        best_swap = None  # (score_loss, out_track, in_track, in_sb)
        for out_t, out_sb in sorted(chosen, key=lambda c: c[1].total):
            for total, in_t, in_sb in scored:
                if in_t.id in chosen_ids:
                    continue
                new_rt = runtime - (out_t.duration or 0.0) + (in_t.duration or 0.0)
                if not (lo <= new_rt <= hi):
                    continue
                if not ok(in_t, ignore=out_t):
                    continue
                loss = out_sb.total - in_sb.total
                if best_swap is None or loss < best_swap[0]:
                    best_swap = (loss, out_t, in_t, in_sb)
                break  # scored is sorted best-first; the first fit for this out_t is its best fit
        if best_swap is not None:
            _, out_t, in_t, in_sb = best_swap
            remove(out_t)
            chosen_ids.discard(out_t.id)
            add(in_t, in_sb)
            chosen_ids.add(in_t.id)

    if runtime < lo:
        # Two-for-one pass: drop the lowest-scored track and refill greedily with shorter tracks.
        for _ in range(3):
            if not chosen or runtime >= lo:
                break
            out_t, _ = min(chosen, key=lambda c: c[1].total)
            remove(out_t)
            chosen_ids.discard(out_t.id)
            before = runtime
            for total, t, sb in scored:
                if runtime >= lo:
                    break
                if t.id in chosen_ids or t.id == out_t.id:
                    continue
                if runtime + (t.duration or 0.0) > hi or not ok(t):
                    continue
                add(t, sb)
                chosen_ids.add(t.id)
            if runtime <= before:  # made it worse: put it back if it fits
                sb_map = {tt.id: ss for _, tt, ss in scored}
                if runtime + (out_t.duration or 0.0) <= hi and ok(out_t):
                    add(out_t, sb_map[out_t.id])
                    chosen_ids.add(out_t.id)
                break

    return chosen, runtime, len(scored)


def fill_slots(
    pool: list[Track],
    profile: GigProfile,
    cfg: Config,
    slots: list[SlotSpec],
    genre_rules: GenreRules,
    seed: Optional[int] = None,
    exclusions: Optional[ExclusionSummary] = None,
    copies: Optional[dict[str, int]] = None,
) -> list[SlotPlan]:
    rng = random.Random(seed)
    copies = copies or {}
    night_artist_count: dict[str, int] = {}
    night_song_keys: set[str] = set()
    plans: list[SlotPlan] = []

    for spec in slots:
        scored = []
        in_band = 0
        for t in pool:
            if t.song_key in night_song_keys:
                continue
            j = rng.uniform(0.0, cfg.jitter) if cfg.jitter > 0 else 0.0
            sb = score_track(t, profile, spec, cfg, genre_rules, jitter=j, copies=copies.get(t.song_key, 1))
            if t.energy is not None and sb.energy <= 0.0:
                continue  # hard-out: way outside this slot's energy band
            if t.energy is not None and spec.energy_min <= t.energy <= spec.energy_max:
                in_band += 1
            scored.append((sb.total, t, sb))
        scored.sort(key=lambda x: x[0], reverse=True)

        chosen, runtime, considered = _fill_one(spec, scored, cfg, night_artist_count, night_song_keys)
        target, tol = spec.target_seconds, cfg.tolerance_seconds
        delta = runtime - target
        filled = abs(delta) <= tol
        warnings: list[str] = []
        if not filled:
            why = [f"{spec.name}: {runtime/60:.1f} min of {spec.minutes:.0f} target ({delta/60:+.1f} min)."]
            why.append(f"{considered} candidates in reach of the {spec.energy_min:.2f}-{spec.energy_max:.2f} energy band, {in_band} inside it, {len(chosen)} used.")
            blocked_artist = sum(1 for _, t, _ in scored if night_artist_count.get(_artist_key(t), 0) >= cfg.artist_cap_night)
            if blocked_artist:
                why.append(f"{blocked_artist} candidates blocked by the {cfg.artist_cap_night}-per-night artist cap.")
            if exclusions is not None:
                big = sorted(
                    [(k, v) for k, v in vars(exclusions).items() if k not in ("eligible", "total") and v],
                    key=lambda kv: kv[1], reverse=True,
                )[:3]
                if big:
                    why.append("Biggest exclusion buckets: " + ", ".join(f"{k}={v}" for k, v in big) + ".")
            if considered < 30:
                why.append("Pool is thin here: relax the energy band, allow more per artist, or index more music.")
            warnings.append(" ".join(why))

        slot_tracks = [SlotTrack(track=t, score=sb) for t, sb in chosen]
        slot_tracks = order_slot(slot_tracks, spec)

        lufs_vals = sorted(st.track.lufs for st in slot_tracks if st.track.lufs is not None)
        med = spread = None
        if lufs_vals:
            n = len(lufs_vals)
            med = lufs_vals[n // 2] if n % 2 else (lufs_vals[n // 2 - 1] + lufs_vals[n // 2]) / 2
            spread = round(lufs_vals[-1] - lufs_vals[0], 1)
        for st in slot_tracks:
            st.gain_db = lufs_gain(st.track.lufs, cfg.target_lufs, st.track.peak_dbfs)
            if med is not None and st.track.lufs is not None and abs(st.track.lufs - med) > cfg.level_flag_lu:
                st.level_flag = True
                warnings.append(f"{spec.name}: '{st.track.display}' is {st.track.lufs - med:+.1f} LU off the slot median.")

        plans.append(SlotPlan(
            spec=spec, tracks=slot_tracks, runtime=round(runtime, 1), delta=round(delta, 1), filled=filled,
            warnings=warnings, lufs_median=round(med, 1) if med is not None else None, lufs_spread=spread,
            candidates_considered=considered,
        ))
    return plans


def build_plan(
    tracks: list[Track],
    parse: SetlistParse,
    cfg: Config,
    name: str,
    venue: str = "",
    slots: Optional[list[SlotSpec]] = None,
    clean: Optional[bool] = None,
    allow_holiday: Optional[bool] = None,
    history_keys: Optional[set[str]] = None,
    seed: Optional[int] = None,
    gig_date: Optional[str] = None,
    top: Optional[int] = None,
    known_artists: Optional[set[str]] = None,
    chart_title_acts: Optional[dict[str, set[str]]] = None,
) -> BuildPlan:
    """The whole plan, computed and never written. Adapters decide what to do with it."""
    slots = slots or cfg.slots
    clean = cfg.clean_default if clean is None else clean
    allow_holiday = cfg.allow_holiday_default if allow_holiday is None else allow_holiday
    history_keys = history_keys or set()
    top = cfg.top_default if top is None else top
    genre_rules = load_genre_rules(str(cfg.genres_file))

    # One key per song across artist spellings ("Doobie Brothers, The", "Jimmy Buffet", "Aerosmith &
    # Run D.M.C.", a mis-tagged "Chris Martin - Viva La Vida"), so exclusions, the never-twice rule
    # and history all see the same song as one.
    apply_song_identity(tracks, known_artists, chart_title_acts)
    # Copies of the same song: a familiarity signal, and the earliest tagged year among them is
    # the song's real era (a 2019 "Classic Rock Collection" copy of Hotel California is a 1976 song).
    copies, song_year = song_copies_and_years(tracks)
    apply_song_years(tracks, song_year)

    by_id = {t.id: t for t in tracks}
    setlist_tracks = [by_id[m.track_id] for m in parse.matched if m.track_id in by_id]
    profile = derive_profile(setlist_tracks)

    pool, summary, _ = eligible_pool(tracks, parse, cfg, clean, allow_holiday, history_keys, top=top)
    plans = fill_slots(pool, profile, cfg, slots, genre_rules, seed=seed, exclusions=summary, copies=copies)

    warnings: list[str] = []
    n_charted = sum(1 for t in tracks if t.chart_peak is not None)
    n_popular = sum(1 for t in tracks if t.popularity is not None or t.chart_peak is not None)
    if top > 0 and n_charted == 0:
        warnings.append("--top was given but no chart data is imported (housekit charts import <csv>); every track was excluded by it.")
    unresolved = [f"line {m.entry.line_no}: {m.entry.raw}" for m in parse.unresolved]
    if unresolved:
        warnings.append(
            f"{len(unresolved)} setlist line(s) unresolved. Their titles are excluded by text match, but check them: "
            "`housekit setlist parse` shows candidates and `housekit setlist resolve` pins one."
        )
    if not setlist_tracks:
        warnings.append("No setlist entries matched the library, so the gig profile is empty: era and genre fit are neutral.")
    n_unanalyzed = sum(1 for t in pool if t.energy is None)
    if n_unanalyzed:
        warnings.append(f"{n_unanalyzed} of {len(pool)} eligible tracks have no audio analysis yet; run `housekit index` to place them by energy.")
    for p in plans:
        warnings.extend(p.warnings)

    settings = cfg.settings_summary()
    settings.update({
        "clean": clean, "allow_holiday": allow_holiday, "history_excluded_keys": len(history_keys),
        "seed": seed, "slots": [vars(s) for s in slots], "top": top, "charted_tracks": n_charted, "popular_tracks": n_popular,
    })
    return BuildPlan(
        name=name, venue=venue, date=gig_date or datetime.now().date().isoformat(), setlist_source=parse.source,
        profile=profile, slots=plans, exclusions=summary, settings=settings, warnings=warnings, unresolved=unresolved,
    )

