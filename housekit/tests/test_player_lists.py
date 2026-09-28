"""Editing cue lists in the player: adding files, reordering, removing, lists made in the player,
looping cues, auditioning a transition."""
import json
import math
import wave
from pathlib import Path

import numpy as np

from housekit.player.cues import Cue, CueList, build_show, cues_from_paths, load_show, quick_tags, save_show
from housekit.player.decode import Edges
from housekit.player.engine import Engine, Voice
from housekit.player.show import Player


def _wav(path: Path, seconds: float = 0.5) -> Path:
    sr = 8000
    t = np.arange(int(seconds * sr)) / sr
    x = (0.3 * np.sin(2 * math.pi * 440 * t) * 32767).astype("<i2")
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes(x.tobytes())
    return path


def _pcm(seconds: float, rate: int, level: float = 0.5) -> np.ndarray:
    return np.full((int(seconds * rate), 2), int(level * 32767), dtype=np.int16)


def test_loop_voice_wraps_seamlessly():
    eng = Engine(rate=100, blocksize=10)
    pcm = _pcm(1.0, 100)
    pcm[:10] = 0  # silence before the loop's in point, never heard
    v = Voice(pcm, 100, in_pt=10, out_pt=33, loop=True)
    eng.play(v)
    out = np.concatenate([eng.render(10) for _ in range(20)])[:, 0]
    assert np.allclose(out, 0.5, atol=1e-3) and not v.done and 10 <= v.pos < 33  # 200 samples from a 23-sample loop, no gap
    eng.fade_out(v, 0.1)
    for _ in range(3):
        eng.render(10)
    assert v.done
    plain = Voice(_pcm(0.3, 100), 100, in_pt=0, out_pt=25)
    eng.play(plain)
    eng.render(10)
    eng.render(10)
    blk = eng.render(10)
    assert plain.done and np.allclose(blk[5:, 0], 0.0) and np.allclose(blk[:5, 0], 0.5, atol=1e-3)


def test_classify_drop_with_nothing_open(tmp_path: Path):
    from housekit.player.cues import classify_drop

    a = _wav(tmp_path / "a.wav")
    b = _wav(tmp_path / "b.wav")
    folder = tmp_path / "sub"
    folder.mkdir()
    m = tmp_path / "list.m3u8"
    m.write_text("#EXTM3U\n", encoding="utf-8")
    (tmp_path / "notes.txt").write_text("x")
    assert classify_drop([a, b, tmp_path / "notes.txt"]) == (None, [a, b])  # loose files: a Quick List
    assert classify_drop([a, folder, b]) == (folder, [a, b])  # a folder opens as the show, the rest is added
    assert classify_drop([m]) == (m, [])
    assert classify_drop([tmp_path / "notes.txt"]) == (None, [])
    assert classify_drop([]) == (None, [])


def test_quick_tags_and_cues_from_paths(tmp_path: Path):
    a = _wav(tmp_path / "loose" / "003 - Toto - Africa.wav")
    b = _wav(tmp_path / "loose" / "b.wav")
    assert quick_tags(a)[:2] == ("Toto", "Africa") and quick_tags(b)[:2] == ("", "b")
    assert 0.4 < quick_tags(a)[2] < 0.6
    m = tmp_path / "loose" / "list.m3u8"
    m.write_text("#EXTM3U\n#EXTINF:1,Boston - More Than A Feeling\nb.wav\nmissing.wav\n", encoding="utf-8")
    show = build_show(tmp_path / "loose")
    cues = cues_from_paths(show, [a, tmp_path / "loose", m, tmp_path / "loose" / "nothing.txt"])
    titles = [(c.artist, c.title) for c in cues]
    assert titles == [("Toto", "Africa"), ("Toto", "Africa"), ("", "b"), ("Boston", "More Than A Feeling")]
    assert all(not Path(c.path).is_absolute() for c in cues)  # inside the show folder: relative


