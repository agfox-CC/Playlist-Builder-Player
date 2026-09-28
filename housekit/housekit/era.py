"""Original-year inference. Tag years on remasters, reissues and compilations are the reissue year,
which throws off era scoring for classic rock in particular. This module works out a track's real
era from the evidence in the catalog, in order of strength:

  chart   the song's first chart year, or its release year from a radio/year-end source
          (a remaster tagged 2013 that charted in 1977 is 1977)
  album chart  the year the album itself first entered the Billboard 200, when that is two or
          more years before the tag year (an unmarked remaster of a non-single album track)
  tag     the tag year, when nothing marks the release as a reissue or compilation
  copy    the earliest credible year among other copies of the same song
  album   the median credible year of the other tracks on the same (non-compilation) album,
          so a whole remastered album inherits the year of the single that charted
  artist  the artist's median credible year, only when their catalog spans 15 years or less
  tag?    the tag year kept for lack of anything better

Pure functions; the catalog stores the result as year_orig / year_source. A hand-set year_ovr wins.
"""
from __future__ import annotations

import re
import statistics
from typing import Any, Optional

from .identity import is_placeholder
from .textnorm import norm_artist, norm_basic

COMPILATION_RE = re.compile(
    r"greatest hits|best of|the best|essential|anthology|collection|definitive|\bgold\b|number ones|#1|"
    r"\bhits\b|singles|ultimate|now that|classics|very best|20th century masters|playlist|top 40|"
    r"\bnow \d+|jock jams|party mix|wedding|super hits|legends|icon|\bmix\b|soundtrack|\bost\b|"
    r"audiophile|road trip|decades|\b\d0s\b|\b(19|20)\d0s\b|yacht rock|rock renaissance",
    re.IGNORECASE,
)
# Words that date a *release* (and are usually next to the reissue year). Format words such as
# "vinyl", "24 bit" or "MFSL" describe the rip, not a reissue, so they are deliberately not here:
# "Back in Black (1980) [24.96 FLAC] vinyl" tagged 1980 is credible.
REISSUE_RE = re.compile(
    r"remaster|re-?master|reissue|re-?issue|anniversary|deluxe|expanded|legacy edition|super deluxe|"
    r"bonus track|collector|redux|box set|\bmono\b|\bstereo\b|edition",
    re.IGNORECASE,
)
_YEAR_RE = re.compile(r"\b(19[4-9]\d|20[0-4]\d)\b")
# A live album is dated by the concert, not the songs: a 2018 recording of a 1976 song is a 1976 song here.
LIVE_RE = re.compile(r"\blive\b|unplugged|in concert|\btour\b|farewell|bootleg|\bbbc\b|live at|live from|live in", re.IGNORECASE)


def looks_reissue(version: str, album: str) -> bool:
    return bool(REISSUE_RE.search(version or "") or REISSUE_RE.search(album or "") or LIVE_RE.search(version or "") or LIVE_RE.search(album or ""))


def looks_compilation(album: str) -> bool:
    return bool(album and COMPILATION_RE.search(album))


def mentioned_years(*texts: str) -> set[int]:
    out: set[int] = set()
    for t in texts:
        for m in _YEAR_RE.finditer(t or ""):
            out.add(int(m.group(1)))
    return out


def reissue_years(*texts: str) -> set[int]:
    """Years printed next to a reissue word: '2013 Remaster', 'Remastered 2011', '40th Anniversary Edition 2016'."""
    out: set[int] = set()
    for t in texts:
        t = t or ""
        for m in REISSUE_RE.finditer(t):
            window = t[max(0, m.start() - 24): m.end() + 24]
            for y in _YEAR_RE.finditer(window):
                out.add(int(y.group(1)))
    return out


def tag_year_credible(year: Optional[int], version: str, album: str) -> bool:
    """Is the tag year the song's own year rather than a reissue's?"""
    if not year:
        return False
    if year in reissue_years(version, album):
        return False  # "(2013 Remaster)" with year 2013: the reissue year
    if year in mentioned_years(version, album):
        return True  # "Back in Black (1980) [24.96 FLAC] vinyl" with year 1980: printed original year
    return not looks_reissue(version, album) and not looks_compilation(album)


def infer_years(rows: list[dict[str, Any]]) -> dict[int, tuple[Optional[int], str]]:
    """rows: dicts with id, artist, album, version, year, chart_year, song_key.
    Returns id -> (year, source)."""
    base: dict[int, tuple[int, str]] = {}
    for r in rows:
        year, chart = r.get("year"), r.get("chart_year")
        if year is not None and year < 1940:
            year = None
        if chart is not None and chart < 1940:
            chart = None
        version, album = r.get("version") or "", r.get("album") or ""
        album_year = r.get("album_year")
        if album_year is not None and album_year < 1940:
            album_year = None
        if chart is not None and (year is None or chart <= year - 2):
            base[r["id"]] = (chart, "chart")
        elif chart is not None and year is not None and abs(chart - year) <= 1:
            base[r["id"]] = (year, "tag")
        elif album_year is not None and not looks_compilation(album) and (year is None or album_year <= year - 2):
            # The album itself charted two or more years before the tag year: an unmarked remaster.
            base[r["id"]] = (album_year, "album chart")
        elif tag_year_credible(year, version, album) and not is_placeholder(r.get("artist") or ""):
            # (a track credited to "Various Artists" sits on a compilation whatever the album is called)
            base[r["id"]] = (year, "tag")

    # copies of the same song
    by_song: dict[str, list[int]] = {}
    for r in rows:
        if r["id"] in base and r.get("song_key"):
            by_song.setdefault(r["song_key"], []).append(base[r["id"]][0])
    for r in rows:
        if r["id"] not in base and r.get("song_key") in by_song:
            base[r["id"]] = (min(by_song[r["song_key"]]), "copy")

    # same album, same artist (not compilations)
    by_album: dict[tuple[str, str], list[int]] = {}
    for r in rows:
        alb = r.get("album") or ""
        if r["id"] in base and alb and not looks_compilation(alb):
            by_album.setdefault((norm_artist(r.get("artist") or ""), norm_basic(alb)), []).append(base[r["id"]][0])
    for r in rows:
        alb = r.get("album") or ""
        if r["id"] not in base and alb and not looks_compilation(alb):
            key = (norm_artist(r.get("artist") or ""), norm_basic(alb))
            if key in by_album:
                base[r["id"]] = (int(statistics.median(by_album[key])), "album")

    # the artist's era, when it is narrow enough to mean something
    by_artist: dict[str, list[int]] = {}
    for r in rows:
        if r["id"] in base:
            by_artist.setdefault(norm_artist(r.get("artist") or ""), []).append(base[r["id"]][0])
    for r in rows:
        if r["id"] not in base:
            ys = by_artist.get(norm_artist(r.get("artist") or ""))
            if ys and len(ys) >= 3:
                ys_sorted = sorted(ys)
                lo, hi = ys_sorted[len(ys_sorted) // 10], ys_sorted[-1 - len(ys_sorted) // 10]
                if hi - lo <= 15:
                    base[r["id"]] = (int(statistics.median(ys)), "artist")

    out: dict[int, tuple[Optional[int], str]] = {}
    for r in rows:
        if r["id"] in base:
            out[r["id"]] = base[r["id"]]
        else:
            y = r.get("year")
            out[r["id"]] = (y, "tag?") if y else (None, "")
    return out
