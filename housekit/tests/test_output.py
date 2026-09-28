import json
from pathlib import Path

import pytest
from conftest import make_config, make_track

from housekit.models import BuildPlan, ExclusionSummary, GigProfile, ScoreBreakdown, SlotPlan, SlotSpec, SlotTrack
from housekit.output import sanitize_filename, write_output


def test_sanitize_filename_windows_and_mac():
    assert sanitize_filename('AC/DC - T.N.T. <"Live">: Vol 1?') == "AC_DC - T.N.T. _Live_ Vol 1_"
    assert sanitize_filename("con") == "_con"
    assert sanitize_filename("Ends with dot.") == "Ends with dot"
    assert sanitize_filename("   ") == "untitled"
    assert sanitize_filename("Beyoncé - Halo") == "Beyoncé - Halo"  # NFC-normalised, unicode kept
    assert len(sanitize_filename("x" * 300)) == 120


def _plan(tracks_per_slot, tmp_path):
    lib = tmp_path / "library"
    lib.mkdir(exist_ok=True)
    sb = ScoreBreakdown(1, 1, 1, 1, 1, 0, 1)
    slots = []
    for i, tracks in enumerate(tracks_per_slot, 1):
        spec = SlotSpec(f"0{i}_Slot", 10, 0.2, 0.8)
        sts = []
        for pos, t in enumerate(tracks, 1):
            sts.append(SlotTrack(track=t, score=sb, position=pos))
        slots.append(SlotPlan(spec=spec, tracks=sts, runtime=sum(t.duration for t in tracks), delta=0, filled=True))
    return BuildPlan(
        name="gig", venue="v", date="2026-09-12", setlist_source="s.txt",
        profile=GigProfile({}, {}, None, None, [], [], 0), slots=slots, exclusions=ExclusionSummary(), settings={},
    )


def test_write_output_copies_and_writes_playlists(tmp_path: Path):
    cfg = make_config(tmp_path)
    lib = cfg.library_roots[0]
    files = []
    for i, name in enumerate(["A - One.flac", "B - Two.mp3", "C - Thrée.m4a"]):
        p = lib / name
        p.write_bytes(b"x" * (100 + i))
        files.append(p)
    t1 = make_track("A", "One", duration=100, path=str(files[0]))
    t2 = make_track("B", "Two", duration=200, path=str(files[1]))
    t3 = make_track("C", "Thrée?", duration=300, path=str(files[2]))
    t4 = make_track("Ghost", "Missing", duration=300, path=str(lib / "nope.flac"))
    plan = _plan([[t1, t2], [t3, t4]], tmp_path)
    out = tmp_path / "gigs" / "gig"
    res = write_output(plan, out, cfg)
    assert res.copied == 3 and len(res.skipped) == 1
    assert (out / "01_Slot" / "001 - A - One.flac").read_bytes() == b"x" * 100
    assert (out / "02_Slot" / "001 - C - Thrée_.m4a").exists()
    slot_m3u = (out / "01_Slot" / "01_Slot.m3u8").read_text(encoding="utf-8").splitlines()
    assert slot_m3u[0] == "#EXTM3U" and slot_m3u[1] == "#EXTINF:100,A - One" and slot_m3u[2] == "001 - A - One.flac"
    master = (out / "housemusic.m3u8").read_text(encoding="utf-8").splitlines()
    assert "01_Slot/002 - B - Two.mp3" in master and "02_Slot/001 - C - Thrée_.m4a" in master
    assert not any("Missing" in line for line in master)
    manifest = json.loads((out / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["plan"]["name"] == "gig" and len(manifest["skipped"]) == 1
    assert (out / "plan.txt").exists()
    # library untouched
    assert sorted(p.name for p in lib.iterdir()) == sorted(f.name for f in files)


def test_refuses_to_write_inside_library(tmp_path: Path):
    cfg = make_config(tmp_path)
    plan = _plan([[]], tmp_path)
    with pytest.raises(ValueError):
        write_output(plan, cfg.library_roots[0] / "gig", cfg)


def test_refuses_nonempty_dir_without_overwrite(tmp_path: Path):
    cfg = make_config(tmp_path)
    plan = _plan([[]], tmp_path)
    out = tmp_path / "gigs" / "gig"
    out.mkdir(parents=True)
    (out / "something.txt").write_text("keep me")
    with pytest.raises(FileExistsError):
        write_output(plan, out, cfg)
    write_output(plan, out, cfg, overwrite=True)
    assert (out / "something.txt").read_text() == "keep me"  # only slot folders / playlists are replaced


def test_name_collisions_get_suffixes(tmp_path: Path):
    cfg = make_config(tmp_path)
    lib = cfg.library_roots[0]
    p = lib / "x.flac"
    p.write_bytes(b"1")
    t1 = make_track("Same", "Name", duration=10, path=str(p))
    t2 = make_track("Same", "Name", duration=10, path=str(p))
    plan = _plan([[t1, t2]], tmp_path)
    # positions differ so names differ by prefix; force identical positions to test the collision path
    plan.slots[0].tracks[1].position = 1
    write_output(plan, tmp_path / "gigs" / "g", cfg)
    names = sorted(f.name for f in (tmp_path / "gigs" / "g" / "01_Slot").iterdir() if f.suffix == ".flac")
    assert names == ["001 - Same - Name (2).flac", "001 - Same - Name.flac"]
