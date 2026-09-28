from pathlib import Path

from housekit.catalog import Catalog
from housekit.models import Features, TagResult
from housekit.textnorm import song_key


def tag(path: str, title: str, artist: str, explicit=None, junk=False, size=100, mtime=1.0, duration=200.0) -> TagResult:
    return TagResult(
        path=path, root="C:/lib", size=size, mtime=mtime, title=title, artist=artist, album="LP", genre_norm="Pop",
        year=1999, tag_confidence=1.0, duration=duration, explicit_auto=explicit, junk_auto=junk,
        song_key=song_key(artist, title),
    )


def test_hand_corrections_survive_reindex(tmp_path: Path):
    cat = Catalog(tmp_path / "lib.db")
    cat.upsert_tags([tag("C:/lib/a.flac", "Song", "Band", explicit=None, junk=True)])
    t = cat.all_tracks()[0]
    assert t.explicit is None and t.junk is True
    cat.set_flag(t.id, "explicit", False)
    cat.set_flag(t.id, "junk", False)
    cat.set_genre(t.id, "Classic Rock")
    # re-index with different auto values (rules changed, say)
    cat.upsert_tags([tag("C:/lib/a.flac", "Song", "Band", explicit=True, junk=True, mtime=2.0)])
    t2 = cat.all_tracks()[0]
    assert t2.id == t.id
    assert t2.explicit is False and t2.junk is False and t2.genre_norm == "Classic Rock"
    # clearing the override falls back to the (new) auto value
    cat.set_flag(t.id, "explicit", None)
    assert cat.all_tracks()[0].explicit is True
    cat.close()


def test_moved_file_inherits_overrides(tmp_path: Path):
    cat = Catalog(tmp_path / "lib.db")
    cat.upsert_tags([tag("C:/lib/old/a.flac", "Song", "Band", explicit=None)])
    t = cat.all_tracks()[0]
    cat.set_flag(t.id, "explicit", True)
    # file moved: old path missing, new path appears
    cat.upsert_tags([tag("C:/lib/new/a.flac", "Song", "Band", explicit=None)])
    cat.mark_missing({"C:/lib/new/a.flac"}, ["C:/lib"])
    assert cat.inherit_overrides_from_missing() == 1
    present = [x for x in cat.all_tracks() if not x.missing]
    assert len(present) == 1 and present[0].path.endswith("new/a.flac") and present[0].explicit is True
    cat.close()


def test_analysis_jobs_incremental(tmp_path: Path):
    cat = Catalog(tmp_path / "lib.db")
    cat.upsert_tags([tag("C:/lib/a.flac", "A", "X"), tag("C:/lib/b.flac", "B", "Y")])
    jobs = cat.analysis_jobs(version=2)
    assert len(jobs) == 2
    a_id = jobs[0][0]
    cat.store_features([Features(track_id=a_id, analysis_version=2, size=100, mtime=1.0, energy=0.5, lufs=-12.0)])
    jobs = cat.analysis_jobs(version=2)
    assert len(jobs) == 1 and jobs[0][0] != a_id
    # version bump forces re-analysis; file change (mtime) too
    assert len(cat.analysis_jobs(version=3)) == 2
    cat.upsert_tags([tag("C:/lib/a.flac", "A", "X", mtime=5.0)])
    assert len(cat.analysis_jobs(version=2)) == 2
    # errored analyses are not retried unless asked
    cat.store_features([Features(track_id=a_id, analysis_version=2, size=100, mtime=5.0, error="boom")])
    assert all(j[0] != a_id for j in cat.analysis_jobs(version=2))
    assert any(j[0] == a_id for j in cat.analysis_jobs(version=2, retry_errors=True))
    cat.close()


