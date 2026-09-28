"""Same song, differently written artist: grouping, playlist dedupe, catalog dupe groups, era copies."""
from pathlib import Path

from conftest import make_config, make_track

from housekit.catalog import Catalog
from housekit.charts import ChartRow
from housekit.identity import apply_song_identity, artist_parts, is_placeholder, song_groups
from housekit.models import TagResult
from housekit.playlists import PlaylistSpec, select_playlist
from housekit.textnorm import song_key


def test_placeholders_and_parts():
    assert all(is_placeholder(a) for a in ("Various Artists", "Various Interprets", "VA", "V.A.", "", "Unknown Artist", "Soundtrack", "Unknown"))
    assert not is_placeholder("Foreigner") and not is_placeholder("Various Artists Tribute Band")
    assert artist_parts("Aerosmith & Run D.M.C.") == ["aerosmith", "run d m c"]
    assert artist_parts("Doobie Brothers, The") == ["doobie brothers"]
    assert artist_parts("England Dan, John Ford Coley") == ["england dan", "john ford coley"]
    assert artist_parts("Tom Petty and the Heartbreakers") == ["tom petty", "heartbreakers"]
    assert artist_parts("Santana feat. Rob Thomas") == ["santana", "rob thomas"]


def test_song_groups_merges_artist_variants_only_within_a_title():
    rows = [
        (1, "The Doobie Brothers", "", "What A Fool Believes"),
        (2, "Doobie Brothers, The", "", "What a Fool Believes"),
        (3, "Jimmy Buffett", "", "Margaritaville"),
        (4, "Jimmy Buffet", "", "Margaritaville"),
        (5, "Foreigner", "", "Cold As Ice"),
        (6, "Various Interprets", "Various Artists", "Cold as Ice"),
        (7, "Aerosmith", "", "Walk This Way"),
        (8, "Aerosmith & Run D.M.C.", "", "Walk This Way [September 1986]"),
        (9, "England Dan & John Ford Coley", "", "I'd Really Love To See You Tonight"),
        (10, "England Dan, John Ford Coley", "", "I\u2019d Really Love to See You Tonight"),
        (11, "Wilson Phillips", "", "Hold On"),
        (12, "Alabama Shakes", "", "Hold On"),
        (13, "Various Artists", "", "Hold On"),
        (14, "Daryl Hall & John Oates", "", "She's Gone"),
        (15, "Hall & Oates", "", "She's Gone"),
        (16, "", "Foreigner", "Cold As Ice"),  # album artist names the act when the artist tag is empty
        (17, "Nobody", "", ""),
        (18, "Aerosmith", "", "Dream On"),
        (19, "Jimmy Buffett", "", "Come Monday"),  # tips the Margaritaville group's name to the usual spelling
    ]
    g = song_groups(rows)
    assert g[3] == "jimmy buffett|margaritaville"
    # with the counts tied, a spelling the chart data knows names the group
    tied = [(3, "Jimmy Buffett", "", "Margaritaville"), (4, "Jimmy Buffet", "", "Margaritaville")]
    assert song_groups(tied)[3] == "jimmy buffet|margaritaville"  # shortest wins a plain tie
    assert song_groups(tied, known_artists={"jimmy buffett"})[3] == "jimmy buffett|margaritaville"
    assert g[1] == g[2] and g[3] == g[4] and g[5] == g[6] == g[16] and g[7] == g[8] and g[9] == g[10] and g[14] == g[15]
    assert g[11] != g[12] and g[13] in (g[11], g[12])  # a placeholder never bridges two real acts
    assert g[7] == "aerosmith|walk this way" and g[5] == "foreigner|cold as ice" and g[1] == "doobie brothers|what a fool believes"
    assert g[18] == "aerosmith|dream on" and g[17] == "nobody|"
    assert len(set(g.values())) == 11


