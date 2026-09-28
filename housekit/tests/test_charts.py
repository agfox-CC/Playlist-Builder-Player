import json
from pathlib import Path

from conftest import make_config, make_track, random_pool
from typer.testing import CliRunner

from housekit.catalog import Catalog
from housekit.charts import chart_label, chart_popularity, read_chart_csv
from housekit.cli import app
from housekit.models import TagResult
from housekit.scoring import familiarity
from housekit.setlist import match_setlist, parse_entries
from housekit.slots import build_plan, eligible_pool
from housekit.textnorm import song_key

HOT100_WEEKLY = """chart_week,current_week,title,performer,last_week,peak_pos,wks_on_chart
2014-11-29,65,Uptown Funk!,Mark Ronson Featuring Bruno Mars,,65,1
2015-01-17,1,Uptown Funk!,Mark Ronson Featuring Bruno Mars,2,1,8
2015-03-28,1,Uptown Funk!,Mark Ronson Featuring Bruno Mars,1,1,18
1989-10-28,7,Free Fallin',Tom Petty,9,7,10
1981-12-05,9,Don't Stop Believin',Journey,12,9,10
2009-06-13,4,Don't Stop Believin',Glee Cast,,4,1
1980-08-23,35,You Shook Me All Night Long,AC/DC,40,35,6
2016-01-16,98,Some Deep Cut,Nobody Famous,,98,1
"""

KAGGLE_STYLE = """date,rank,song,artist,last-week,peak-rank,weeks-on-board
1982-05-08,1,Ebony And Ivory,Paul McCartney & Stevie Wonder,2,1,5
1982-05-15,1,Ebony And Ivory,Paul McCartney & Stevie Wonder,1,1,6
"""

PER_SONG = """title,artist,peak
Africa,Toto,1
Rosanna,Toto,2
"""


def test_read_chart_csv_handles_both_layouts(tmp_path: Path):
    p = tmp_path / "hot100.csv"
    p.write_text(HOT100_WEEKLY, encoding="utf-8")
    rows = list(read_chart_csv(p))
    assert len(rows) == 8
    assert rows[2].title == "Uptown Funk!" and rows[2].peak == 1 and rows[2].weeks == 18 and rows[2].year == 2015
    p2 = tmp_path / "charts.csv"
    p2.write_text(KAGGLE_STYLE, encoding="utf-8")
    rows = list(read_chart_csv(p2))
    assert rows[1].artist == "Paul McCartney & Stevie Wonder" and rows[1].peak == 1 and rows[1].weeks == 6
    p3 = tmp_path / "songs.csv"
    p3.write_text(PER_SONG, encoding="utf-8")
    rows = list(read_chart_csv(p3))
    assert rows[0].weeks is None and rows[0].peak == 1


def test_popularity_scale():
    assert chart_popularity(1, 26) == 1.0
    assert chart_popularity(1, 1) < chart_popularity(1, 20)
    assert chart_popularity(100, 1) < 0.05
    assert chart_popularity(None, None) == 0.0
    assert chart_label(3, 22) == "#3/22w" and chart_label(None, None) == ""


def _tag(path, title, artist, album_artist=""):
    return TagResult(path=path, root="C:/lib", size=1, mtime=1.0, title=title, artist=artist, album_artist=album_artist,
                     duration=200.0, song_key=song_key(artist, title), tag_confidence=1.0)


