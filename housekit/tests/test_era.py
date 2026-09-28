from pathlib import Path

from housekit.catalog import Catalog
from housekit.era import infer_years, looks_compilation, reissue_years, tag_year_credible
from housekit.models import TagResult
from housekit.textnorm import song_key


def row(i, artist, album, title, year, chart_year=None, version=""):
    return {"id": i, "artist": artist, "album": album, "title": title, "year": year, "chart_year": chart_year,
            "version": version, "song_key": song_key(artist, title)}


def test_reissue_detection():
    assert reissue_years("2013 Remaster", "") == {2013}
    assert reissue_years("", "Hotel California (40th Anniversary Deluxe Edition 2017)") == {2017}
    assert not tag_year_credible(2013, "2013 Remaster", "Hotel California")
    assert tag_year_credible(1980, "", "Back in Black (1980) [24.96 FLAC] vinyl")  # original year printed on the release
    assert not tag_year_credible(2019, "", "Classic Rock Audiophile Collection")
    assert tag_year_credible(1976, "", "Hotel California")
    assert not tag_year_credible(1994, "", "Hell Freezes Over (Live)")  # concert year, not the song's
    assert not tag_year_credible(2018, "Live", "Farewell 1 Tour: Live From Melbourne")
    assert looks_compilation("The Best Road Trip Album In The World") and not looks_compilation("Aja")


def test_live_recording_takes_the_songs_year_from_a_studio_copy():
    rows = [
        row(1, "Eagles", "Hotel California", "Wasted Time", 1976),
        row(2, "Eagles", "Hell Freezes Over (Live)", "Wasted Time", 1994),
    ]
    out = infer_years(rows)
    assert out[1] == (1976, "tag") and out[2] == (1976, "copy")


def test_chart_year_beats_remaster_year():
    out = infer_years([row(1, "Eagles", "Hotel California", "Hotel California", 2013, chart_year=1977, version="2013 Remaster")])
    assert out[1] == (1977, "chart")
    # tag agrees with chart: keep the tag
    out = infer_years([row(1, "Eagles", "Hotel California", "Hotel California", 1977, chart_year=1977)])
    assert out[1] == (1977, "tag")


def test_album_inherits_year_from_its_charted_single():
    rows = [
        row(1, "Eagles", "Hotel California (2013 Remaster)", "Hotel California", 2013, chart_year=1977),
        row(2, "Eagles", "Hotel California (2013 Remaster)", "Wasted Time", 2013),
        row(3, "Eagles", "Hotel California (2013 Remaster)", "Victim of Love", 2013),
        row(4, "Eagles", "Hotel California (2013 Remaster)", "New Kid in Town", 2013, chart_year=1976),
    ]
    out = infer_years(rows)
    assert out[1] == (1977, "chart") and out[4] == (1976, "chart")
    assert out[2] == (1976, "album") and out[3] == (1976, "album")  # median of 1977, 1976 -> 1976


def test_copies_and_compilations_and_artist_span():
    rows = [
        row(1, "Toto", "Toto IV", "Africa", 1982),
        row(2, "Toto", "Yacht Rock: 200 Smooth-Sailing Classics", "Africa", 2024),  # compilation copy
        row(3, "Toto", "Yacht Rock: 200 Smooth-Sailing Classics", "Rosanna", 2024),  # compilation, no other copy
        row(4, "Toto", "Toto IV", "Rosanna", 1982),
        row(5, "Toto", "The Seventh One", "Pamela", 1988),
        row(6, "Toto", "Some Reissue (Deluxe Edition)", "Deep Cut", 2019),  # nothing but the artist's era
    ]
    out = infer_years(rows)
    assert out[2] == (1982, "copy")
    assert out[3] == (1982, "copy")
    assert out[6][1] == "artist" and 1982 <= out[6][0] <= 1988  # Toto's credible years span 6 years
    # an artist with a 40-year career gives no artist-level guess
    rows = [row(i, "Aerosmith", f"Album {i}", f"Song {i}", y) for i, y in enumerate([1973, 1976, 1987, 1993, 2001, 2012], 1)]
    rows.append(row(99, "Aerosmith", "Greatest Hits", "Mystery", 2011))
    out = infer_years(rows)
    assert out[99] == (2011, "tag?")


def test_catalog_stores_and_hand_override_wins(tmp_path: Path):
    cat = Catalog(tmp_path / "lib.db")
    t = TagResult(path="C:/lib/a.flac", root="C:/lib", size=1, mtime=1.0, title="Hotel California", artist="Eagles",
                  album="Hotel California", version="2013 Remaster", year=2013, duration=390.0, tag_confidence=1.0,
                  song_key=song_key("Eagles", "Hotel California"))
    cat.upsert_tags([t])
    tid = cat.all_tracks()[0].id
    cat.conn.execute("UPDATE tracks SET chart_year=1977, chart_peak=1, chart_weeks=19 WHERE id=?", (tid,))
    counts = cat.compute_era_years()
    assert counts == {"chart": 1}
    tr = cat.get_track(tid)
    assert (tr.year, tr.year_tag, tr.year_source) == (1977, 2013, "chart")
    cat.set_year(tid, 1976)
    tr = cat.get_track(tid)
    assert (tr.year, tr.year_source) == (1976, "hand")
    cat.set_year(tid, None)
    assert cat.get_track(tid).year == 1977
    assert cat.export_rows()[0]["year_source"] == "chart"
    cat.close()
