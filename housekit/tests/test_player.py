"""The standalone player: mixer math offline, edge analysis, cue lists from folders, the transport."""
import json
import math
import shutil
import wave
from pathlib import Path

import numpy as np
import pytest
from typer.testing import CliRunner

from housekit.player.cues import Cue, build_show, load_show, read_m3u8, save_show
from housekit.player.decode import Edges, analyze_edges, db_to_gain, decode, envelope
from housekit.player.engine import Engine, Voice
from housekit.player.show import Player

needs_ffmpeg = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not on PATH")


def _pcm(seconds: float, rate: int, level: float = 0.5) -> np.ndarray:
    n = int(seconds * rate)
    return np.full((n, 2), int(level * 32767), dtype=np.int16)


def test_engine_mixes_fades_and_schedules_sample_accurately():
    eng = Engine(rate=100, blocksize=10)
    v1 = Voice(_pcm(1.0, 100), 100, fade_in=20, gain=1.0)  # 100 samples, 0.5 level
    eng.play(v1)
    blocks = [eng.render(10) for _ in range(4)]
    a = np.concatenate(blocks)[:, 0]
    assert a[0] < 0.05 and abs(a[19] - 0.5) < 0.05 and abs(a[30] - 0.5) < 1e-4  # sine fade-in over 20 samples
    assert eng.clock == 40
    # a second voice scheduled at sample 55 starts inside the block covering 50-60
    v2 = Voice(_pcm(0.5, 100), 100, gain=0.5)
    eng.play(v2, at_clock=55)
    blk = eng.render(10)  # 40-50: only v1
    assert np.allclose(blk[:, 0], 0.5, atol=1e-3)
    blk = eng.render(10)  # 50-60
    assert np.allclose(blk[:5, 0], 0.5, atol=1e-3) and np.allclose(blk[5:, 0], 0.75, atol=1e-3)
    # manual fade finishes the voice
    eng.fade_out(v1, 0.1)
    out = np.concatenate([eng.render(10) for _ in range(2)])[:, 0]
    assert out[0] > 0.7 and out[-1] < 0.26 and v1.done
    assert [v is v2 for v in eng.active()] == [True]
    # pause: silence, clock frozen
    eng.pause()
    assert not eng.render(10).any() and eng.clock == 80
    eng.resume()
    # a pad ducks the bed
    pad = Voice(_pcm(1.0, 100, level=0.25), 100, is_pad=True)
    eng.play(pad)
    for _ in range(6):
        blk = eng.render(10)
    assert abs(eng._duck_cur - eng.duck_level) < 1e-6
    assert blk[0, 0] < 0.25 + 0.25 * eng.duck_level + 0.01
    # master gain
    eng.set_master_db(-6.0)
    for _ in range(3):
        blk = eng.render(10)
    assert abs(eng._master_cur - db_to_gain(-6.0)) < 1e-6
    # stop all fades everything, including pads
    eng.stop_all(0.05)
    for _ in range(3):
        eng.render(10)
    assert eng.active(pads=True) == []


def test_mono_folds_both_channels():
    eng = Engine(rate=100, blocksize=10)
    pcm = np.zeros((100, 2), dtype=np.int16)
    pcm[:, 0], pcm[:, 1] = 16384, -16384  # hard left and inverted right: cancels in mono
    eng.play(Voice(pcm, 100))
    assert not np.allclose(eng.render(10), 0)
    eng.mono = True
    out = eng.render(10)
    assert np.allclose(out, 0)
    pcm2 = np.zeros((100, 2), dtype=np.int16)
    pcm2[:, 0] = 16384  # left only: half level on both sides
    eng2 = Engine(rate=100, blocksize=10)
    eng2.mono = True
    eng2.play(Voice(pcm2, 100))
    out = eng2.render(10)
    assert np.allclose(out[:, 0], 0.25) and np.allclose(out[:, 1], 0.25)


def test_crossfade_is_equal_power():
    eng = Engine(rate=100, blocksize=10)
    a = Voice(_pcm(1.0, 100), 100, fade_out=40)
    alt = _pcm(1.0, 100)
    alt[1::2] *= -1  # uncorrelated with a's constant signal: the realistic case for two songs
    b = Voice(alt, 100, fade_in=40)
    eng.play(a)
    eng.play(b, at_clock=60)  # overlaps a's last 40 samples
    out = np.concatenate([eng.render(10) for _ in range(10)])[:, 0]
    power = (out[60:100] ** 2).reshape(10, 4).mean(axis=1)
    # each alone carries 0.25 of power; sin^2 + cos^2 keeps the sum there through the overlap
    assert np.allclose(power, 0.25, atol=0.03)
    assert np.allclose(out[:60] ** 2, 0.25, atol=1e-3) and np.allclose(out[100:] ** 2, 0.25, atol=1e-3)


