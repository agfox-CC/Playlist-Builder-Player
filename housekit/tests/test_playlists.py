import json
from pathlib import Path

from conftest import make_config, make_track

from housekit.output import write_playlist
from housekit.playlists import PlaylistSpec, parse_range, select_playlist


def dan_library():
    def sd(title, year, peak=None, weeks=None, **kw):
        t = make_track("Steely Dan", title, year=year, **kw)
        t.chart_peak, t.chart_weeks = peak, weeks
        return t

    lib = [
        sd("Rikki Don't Lose That Number", 1974, 4, 17),
        sd("Do It Again", 1972, 6, 16),
        sd("Hey Nineteen", 1980, 10, 17),
        sd("Peg", 1977, 11, 13),
        sd("Reelin' in the Years", 1973, 11, 13),
        sd("Peg", 1977, 11, 13, album="Greatest Hits", codec="MP3"),  # second copy of Peg
        sd("Deacon Blues", 1977, 19, 12),
        sd("Bad Sneakers", 1975, 103, 3),
        sd("Kid Charlemagne", 1976),  # never charted
        # never charted but owned three times on compilations: strong library hint, still below any charted song
        sd("Black Cow", 1977, album="Greatest Hits"),
        sd("Black Cow", 2019, album="Audiophile Collection", codec="MP3"),
        sd("Black Cow", 2023, album="The Very Best Of", codec="MP3"),
        sd("Aja", 1977, duration=480),
        sd("Bodhisattva (Live)", 1974, live=True, version="Live"),
        sd("Intro", 1980, junk=True, duration=40),
        make_track("Donald Fagen", "I.G.Y.", year=1982),
        make_track("Toto", "Africa", year=1982),
    ]
    lib[-1].chart_peak, lib[-1].chart_weeks = 1, 21
    return lib


def test_parse_energy_words_and_numbers():
    from housekit.playlists import ENERGY_LEVELS, energy_level_names, parse_energy

    assert parse_energy(None) == (None, None) and parse_energy("Any") == (None, None)
    assert parse_energy("upbeat") == (0.45, 0.65)
    assert parse_energy("High energy") == (0.60, 0.80)
    assert parse_energy("high-energy") == (0.60, 0.80)
    assert parse_energy("Mellow - background, dinner") == (0.0, 0.38)  # the full dropdown label
    assert parse_energy("0.55-0.8") == (0.55, 0.8)
    assert energy_level_names()[0] == "Any" and len(energy_level_names()) == len(ENERGY_LEVELS)


def test_parse_range():
    assert parse_range("1975-1985", as_int=True) == (1975, 1985)
    assert parse_range("1980", as_int=True) == (1980, 1980)
    assert parse_range("-1989", as_int=True) == (None, 1989)
    assert parse_range("2000-", as_int=True) == (2000, None)
    assert parse_range("0.3-0.6") == (0.3, 0.6)
    assert parse_range(None) == (None, None)


def test_artist_playlist_ranks_by_chart_and_dedupes(tmp_path: Path):
    cfg = make_config(tmp_path)
    res = select_playlist(dan_library(), PlaylistSpec(name="Dan", artists=["steely dan"], count=20), cfg)
    titles = [pt.track.title for pt in res.tracks]
    # charted songs first, best peak first; never-charted after; live and junk excluded; Peg once
    assert titles[:3] == ["Rikki Don't Lose That Number", "Do It Again", "Hey Nineteen"]
    assert titles.count("Peg") == 1
    assert "Bodhisattva (Live)" not in titles and "Intro" not in titles
    assert "I.G.Y." not in titles and "Africa" not in titles
    assert titles.index("Kid Charlemagne") > titles.index("Bad Sneakers")
    # three compilation copies of an album track never outrank a song that actually charted
    assert titles.index("Black Cow") > titles.index("Bad Sneakers")
    assert titles.count("Black Cow") == 1
    cow = next(pt.track for pt in res.tracks if pt.track.title == "Black Cow")
    assert cow.year == 1977  # earliest year among the copies, not the 2023 reissue
    assert [pt.position for pt in res.tracks] == list(range(1, len(titles) + 1))
    assert res.warnings and "asked for 20" in res.warnings[0]
    # best copy wins: the FLAC album copy of Peg, not the MP3 compilation copy
    peg = next(pt.track for pt in res.tracks if pt.track.title == "Peg")
    assert peg.codec == "FLAC"


