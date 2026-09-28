from pathlib import Path

from housekit.config import DATA_DIR
from housekit.tags import content_flags, guess_from_path, load_content_rules, load_genre_rules, normalize_genre

ROOT = Path("C:/lib")


def g(rel: str) -> dict:
    return guess_from_path(ROOT / rel, ROOT)


def test_year_artist_album_folder():
    r = g("(1970) Derek and the Dominos - Layla and other Assorted Love Songs [FLAC,Tracks]/01 - I looked away.flac")
    assert r["artist"] == "Derek and the Dominos"
    assert r["album"] == "Layla and other Assorted Love Songs"
    assert r["year"] == 1970 and r["track_no"] == 1 and r["title"] == "I looked away"


def test_artist_album_layout_and_disc_folders():
    r = g("Aaron Lewis/The Road (Deluxe Version)/03. Endless Summer.mp3")
    assert (r["artist"], r["album"], r["title"], r["track_no"]) == ("Aaron Lewis", "The Road (Deluxe Version)", "Endless Summer", 3)
    r = g("Garth Brooks - The Ultimate Hits/CD1/Friends In Low Places.mp3")
    assert (r["artist"], r["album"], r["title"]) == ("Garth Brooks", "The Ultimate Hits", "Friends In Low Places")
    r = g("Eagles/Selected Works/Disc 2/05 - Hotel California.flac")
    assert (r["artist"], r["album"], r["title"]) == ("Eagles", "Selected Works", "Hotel California")


def test_single_folder_is_artist_and_compilations_keep_filename_artist():
    r = g("Toto/01 - Africa.wav")
    assert (r["artist"], r["title"]) == ("Toto", "Africa")
    r = g("Top Hits/Oasis - The Shock Of The Lightning.mp3")
    assert (r["artist"], r["title"]) == ("Oasis", "The Shock Of The Lightning")
    r = g("Various Artists/V.A. - Blues Guitar Masters (2023 Blues)/29. Little Feat - Rad Gumbo.mp3")
    assert (r["artist"], r["title"], r["track_no"]) == ("Little Feat", "Rad Gumbo", 29)
    # under a real artist folder, "NN - A - B" keeps A - B as the title (subtitle, not artist)
    r = g("Pink Floyd/The Wall/01 - Another Brick - Part 1.flac")
    assert (r["artist"], r["title"]) == ("Pink Floyd", "Another Brick - Part 1")


def test_genre_normalization_rules():
    rules = load_genre_rules(str(DATA_DIR / "genres.toml"))
    assert normalize_genre("Pop, Rock, Hard Rock", 1987, rules) == "Classic Rock"
    assert normalize_genre("Country-Rock", 2018, rules) == "Classic Rock"
    assert normalize_genre("Country", 2016, rules) == "Modern Country"
    assert normalize_genre("Country", 1978, rules) == "Country"
    assert normalize_genre("Rock", None, rules) == "Classic Rock"
    assert normalize_genre("Soul, Funk, R&B", 1972, rules) == "Funk"
    assert normalize_genre("Yacht Rock", 2024, rules) == "Yacht Rock"
    assert normalize_genre("", 1990, rules) == "Other"
    assert rules.similarity_of("Classic Rock", "Southern Rock") == rules.similarity_of("Southern Rock", "Classic Rock") == 0.75


def test_content_flags():
    rules = load_content_rules(str(DATA_DIR / "content_rules.toml"))

    def flags(title, artist="X", album="", genre="", duration=200.0, advisory=""):
        return content_flags(title, title, "", artist, album, genre, duration, advisory, rules)

    assert flags("Song", advisory="1")["explicit_auto"] is True
    assert flags("Song", advisory="2")["explicit_auto"] is False
    assert flags("Song")["explicit_auto"] is None  # unknown, never guessed clean
    assert flags("Song [Explicit]")["explicit_auto"] is True
    assert flags("Song (Radio Edit)")["explicit_auto"] is False
    assert flags("F**k You", artist="Cee Lo Green")["explicit_auto"] is True  # known list, normalized
    assert flags("Intro")["junk_auto"] and flags("Skit (Interlude)")["junk_auto"] and flags("Track 7")["junk_auto"]
    assert flags("Real Song", duration=45)["junk_auto"] and not flags("Real Song")["junk_auto"]
    assert flags("Song", genre="Comedy")["junk_auto"]
    assert flags("Jingle Bell Rock")["holiday_auto"] and flags("Song", album="A Very Special Christmas")["holiday_auto"]
    assert flags("Song (Live at Wembley)")["live_auto"] and flags("Song", album="MTV Unplugged")["live_auto"]
    assert not flags("Living On A Prayer")["live_auto"]  # "Living" must not trip the live pattern
    assert flags("Song (Instrumental)")["instrumental_auto"]
