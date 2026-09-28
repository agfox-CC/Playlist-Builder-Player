"""The catalog follows the music when a portable bundle (or the library) moves."""
from pathlib import Path

from housekit.catalog import Catalog
from housekit.models import Features, TagResult
from housekit.textnorm import song_key


def _tag(path: Path, root: Path, title: str, artist: str = "Band") -> TagResult:
    return TagResult(path=str(path), root=str(root), size=3, mtime=1.0, title=title, artist=artist, duration=200.0,
                     tag_confidence=1.0, song_key=song_key(artist, title))


def _make_files(root: Path, names: list[str]) -> list[Path]:
    out = []
    for n in names:
        p = root / n
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(b"xyz")
        out.append(p)
    return out


def test_bundle_moved_to_new_folder_keeps_analysis_and_corrections(tmp_path: Path):
    old_lib = tmp_path / "old" / "MP3"
    files = _make_files(old_lib, ["Toto/Africa.mp3", "Toto/Rosanna.mp3"])
    cat = Catalog(tmp_path / "lib.db")
    cat.upsert_tags([_tag(files[0], old_lib, "Africa", "Toto"), _tag(files[1], old_lib, "Rosanna", "Toto")])
    ids = {t.title: t.id for t in cat.all_tracks()}
    cat.store_features([Features(track_id=ids["Africa"], analysis_version=4, size=3, mtime=1.0, energy=0.4, lufs=-12.0)])
    cat.set_flag(ids["Rosanna"], "explicit", False)
    cat.log_gig("g", "2026-01-01", "", "out", [(ids["Africa"], "01_Doors", 1, song_key("Toto", "Africa"), "Toto - Africa")])

    # the whole thing moves: old/MP3 -> new/MP3
    new_lib = tmp_path / "new" / "MP3"
    new_lib.parent.mkdir(parents=True)
    (tmp_path / "old" / "MP3").rename(new_lib)
    rep = cat.relocate_roots([new_lib])
    assert rep["relocated"] == [(str(old_lib), str(new_lib))] and rep["merged"] == 0 and rep["marked_missing"] == 0
    tracks = {t.title: t for t in cat.all_tracks()}
    assert tracks["Africa"].path == str(new_lib / "Toto" / "Africa.mp3") and tracks["Africa"].root == str(new_lib)
    assert tracks["Africa"].energy == 0.4  # analysis kept, no re-scan needed
    assert tracks["Rosanna"].explicit is False  # hand correction kept
    assert not tracks["Africa"].missing
    assert cat.recent_song_keys(1) == {song_key("Toto", "Africa")}
    # second call is a no-op
    assert cat.relocate_roots([new_lib]) == {"relocated": [], "merged": 0, "marked_missing": 0, "restored": 0}
    cat.close()


def test_roots_moved_inside_new_root_and_merged_with_rescanned_rows(tmp_path: Path):
    """FLAC and MP3 were moved into Music, the app was pointed at Music and reindexed before the
    catalog followed: old rows must merge into the new ones rather than sit as dead duplicates."""
    old_flac = tmp_path / "old" / "FLAC"
    old_mp3 = tmp_path / "old" / "MP3"
    f1 = _make_files(old_flac, ["Eagles/Hotel California.flac"])[0]
    m1 = _make_files(old_mp3, ["Toto/Africa.mp3"])[0]
    cat = Catalog(tmp_path / "lib.db")
    cat.upsert_tags([_tag(f1, old_flac, "Hotel California", "Eagles"), _tag(m1, old_mp3, "Africa", "Toto")])
    old_ids = {t.title: t.id for t in cat.all_tracks()}
    cat.set_flag(old_ids["Africa"], "junk", True)  # a correction made before the move
    cat.log_gig("g", "2026-01-01", "", "out", [(old_ids["Africa"], "01_Doors", 1, song_key("Toto", "Africa"), "x")])

    music = tmp_path / "new" / "Music"
    music.mkdir(parents=True)
    old_flac.rename(music / "FLAC")
    old_mp3.rename(music / "MP3")
    # a rescan under Music already created fresh rows (with analysis) for the same files
    cat.upsert_tags([_tag(music / "FLAC" / "Eagles" / "Hotel California.flac", music, "Hotel California", "Eagles"),
                     _tag(music / "MP3" / "Toto" / "Africa.mp3", music, "Africa", "Toto")])
    new_ids = {t.path: t.id for t in cat.all_tracks() if str(t.root) == str(music)}
    cat.store_features([Features(track_id=i, analysis_version=4, size=3, mtime=1.0, energy=0.5, lufs=-10.0) for i in new_ids.values()])
    assert len(cat.all_tracks()) == 4

    rep = cat.relocate_roots([music])
    assert rep["merged"] == 2 and len(rep["relocated"]) == 2
    tracks = cat.all_tracks()
    assert len(tracks) == 2 and all(t.root == str(music) and not t.missing for t in tracks)
    africa = next(t for t in tracks if t.title == "Africa")
    assert africa.junk is True  # correction carried over
    assert africa.energy == 0.5  # analysis of the surviving row
    assert cat.recent_song_keys(1) == {song_key("Toto", "Africa")}  # history follows the merge
    assert cat.rebuild_dupe_groups() == 0
    cat.close()


def test_unfindable_root_is_marked_missing_and_comes_back(tmp_path: Path):
    old = tmp_path / "usb" / "Music"
    p = _make_files(old, ["a.mp3"])[0]
    cat = Catalog(tmp_path / "lib.db")
    cat.upsert_tags([_tag(p, old, "A")])
    (tmp_path / "usb").rename(tmp_path / "unplugged")  # drive gone, nothing similar configured
    rep = cat.relocate_roots([tmp_path / "elsewhere"])
    assert rep["marked_missing"] == 1 and rep["relocated"] == []
    assert cat.all_tracks(include_missing=True)[0].missing and cat.all_tracks() == []
    # the drive comes back under the same path: a scan upserts the row and it is present again
    (tmp_path / "unplugged").rename(tmp_path / "usb")
    cat.upsert_tags([_tag(p, old, "A")])
    assert not cat.all_tracks()[0].missing
    cat.close()