def test_saved_lists_keep_order_names_and_player_made_lists(tmp_path: Path):
    gig = tmp_path / "gig"
    _wav(gig / "01_Doors" / "001 - A - One.wav")
    _wav(gig / "01_Doors" / "002 - B - Two.wav")
    (gig / "01_Doors" / "01_Doors.m3u8").write_text("#EXTM3U\n001 - A - One.wav\n002 - B - Two.wav\n", encoding="utf-8")
    _wav(gig / "02_Break1" / "001 - C - Three.wav")
    (gig / "02_Break1" / "02_Break1.m3u8").write_text("#EXTM3U\n001 - C - Three.wav\n", encoding="utf-8")
    show = load_show(gig)
    assert [cl.source for cl in show.lists] == [str(Path("01_Doors") / "01_Doors.m3u8"), str(Path("02_Break1") / "02_Break1.m3u8")]
    # rename a folder list, reverse its cues, add a list made in the player, reorder lists
    show.lists[0].name = "Doors (edited)"
    show.lists[0].cues.reverse()
    extra = _wav(tmp_path / "elsewhere" / "sting.wav")
    show.lists.append(CueList(name="Stings", cues=[Cue(path=str(extra), title="sting"), Cue(path="gone.wav", title="gone")]))
    show.lists.reverse()
    save_show(show)
    again = load_show(gig)
    assert [cl.name for cl in again.lists] == ["Stings", "02_Break1", "Doors (edited)"]
    assert [c.title for c in again.lists[0].cues] == ["sting"]  # the vanished file dropped
    assert [c.title for c in again.lists[2].cues] == ["Two", "One"] and again.lists[2].source.endswith("01_Doors.m3u8")
    # a playlist file that appears later is added at the end; a new file in a folder joins its list
    _wav(gig / "03_Break2" / "001 - D - Four.wav")
    (gig / "03_Break2" / "03_Break2.m3u8").write_text("#EXTM3U\n001 - D - Four.wav\n", encoding="utf-8")
    _wav(gig / "01_Doors" / "003 - E - Five.wav")
    (gig / "01_Doors" / "01_Doors.m3u8").write_text("#EXTM3U\n001 - A - One.wav\n002 - B - Two.wav\n003 - E - Five.wav\n", encoding="utf-8")
    third = load_show(gig)
    assert [cl.name for cl in third.lists] == ["Stings", "02_Break1", "Doors (edited)", "03_Break2"]
    assert [c.title for c in third.lists[2].cues] == ["Two", "One", "Five"]
    # an old cues.json without sources still matches by name
    data = json.loads((gig / "cues.json").read_text(encoding="utf-8"))
    for cl in data["lists"]:
        cl.pop("source", None)
    data["lists"][2]["name"] = "01_Doors"
    (gig / "cues.json").write_text(json.dumps(data), encoding="utf-8")
    old = load_show(gig)
    assert [c.title for c in next(cl for cl in old.lists if cl.name == "01_Doors").cues] == ["Two", "One", "Five"]


def _player(tmp_path: Path, seconds: float = 20.0, ending: str = "cold") -> tuple[Player, Engine]:
    d = tmp_path / "loose"
    for n in ("a.wav", "b.wav", "c.wav", "d.wav"):
        _wav(d / n)
    show = build_show(d)
    eng = Engine(rate=100, blocksize=10)
    p = Player(show, eng, decoder=lambda path, rate: _pcm(seconds, rate),
               analyzer=lambda pcm, rate: Edges(0, len(pcm), ending, 0.0, rate))
    return p, eng


def test_player_edits_keep_the_standby_cue(tmp_path: Path):
    p, eng = _player(tmp_path)
    names = lambda: [c.title for c in p.cue_list.cues]  # noqa: E731
    assert names() == ["a", "b", "c", "d"]
    p.select(2)  # standby on c
    p.move_cue(0, 3)  # a to the end
    assert names() == ["b", "c", "d", "a"] and p.standby_cue.title == "c"
    p.remove_cue(0)
    assert names() == ["c", "d", "a"] and p.standby == 0 and p.standby_cue.title == "c"
    at = p.add_cues([Cue(path="x.wav", title="x"), Cue(path="y.wav", title="y")], at=1)
    assert at == 1 and names() == ["c", "x", "y", "d", "a"]
    p.add_cues([Cue(path="z.wav", title="z")])
    assert names()[-1] == "z"
    assert p.remove_cue(99) is None
    # lists
    i = p.new_list("Stings", [Cue(path="s.wav", title="s")])
    assert i == 1 and p.show.lists[1].source == "" and [c.title for c in p.show.lists[1].cues] == ["s"]
    j = p.duplicate_list(0)
    assert p.show.lists[j].name == "loose copy" and [c.title for c in p.show.lists[j].cues] == names()
    p.show.lists[j].cues[0].title = "changed"
    assert p.show.lists[0].cues[0].title == "c"  # a real copy
    p.rename_list(j, "  Encore  ")
    assert p.show.lists[j].name == "Encore"
    p.select_list(j)
    p.delete_list(j)
    assert p.list_index == 1 and [cl.name for cl in p.show.lists] == ["loose", "Stings"]
    p.select_list(0)
    p.delete_list(0)
    assert p.list_index == 0 and p.cue_list.name == "Stings"
    p.close()


