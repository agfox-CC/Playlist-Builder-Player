from collections import Counter
from datetime import date

import pytest
from conftest import make_config, make_track, random_pool

from housekit.setlist import match_setlist, parse_entries
from housekit.slots import build_plan, eligible_pool
from housekit.textnorm import norm_artist, norm_basic, norm_title

SETLIST = [
    "Don't Stop Believin' - Journey",
    "Free Fallin' (Remastered) - Tom Petty",
    "Uptown Funk feat. Bruno Mars - Mark Ronson",
    "The Killers - Mr Brightside",  # swapped on purpose
    "Some Original We Wrote - Us",
]


def pool_with_setlist_songs():
    pool = random_pool(400)
    pool += [
        make_track("Journey", "Don't Stop Believin'", year=1981, energy=0.6),
        make_track("Journey", "Dont Stop Believing", year=2006, energy=0.6, live=True, version="Live"),
        make_track("Glee Cast", "Don't Stop Believing", year=2009, energy=0.6),
        make_track("Journey", "Separate Ways", year=1983, energy=0.6),
        make_track("Tom Petty", "Free Fallin'", year=1989, energy=0.4),
        make_track("Tom Petty and the Heartbreakers", "Free Fallin' - 2011 Remaster", year=2011, energy=0.4),
        make_track("Tom Petty", "American Girl", year=1976, energy=0.6),
        make_track("Mark Ronson", "Uptown Funk (feat. Bruno Mars)", year=2014, energy=0.8),
        make_track("The Killers", "Mr. Brightside", year=2004, energy=0.7),
        make_track("Local Band", "Some Original We Wrote", year=2020, energy=0.5),
    ]
    return pool


def plan_for(tmp_path, pool, **kw):
    cfg = make_config(tmp_path)
    parse = match_setlist(parse_entries(SETLIST), pool, threshold=80)
    return cfg, parse, build_plan(pool, parse, cfg, name="test", **kw)


def all_slot_tracks(plan):
    return [st.track for sp in plan.slots for st in sp.tracks]


def test_no_setlist_song_in_any_slot(tmp_path):
    cfg, parse, plan = plan_for(tmp_path, pool_with_setlist_songs(), seed=1)
    banned = {"dont stop believing", "free falling", "uptown funk", "mr brightside", "some original we wrote"}
    for t in all_slot_tracks(plan):
        assert norm_title(t.title) not in banned, f"{t.display} leaked into a slot"
    # 3x Don't Stop Believin' (incl. Glee + live), 2x Free Fallin', Uptown Funk, Mr. Brightside, the original
    assert plan.exclusions.setlist == 8
    # same artist, different song is allowed and expected
    picked = {(norm_artist(t.artist), norm_title(t.title)) for t in all_slot_tracks(plan)}
    assert plan.all_filled


def test_slots_fill_within_tolerance_and_never_truncate(tmp_path):
    for n, seed in ((400, 1), (150, 2), (80, 3)):
        cfg = make_config(tmp_path)
        pool = random_pool(n, seed=seed)
        parse = match_setlist(parse_entries(["Nothing Here - Nobody"]), pool, threshold=80)
        plan = build_plan(pool, parse, cfg, name="t", seed=seed)
        for sp in plan.slots:
            assert sp.filled, f"{sp.spec.name} not filled with pool {n}: {sp.warnings}"
            assert abs(sp.runtime - sp.spec.target_seconds) <= cfg.tolerance_seconds
            assert abs(sum(st.track.duration for st in sp.tracks) - sp.runtime) < 0.1  # whole tracks only
            assert [st.position for st in sp.tracks] == list(range(1, len(sp.tracks) + 1))


def test_unfillable_slot_is_reported_not_silent(tmp_path):
    cfg = make_config(tmp_path)
    pool = [make_track(f"A{i}", f"S{i}", duration=200, energy=0.5) for i in range(3)]
    parse = match_setlist(parse_entries(["x - y"]), pool, threshold=80)
    plan = build_plan(pool, parse, cfg, name="t")
    assert not plan.all_filled
    assert any("candidates" in w for w in plan.warnings)


def test_artist_and_album_caps(tmp_path):
    cfg = make_config(tmp_path)
    # 6 artists x 20 songs each, two albums per artist: caps must bite hard
    pool = []
    for a in range(6):
        for s in range(20):
            pool.append(make_track(f"Artist {a}", f"Song {a}-{s}", duration=200, energy=0.5, album=f"Artist {a} LP {s % 2}"))
    parse = match_setlist(parse_entries(["x - y"]), pool, threshold=80)
    plan = build_plan(pool, parse, cfg, name="t")
    night = Counter()
    for sp in plan.slots:
        per_slot = Counter(norm_artist(st.track.artist) for st in sp.tracks)
        assert all(c <= cfg.artist_cap_slot for c in per_slot.values())
        per_album = Counter(norm_basic(st.track.album) for st in sp.tracks)
        assert all(c <= cfg.album_cap_slot for c in per_album.values())
        night.update(per_slot)
    assert all(c <= cfg.artist_cap_night for c in night.values())


