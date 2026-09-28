from pathlib import Path

from conftest import make_track

from housekit.setlist import excluded_titles, load_overrides, match_setlist, parse_entries, save_override

SETLIST_TEXT = """\
Zoobilee 2026 - Stage Sheet
SET 1
1. Don't Stop Believin' - Journey
2. Free Fallin' (Remastered) - Tom Petty
3) Uptown Funk feat. Bruno Mars - Mark Ronson

Set Two
Mr Brightside - The Killers
Wagon Wheel - Darius Rucker
Some Original Song We Wrote - Us

SET 3
Sweet Caroline - Neil Diamond (G)
"""


def library():
    return [
        make_track("Journey", "Don't Stop Believin'", year=1981),
        make_track("Journey", "Don't Stop Believin'", year=2006, version="Live", live=True),
        make_track("Journey", "Separate Ways", year=1983),
        make_track("Tom Petty", "Free Fallin'", year=1989),
        make_track("Tom Petty and the Heartbreakers", "American Girl", year=1976),
        make_track("Mark Ronson", "Uptown Funk (feat. Bruno Mars)", year=2014),
        make_track("The Killers", "Mr. Brightside", year=2004),
        make_track("Old Crow Medicine Show", "Wagon Wheel", year=2004),
        make_track("Neil Diamond", "Sweet Caroline", year=1969),
        make_track("Glee Cast", "Don't Stop Believing", year=2009),
        make_track("Random Band", "Unrelated Song", year=1999),
    ]


def test_parse_entries_headers_numbering_and_notes():
    entries = parse_entries(SETLIST_TEXT.splitlines())
    raws = [e.part_a for e in entries]
    assert "Don't Stop Believin'" in raws
    assert "Sweet Caroline" in raws  # "(G)" key note stripped
    assert all("SET" not in e.part_a.upper() or "SOME" in e.part_a.upper() for e in entries)
    sets = {e.set_name for e in entries}
    assert any("SET 1" in s for s in sets) and any("Set Two" in s for s in sets)
    by_a = {e.part_a: e for e in entries}
    assert by_a["Don't Stop Believin'"].part_b == "Journey"
    assert by_a["Some Original Song We Wrote"].set_name == "Set Two"
    # the sheet's title line becomes an entry too: it will surface as unresolved rather than vanish
    assert "Zoobilee 2026" in by_a


def test_orientation_detected_and_matches_with_confidence():
    entries = parse_entries(SETLIST_TEXT.splitlines())
    parse = match_setlist(entries, library(), threshold=80)
    assert parse.orientation == "title-artist"
    by_raw = {m.entry.part_a: m for m in parse.entries}
    assert by_raw["Don't Stop Believin'"].status == "matched"
    assert by_raw["Don't Stop Believin'"].track_display == "Journey - Don't Stop Believin'"
    assert by_raw["Free Fallin' (Remastered)"].status == "matched"
    assert by_raw["Uptown Funk feat. Bruno Mars"].status == "matched"
    assert by_raw["Mr Brightside"].status == "matched"
    assert by_raw["Sweet Caroline"].status == "matched"
    # Wagon Wheel: title matches, artist differs (Rucker vs OCMS) - still expected to resolve
    assert by_raw["Wagon Wheel"].status == "matched"
    # the original (and the sheet's title line) are unresolved and *surfaced*, never dropped
    unres = sorted(m.entry.part_a for m in parse.unresolved)
    assert unres == ["Some Original Song We Wrote", "Zoobilee 2026"]


def test_swapped_orientation_still_works():
    lines = ["Journey - Don't Stop Believin'", "Tom Petty - Free Fallin'", "The Killers - Mr. Brightside"]
    parse = match_setlist(parse_entries(lines), library(), threshold=80)
    assert parse.orientation == "artist-title"
    assert all(m.status == "matched" for m in parse.entries)
    assert parse.entries[0].title == "Don't Stop Believin'"


def test_excluded_titles_cover_unresolved_and_all_versions():
    entries = parse_entries(SETLIST_TEXT.splitlines())
    parse = match_setlist(entries, library(), threshold=80)
    titles = excluded_titles(parse)
    assert "dont stop believing" in titles
    assert "some original song we wrote" in titles  # unresolved lines still excluded by text


def test_overrides_roundtrip(tmp_path: Path):
    p = tmp_path / "setlist_overrides.toml"
    save_override(p, "Some Original Song We Wrote - Us", not_in_library=True)
    save_override(p, "Wagon Wheel - Darius Rucker", track_id=42, note="pinned")
    ov = load_overrides(p)
    assert ov["some original song we wrote us"]["not_in_library"] is True
    assert ov["wagon wheel darius rucker"]["track_id"] == 42
    # overwriting the same entry replaces rather than duplicates
    save_override(p, "Wagon Wheel - Darius Rucker", track_id=43)
    ov = load_overrides(p)
    assert ov["wagon wheel darius rucker"]["track_id"] == 43
    assert len(ov) == 2


def test_clean_line_strips_markers_and_keys():
    from housekit.setlist import clean_line

    assert clean_line("*SHINE") == "SHINE"
    assert clean_line("D  - THE MIDDLE") == "THE MIDDLE"
    assert clean_line("Am: Zombie") == "Zombie"
    assert clean_line("* 3. Song Name*") == "Song Name"
    assert clean_line("Sweet Caroline (G) 120bpm") == "Sweet Caroline"


