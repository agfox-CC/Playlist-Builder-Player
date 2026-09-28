"""Multi-source popularity: radio spins, Billboard 200 albums, scraped year-end charts; offline loading;
decade filters; clearing the catalog."""
import csv
import json
from pathlib import Path

import pytest
from conftest import make_config, make_track
from typer.testing import CliRunner

from housekit.catalog import Catalog
from housekit.charts import (
    META_UPDATED, SOURCES, aggregate, guess_source, load_local_charts, local_chart_files, parse_yearend_page, pop_label,
    read_radio_csv, read_source, read_yearend_csv, score_for, scrape_yearend,
)
from housekit.cli import app
from housekit.era import infer_years
from housekit.models import TagResult
from housekit.playlists import DECADES, PlaylistSpec, decade_label, parse_decade, select_playlist, year_in_era
from housekit.scoring import familiarity
from housekit.textnorm import song_key

RADIO = """Song Clean,ARTIST CLEAN,Release Year,COMBINED,First?,Year?,PlayCount,F*G
Caught Up in You,.38 Special,1982,Caught Up in You by .38 Special,1,1,82,82
Hold On Loosely,.38 Special,1981,Hold On Loosely by .38 Special,1,1,85,85
Back In Black,AC/DC,1980,Back In Black by AC/DC,1,1,97,97
Sweet Home Alabama,Lynyrd Skynyrd,1974,Sweet Home Alabama by Lynyrd Skynyrd,1,1,132,132
"""

BB200 = """chart_week,current_week,title,performer,last_week,peak_pos,wks_on_chart
1980-08-16,4,Back In Black,AC/DC,7,4,3
1980-12-06,4,Back In Black,AC/DC,4,4,19
1977-10-15,3,Aja,Steely Dan,5,3,4
"""

YEAREND = """chart,label,year,rank,title,artist
hot-country-songs,country,1975,1,Rhinestone Cowboy,Glen Campbell
hot-mainstream-rock-tracks,mainstream rock,1981,7,Hold On Loosely,.38 Special
hot-country-songs,country,2023,1,Last Night,Morgan Wallen
"""


def _page(rows):
    """Minimal billboard.com year-end markup: one row container per entry."""
    out = ["<html><body>"]
    for rank, title, artist in rows:
        out.append(
            '<div class="o-chart-results-list-row-container"><ul><li><span class="c-label a-font-primary-bold-l">\n'
            f"{rank}\n</span></li><li><h3 id=\"title-of-a-story\" class=\"c-title\">\n\t{title}\n</h3>"
            f'<span class="c-label a-no-trucate a-font-primary-s">\n\t{artist}\n</span></li></ul></div>'
        )
    out.append("</body></html>")
    return "".join(out)


def _tag(path, title, artist, album="", year=None):
    return TagResult(path=path, root="C:/lib", size=1, mtime=1.0, title=title, artist=artist, album=album, year=year,
                     duration=200.0, song_key=song_key(artist, title), tag_confidence=1.0)


def test_readers(tmp_path: Path):
    r = tmp_path / "classic-rock-song-list.csv"
    r.write_text(RADIO, encoding="utf-8")
    rows = list(read_radio_csv(r))
    assert len(rows) == 4 and rows[3].spins == 132 and rows[3].year == 1974 and rows[3].peak is None
    y = tmp_path / "yearend-genre-charts.csv"
    y.write_text(YEAREND, encoding="utf-8")
    rows = list(read_yearend_csv(y))
    assert rows[1].rank == 7 and rows[1].chart == "mainstream rock" and rows[1].year == 1981
    b = tmp_path / "billboard-200-current.csv"
    b.write_text(BB200, encoding="utf-8")
    rows = list(read_source("billboard200", b))
    assert all(x.kind == "album" for x in rows) and rows[1].peak == 4 and rows[1].weeks == 19
    assert guess_source(r) == "radio" and guess_source(b) == "billboard200" and guess_source(y) == "yearend"
    assert guess_source(tmp_path / "whatever.csv") == "hot100"
    with pytest.raises(ValueError):
        list(read_radio_csv(b))  # no spins column