def test_dupe_groups_and_history(tmp_path: Path):
    cat = Catalog(tmp_path / "lib.db")
    cat.upsert_tags([
        tag("C:/lib/a.flac", "Same Song", "Band"), tag("C:/lib/a2.mp3", "Same Song (Remastered)", "The Band"),
        tag("C:/lib/b.flac", "Other", "Band"),
    ])
    assert cat.rebuild_dupe_groups() == 1
    tracks = {t.title: t for t in cat.all_tracks()}
    assert tracks["Same Song"].dupe_group == tracks["Same Song"].song_key
    assert tracks["Other"].dupe_group is None

    gid = cat.log_gig("g1", "2026-01-01", "v", "out", [(tracks["Other"].id, "01_Doors", 1, tracks["Other"].song_key, "Band - Other")])
    cat.log_gig("g2", "2026-02-01", "v", "out", [(tracks["Same Song"].id, "01_Doors", 1, tracks["Same Song"].song_key, "x")])
    assert cat.recent_song_keys(1) == {tracks["Same Song"].song_key}
    assert cat.recent_song_keys(2) == {tracks["Same Song"].song_key, tracks["Other"].song_key}
    assert cat.recent_song_keys(0) == set()
    assert [g.name for g in cat.list_gigs()] == ["g2", "g1"]
    assert cat.delete_gig(gid)
    assert [g.name for g in cat.list_gigs()] == ["g2"]
    cat.close()


def test_untagged_genre_inherits_from_artist(tmp_path: Path):
    cat = Catalog(tmp_path / "lib.db")
    rows = [tag("C:/lib/q1.flac", "One", "Queen"), tag("C:/lib/q2.flac", "Two", "Queen"), tag("C:/lib/q3.flac", "Three", "Queen")]
    rows[0].genre_raw, rows[0].genre_norm = "Rock", "Classic Rock"
    rows[1].genre_raw, rows[1].genre_norm = "Rock", "Classic Rock"
    rows[2].genre_raw, rows[2].genre_norm = "", "Other"  # no tag on this one
    cat.upsert_tags(rows)
    assert cat.fill_genres_from_artist() == 1
    assert {t.title: t.genre_norm for t in cat.all_tracks()}["Three"] == "Classic Rock"
    # a hand-set genre still wins
    t3 = next(t for t in cat.all_tracks() if t.title == "Three")
    cat.set_genre(t3.id, "Pop")
    cat.fill_genres_from_artist()
    assert cat.get_track(t3.id).genre_norm == "Pop"
    cat.close()


def test_recompute_song_keys_after_rule_change(tmp_path: Path):
    """Two copies of a song indexed under different normalization rules must end up with one key."""
    cat = Catalog(tmp_path / "lib.db")
    a = tag("C:/lib/a.mp3", "R.O.C.K. In The U.S.A.", "John Cougar Mellencamp")
    b = tag("C:/lib/b.flac", "R.O.C.K. In The U.S.A.", "John Cougar Mellencamp")
    a.song_key = "john cougar mellencamp|r o c k in the u s a"  # what an older rule set produced
    cat.upsert_tags([a, b])
    keys = {t.song_key for t in cat.all_tracks()}
    assert len(keys) == 2  # stale vs current: this is the bug the recompute fixes
    ids = {t.path: t.id for t in cat.all_tracks()}
    cat.log_gig("g", "2026-01-01", "", "out", [(ids["C:/lib/a.mp3"], "01_Doors", 1, a.song_key, "x")])
    assert cat.recompute_song_keys() == 1
    keys = {t.song_key for t in cat.all_tracks()}
    assert len(keys) == 1
    assert cat.recent_song_keys(1) == keys  # history follows the new key
    assert cat.rebuild_dupe_groups() == 1
    assert cat.recompute_song_keys() == 0  # idempotent
    cat.close()


def test_search_and_status(tmp_path: Path):
    cat = Catalog(tmp_path / "lib.db")
    cat.upsert_tags([tag("C:/lib/a.flac", "Free Fallin'", "Tom Petty"), tag("C:/lib/b.flac", "American Girl", "Tom Petty")])
    assert [t.title for t in cat.search("petty fall")] == ["Free Fallin'"]
    st = cat.status(["C:/lib"])
    assert st.tracks == 2 and st.analyzed == 0 and st.flags["explicit_unknown"] == 2
    cat.close()