def test_playlist_shows_each_song_once_across_artist_spellings(tmp_path: Path):
    cfg = make_config(tmp_path)
    lib = [
        make_track("Doobie Brothers, The", "What a Fool Believes", year=1978),
        make_track("The Doobie Brothers", "What A Fool Believes", year=1978),
        make_track("Jimmy Buffet", "Margaritaville", year=1977),  # the one-off spelling, listed first and lossless
        make_track("Jimmy Buffett", "Margaritaville", year=1977, codec="MP3"),
        make_track("Jimmy Buffett", "Come Monday", year=1974),  # the library's usual spelling
        make_track("Foreigner", "Cold As Ice", year=1977),
        make_track("Various Interprets", "Cold as Ice", year=1977, tag_confidence=0.6),
        make_track("Aerosmith", "Walk This Way", year=1975),
        make_track("Aerosmith & Run D.M.C.", "Walk This Way [September 1986]", year=1986),
        make_track("Wilson Phillips", "Hold On", year=1990, duration=260),
        make_track("Alabama Shakes", "Hold On", year=2012, duration=243),  # two songs: different lengths, no chart record
        make_track("Coldplay", "Viva La Vida", year=2008, duration=242),
        make_track("Chris Martin", "Viva La Vida", year=2008, duration=241, codec="MP3"),  # the singer's name on the band's song
    ]
    res = select_playlist(lib, PlaylistSpec(name="x", count=50), cfg)
    picked = [(pt.track.artist, pt.track.title) for pt in res.tracks]
    assert sorted(t.lower() for _, t in picked) == ["cold as ice", "come monday", "hold on", "hold on", "margaritaville", "viva la vida", "walk this way", "what a fool believes"]
    assert ("Coldplay", "Viva La Vida") in picked  # one song under two names (same running time); the lossless copy plays
    assert ("Foreigner", "Cold As Ice") in picked  # the named act beats the placeholder copy
    assert ("The Doobie Brothers", "What A Fool Believes") in picked  # plain name over the "X, The" spelling
    assert ("Jimmy Buffett", "Margaritaville") in picked  # the library's usual spelling, even though the other copy is lossless
    # copies are counted per song, whatever the spelling; the group is named after the usual spelling
    groups = apply_song_identity(lib)
    assert groups[lib[0].id] == groups[lib[1].id] and lib[0].song_key == lib[1].song_key
    assert lib[2].song_key == lib[3].song_key == "jimmy buffett|margaritaville"


def test_wrong_artist_tag_is_the_same_song():
    """User rule (2026-09-20): the same title is the same song when the artist tag is simply wrong."""
    rows = [
        (1, "Coldplay", "", "Viva La Vida", 242.0),
        (2, "Chris Martin", "", "Viva La Vida", 241.0),  # the singer's name on the band's song
        (3, "Taylor Swift", "", "Shake It Off", 219.0),
        (4, "Mariah Carey", "", "Shake It Off", 233.0),  # a different hit with the same title
        (5, "Unknown Band", "", "Shake It Off", 220.0),  # a nameless copy: the running time says it is Taylor's
        (6, "Some Band", "", "Shake It Off", 300.0),  # matches nothing: stays its own song
        (7, "Wilson Phillips", "", "Hold On", 260.0),
        (8, "Alabama Shakes", "", "Hold On", 243.0),  # no chart record here and different lengths: two songs
        (9, "The Band Nobody Knows", "", "Hold On", 244.0),  # the same length as Alabama Shakes: one recording, mis-tagged
        (10, "Daryl Hall & John Oates", "", "Maneater", 270.0),
        (11, "Hall and Oates", "", "Maneater", 271.0),
    ]
    acts = {"viva la vida": {"Coldplay"}, "shake it off": {"Taylor Swift", "Mariah Carey"}, "maneater": {"Daryl Hall & John Oates"}}
    g = song_groups(rows, known_artists={"coldplay", "taylor swift", "mariah carey"}, chart_title_acts=acts)
    assert g[1] == g[2] == "coldplay|viva la vida"
    assert g[3] != g[4] and g[5] == g[3] and g[6] not in (g[3], g[4])
    assert g[7] != g[8] and g[9] == g[8]
    assert g[10] == g[11]
    # without the chart's word, Coldplay and Chris Martin still merge on the running time alone ...
    g2 = song_groups(rows[:2])
    assert g2[1] == g2[2]
    # ... and without durations nothing merges that the names don't
    g3 = song_groups([r[:4] for r in rows[:2]])
    assert g3[1] != g3[2]
    # a different length under another name could be a cover or another song: left apart
    g4 = song_groups([(1, "Coldplay", "", "Viva La Vida", 242.0), (2, "Chris Martin", "", "Viva La Vida", 330.0)], chart_title_acts=acts)
    assert g4[1] != g4[2]
    # the same length is not enough when the analysis says otherwise: "Angel" by Aerosmith (5:07)
    # and by DMX (5:05) run alike but at different tempos and energies
    angel = [(1, "Aerosmith", "", "Angel", 307.0, 66.0, 0.42), (2, "DMX", "", "Angel", 305.0, 92.0, 0.71)]
    a = song_groups(angel)
    assert a[1] != a[2]
    twin = [(1, "Aerosmith", "", "Angel", 307.0, 66.0, 0.42), (2, "Steven Tyler", "", "Angel", 305.0, 132.0, 0.45)]  # double-time tempo
    assert song_groups(twin)[1] == song_groups(twin)[2]
    # two acts the charts both credit with a title are two hits, however alike they run
    same_len = [(1, "Taylor Swift", "", "Shake It Off", 219.0), (2, "Mariah Carey", "", "Shake It Off", 220.0)]
    assert song_groups(same_len, chart_title_acts=acts)[1] != song_groups(same_len, chart_title_acts=acts)[2]
    assert song_groups(same_len)[1] == song_groups(same_len)[2]  # without the charts' word they would merge