def test_scores_and_labels():
    assert aggregate([]) == {}
    assert score_for("radio", {"spins": 132}, spins_max=132) == 1.0
    assert 0.5 < score_for("radio", {"spins": 82}, spins_max=132) < 1.0
    assert score_for("yearend", {"rank": 1}) == 0.9 and score_for("yearend", {"rank": 100}) == 0.365
    assert score_for("billboard200", {"peak": 1, "weeks": 26}) == 0.85
    assert score_for("hot100", {"peak": 1, "weeks": 26}) == 1.0
    assert pop_label(3, 22, "hot100", 0.9) == "#3/22w"
    assert pop_label(None, None, "radio", 0.8) == "radio"
    assert pop_label(None, None, "billboard200", 0.8) == "album"
    assert pop_label(None, None, "", None) == ""


def test_parse_yearend_page():
    html = _page([(1, "Last Night", "Morgan Wallen"), (2, "Fast Car", "Luke Combs"), (100, "Fear And Friday&#8217;s", "Zach Bryan")])
    rows = parse_yearend_page(html)
    assert rows == [(1, "Last Night", "Morgan Wallen"), (2, "Fast Car", "Luke Combs"), (100, "Fear And Friday\u2019s", "Zach Bryan")]
    assert parse_yearend_page("<html>nothing here</html>") == []


def test_scrape_skips_standin_pages_and_resumes(tmp_path: Path):
    pages = {
        2021: _page([(1, "Levitating", "Dua Lipa")]),
        2020: _page([(1, "Blinding Lights", "The Weeknd"), (2, "Circles", "Post Malone")]),
    }
    calls = []

    def fetch(slug, year):
        calls.append((slug, year))
        if year == 2018:
            return None  # 404
        if year in pages:
            return pages[year]
        return pages[2020]  # billboard serves the nearest later year for a year with no page

    dest = tmp_path / "charts" / "yearend.csv"
    charts = {"hot-100-songs": ("hot100 year-end", 2018)}
    res = scrape_yearend(dest, charts, range(2018, 2022), delay=0, fetch=fetch)
    assert res["fetched"] == 4 and res["rows"] == 3 and res["standins"] == 1 and res["errors"] == 0
    assert [c[1] for c in calls] == [2021, 2020, 2019, 2018]  # newest first, so stand-ins are recognised
    with open(dest, encoding="utf-8-sig", newline="") as f:
        got = {(int(r["year"]), int(r["rank"]), r["title"]) for r in csv.DictReader(f)}
    assert got == {(2021, 1, "Levitating"), (2020, 1, "Blinding Lights"), (2020, 2, "Circles")}
    # resume: 2020 and 2021 are done; a new 2022 page is added, 2019 is still recognised as 2020's stand-in
    pages[2022] = _page([(1, "Heat Waves", "Glass Animals")])
    calls.clear()
    res = scrape_yearend(dest, charts, range(2018, 2023), delay=0, fetch=fetch)
    assert [c[1] for c in calls] == [2022, 2019, 2018] and res["rows"] == 1 and res["standins"] == 1
    with open(dest, encoding="utf-8-sig", newline="") as f:
        years = sorted({int(r["year"]) for r in csv.DictReader(f)})
    assert years == [2020, 2021, 2022]
    # stop flag honoured
    res = scrape_yearend(tmp_path / "x.csv", charts, range(2020, 2022), delay=0, fetch=fetch, should_stop=lambda: True)
    assert res["stopped"] is True and res["fetched"] == 0