def test_import_aggregates_and_matches_library(tmp_path: Path):
    cat = Catalog(tmp_path / "lib.db")
    cat.upsert_tags([
        _tag("C:/lib/uf.flac", "Uptown Funk (feat. Bruno Mars)", "Mark Ronson"),
        _tag("C:/lib/ff.flac", "Free Fallin'", "Tom Petty and the Heartbreakers"),
        _tag("C:/lib/dsb.flac", "Don't Stop Believin'", "Journey"),
        _tag("C:/lib/dsb2.flac", "Dont Stop Believing", "Glee Cast"),
        _tag("C:/lib/acdc.flac", "You Shook Me All Night Long", "ACDC"),
        _tag("C:/lib/other.flac", "Don't Stop Believin'", "Some Bar Band"),  # same title, unknown artist: no chart data
        _tag("C:/lib/x.flac", "Unrelated Song", "Nobody"),
    ])
    p = tmp_path / "hot100.csv"
    p.write_text(HOT100_WEEKLY, encoding="utf-8")
    n = cat.import_chart_rows(read_chart_csv(p), source="test")
    assert n == 6  # Uptown Funk rows collapse into one song
    assert cat.apply_chart_data() == 5
    by = {t.path.split("/")[-1]: t for t in cat.all_tracks()}
    assert (by["uf.flac"].chart_peak, by["uf.flac"].chart_weeks, by["uf.flac"].chart_year) == (1, 18, 2014)
    assert by["ff.flac"].chart_peak == 7  # "and the Heartbreakers" stripped
    assert by["dsb.flac"].chart_peak == 9 and by["dsb2.flac"].chart_peak == 4  # right artist per cover
    assert by["acdc.flac"].chart_peak == 35  # fuzzy artist: ACDC vs AC/DC
    assert by["other.flac"].chart_peak is None and by["x.flac"].chart_peak is None
    stats = cat.chart_stats()
    assert stats["charted_tracks"] == 5 and stats["top40_tracks"] == 5
    # re-import is idempotent and keeps the best peak
    cat.import_chart_rows(read_chart_csv(p), source="test")
    assert cat.apply_chart_data() == 5
    cat.close()


def test_charted_songs_outscore_uncharted_and_top_filter(tmp_path: Path):
    hit = make_track("A", "Hit", energy=0.5)
    hit.chart_peak, hit.chart_weeks = 2, 20
    minor = make_track("B", "Minor Hit", energy=0.5)
    minor.chart_peak, minor.chart_weeks = 85, 2
    deep = make_track("C", "Deep Cut", energy=0.5)
    assert familiarity(hit) > familiarity(minor) > familiarity(deep, copies=1)
    assert familiarity(deep, copies=3) <= 0.85  # library hints never beat a real hit

    cfg = make_config(tmp_path)
    pool = random_pool(100) + [hit, minor, deep]
    parse = match_setlist(parse_entries(["x - y"]), pool, threshold=80)
    kept, s, _ = eligible_pool(pool, parse, cfg, clean=False, allow_holiday=False, history_keys=set(), top=40)
    assert [t.title for t in kept] == ["Hit"] and s.not_top == 102
    plan = build_plan(pool, parse, cfg, name="t", top=40, slots=[cfg.slots[1]])
    assert plan.settings["top"] == 40 and not plan.all_filled  # one song can't fill 20 minutes
    assert any("candidates" in w for w in plan.warnings)


def test_charts_cli_roundtrip(tmp_path: Path):
    import wave

    import numpy as np

    lib = tmp_path / "lib"
    (lib / "Toto").mkdir(parents=True)
    for name in ("01 - Africa.wav", "02 - Rosanna.wav"):
        sr = 44100
        t = np.arange(sr * 100) / sr
        x = (0.3 * np.sin(2 * np.pi * 220 * t) * 32767).astype("<i2")
        with wave.open(str(lib / "Toto" / name), "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(sr)
            w.writeframes(x.tobytes())
    (tmp_path / "housekit.toml").write_text('[library]\nroots = ["lib"]\n[analysis]\njobs = 1\n', encoding="utf-8")
    csv = tmp_path / "songs.csv"
    csv.write_text(PER_SONG, encoding="utf-8")
    runner = CliRunner()
    base = ["--config", str(tmp_path / "housekit.toml"), "--json"]
    assert runner.invoke(app, [*base, "index", "--no-analyze"], catch_exceptions=False).exit_code == 0
    r = runner.invoke(app, [*base, "charts", "import", str(csv)], catch_exceptions=False)
    assert r.exit_code == 0, r.stdout
    out = json.loads(r.stdout)
    assert out["songs_imported"] == 2 and out["tracks_matched"] == 2
    r = runner.invoke(app, [*base, "search", "africa"], catch_exceptions=False)
    assert json.loads(r.stdout)[0]["chart_peak"] == 1
    r = runner.invoke(app, [*base, "charts", "status"], catch_exceptions=False)
    assert json.loads(r.stdout)["top40_tracks"] == 2