def test_filters_and_orders(tmp_path: Path):
    cfg = make_config(tmp_path)
    lib = dan_library()
    res = select_playlist(lib, PlaylistSpec(name="x", top=10, count=50), cfg)
    assert {pt.track.title for pt in res.tracks} == {"Rikki Don't Lose That Number", "Do It Again", "Hey Nineteen", "Africa"}
    res = select_playlist(lib, PlaylistSpec(name="x", year_min=1977, year_max=1977, count=50), cfg)
    assert {pt.track.title for pt in res.tracks} == {"Peg", "Deacon Blues", "Aja", "Black Cow"}
    res = select_playlist(lib, PlaylistSpec(name="x", artists=["steely dan"], minutes=10), cfg)
    assert res.runtime >= 600 and len(res.tracks) == 3
    res = select_playlist(lib, PlaylistSpec(name="x", artists=["steely dan"], count=5, order="year"), cfg)
    years = [pt.track.year for pt in res.tracks]
    assert years == sorted(years)
    res = select_playlist(lib, PlaylistSpec(name="x", artists=["steely dan"], allow_live=True, count=50), cfg)
    assert any(pt.track.live for pt in res.tracks)
    res = select_playlist(lib, PlaylistSpec(name="x", search="charlemagne", count=50), cfg)
    assert [pt.track.title for pt in res.tracks] == ["Kid Charlemagne"]


def test_ordered_lists_keep_acts_apart(tmp_path: Path):
    """User rule (2026-09-20): never the same act back to back, whatever the order."""
    from housekit.playlists import lead_artist, space_in_order

    cfg = make_config(tmp_path)
    lib = []

    def hit(artist, title, weeks, year, energy=0.5):
        t = make_track(artist, title, year=year, energy=energy)
        t.chart_peak, t.chart_weeks = 1, weeks
        lib.append(t)

    hit("Ed Sheeran", "Shape of You", 59, 2017)
    hit("Ed Sheeran", "Perfect", 57, 2017, 0.36)
    hit("Mark Ronson", "Uptown Funk", 56, 2015, 0.64)
    hit("Adele", "Rolling in the Deep", 65, 2011, 0.47)
    hit("Adele", "Set Fire to the Rain", 43, 2011, 0.41)
    hit("Lorde", "Royals", 44, 2013, 0.53)
    for order in ("rank", "energy", "year"):
        res = select_playlist(lib, PlaylistSpec(name="x", count=20, order=order), cfg)
        acts = [lead_artist(pt.track) for pt in res.tracks]
        assert len(acts) == 6 and all(a != b for a, b in zip(acts, acts[1:])), (order, acts)
    titles = [pt.track.title for pt in select_playlist(lib, PlaylistSpec(name="x", count=20, order="rank"), cfg).tracks]
    # rank order otherwise kept: the longest run still leads; Perfect slips one place behind Uptown Funk
    assert titles[:4] == ["Rolling in the Deep", "Shape of You", "Uptown Funk", "Perfect"]
    # an act asked for by name is exempt: a Steely Dan list stays in rank order
    dan = select_playlist(dan_library(), PlaylistSpec(name="Dan", artists=["steely dan"], count=20), cfg)
    assert [pt.track.title for pt in dan.tracks][:3] == ["Rikki Don't Lose That Number", "Do It Again", "Hey Nineteen"]
    # the spacer itself: pulls forward, then falls back to an earlier gap, and gives up only when it must
    assert space_in_order(list("AABBC"), lambda c: c) == list("ABABC")
    assert space_in_order(list("ABCDD"), lambda c: c) == list("ADBCD") or space_in_order(list("ABCDD"), lambda c: c) == list("ABDCD")
    assert space_in_order(list("AAAB"), lambda c: c) == list("ABAA")
    assert space_in_order([], lambda c: c) == []


