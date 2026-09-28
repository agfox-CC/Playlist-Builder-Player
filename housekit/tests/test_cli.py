import json
import wave
from pathlib import Path

import numpy as np
from typer.testing import CliRunner

from housekit.cli import app

runner = CliRunner()


def _write_config(tmp_path: Path) -> Path:
    lib = tmp_path / "lib"
    lib.mkdir()
    cfg = tmp_path / "housekit.toml"
    cfg.write_text(
        f"""
[library]
roots = ["lib"]
db = "library.db"
[output]
dir = "gigs"
[analysis]
jobs = 2
[scoring]
jitter = 0
""".strip(),
        encoding="utf-8",
    )
    return cfg


def _tone_wav(path: Path, seconds: float = 100.0, freq: float = 220.0) -> None:
    sr = 44100
    t = np.arange(int(seconds * sr)) / sr
    x = (0.3 * np.sin(2 * np.pi * freq * t) * 32767).astype("<i2")
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes(x.tobytes())


def run(*args, cfg: Path):
    return runner.invoke(app, ["--config", str(cfg), "--json", *args], catch_exceptions=False)


def test_config_and_status_json(tmp_path: Path):
    cfg = _write_config(tmp_path)
    r = run("config", cfg=cfg)
    assert r.exit_code == 0
    data = json.loads(r.stdout)
    assert data["library_roots"] == [str(tmp_path / "lib")]
    assert data["slots"][0]["name"] == "01_Doors"
    r = run("status", cfg=cfg)
    assert r.exit_code == 0 and json.loads(r.stdout)["tracks"] == 0


def test_index_search_flag_setlist_build_roundtrip(tmp_path: Path):
    cfg = _write_config(tmp_path)
    lib = tmp_path / "lib"
    (lib / "Journey").mkdir()
    (lib / "Toto").mkdir()
    _tone_wav(lib / "Journey" / "01 - Don't Stop Believin'.wav")
    _tone_wav(lib / "Toto" / "01 - Africa.wav", freq=330)
    _tone_wav(lib / "Toto" / "02 - Rosanna.wav", freq=440)

    r = run("index", "--no-analyze", cfg=cfg)
    assert r.exit_code == 0, r.stdout
    res = json.loads(r.stdout)
    assert res["files_seen"] == 3 and res["tags_read"] == 3

    r = run("search", "toto", cfg=cfg)
    hits = json.loads(r.stdout)
    assert {h["title"] for h in hits} == {"Africa", "Rosanna"}
    assert all(h["artist"] == "Toto" and h["tag_confidence"] < 1.0 for h in hits)  # from folder/filename
    africa = next(h for h in hits if h["title"] == "Africa")

    r = run("flag", str(africa["id"]), "explicit=no", "genre=Yacht Rock", cfg=cfg)
    assert r.exit_code == 0
    t = json.loads(r.stdout)
    assert t["explicit"] is False and t["genre_norm"] == "Yacht Rock"

    # re-index must keep the hand correction
    r = run("index", "--no-analyze", "--rescan", cfg=cfg)
    assert r.exit_code == 0
    r = run("search", "africa", cfg=cfg)
    assert json.loads(r.stdout)[0]["explicit"] is False

    setlist = tmp_path / "set.txt"
    setlist.write_text("Set 1\n1. Dont Stop Believing - Journey\n2. Song We Do Not Own - Nobody\n", encoding="utf-8")
    r = run("setlist", "parse", str(setlist), cfg=cfg)
    parse = json.loads(r.stdout)
    statuses = [e["status"] for e in parse["entries"]]
    assert statuses == ["matched", "unresolved"]

    r = run("setlist", "resolve", "2. Song We Do Not Own - Nobody", "--not-in-library", cfg=cfg)
    assert r.exit_code == 0
    r = run("setlist", "parse", str(setlist), cfg=cfg)
    assert [e["status"] for e in json.loads(r.stdout)["entries"]] == ["matched", "not_in_library"]

    r = run("build", "--setlist", str(setlist), "--out", "TestGig", "--dry-run", "--doors", "2", "--break1", "1",
            "--break2", "1", "--post", "1", cfg=cfg)
    assert r.exit_code == 0, r.stdout
    out = json.loads(r.stdout)
    assert out["dry_run"] is True and out["output"] is None
    picked = [t["track"]["title"] for s in out["plan"]["slots"] for t in s["tracks"]]
    assert "Don't Stop Believin'" not in picked
    assert not (tmp_path / "gigs" / "TestGig").exists()

    r = run("build", "--setlist", str(setlist), "--out", "TestGig", "--doors", "2", "--break1", "1", "--break2", "1",
            "--post", "1", cfg=cfg)
    assert r.exit_code == 0, r.stdout
    out = json.loads(r.stdout)
    assert (tmp_path / "gigs" / "TestGig" / "housemusic.m3u8").exists()
    assert out["output"]["copied"] >= 1
    r = run("history", cfg=cfg)
    gigs = json.loads(r.stdout)
    assert len(gigs) == 1 and gigs[0]["name"] == "TestGig"

    # second build into the same folder is refused without --overwrite
    r = run("build", "--setlist", str(setlist), "--out", "TestGig", "--doors", "2", "--break1", "1", "--break2", "1",
            "--post", "1", cfg=cfg)
    assert r.exit_code == 2

    # the standing setlist: a folder of set files next to the config, used when --setlist is omitted
    standing = tmp_path / "setlists"
    standing.mkdir()
    (standing / "SET 1.txt").write_text("Dont Stop Believing\n", encoding="utf-8")
    (standing / "SET 2.txt").write_text("Africa\n", encoding="utf-8")
    r = run("setlist", "parse", cfg=cfg)
    parse = json.loads(r.stdout)
    assert [e["entry"]["set_name"] for e in parse["entries"]] == ["SET 1", "SET 2"]
    r = run("build", "--dry-run", "--doors", "1", "--break1", "1", "--break2", "1", "--post", "1", cfg=cfg)
    assert r.exit_code == 0, r.stdout
    out = json.loads(r.stdout)
    assert out["plan"]["setlist_source"].endswith("setlists")
    picked = [t["track"]["title"] for s in out["plan"]["slots"] for t in s["tracks"]]
    assert "Africa" not in picked and "Don't Stop Believin'" not in picked
    assert out["plan"]["name"].endswith(" Gig")