def test_multi_source_import_apply_era_and_clear(tmp_path: Path):
    cat = Catalog(tmp_path / "lib.db")
    cat.upsert_tags([
        _tag("C:/lib/hol.flac", "Hold On Loosely", ".38 Special", album="Wild-Eyed Southern Boys", year=1981),
        _tag("C:/lib/bib.flac", "Back In Black", "AC/DC", album="Back In Black", year=2003),  # remaster tag year
        _tag("C:/lib/bib2.flac", "Shoot To Thrill", "AC/DC", album="Back In Black", year=2003),  # album track, dated by the album
        _tag("C:/lib/aja.flac", "Aja", "Steely Dan", album="Aja", year=1977),
        _tag("C:/lib/x.flac", "Unknown Song", "Nobody", album="Nothing", year=1999),
    ])
    for name, text in (("radio", RADIO), ("billboard200", BB200), ("yearend", YEAREND)):
        p = tmp_path / Path(SOURCES[name]["file"]).name
        p.write_text(text, encoding="utf-8")
        kind = SOURCES[name]["kind"]
        n = cat.import_chart_rows(read_source(kind, p), source=name, kind=kind)
        assert n == {"radio": 4, "billboard200": 2, "yearend": 3}[name]
    assert cat.apply_chart_data() == 4
    by = {t.path.split("/")[-1]: t for t in cat.all_tracks()}
    hol = by["hol.flac"]
    assert hol.pop_source == "radio" and hol.popularity > 0.9 and hol.chart_peak is None  # radio beats a #7 year-end rank
    bib = by["bib.flac"]
    assert bib.pop_source == "radio" and bib.album_year == 1980
    assert by["bib2.flac"].pop_source == "billboard200" and by["bib2.flac"].album_year == 1980
    assert by["aja.flac"].pop_source == "billboard200" and 0.5 < by["aja.flac"].popularity < 0.85
    assert by["x.flac"].popularity is None and by["x.flac"].pop_source == ""
    stats = cat.chart_stats()
    assert stats["per_source"] == {"radio": 4, "yearend": 3, "billboard200": 2}
    assert stats["chart_songs"] == 7 and stats["chart_albums"] == 2 and stats["popular_tracks"] == 4 and stats["charted_tracks"] == 0
    assert cat.status(["C:/lib"]).flags["popular"] == 4
    # era: the album's own chart year dates a remaster-tagged album track
    cat.compute_era_years()
    by = {t.path.split("/")[-1]: t for t in cat.all_tracks()}
    assert (by["bib.flac"].year, by["bib.flac"].year_source) == (1980, "chart")  # the radio file's release year
    assert (by["bib2.flac"].year, by["bib2.flac"].year_source) == (1980, "album chart")  # no song data: the album's debut
    assert (by["aja.flac"].year, by["aja.flac"].year_source) == (1977, "tag")
    assert by["hol.flac"].year == 1981
    # familiarity and ranking use the popularity score
    assert familiarity(by["hol.flac"]) > familiarity(by["aja.flac"]) > familiarity(by["x.flac"])
    # clearing keeps the chart data unless told otherwise
    cat.clear_catalog(keep_charts=True)
    assert cat.all_tracks() == [] and cat.chart_stats()["chart_songs"] == 7 and cat.chart_stats()["chart_albums"] == 2
    cat.clear_catalog(keep_charts=False)
    assert cat.chart_stats()["chart_songs"] == 0 and cat.chart_stats()["chart_albums"] == 0
    cat.close()


def test_infer_years_album_chart_tier():
    rows = [
        {"id": 1, "artist": "AC/DC", "album": "Back In Black", "version": "", "year": 2003, "chart_year": None, "album_year": 1980, "song_key": "a"},
        {"id": 2, "artist": "AC/DC", "album": "Back In Black", "version": "", "year": 1980, "chart_year": None, "album_year": 1980, "song_key": "b"},
        {"id": 3, "artist": "X", "album": "Greatest Hits", "version": "", "year": 2010, "chart_year": None, "album_year": 1990, "song_key": "c"},
        {"id": 4, "artist": "Y", "album": "Album", "version": "", "year": None, "chart_year": None, "album_year": 1975, "song_key": "d"},
        {"id": 5, "artist": "Z", "album": "Z", "version": "", "year": 2008, "chart_year": 2008, "album_year": 1994, "song_key": "e"},
    ]
    out = infer_years(rows)
    assert out[1] == (1980, "album chart")  # unmarked remaster: the album charted 23 years before the tag
    assert out[2] == (1980, "tag")
    assert out[3] == (2010, "tag?")  # compilations are not dated by an album chart entry
    assert out[4] == (1975, "album chart")
    assert out[5] == (2008, "tag")  # the song's own chart run agrees with the tag; the album name is older