def shorthand_library():
    return [
        make_track("Guns N' Roses", "Sweet Child O' Mine"),
        make_track("Wild Cherry", "Play That Funky Music"),
        make_track("Mudvayne", "Dig"),
        make_track("Aerosmith", "No More No More"),
        make_track("Cyndi Lauper", "Girls Just Wanna Have Fun"),
        make_track("Sam Smith", "I'm Not the Only One"),
        make_track("Joan Jett", "I Hate Myself for Loving You"),
        make_track("The White Stripes", "Seven Nation Army"),
        make_track("Beastie Boys", "(You Gotta) Fight for Your Right (To Party)"),
        make_track("Collective Soul", "Shine"),
        make_track("Pink Floyd", "Shine On You Crazy Diamond"),
        make_track("Tears for Fears", "Everybody Wants to Rule the World"),
        make_track("Foghat", "Slow Ride"),
    ]


def test_stage_sheet_shorthand_matches_and_refuses():
    lines = [
        "SWEET CHILD", "FUNKY MUSIC", "NO DIGGITY", "JUST A GIRL", "IM THE ONLY ONE", "HATE MYSELF 4 LOVIN U",
        "7 NATION ARMY", "*FIGHT 4 YOUR RIGHT", "*SHINE", "EVERYBODY RULES WORLD", "*HANDS 2 YERSF-SLOW RIDE",
    ]
    parse = match_setlist(parse_entries(lines), shorthand_library(), threshold=80)
    assert parse.orientation == "title-only"
    got = {m.entry.part_a: (m.status, m.track_display) for m in parse.entries}
    assert got["SWEET CHILD"] == ("matched", "Guns N' Roses - Sweet Child O' Mine")
    assert got["FUNKY MUSIC"] == ("matched", "Wild Cherry - Play That Funky Music")
    assert got["HATE MYSELF 4 LOVIN U"][0] == "matched"
    assert got["7 NATION ARMY"] == ("matched", "The White Stripes - Seven Nation Army")
    assert got["FIGHT 4 YOUR RIGHT"][0] == "matched"
    assert got["SHINE"] == ("matched", "Collective Soul - Shine")  # not the 26-char Pink Floyd title
    assert got["EVERYBODY RULES WORLD"][0] == "matched"
    # a library title *shorter* than the sheet line never gets substring credit
    assert got["NO DIGGITY"][0] == "unresolved"
    assert got["JUST A GIRL"][0] == "unresolved"  # words out of order in "Girls Just Wanna Have Fun"
    assert got["IM THE ONLY ONE"][0] == "unresolved"  # negation differs
    assert got["HANDS 2 YERSF-SLOW RIDE"][0] == "unresolved"
    # ...but the medley's second half is still excluded by text
    assert "slow ride" in excluded_titles(parse)


def test_medley_line_pins_several_tracks(tmp_path: Path):
    lib = shorthand_library()
    slow = next(t for t in lib if t.title == "Slow Ride")
    shine = next(t for t in lib if t.title == "Shine")
    p = tmp_path / "ov.toml"
    save_override(p, "*HANDS 2 YERSF-SLOW RIDE", track_ids=[slow.id, shine.id])
    parse = match_setlist(parse_entries(["*HANDS 2 YERSF-SLOW RIDE"]), lib, threshold=80, overrides=load_overrides(p))
    assert [(m.status, m.track_id) for m in parse.entries] == [("override", slow.id), ("override", shine.id)]


def _write_docx(path: Path, paragraphs: list[str], table_rows: list[list[str]] | None = None) -> None:
    import zipfile

    W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
    body = "".join(f"<w:p><w:r><w:t>{p}</w:t></w:r></w:p>" for p in paragraphs)
    if table_rows:
        rows = "".join(
            "<w:tr>" + "".join(f"<w:tc><w:p><w:r><w:t>{c}</w:t></w:r></w:p></w:tc>" for c in row) + "</w:tr>"
            for row in table_rows
        )
        body += f"<w:tbl>{rows}</w:tbl>"
    doc = f'<?xml version="1.0" encoding="UTF-8"?><w:document xmlns:w="{W}"><w:body>{body}</w:body></w:document>'
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("[Content_Types].xml", '<?xml version="1.0"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"/>')
        z.writestr("word/document.xml", doc)


def test_docx_and_folder_of_sets(tmp_path: Path):
    from housekit.setlist import read_lines

    d = tmp_path / "sets"
    d.mkdir()
    _write_docx(d / "SET 1.docx", ["WE'RE AN 'MERICAN BAND", "*SHINE"])
    _write_docx(d / "SET 2.docx", [], table_rows=[["1", "Sweet Child", "GNR", "D"], ["2", "Funky Music", "Wild Cherry", "120 bpm"]])
    (d / "SET 10.txt").write_text("Zombie\n", encoding="utf-8")
    lines = read_lines(d)
    assert lines == ["SET 1", "WE'RE AN 'MERICAN BAND", "*SHINE", "SET 2", "Sweet Child - GNR", "Funky Music - Wild Cherry", "SET 10", "Zombie"]
    entries = parse_entries(lines)
    assert [e.set_name for e in entries] == ["SET 1", "SET 1", "SET 2", "SET 2", "SET 10"]
    assert entries[2].part_a == "Sweet Child" and entries[2].part_b == "GNR"


def test_override_applied_in_match():
    lib = library()
    wagon = next(t for t in lib if t.title == "Wagon Wheel")
    entries = parse_entries(["Wagon Wheel - Darius Rucker", "Some Original Song We Wrote - Us"])
    overrides = {
        "wagon wheel darius rucker": {"entry": "Wagon Wheel - Darius Rucker", "track_id": wagon.id},
        "some original song we wrote us": {"entry": "x", "not_in_library": True},
    }
    parse = match_setlist(entries, lib, threshold=80, overrides=overrides)
    assert parse.entries[0].status == "override" and parse.entries[0].track_id == wagon.id
    assert parse.entries[1].status == "not_in_library"
    assert not parse.unresolved