def test_random_order_spaces_artists(tmp_path: Path):
    from housekit.playlists import lead_artist, spread_artists

    cfg = make_config(tmp_path)
    lib = [make_track(a, f"{a} song {i}", year=1980) for a in ("Aerosmith", "Boston", "Kansas", "Heart") for i in range(5)]
    lib += [make_track("Aerosmith & Run D.M.C.", "Walk This Way", year=1986), make_track("Various Artists", "Filler", year=1980, album="Comp")]
    for seed in range(8):
        res = select_playlist(lib, PlaylistSpec(name="x", count=50, order="random", seed=seed), cfg)
        acts = [lead_artist(pt.track) for pt in res.tracks]
        assert len(acts) == 22
        assert all(a != b for a, b in zip(acts, acts[1:])), acts  # 5 per act out of 22: always possible
    assert lead_artist(lib[-2]) == "aerosmith"  # the collaboration counts as an Aerosmith song
    # different seeds give different orders; the same seed the same order
    a = [pt.track.id for pt in select_playlist(lib, PlaylistSpec(name="x", count=50, order="random", seed=1), cfg).tracks]
    b = [pt.track.id for pt in select_playlist(lib, PlaylistSpec(name="x", count=50, order="random", seed=2), cfg).tracks]
    c = [pt.track.id for pt in select_playlist(lib, PlaylistSpec(name="x", count=50, order="random", seed=1), cfg).tracks]
    assert a != b and a == c
    # impossible mix: five by one act, one by another -> the fewest clashes possible (three), not a crash
    lumpy = [make_track("Rush", f"Rush {i}", year=1980) for i in range(5)] + [make_track("Yes", "Roundabout", year=1971)]
    res = select_playlist(lumpy, PlaylistSpec(name="x", count=50, order="random", seed=3), cfg)
    acts = [lead_artist(pt.track) for pt in res.tracks]
    assert sum(1 for x, y in zip(acts, acts[1:]) if x == y) == 3
    assert spread_artists([], lead_artist, __import__("random").Random(0)) == []


def test_fill_tops_up_short_artist_list_with_similar_songs(tmp_path: Path):
    from conftest import random_pool

    cfg = make_config(tmp_path)
    lib = random_pool(400)
    # a sparse act: three 80s pop songs, one of them on a live album
    phil = [make_track("Phil Collins", t, year=y, genre="Pop", energy=e) for t, y, e in
            (("In the Air Tonight", 1981, 0.3), ("Sussudio", 1985, 0.65), ("One More Night", 1985, 0.2))]
    phil.append(make_track("Phil Collins", "Easy Lover (Live)", year=1990, genre="Pop", live=True, version="Live"))
    # a neighbour with many songs: the per-act cap keeps it from taking over
    genesis = [make_track("Genesis", f"Genesis {i}", year=1986, genre="Pop", energy=0.4) for i in range(8)]
    lib = lib + phil + genesis
    # without --fill: the three songs and a hint
    res = select_playlist(lib, PlaylistSpec(name="x", artists=["phil collins"], count=12), cfg)
    assert len(res.tracks) == 3 and not any(pt.filled for pt in res.tracks)
    assert "asked for 12" in res.warnings[0] and "--fill" in res.warnings[0]
    # with --fill: exactly 12, the three real ones first (rank order), the rest marked and by other acts
    res = select_playlist(lib, PlaylistSpec(name="x", artists=["phil collins"], count=12, fill=True), cfg)
    assert len(res.tracks) == 12
    assert [pt.track.artist for pt in res.tracks[:3]] == ["Phil Collins"] * 3
    fills = [pt for pt in res.tracks if pt.filled]
    assert len(fills) == 9 and all(pt.track.artist != "Phil Collins" for pt in fills)
    assert [pt.position for pt in res.tracks] == list(range(1, 13))
    # similar means the same era, (mostly) the same genre and a close energy, and no act more than
    # twice: Genesis (80s pop at the matched songs' mean energy) leads, but only two of its eight
    assert all(1978 <= pt.track.year <= 1992 for pt in fills), [(pt.track.artist, pt.track.year) for pt in fills]
    assert sum(1 for pt in fills if pt.track.genre_norm == "Pop") >= 7, [pt.track.genre_norm for pt in fills]
    assert all(abs(pt.track.energy - 0.383) <= 0.25 for pt in fills), [pt.track.energy for pt in fills]
    acts = [pt.track.artist for pt in fills]
    assert acts[0] == "Genesis" and acts.count("Genesis") == 2 and acts[1] != "Genesis"  # ranked first, but never back to back
    assert max(acts.count(a) for a in acts) <= 2
    assert not any(pt.track.live for pt in fills)
    assert "9 added from similar artists" in res.warnings[0]
    # the fill honours the other filters: a decade tick or clean mode applies to the fills too
    res = select_playlist(lib, PlaylistSpec(name="x", artists=["phil collins"], count=12, fill=True, decades=[1980], clean=True), cfg)
    # (clean mode trusts unrated pre-1985 songs, so "not explicit" rather than "rated clean")
    assert all(1980 <= pt.track.year <= 1989 and pt.track.explicit is not True for pt in res.tracks if pt.filled)
    assert any(pt.filled for pt in res.tracks)
    # minutes work the same way
    res = select_playlist(lib, PlaylistSpec(name="x", artists=["phil collins"], minutes=60, fill=True), cfg)
    assert res.runtime >= 3600 and any(pt.filled for pt in res.tracks) and "60" in res.warnings[0]
    # a list that is not short, or has no artist / word filter to leave room, is untouched
    res = select_playlist(lib, PlaylistSpec(name="x", artists=["phil collins"], count=3, fill=True), cfg)
    assert len(res.tracks) == 3 and not res.warnings
    res = select_playlist(lib, PlaylistSpec(name="x", genres=["Pop"], count=500, fill=True), cfg)
    assert not any(pt.filled for pt in res.tracks)
    # the text output marks them
    from housekit.output import playlist_text

    res = select_playlist(lib, PlaylistSpec(name="x", artists=["phil collins"], count=12, fill=True), cfg)
    txt = playlist_text(res)
    assert "12 songs (9 similar)" in txt and txt.count("(similar)") == 9 + 1  # per line, plus the warning


