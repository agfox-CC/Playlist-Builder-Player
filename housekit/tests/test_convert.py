import math
import shutil
import wave
from pathlib import Path

import numpy as np
import pytest
from conftest import make_config

from housekit.catalog import Catalog
from housekit.convert import (FORMATS, ConvertOptions, build_command, collect_inputs, convert_files, loudness_filter,
                              plan_outputs, rewrite_playlists)
from housekit.models import TagResult
from housekit.textnorm import song_key

needs_ffmpeg = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not on PATH")
SR = 44100


def _wav(path: Path, seconds: float = 3.0, freq: float = 440.0, gain: float = 0.3) -> Path:
    t = np.arange(int(seconds * SR)) / SR
    x = (gain * np.sin(2 * math.pi * freq * t) * 32767).astype("<i2")
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(2)
        w.setsampwidth(2)
        w.setframerate(SR)
        w.writeframes(np.repeat(x, 2).tobytes())
    return path


def test_collect_inputs_and_plan(tmp_path: Path):
    gig = tmp_path / "gig"
    a = _wav(gig / "01_Doors" / "001 - A - One.wav")
    b = _wav(gig / "02_Break1" / "001 - B - Two.wav")
    loose = _wav(tmp_path / "loose.wav")
    (gig / "notes.txt").write_text("x")
    files, folders = collect_inputs([gig, loose, loose])  # duplicate input ignored
    assert [f for f, _ in files] == [a, b, loose] and folders == [gig]
    plan = plan_outputs(files, tmp_path / "out", ConvertOptions(fmt="mp3-320"))
    assert plan[0][1] == tmp_path / "out" / "gig" / "01_Doors" / "001 - A - One.mp3"
    assert plan[2][1] == tmp_path / "out" / "loose.mp3"
    flat = plan_outputs(files, tmp_path / "out", ConvertOptions(fmt="flac", keep_structure=False))
    assert {p[1].parent for p in flat} == {tmp_path / "out"}
    # in place: same folder, new extension; a file already in the format keeps its name and a same-named
    # source of another format steps aside
    _wav(gig / "01_Doors" / "003 - C - Three.wav")
    (gig / "01_Doors" / "003 - C - Three.mp3").write_bytes(b"already mp3")
    files, _ = collect_inputs([gig])
    plan = dict(plan_outputs(files, None, ConvertOptions(fmt="mp3-320")))
    assert plan[a] == gig / "01_Doors" / "001 - A - One.mp3"
    assert plan[gig / "01_Doors" / "003 - C - Three.mp3"] == gig / "01_Doors" / "003 - C - Three.mp3"
    assert plan[gig / "01_Doors" / "003 - C - Three.wav"] == gig / "01_Doors" / "003 - C - Three (2).mp3"


def test_command_and_loudness_choices(tmp_path: Path):
    cmd = build_command(Path("in.flac"), Path("out.mp3"), "mp3-320", with_cover=True, audio_filter="volume=-3.0dB", sample_rate=48000)
    assert "-map_metadata" in cmd and "libmp3lame" in cmd and "-id3v2_version" in cmd
    assert cmd[cmd.index("-af") + 1] == "volume=-3.0dB" and cmd[cmd.index("-ar") + 1] == "48000"
    assert "0:v?" in cmd  # artwork carried over
    cmd = build_command(Path("in.flac"), Path("out.wav"), "wav")
    assert "-vn" in cmd and "pcm_s16le" in cmd
    # measured track: plain gain; unmeasured: loudnorm
    assert loudness_filter(-16.0, (-10.0, -0.5)) == "volume=-6.0dB"
    assert loudness_filter(-16.0, (-16.05, -3.0)) == ""  # already there
    assert loudness_filter(-16.0, None).startswith("loudnorm=I=-16")
    assert set(FORMATS) >= {"mp3-320", "m4a-256", "flac", "wav", "ogg"}


@needs_ffmpeg
def test_convert_folder_rewrites_playlist_and_refuses_library(tmp_path: Path):
    cfg = make_config(tmp_path)
    gig = tmp_path / "gig"
    _wav(gig / "01_Doors" / "001 - A - One.wav", freq=330)
    _wav(gig / "01_Doors" / "002 - B - Two.wav", freq=440)
    (gig / "01_Doors" / "01_Doors.m3u8").write_text("#EXTM3U\n#EXTINF:3,A - One\n001 - A - One.wav\n#EXTINF:3,B - Two\n002 - B - Two.wav\n", encoding="utf-8")
    (gig / "housemusic.m3u8").write_text("#EXTM3U\n01_Doors/001 - A - One.wav\n01_Doors/002 - B - Two.wav\n", encoding="utf-8")
    out = tmp_path / "converted"
    events = []
    res = convert_files([gig], out, ConvertOptions(fmt="mp3-192", jobs=2), on_progress=events.append, forbidden_dirs=cfg.library_roots)
    assert res.converted == 2 and not res.errors and len(events) == 2
    assert (out / "gig" / "01_Doors" / "001 - A - One.mp3").stat().st_size > 1000
    pl = (out / "gig" / "01_Doors" / "01_Doors.m3u8").read_text(encoding="utf-8").splitlines()
    assert pl[2] == "001 - A - One.mp3" and pl[4] == "002 - B - Two.mp3"
    master = (out / "gig" / "housemusic.m3u8").read_text(encoding="utf-8").splitlines()
    assert master[1] == "01_Doors/001 - A - One.mp3"
    # second run skips what is already there
    res2 = convert_files([gig], out, ConvertOptions(fmt="mp3-192"), forbidden_dirs=cfg.library_roots)
    assert res2.converted == 0 and res2.skipped == 2
    # never into the library
    with pytest.raises(ValueError):
        convert_files([gig], cfg.library_roots[0] / "x", ConvertOptions(), forbidden_dirs=cfg.library_roots)