def test_analyze_edges_finds_silence_and_ending_type():
    rate = 1000
    t = np.arange(rate * 4) / rate
    tone = 0.5 * np.sin(2 * math.pi * 50 * t)
    fade = tone.copy()
    fade[-2 * rate:] *= np.linspace(1, 0, 2 * rate)  # two-second fade
    sil = np.zeros(rate // 2)
    faded = np.concatenate([sil, fade, sil])
    pcm = (np.stack([faded, faded], axis=1) * 32767).astype(np.int16)
    e = analyze_edges(pcm, rate)
    assert 0.45 <= e.in_seconds <= 0.5 and 4.3 <= e.out_seconds <= 4.6
    assert e.ending == "fade" and 1.0 <= e.fade_seconds <= 2.5
    cold = np.concatenate([sil, tone, sil])
    pcm = (np.stack([cold, cold], axis=1) * 32767).astype(np.int16)
    e = analyze_edges(pcm, rate)
    assert e.ending == "cold" and abs(e.out_seconds - 4.5) < 0.1
    assert analyze_edges(np.zeros((0, 2), dtype=np.int16), rate) == Edges(0, 0, "cold", 0.0, rate)
    assert analyze_edges(np.zeros((rate, 2), dtype=np.int16), rate).ending == "cold"


def test_envelope_overview():
    pcm = np.zeros((6000, 2), dtype=np.int16)
    pcm[2000:4000] = 16384  # loud middle third
    env = envelope(pcm, cols=60)
    assert env.shape == (60,) and env.dtype == np.float32
    assert env[:20].max() == 0 and env[40:].max() == 0 and 0.6 < env[20:40].min() <= 1.0
    assert envelope(np.zeros((0, 2), dtype=np.int16), cols=10).shape == (10,)


def _wav(path: Path, seconds: float = 2.0, freq: float = 440.0) -> Path:
    sr = 44100
    t = np.arange(int(seconds * sr)) / sr
    x = (0.3 * np.sin(2 * math.pi * freq * t) * 32767).astype("<i2")
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes(x.tobytes())
    return path


@needs_ffmpeg
def test_decode_through_ffmpeg(tmp_path: Path):
    p = _wav(tmp_path / "tone.wav", seconds=1.0)
    pcm = decode(p, rate=48000)
    assert pcm.shape[1] == 2 and abs(len(pcm) - 48000) < 200 and pcm.dtype == np.int16
    assert 0.25 < np.abs(pcm[:, 0]).max() / 32768 < 0.35
    with pytest.raises(RuntimeError):
        decode(tmp_path / "missing.wav")


def _gig(tmp_path: Path) -> Path:
    gig = tmp_path / "gigs" / "2026-10-01 Bar"
    d = gig / "01_Doors"
    for name in ("001 - Toto - Africa.wav", "002 - Boston - More Than A Feeling.wav"):
        _wav(d / name, seconds=0.5)
    (d / "01_Doors.m3u8").write_text("#EXTM3U\n#EXTINF:240,Toto - Africa\n001 - Toto - Africa.wav\n#EXTINF:280,Boston - More Than A Feeling\n002 - Boston - More Than A Feeling.wav\n", encoding="utf-8")
    d2 = gig / "02_Break1"
    _wav(d2 / "001 - Kansas - Carry On.wav", seconds=0.5)
    (d2 / "02_Break1.m3u8").write_text("#EXTM3U\n001 - Kansas - Carry On.wav\n", encoding="utf-8")
    (gig / "housemusic.m3u8").write_text("#EXTM3U\n01_Doors/001 - Toto - Africa.wav\n", encoding="utf-8")
    manifest = {"plan": {"slots": [
        {"spec": {"name": "01_Doors"}, "tracks": [
            {"out_name": "001 - Toto - Africa.wav", "gain_db": -2.5, "track": {"artist": "Toto", "title": "Africa", "duration": 240.0}},
            {"out_name": "002 - Boston - More Than A Feeling.wav", "gain_db": 1.0, "track": {"artist": "Boston", "title": "More Than A Feeling", "duration": 280.0}},
        ]},
        {"spec": {"name": "02_Break1"}, "tracks": [{"out_name": "001 - Kansas - Carry On.wav", "gain_db": None, "track": {"artist": "Kansas", "title": "Carry On", "duration": 200.0}}]},
    ]}}
    (gig / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    return gig


def test_show_from_gig_playlist_and_plain_folders(tmp_path: Path, tmp_path_factory):
    gig = _gig(tmp_path)
    show = build_show(gig)
    assert [cl.name for cl in show.lists] == ["01_Doors", "02_Break1"]
    doors = show.lists[0].cues
    assert [c.display for c in doors] == ["Toto - Africa", "Boston - More Than A Feeling"]
    assert doors[0].gain_db == -2.5 and doors[1].gain_db == 1.0 and doors[0].duration == 240.0
    assert doors[0].path == str(Path("01_Doors") / "001 - Toto - Africa.wav") and show.abs_path(doors[0].path).is_file()
    assert show.lists[1].cues[0].gain_db == 0.0
    # a playlist folder with its own manifest: gain from measured loudness
    pl = tmp_path / "playlists" / "Dan"
    _wav(pl / "001 - Steely Dan - Peg.wav", seconds=0.5)
    (pl / "Dan.m3u8").write_text("#EXTM3U\n#EXTINF:230,Steely Dan - Peg\n001 - Steely Dan - Peg.wav\n", encoding="utf-8")
    (pl / "manifest.json").write_text(json.dumps({"playlist": {"tracks": [{"out_name": "001 - Steely Dan - Peg.wav", "track": {"artist": "Steely Dan", "title": "Peg", "lufs": -10.0, "peak_dbfs": -0.5, "duration": 230.0}}]}}), encoding="utf-8")
    s2 = build_show(pl)
    assert len(s2.lists) == 1 and s2.lists[0].cues[0].gain_db == -6.0  # -10 LUFS -> -16
    # a plain folder: files in name order, titles from names
    plain = tmp_path / "loose"
    _wav(plain / "b.wav", 0.2)
    _wav(plain / "a.wav", 0.2)
    (plain / "notes.txt").write_text("x")
    s3 = build_show(plain)
    assert [c.title for c in s3.lists[0].cues] == ["a", "b"]
    # an m3u8 given directly, with absolute entries
    m = tmp_path / "abs.m3u8"
    m.write_text(f"#EXTM3U\n{plain / 'a.wav'}\n", encoding="utf-8")
    s4 = build_show(m)
    assert s4.lists[0].cues[0].title == "a" and s4.abs_path(s4.lists[0].cues[0].path).is_file()
    elsewhere = tmp_path_factory.mktemp("elsewhere")  # an entry outside the playlist's folder stays absolute
    _wav(elsewhere / "c.wav", 0.2)
    far = tmp_path / "far.m3u8"
    far.write_text(f"#EXTM3U\n{elsewhere / 'c.wav'}\n", encoding="utf-8")
    s5 = build_show(far)
    assert Path(s5.lists[0].cues[0].path).is_absolute() and s5.abs_path(s5.lists[0].cues[0].path).is_file()
    assert read_m3u8(tmp_path / "nope.m3u8") == []


def test_cues_json_roundtrip_keeps_edits_and_follows_files(tmp_path: Path):
    gig = _gig(tmp_path)
    show = load_show(gig)  # no cues.json yet: built fresh
    c = show.lists[0].cues[1]
    c.fade_out, c.crossfade, c.auto_follow, c.in_point = 3.0, 4.0, False, 1.5
    show.settings["crossfade"] = 8.0
    show.pads[0].path, show.pads[0].name = str(gig / "01_Doors" / "001 - Toto - Africa.wav"), "walk-on"
    assert save_show(show).name == "cues.json"
    again = load_show(gig)
    d = again.lists[0].cues[1]
    assert (d.fade_out, d.crossfade, d.auto_follow, d.in_point, d.gain_db) == (3.0, 4.0, False, 1.5, 1.0)
    assert again.settings["crossfade"] == 8.0 and again.settings["auto_trim"] is True and again.pads[0].name == "walk-on"
    # a file added to the slot later shows up; a removed one is dropped
    _wav(gig / "01_Doors" / "003 - Eagles - Take It Easy.wav", 0.3)
    (gig / "01_Doors" / "01_Doors.m3u8").write_text(
        "#EXTM3U\n002 - Boston - More Than A Feeling.wav\n003 - Eagles - Take It Easy.wav\n", encoding="utf-8")
    (gig / "01_Doors" / "001 - Toto - Africa.wav").unlink()
    third = load_show(gig)
    assert [c.title for c in third.lists[0].cues] == ["More Than A Feeling", "Take It Easy"]  # name parsed from the file
    assert third.lists[0].cues[1].artist == "Eagles" and third.lists[0].cues[0].crossfade == 4.0


def _fake_decoder(seconds_by_name: dict):
    def decoder(path: Path, rate: int) -> np.ndarray:
        secs = seconds_by_name.get(Path(path).name, 1.0)
        return _pcm(secs, rate)
    return decoder


def _fake_analyzer(ending: str = "cold"):
    def analyzer(pcm: np.ndarray, rate: int) -> Edges:
        return Edges(0, len(pcm), ending, 2.0 if ending == "fade" else 0.0, rate)
    return analyzer


def _drain(player: Player, engine: Engine, blocks: int, frames: int = 10) -> None:
    for _ in range(blocks):
        engine.render(frames)
        player.tick()


def test_player_go_follow_on_and_transport(tmp_path: Path):
    gig = _gig(tmp_path)
    show = build_show(gig)
    eng = Engine(rate=100, blocksize=10)
    p = Player(show, eng, decoder=_fake_decoder({"001 - Toto - Africa.wav": 2.0, "002 - Boston - More Than A Feeling.wav": 1.0}), analyzer=_fake_analyzer("fade"))
    show.settings["crossfade"] = 0.5
    assert p.standby_cue.title == "Africa" and p.state == "stopped"
    cue = p.go()
    assert cue.title == "Africa" and p.state == "playing" and p.standby == 1
    _drain(p, eng, 2)  # decoding of the next cue happens on the worker; wait for it
    for _ in range(50):
        if p.queued is not None:
            break
        p.tick()
        import time

        time.sleep(0.02)
    assert p.queued is not None and p.queued[0].title == "More Than A Feeling"
    start_clock = eng.scheduled()[0][0]
    # 2 s track, 0.5 s crossfade: the follow-on is scheduled 50 samples before the end (200 samples)
    assert start_clock == 150 and p.current[1].fade_out == 50 and p.queued[1].fade_in == 50
    st = p.status()
    assert st["current"].title == "Africa" and st["queued"].title == "More Than A Feeling" and st["next_ready"]
    _drain(p, eng, 16)  # past sample 160: the follow-on has started and become current
    assert p.current[0].title == "More Than A Feeling" and p.queued is None and p.standby == 2
    assert p.status()["next"] is None  # end of the list
    _drain(p, eng, 20)  # 1 s track ends at 250 -> stopped
    assert p.state == "stopped" and p.current is None
    # pause / resume, stop, panic, pads
    p.select(0)
    p.go()
    p.pause()
    assert p.state == "paused" and not eng.render(10).any()
    p.pause()
    assert p.state == "playing"
    p.stop()
    eng.render(10)  # the stop is a 50 ms fade; one block finishes it
    assert p.state == "stopped" and eng.active() == []
    show.pads[0].path, show.pads[0].name = "001 - Toto - Africa.wav", "sting"
    assert p.play_pad(0).name == "sting" and p.status()["pads"] == ["1"]
    assert p.play_pad(0) is not None  # pressing again fades it
    assert p.play_pad(3) is None  # empty pad
    p.panic()
    p.close()


def test_player_go_over_playing_crossfades_and_bad_files_are_skipped(tmp_path: Path):
    gig = _gig(tmp_path)
    show = build_show(gig)
    eng = Engine(rate=100, blocksize=10)

    def decoder(path: Path, rate: int):
        if "Boston" in path.name:
            raise RuntimeError("corrupt file")
        return _pcm(3.0, rate)

    p = Player(show, eng, decoder=decoder, analyzer=_fake_analyzer("cold"))
    p.go()
    first = p.current[1]
    _drain(p, eng, 3)
    assert p.go() is None and "corrupt" in p.error and p.standby == 2  # Boston skipped, standby moves on
    p.select_list(1)
    assert p.standby_cue.title == "Carry On"
    assert p.go().title == "Carry On" and first.manual_len == 200  # the old cue fades over go_crossfade (2 s)
    assert p.current[1].fade_in == 200
    p.close()


def test_player_cli_help():
    from housekit.cli import app

    r = CliRunner().invoke(app, ["player", "--help"])
    assert r.exit_code == 0 and "cue lists" in r.stdout.lower()