def test_write_playlist_copy_and_no_copy(tmp_path: Path):
    cfg = make_config(tmp_path)
    lib_dir = cfg.library_roots[0]
    a = lib_dir / "a.flac"
    b = lib_dir / "b.mp3"
    a.write_bytes(b"aaa")
    b.write_bytes(b"bb")
    t1 = make_track("Steely Dan", "Peg", path=str(a), duration=100)
    t2 = make_track("Steely Dan", "Aja", path=str(b), duration=200)
    res = select_playlist([t1, t2], PlaylistSpec(name="Dan Top", artists=["steely"], count=5), cfg)
    out = write_playlist(res, tmp_path / "playlists" / "Dan Top", cfg)
    d = tmp_path / "playlists" / "Dan Top"
    assert out.copied == 2 and (d / "Dan Top.m3u8").exists() and (d / "playlist.txt").exists()
    m3u = (d / "Dan Top.m3u8").read_text(encoding="utf-8").splitlines()
    assert m3u[2].startswith("001 - Steely Dan - ")
    # no-copy variant points at the library files and copies nothing
    out2 = write_playlist(res, tmp_path / "playlists" / "Dan Link", cfg, copy=False)
    assert out2.copied == 0
    m3u = (tmp_path / "playlists" / "Dan Link" / "Dan Top.m3u8").read_text(encoding="utf-8").splitlines()  # named after the playlist
    assert m3u[2] in (str(a), str(b))
    # refuses to overwrite silently
    import pytest

    with pytest.raises(FileExistsError):
        write_playlist(res, d, cfg)
    write_playlist(res, d, cfg, overwrite=True)


def test_playlist_cli(tmp_path: Path):
    import wave

    import numpy as np
    from typer.testing import CliRunner

    from housekit.cli import app

    lib = tmp_path / "lib"
    (lib / "Steely Dan").mkdir(parents=True)
    for name in ("01 - Peg.wav", "02 - Aja.wav", "03 - Peg (Live).wav"):
        sr = 44100
        x = (0.2 * np.sin(2 * np.pi * 220 * np.arange(sr * 100) / sr) * 32767).astype("<i2")
        with wave.open(str(lib / "Steely Dan" / name), "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(sr)
            w.writeframes(x.tobytes())
    (tmp_path / "housekit.toml").write_text('[library]\nroots = ["lib"]\n[output]\nplaylists = "pl"\n[analysis]\njobs = 1\n', encoding="utf-8")
    runner = CliRunner()
    base = ["--config", str(tmp_path / "housekit.toml"), "--json"]
    assert runner.invoke(app, [*base, "index", "--no-analyze"], catch_exceptions=False).exit_code == 0
    r = runner.invoke(app, [*base, "playlist", "Dan Top 20", "--artist", "steely dan", "--no-copy"], catch_exceptions=False)
    assert r.exit_code == 0, r.stdout
    out = json.loads(r.stdout)
    titles = [pt["track"]["title"] for pt in out["playlist"]["tracks"]]
    assert sorted(titles) == ["Aja", "Peg"]  # live copy excluded, Peg once
    assert (tmp_path / "pl" / "Dan Top 20" / "Dan Top 20.m3u8").exists()
    assert out["output"]["copied"] == 0