def test_looping_cue_never_follows_on_and_go_moves_past_it(tmp_path: Path):
    p, eng = _player(tmp_path, seconds=1.0)
    p.cue_list.cues[0].loop = True
    p.go()
    for _ in range(40):  # four times the cue's length
        eng.render(10)
        p.tick()
    assert p.state == "playing" and p.current[0].title == "a" and p.queued is None and p.status()["looping"]
    p.go()  # the next cue, over a crossfade
    assert p.current[0].title == "b" and not p.status()["looping"]
    p.close()


def test_seek_crossfades_to_the_new_spot_and_retimes_the_follow_on(tmp_path: Path):
    p, eng = _player(tmp_path, seconds=20.0)
    p.go()
    old = p.current[1]
    for _ in range(10):
        eng.render(10)
        p.tick()
    for _ in range(200):  # let the follow-on get scheduled
        p.tick()
        if p.queued is not None:
            break
        import time

        time.sleep(0.01)
    assert p.queued is not None
    first_start = eng.scheduled()[0][0]
    assert p.seek(1500, xfade=0.2)  # 15 s in, out of 20
    new = p.current[1]
    assert new is not old and new.pos == 1500 and new.ramp_from == 1500 and new.ramp_len == 20 and old.manual_len == 20
    assert p.queued is None  # the old follow-on timing is dropped
    out = np.concatenate([eng.render(10) for _ in range(3)])[:, 0]
    # a short equal-power crossfade: never below the level; the constant test signal is fully
    # correlated at both positions, so the sum peaks at sqrt(2) x 0.5 mid-fade (real music does not)
    assert old.done and abs(new.elapsed - 15.3) < 0.01 and out.min() >= 0.49 and out.max() <= 0.72
    for _ in range(200):
        p.tick()
        if p.queued is not None:
            break
        import time

        time.sleep(0.01)
    assert p.queued is not None and eng.scheduled()[0][0] < first_start  # re-timed from the new position (much sooner)
    assert p.seek_seconds(-5.0) and abs(p.current[1].elapsed - 10.3) < 0.01
    assert p.seek(-100) and p.current[1].pos == p.current[1].in_pt  # clamped to the start
    p.stop()
    assert not p.seek(100) and not p.seek_seconds(1)
    p.close()


def test_audition_plays_the_join_then_returns_to_the_cue(tmp_path: Path):
    p, eng = _player(tmp_path, seconds=20.0)
    assert p.audition(1, tail=5.0, into=2.0)  # cue b's last 5 s into c
    v = p.current[1]
    assert p.current[0].title == "b" and v.pos == v.out_pt - 500 and p.status()["audition"]
    for _ in range(200):  # wait for the worker to decode c and the join to be scheduled
        p.tick()
        if p.queued is not None:
            break
        import time

        time.sleep(0.01)
    assert p.queued is not None and p.queued[0].title == "c"
    ran = 0
    while p.state == "playing" and ran < 2000:
        eng.render(10)
        p.tick()
        ran += 1
    assert p.state == "stopped" and p.standby_cue.title == "b"  # back on the cue we were checking
    assert 500 + 200 - 30 <= ran * 10 <= 500 + 200 + 60  # 5 s tail, then 2 s into c, then a half-second fade
    assert not p.audition(3) and not p.audition(99)  # nothing after the last cue
    p.close()