def test_album_year_prefers_the_debut_matching_the_tag():
    row = {"first_year": 1994, "debut_years": "1994,2001,2008"}  # a self-titled record, three times
    assert Catalog._album_year_for(row, 2008) == 2008
    assert Catalog._album_year_for(row, 2016) == 2008  # a remaster tagged later: the latest debut before it
    assert Catalog._album_year_for(row, 1990) == 1994
    assert Catalog._album_year_for(row, None) == 1994
    assert Catalog._album_year_for({"first_year": 1980, "debut_years": None}, 2003) == 1980


def test_load_local_charts_offline(tmp_path: Path):
    for name, text in (("radio", RADIO), ("billboard200", BB200), ("yearend", YEAREND)):
        p = tmp_path / SOURCES[name]["file"]
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
    hot = tmp_path / "elsewhere" / "hot.csv"
    hot.parent.mkdir()
    hot.write_text("title,artist,peak\nAja,Steely Dan,50\n", encoding="utf-8")
    assert set(local_chart_files(tmp_path)) == {"radio", "billboard200", "yearend"}
    assert set(local_chart_files(tmp_path, hot100_file=hot)) == {"radio", "billboard200", "yearend", "hot100"}
    cat = Catalog(tmp_path / "lib.db")
    cat.upsert_tags([_tag("C:/lib/aja.flac", "Aja", "Steely Dan", album="Aja", year=1977)])
    res = load_local_charts(cat, tmp_path, hot100_file=hot)
    assert {s["source"] for s in res["sources"]} == {"radio", "billboard200", "yearend", "hot100"}
    assert res["tracks_matched"] == 1 and res["chart_songs"] == 8 and res["chart_albums"] == 2
    t = cat.all_tracks()[0]
    assert t.chart_peak == 50 and t.pop_source == "billboard200"  # the album run outscores a #50 single
    assert cat.get_meta(META_UPDATED)  # dated by the files, so the weekly refresh knows when it is due
    cat.close()
    assert load_local_charts(Catalog(tmp_path / "empty.db"), tmp_path / "nowhere")["sources"] == []


def test_decade_helpers():
    assert [parse_decade(x) for x in ("50", "50s", "1950", "1950s", "'70s", "00", "2000s", "10s", "20")] == [1950, 1950, 1950, 1950, 1970, 2000, 2000, 2010, 2020]
    with pytest.raises(ValueError):
        parse_decade("seventies")
    assert [decade_label(d) for d in DECADES] == ["50s", "60s", "70s", "80s", "90s", "2000s", "2010s", "2020s"]
    assert year_in_era(1985, [], None, None) and year_in_era(None, [], None, None)
    assert year_in_era(1975, [1970], None, None) and not year_in_era(1985, [1970], None, None)
    assert year_in_era(1985, [1970], 1985, 1985)  # decades OR the custom range
    assert not year_in_era(None, [1970], None, None) and not year_in_era(None, [], 1980, None)
    assert year_in_era(1999, [], 1990, None) and not year_in_era(1989, [], 1990, None)


def test_playlist_decades_filter(tmp_path: Path):
    cfg = make_config(tmp_path)
    lib = [make_track("A", "Seventies", year=1975), make_track("B", "Eighties", year=1985), make_track("C", "Nineties", year=1995),
           make_track("D", "Noughties", year=2005), make_track("E", "Undated", year=None)]
    res = select_playlist(lib, PlaylistSpec(name="x", decades=[1970, 2000], count=50), cfg)
    assert {pt.track.title for pt in res.tracks} == {"Seventies", "Noughties"}
    res = select_playlist(lib, PlaylistSpec(name="x", decades=[1970], year_min=1995, year_max=1999, count=50), cfg)
    assert {pt.track.title for pt in res.tracks} == {"Seventies", "Nineties"}
    res = select_playlist(lib, PlaylistSpec(name="x", count=50), cfg)
    assert len(res.tracks) == 5


def test_familiarity_prefers_popularity_score():
    a = make_track("A", "Radio Staple")
    a.popularity, a.pop_source = 0.95, "radio"
    b = make_track("B", "Minor Hit")
    b.chart_peak, b.chart_weeks = 20, 10  # no popularity column filled (older catalog): falls back to the peak
    c = make_track("C", "Deep Cut")
    assert familiarity(a) > familiarity(b) > familiarity(c, copies=3)
    assert familiarity(a) == round(0.45 + 0.55 * 0.95, 3)