def test_clean_mode_excludes_unknown_and_explicit(tmp_path):
    cfg = make_config(tmp_path)
    pool = random_pool(300)
    parse = match_setlist(parse_entries(["x - y"]), pool, threshold=80)
    clean_pool, summary, _ = eligible_pool(pool, parse, cfg, clean=True, allow_holiday=False, history_keys=set())
    assert all(t.explicit is False or (t.explicit is None and t.year < cfg.clean_trust_before_year) for t in clean_pool)
    assert summary.explicit > 0 and summary.explicit_unknown > 0
    cfg.clean_trust_before_year = 0
    clean_pool, summary, _ = eligible_pool(pool, parse, cfg, clean=True, allow_holiday=False, history_keys=set())
    assert all(t.explicit is False for t in clean_pool)
    dirty_pool, _, _ = eligible_pool(pool, parse, cfg, clean=False, allow_holiday=False, history_keys=set())
    assert len(dirty_pool) > len(clean_pool)


def test_history_and_holiday_and_junk_exclusions(tmp_path):
    cfg = make_config(tmp_path)
    pool = [
        make_track("A", "Played Last Month", energy=0.5),
        make_track("B", "Fresh", energy=0.5),
        make_track("C", "Jingle Bell Rock", energy=0.5, holiday=True),
        make_track("D", "Intro", energy=0.5, junk=True),
        make_track("E", "Too Short", energy=0.5, duration=60),
        make_track("F", "Too Long", energy=0.5, duration=9 * 60),
    ]
    parse = match_setlist(parse_entries(["x - y"]), pool, threshold=80)
    hist = {pool[0].song_key}
    p, s, _ = eligible_pool(pool, parse, cfg, clean=False, allow_holiday=False, history_keys=hist, today=date(2026, 9, 12))
    assert [t.title for t in p] == ["Fresh"]
    assert s.history == 1 and s.holiday == 1 and s.junk == 1 and s.too_short == 1 and s.too_long == 1
    p, _, _ = eligible_pool(pool, parse, cfg, clean=False, allow_holiday=False, history_keys=set(), today=date(2026, 12, 12))
    assert any(t.title == "Jingle Bell Rock" for t in p)


def test_energy_band_respected(tmp_path):
    cfg = make_config(tmp_path)
    pool = random_pool(400)
    parse = match_setlist(parse_entries(["x - y"]), pool, threshold=80)
    plan = build_plan(pool, parse, cfg, name="t", seed=5)
    for sp in plan.slots:
        for st in sp.tracks:
            e = st.track.energy
            assert sp.spec.energy_min - cfg.energy_falloff < e < sp.spec.energy_max + cfg.energy_falloff


def test_copies_set_song_year_and_boost_familiarity(tmp_path):
    cfg = make_config(tmp_path)
    # a 1976 song owned three times: album copy, and two 2019/2020 compilation copies
    a1 = make_track("Eagles", "Hotel California", year=1976, energy=0.5, album="Hotel California")
    a2 = make_track("Eagles", "Hotel California", year=2019, energy=0.5, album="Classic Rock Audiophile Collection")
    a3 = make_track("Eagles", "Hotel California", year=2020, energy=0.5, album="Classic Rock Favorites")
    solo = make_track("Nobody", "One Copy Wonder", year=1976, energy=0.5, album="Some LP")
    pool = [a1, a2, a3, solo] + random_pool(50)
    parse = match_setlist(parse_entries(["x - y"]), pool, threshold=80)
    build_plan(pool, parse, cfg, name="t", slots=[cfg.slots[0]])
    assert a2.year == 1976 and a3.year == 1976  # earliest copy's year wins for era scoring
    from housekit.scoring import familiarity
    assert familiarity(a1, copies=3) > familiarity(solo, copies=1)


def test_seed_changes_plan_but_keeps_validity(tmp_path):
    pool = pool_with_setlist_songs()
    cfg = make_config(tmp_path, jitter=0.05)
    parse = match_setlist(parse_entries(SETLIST), pool, threshold=80)
    p1 = build_plan(pool, parse, cfg, name="t", seed=1)
    p2 = build_plan(pool, parse, cfg, name="t", seed=2)
    ids1 = [t.id for t in all_slot_tracks(p1)]
    ids2 = [t.id for t in all_slot_tracks(p2)]
    assert ids1 != ids2
    assert p1.all_filled and p2.all_filled
