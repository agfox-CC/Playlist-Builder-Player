import json
from pathlib import Path

from conftest import make_config, make_track
from typer.testing import CliRunner

from housekit.catalog import Catalog
from housekit.charts import charts_due, download_chart_csv, update_charts
from housekit.config import detect_library_roots, load_config, write_config
from housekit.indexer import walk_roots
from housekit.jobs import BuildRequest, run_build, run_chart_update, run_playlist
from housekit.models import TagResult
from housekit.playlists import PlaylistSpec
from housekit.textnorm import song_key

HOT100 = """chart_week,current_week,title,performer,last_week,peak_pos,wks_on_chart
1982-05-08,1,Africa,Toto,2,1,20
1982-05-15,1,Africa,Toto,1,1,21
"""


def _music(root: Path, *names: str) -> None:
    for n in names:
        p = root / n
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(b"x")


def test_walk_skips_app_and_output_folders(tmp_path: Path):
    lib = tmp_path / "Music"
    _music(lib, "Toto/Africa.mp3", "HouseKit/gigs/2026 Gig/01_Doors/001 - Toto - Africa.mp3", "HouseKit/playlists/x/001 - a.mp3", "Other/b.flac")
    files = walk_roots([lib], skip=[lib / "HouseKit", lib / "HouseKit" / "gigs", lib / "HouseKit" / "playlists"])
    assert sorted(str(p.relative_to(lib)).replace("\\", "/") for p, _ in files) == ["Other/b.flac", "Toto/Africa.mp3"]


def test_detect_roots_and_write_config(tmp_path: Path):
    lib = tmp_path / "Music"
    _music(lib, "FLAC/Artist/song.flac", "MP3/song.mp3", "Photos/holiday.jpg")
    app = lib / "HouseKit"
    app.mkdir()
    roots = detect_library_roots(app)
    assert [r.name for r in roots] == ["FLAC", "MP3"]
    path = write_config(app, roots, chart_update_days=3)
    text = path.read_text(encoding="utf-8")
    assert 'roots = ["../FLAC", "../MP3"]' in text and "auto_update_days = 3" in text
    cfg = load_config(path)
    assert [r.name for r in cfg.library_roots] == ["FLAC", "MP3"]
    assert cfg.output_dir == app / "gigs" and cfg.setlist_path == app / "setlists" and cfg.chart_update_days == 3
    assert (app / "gigs").is_dir() and (app / "setlists").is_dir() and (app / "charts").is_dir()
    # music directly in the parent folder: the parent itself is the root
    lib2 = tmp_path / "Flat"
    _music(lib2, "a.mp3")
    app2 = lib2 / "HouseKit"
    app2.mkdir()
    assert detect_library_roots(app2) == [lib2.resolve()]