def _make_wavs(lib: Path, names: list[str]) -> None:
    import wave

    import numpy as np

    lib.mkdir(parents=True, exist_ok=True)
    sr = 22050
    x = (0.2 * np.sin(2 * np.pi * 220 * np.arange(sr * 100) / sr) * 32767).astype("<i2")
    for name in names:
        with wave.open(str(lib / name), "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(sr)
            w.writeframes(x.tobytes())


def test_cli_charts_load_decade_and_clear(tmp_path: Path):
    lib = tmp_path / "lib" / "Lynyrd Skynyrd"
    _make_wavs(lib, ["01 - Sweet Home Alabama.wav", "02 - Simple Man.wav"])
    (tmp_path / "housekit.toml").write_text('[library]\nroots = ["lib"]\n[analysis]\njobs = 1\n', encoding="utf-8")
    (tmp_path / "charts").mkdir()
    (tmp_path / SOURCES["radio"]["file"]).write_text(RADIO, encoding="utf-8")
    runner = CliRunner()
    base = ["--config", str(tmp_path / "housekit.toml"), "--json"]
    assert runner.invoke(app, [*base, "index", "--no-analyze"], catch_exceptions=False).exit_code == 0
    r = runner.invoke(app, [*base, "charts", "load"], catch_exceptions=False)
    assert r.exit_code == 0, r.stdout
    out = json.loads(r.stdout)
    assert [s["source"] for s in out["sources"]] == ["radio"] and out["tracks_matched"] == 1 and out["popular_tracks"] == 1
    r = runner.invoke(app, [*base, "search", "sweet home"], catch_exceptions=False)
    t = json.loads(r.stdout)[0]
    assert t["pop_source"] == "radio" and t["popularity"] == 1.0 and t["chart_peak"] is None
    r = runner.invoke(app, [*base, "charts", "status"], catch_exceptions=False)
    st = json.loads(r.stdout)
    assert st["per_source"] == {"radio": 4} and list(st["files"]) == ["radio"]
    # a per-song file under its own name
    (tmp_path / "mine.csv").write_text("title,artist,peak\nSimple Man,Lynyrd Skynyrd,77\n", encoding="utf-8")
    r = runner.invoke(app, [*base, "charts", "import", str(tmp_path / "mine.csv"), "--source", "mine"], catch_exceptions=False)
    assert r.exit_code == 0, r.stdout
    out = json.loads(r.stdout)
    assert out["source"] == "mine" and out["kind"] == "hot100" and out["records"] == 1 and out["tracks_matched"] == 2
    r = runner.invoke(app, [*base, "search", "simple man"], catch_exceptions=False)
    assert json.loads(r.stdout)[0]["chart_peak"] == 77
    # decades reach the playlist spec
    r = runner.invoke(app, [*base, "playlist", "Seventies", "--decade", "70s", "--decade", "80", "--dry-run"], catch_exceptions=False)
    assert r.exit_code == 0, r.stdout
    assert json.loads(r.stdout)["playlist"]["spec"]["decades"] == [1970, 1980]
    r = runner.invoke(app, [*base, "playlist", "Bad", "--decade", "seventies", "--dry-run"], catch_exceptions=False)
    assert r.exit_code == 2
    # clearing forgets the tracks, keeps the chart data
    r = runner.invoke(app, [*base, "clear-catalog", "--yes"], catch_exceptions=False)
    assert r.exit_code == 0, r.stdout
    out = json.loads(r.stdout)
    assert out["tracks_removed"] == 2 and out["chart_songs_kept"] == 5
    r = runner.invoke(app, [*base, "status"], catch_exceptions=False)
    assert json.loads(r.stdout)["tracks"] == 0
    # without --yes it asks, and "no" leaves things alone
    r = runner.invoke(app, ["--config", str(tmp_path / "housekit.toml"), "clear-catalog"], input="n\n", catch_exceptions=False)
    assert r.exit_code == 1