@needs_ffmpeg
def test_convert_in_place_replaces_files_and_playlists(tmp_path: Path):
    cfg = make_config(tmp_path)
    gig = tmp_path / "gigs" / "2026-10-01 Bar"
    _wav(gig / "01_Doors" / "001 - A - One.wav", freq=330)
    _wav(gig / "01_Doors" / "002 - B - Two.wav", freq=440)
    (gig / "01_Doors" / "01_Doors.m3u8").write_text("#EXTM3U\n#EXTINF:3,A - One\n001 - A - One.wav\n#EXTINF:3,B - Two\n002 - B - Two.wav\n", encoding="utf-8")
    (gig / "housemusic.m3u8").write_text("#EXTM3U\n01_Doors/001 - A - One.wav\n01_Doors/002 - B - Two.wav\n", encoding="utf-8")
    (gig / "plan.txt").write_text("plan", encoding="utf-8")
    res = convert_files([gig], None, ConvertOptions(fmt="mp3-192", jobs=2), forbidden_dirs=cfg.library_roots)
    assert res.converted == 2 and not res.errors and res.out_dir == str(gig)
    assert sorted(p.name for p in (gig / "01_Doors").iterdir()) == ["001 - A - One.mp3", "002 - B - Two.mp3", "01_Doors.m3u8"]
    assert (gig / "01_Doors" / "001 - A - One.mp3").stat().st_size > 1000
    pl = (gig / "01_Doors" / "01_Doors.m3u8").read_text(encoding="utf-8").splitlines()
    assert pl[2] == "001 - A - One.mp3" and pl[4] == "002 - B - Two.mp3"
    assert (gig / "housemusic.m3u8").read_text(encoding="utf-8").splitlines()[1] == "01_Doors/001 - A - One.mp3"
    assert sorted(res.playlists) == sorted([str(gig / "01_Doors" / "01_Doors.m3u8"), str(gig / "housemusic.m3u8")])
    assert (gig / "plan.txt").read_text(encoding="utf-8") == "plan"
    # a second run finds everything already in the format and leaves it alone
    res2 = convert_files([gig], None, ConvertOptions(fmt="mp3-192"), forbidden_dirs=cfg.library_roots)
    assert res2.converted == 0 and res2.skipped == 2 and all(i.detail == "already .mp3" for i in res2.items) and res2.playlists == []
    # never in place inside the library
    lib_song = _wav(cfg.library_roots[0] / "song.wav")
    with pytest.raises(ValueError):
        convert_files([lib_song], None, ConvertOptions(), forbidden_dirs=cfg.library_roots)
    with pytest.raises(ValueError):
        convert_files([cfg.library_roots[0]], None, ConvertOptions(), forbidden_dirs=cfg.library_roots)
    assert lib_song.exists()


@needs_ffmpeg
def test_loudness_matching_uses_catalog_gain(tmp_path: Path):
    from housekit.jobs import run_convert

    cfg = make_config(tmp_path)
    src = _wav(cfg.library_roots[0] / "quiet.wav", gain=0.05)
    cat = Catalog(cfg.db_path)
    cat.upsert_tags([TagResult(path=str(src), root=str(cfg.library_roots[0]), size=1, mtime=1.0, title="Quiet", artist="Q",
                               duration=3.0, tag_confidence=1.0, sample_rate=SR, song_key=song_key("Q", "Quiet"))])
    from housekit.models import Features

    tid = cat.all_tracks()[0].id
    cat.store_features([Features(track_id=tid, analysis_version=4, size=1, mtime=1.0, energy=0.3, lufs=-30.0, peak_dbfs=-26.0)])
    cat.close()
    res = run_convert(cfg, [src], ConvertOptions(fmt="wav", match_loudness=-16.0), tmp_path / "out")
    assert res.converted == 1
    with wave.open(str(tmp_path / "out" / "quiet.wav"), "rb") as w:
        data = np.frombuffer(w.readframes(w.getnframes()), dtype="<i2").astype(float)
    rms_out = 20 * math.log10(np.sqrt(np.mean(data ** 2)) / 32767)
    with wave.open(str(src), "rb") as w:
        data_in = np.frombuffer(w.readframes(w.getnframes()), dtype="<i2").astype(float)
    rms_in = 20 * math.log10(np.sqrt(np.mean(data_in ** 2)) / 32767)
    assert 12 < rms_out - rms_in < 15  # +14 dB gain (-30 -> -16), peak-capped at -1 dBTP so ~13-14 dB


def test_distinct_values_for_autocomplete(tmp_path: Path):
    cat = Catalog(tmp_path / "lib.db")
    rows = [TagResult(path=f"C:/lib/{i}.flac", root="C:/lib", size=1, mtime=1.0, title=t, artist=a, album=al, duration=200.0,
                      tag_confidence=1.0, song_key=song_key(a, t)) for i, (a, t, al) in enumerate([
                          ("Toto", "Africa", "Toto IV"), ("Toto", "Rosanna", "Toto IV"), ("Steely Dan", "Peg", "Aja")])]
    cat.upsert_tags(rows)
    v = cat.distinct_values()
    assert v["artists"] == ["Toto", "Steely Dan"] and v["albums"] == ["Toto IV", "Aja"]
    assert set(v["titles"]) == {"Africa", "Rosanna", "Peg"}
    cat.close()