def test_chart_update_from_file_url_and_due_logic(tmp_path: Path, monkeypatch):
    src = tmp_path / "remote.csv"
    src.write_text(HOT100, encoding="utf-8")
    url = src.resolve().as_uri()
    dest = tmp_path / "charts" / "hot.csv"
    assert download_chart_csv(url, dest) > 0 and dest.exists()

    lib = tmp_path / "library"  # the root make_config() uses, so the catalog's relocation pass finds the file
    lib.mkdir(exist_ok=True)
    (lib / "a.flac").write_bytes(b"x")
    cat = Catalog(tmp_path / "lib.db")
    cat.upsert_tags([TagResult(path=str(lib / "a.flac"), root=str(lib), size=1, mtime=1.0, title="Africa", artist="Toto", album="Toto IV",
                               duration=200.0, tag_confidence=1.0, song_key=song_key("Toto", "Africa"))])
    assert charts_due(cat, 7) is True  # never imported
    res = update_charts(cat, url, dest)  # no base dir: only the Hot 100 file
    assert res["songs"] == 1 and res["tracks_matched"] == 1 and [s["source"] for s in res["sources"]] == ["hot100"]
    assert charts_due(cat, 7) is False
    assert charts_due(cat, 0) is False  # disabled
    assert cat.get_track(cat.all_tracks()[0].id).chart_peak == 1
    cat.close()

    # the weekly refresh also pulls the other automatic sources (Billboard 200) into <base>/charts
    from housekit import charts as chartsmod

    bb = tmp_path / "bb200-remote.csv"
    bb.write_text("chart_week,current_week,title,performer,last_week,peak_pos,wks_on_chart\n1982-05-08,4,Toto IV,Toto,5,4,30\n", encoding="utf-8")
    monkeypatch.setitem(chartsmod.SOURCES["billboard200"], "url", bb.resolve().as_uri())
    cfg = make_config(tmp_path, db_path=tmp_path / "lib.db", chart_url=url, chart_file=dest, chart_update_days=7)
    assert run_chart_update(cfg) is None  # not due
    res = run_chart_update(cfg, force=True)
    assert res["songs"] == 1 and res["chart_albums"] == 1 and (tmp_path / "charts" / "billboard-200-current.csv").exists()
    assert {s["source"] for s in res["sources"] if "error" not in s} == {"hot100", "billboard200"}
    with Catalog(tmp_path / "lib.db") as c2:
        assert c2.all_tracks()[0].album_year == 1982

    bad = tmp_path / "bad.csv"
    bad.write_text("nope,nope\n1,2\n" * 200, encoding="utf-8")
    import pytest

    with pytest.raises(RuntimeError):
        download_chart_csv(bad.resolve().as_uri(), tmp_path / "charts" / "bad.csv")


def test_jobs_build_and_playlist_end_to_end(tmp_path: Path):
    cfg = make_config(tmp_path)
    lib = cfg.library_roots[0]
    cat = Catalog(cfg.db_path)
    rows = []
    for i in range(30):
        p = lib / f"song{i}.flac"
        p.write_bytes(b"x")
        rows.append(TagResult(path=str(p), root=str(lib), size=1, mtime=1.0, title=f"Song {i}", artist=f"Artist {i % 10}",
                              duration=200.0, tag_confidence=1.0, year=1985, genre_norm="Pop", song_key=song_key(f"Artist {i % 10}", f"Song {i}")))
    cat.upsert_tags(rows)
    cat.close()
    sets = tmp_path / "setlists"
    sets.mkdir()
    (sets / "SET 1.txt").write_text("Song 1\n", encoding="utf-8")
    cfg.setlist_path = sets
    plan, written, out = run_build(cfg, BuildRequest(out=Path("2026-10-01 Bar"), slot_minutes=[3, 3, 3, 3]))
    assert out == cfg.output_dir / "2026-10-01 Bar" and plan.date == "2026-10-01" and written is not None
    assert (out / "housemusic.m3u8").exists()
    assert all(st.track.title != "Song 1" for sp in plan.slots for st in sp.tracks)
    with Catalog(cfg.db_path) as c2:
        assert [g.name for g in c2.list_gigs()] == ["2026-10-01 Bar"]
    res, w, pout = run_playlist(cfg, PlaylistSpec(name="Artist 3", artists=["artist 3"], count=10), copy=False)
    assert pout == cfg.playlists_dir / "Artist 3" and len(res.tracks) == 3 and w is not None and w.copied == 0


def test_setup_cli_writes_config(tmp_path: Path, monkeypatch):
    from housekit import config as cfgmod
    from housekit.cli import app

    lib = tmp_path / "Music"
    _music(lib, "MP3/a.mp3")
    appdir = lib / "HouseKit"
    appdir.mkdir()
    monkeypatch.setattr(cfgmod, "app_dir", lambda: appdir)
    import housekit.cli as climod

    r = CliRunner().invoke(climod.app, ["--config", str(tmp_path / "none.toml"), "--json", "setup"], catch_exceptions=False)
    assert r.exit_code == 0, r.stdout
    out = json.loads(r.stdout)
    assert out["config"] == str(appdir / "housekit.toml") and out["roots"] == [str((lib / "MP3").resolve())]