def _tag(path, title, artist, album="", year=None, album_artist="", duration=200.0):
    return TagResult(path=path, root="C:/lib", size=1, mtime=1.0, title=title, artist=artist, album=album, album_artist=album_artist,
                     year=year, duration=duration, song_key=song_key(artist, title), tag_confidence=1.0)


def test_catalog_dupe_groups_and_era_copies_follow_identity(tmp_path: Path):
    cat = Catalog(tmp_path / "lib.db")
    cat.upsert_tags([
        _tag("C:/lib/a.flac", "Margaritaville", "Jimmy Buffett", album="Changes In Latitudes", year=1977),
        _tag("C:/lib/b.mp3", "Margaritaville", "Jimmy Buffet", album="Greatest Hits", year=2010),
        _tag("C:/lib/c.flac", "Cold As Ice", "Foreigner", album="Foreigner", year=1977),
        _tag("C:/lib/d.mp3", "Cold as Ice", "Various Interprets", album="Rock Ballads", year=2005, album_artist="Various Artists"),
        _tag("C:/lib/e.flac", "Hold On", "Wilson Phillips", album="Wilson Phillips", year=1990, duration=260.0),
        _tag("C:/lib/f.flac", "Hold On", "Alabama Shakes", album="Boys & Girls", year=2012, duration=243.0),
        _tag("C:/lib/g.flac", "Viva La Vida", "Coldplay", album="Viva La Vida", year=2008, duration=242.0),
        _tag("C:/lib/h.mp3", "Viva La Vida", "Chris Martin", album="Hottest Hits", year=2020, duration=243.0),  # wrong artist, same recording
    ])
    cat.import_chart_rows([
        ChartRow(artist="Jimmy Buffett", title="Margaritaville", peak=8, weeks=22, year=1977),
        ChartRow(artist="Coldplay", title="Viva La Vida", peak=1, weeks=51, year=2008),
    ], source="hot100")
    assert cat.chart_title_acts() == {"margaritaville": {"Jimmy Buffett"}, "viva la vida": {"Coldplay"}}
    assert cat.rebuild_dupe_groups() == 3
    by = {t.path.split("/")[-1]: t for t in cat.all_tracks()}
    assert by["a.flac"].dupe_group == by["b.mp3"].dupe_group == "jimmy buffett|margaritaville"  # the chart's spelling names the group
    assert by["c.flac"].dupe_group == by["d.mp3"].dupe_group == "foreigner|cold as ice"
    assert by["e.flac"].dupe_group is None and by["f.flac"].dupe_group is None
    assert by["g.flac"].dupe_group == by["h.mp3"].dupe_group == "coldplay|viva la vida"  # the chart's spelling names it
    cat.compute_era_years()
    by = {t.path.split("/")[-1]: t for t in cat.all_tracks()}
    assert (by["b.mp3"].year, by["b.mp3"].year_source) == (1977, "copy")  # the compilation copy is dated by its sibling
    assert (by["d.mp3"].year, by["d.mp3"].year_source) == (1977, "copy")
    assert by["h.mp3"].year == 2008  # and the mis-tagged copy by the song it really is
    cat.close()
