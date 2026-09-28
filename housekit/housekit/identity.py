"""Which tracks are the same song, across the ways an artist gets written.

`song_key` (normalized artist | normalized title) catches most copies, but a library tags the
same act many ways: "Doobie Brothers, The", "Jimmy Buffet" for Buffett, "England Dan, John Ford
Coley" for "England Dan & John Ford Coley", "Aerosmith & Run D.M.C." for a Walk This Way, or a
placeholder like "Various Interprets" on a compilation rip. Within one normalized title, tracks
whose artists are the same act are grouped; every member gets the group's key. Playlists, gig
builds, the catalog's dupe groups and year inference all use these keys, so a song appears once.
"""
from __future__ import annotations

import re
from collections import Counter
from typing import Iterable, Optional

from .textnorm import TRAILING_ARTICLE_RE, norm_artist, norm_basic, norm_title, strip_article

_PLACEHOLDER_RE = re.compile(
    r"^(various(\s+(artists?|interprets?|interpretes|performers|composers))?|va|v\s?a|unknown(\s+artist)?|artist|"
    r"soundtrack|original\s+soundtrack|ost|compilation|diverse|misc|miscellaneous|multiple\s+artists|no\s+artist|"
    r"none|n\s?a|not\s+available|track\s+artist)$"
)
_SPLIT_RE = re.compile(r"\s*(?:,|&|\+|/|;|\bfeat\.?|\bfeaturing\b|\bft\.?|\bwith\b|\bvs\.?|\band\b|\bx\b)\s*", re.IGNORECASE)


def is_placeholder(artist: str) -> bool:
    """A tag that names no act: empty, "Various Artists", "Unknown", "Soundtrack" ..."""
    s = norm_basic(artist or "")
    return not s or bool(_PLACEHOLDER_RE.match(s))


def artist_parts(artist: str) -> list[str]:
    """The acts credited, in order: "Aerosmith & Run D.M.C." -> ["aerosmith", "run d m c"];
    "Tom Petty and the Heartbreakers" -> ["tom petty", "heartbreakers"]."""
    raw = (artist or "").strip()
    m = TRAILING_ARTICLE_RE.match(raw)
    if m:
        raw = f"{m.group(2)} {m.group(1)}"
    out = []
    for p in _SPLIT_RE.split(raw):
        p = strip_article(norm_basic(p))
        if p:
            out.append(p)
    return out


def _same_act(a: str, b: str, pa: list[str], pb: list[str]) -> bool:
    """Two artist strings on the same title that mean the same act (or share one)."""
    from rapidfuzz import fuzz

    if pa and pb and pa[0] == pb[0]:
        return True  # same lead artist: "Aerosmith" / "Aerosmith & Run D.M.C."
    if set(pa) & set(pb):
        return True  # an act credited on both
    if fuzz.ratio(a, b) >= 88:
        return True  # a misspelling: "jimmy buffet" / "jimmy buffett"
    if min(len(a), len(b)) >= 5 and fuzz.token_set_ratio(a, b) == 100:
        return True  # one name inside the other: "hall oates" / "daryl hall john oates"
    return False


_DUR_TOL = 4.0  # seconds: masters and rips of one recording differ by trailing silence, not more
_TEMPO_TOL = 0.03  # 3 %, at the same, half or double tempo (beat trackers halve and double)
_ENERGY_TOL = 0.08


def _median(vals: list[float]) -> Optional[float]:
    s = sorted(vals)
    return s[len(s) // 2] if s else None


def _profile(feats: list[tuple]) -> tuple:
    """Median (duration, tempo, energy) of a cluster's copies, each over the copies that have it."""
    return tuple(_median([float(f[i]) for f in feats if f[i] is not None]) for i in range(3))


def same_recording(a: tuple, b: tuple, dur_tol: float = _DUR_TOL) -> bool:
    """Two (duration, tempo, energy) profiles that can only be the same recording: the same length
    within `dur_tol` seconds and, where the analysis knows them, the same tempo (allowing the beat
    tracker's octave errors) and about the same energy."""
    da, ta, ea = a
    db, tb, eb = b
    if da is None or db is None or abs(da - db) > dur_tol:
        return False
    if ta and tb:
        r = ta / tb
        if not any(abs(r - m) <= _TEMPO_TOL * m for m in (0.5, 1.0, 2.0)):
            return False
    if ea is not None and eb is not None and abs(ea - eb) > _ENERGY_TOL:
        return False
    return True


def _merge_wrong_tags(keys: list[str], parts: dict[str, list[str]], find, union, feats: dict[str, list[tuple]],
                      chart_acts: Optional[set[str]], dur_tol: float) -> None:
    """Within one title, join clusters of *different* acts that hold the same recording: a copy whose
    artist tag is simply wrong ("Chris Martin" on Coldplay's song), or filed under a collaborator or
    a compilation act. The charts guard the other way: two acts they both credit with the title are
    two hits, however alike they run. `find`/`union` work the title's union-find."""
    clusters: dict[str, list[str]] = {}
    for k in keys:
        clusters.setdefault(find(k), []).append(k)
    if len(clusters) < 2:
        return
    roots = list(clusters)
    prof = {r: _profile([f for k in ks for f in feats.get(k, [])]) for r, ks in clusters.items()}
    backing: dict[str, str] = {}  # library cluster -> the charting act it is
    if chart_acts:
        acts = sorted(chart_acts)
        an = {c: norm_artist(c) for c in acts}
        ap = {c: artist_parts(c) for c in acts}
        cparent = {c: c for c in acts}

        def cfind(x: str) -> str:
            while cparent[x] != x:
                cparent[x] = cparent[cparent[x]]
                x = cparent[x]
            return x

        for i, c in enumerate(acts):  # the same charting act written two ways is one act
            for d in acts[i + 1:]:
                if cfind(c) != cfind(d) and _same_act(an[c], an[d], ap[c], ap[d]):
                    cparent[cfind(d)] = cfind(c)
        for r in roots:
            for k in clusters[r]:
                hit = next((cfind(c) for c in acts if _same_act(k, an[c], parts[k], ap[c])), None)
                if hit is not None:
                    backing[r] = hit
                    break
        by_act: dict[str, list[str]] = {}
        for r, c in backing.items():
            by_act.setdefault(c, []).append(r)
        for rs in by_act.values():  # two library spellings of the one act that charted: one song
            for r in rs[1:]:
                union(rs[0], r)
    for i, r in enumerate(roots):
        for s in roots[i + 1:]:
            if find(r) == find(s):
                continue
            br, bs = backing.get(r), backing.get(s)
            if br is not None and bs is not None and br != bs:
                continue  # two different hits with this title
            if same_recording(prof[r], prof[s], dur_tol):
                union(r, s)


def song_groups(
    rows: Iterable[tuple],
    known_artists: Optional[set[str]] = None,
    chart_title_acts: Optional[dict[str, set[str]]] = None,
    duration_tol: float = _DUR_TOL,
) -> dict[int, str]:
    """rows: (id, artist, album_artist, title[, duration[, tempo[, energy]]]). Returns id -> group key
    ("artist|title" of the group's representative artist: a spelling the chart data knows, then the
    one the whole library uses most, then the shortest). Placeholder artists borrow the album artist
    when that names an act, otherwise they join the biggest real group of that title.

    Two different acts on one title are two songs, unless their copies are plainly the same
    recording (`same_recording`: the same length within `duration_tol` seconds, and the same tempo
    and energy where the analysis knows them): "Chris Martin - Viva La Vida" beside "Coldplay -
    Viva La Vida" is one song. `chart_title_acts` (normalized title -> the acts that charted with
    it) keeps two acts the charts both credit with the title apart whatever their lengths, and
    joins two library spellings of one charting act. Different songs that share a title ("Angel",
    "Home", the many "Crazy"s) stay apart because they run differently."""
    known = known_artists or set()
    title_acts = chart_title_acts or {}
    out: dict[int, str] = {}
    by_title: dict[str, list[tuple[int, str, str, tuple]]] = {}
    library_count: Counter = Counter()
    for row in rows:
        id_, artist, album_artist, title = row[0], row[1], row[2], row[3]
        feat = (row[4] if len(row) > 4 else None, row[5] if len(row) > 5 else None, row[6] if len(row) > 6 else None)
        tn = norm_title(title or "")
        a = artist or ""
        if is_placeholder(a):
            a = album_artist if album_artist and not is_placeholder(album_artist) else ""
        an = norm_artist(a)
        if an:
            library_count[an] += 1
        if not tn:
            out[id_] = f"{an}|"
            continue
        by_title.setdefault(tn, []).append((id_, a, an, feat))

    for tn, members in by_title.items():
        arts: dict[str, list[int]] = {}
        raw_of: dict[str, str] = {}
        feats: dict[str, list[tuple]] = {}
        for id_, a, an, f in members:
            arts.setdefault(an, []).append(id_)
            raw_of.setdefault(an, a)
            if f[0]:
                feats.setdefault(an, []).append(f)
        keys = [k for k in arts if k]
        parent = {k: k for k in keys}

        def find(x: str) -> str:
            while parent[x] != x:
                parent[x] = parent[parent[x]]
                x = parent[x]
            return x

        def union(a: str, b: str) -> None:
            ra, rb = find(a), find(b)
            if ra != rb:
                parent[rb] = ra

        if len(keys) > 1:
            parts = {k: artist_parts(raw_of[k]) for k in keys}
            for i, a in enumerate(keys):
                for b in keys[i + 1:]:
                    if find(a) != find(b) and _same_act(a, b, parts[a], parts[b]):
                        union(a, b)
            _merge_wrong_tags(keys, parts, find, union, feats, title_acts.get(tn), duration_tol)
        clusters: dict[str, list[str]] = {}
        for k in keys:
            clusters.setdefault(find(k), []).append(k)
        sized: list[tuple[int, str]] = []
        for ks in clusters.values():
            rep = min(ks, key=lambda k: (k not in known, -library_count[k], -len(arts[k]), len(k), k))
            key = f"{rep}|{tn}"
            n = 0
            for k in ks:
                for id_ in arts[k]:
                    out[id_] = key
                    n += 1
            sized.append((n, key))
        for id_ in arts.get("", []):
            out[id_] = max(sized)[1] if sized else f"|{tn}"
    return out


def apply_song_identity(tracks, known_artists: Optional[set[str]] = None,
                        chart_title_acts: Optional[dict[str, set[str]]] = None) -> dict[int, str]:
    """Set every track's song_key to its group key, in memory. Returns the id -> key map.
    `known_artists`: artist keys from the chart data, used to name a group by a spelling that is
    known to be right ("jimmy buffett" over a library's "jimmy buffet"); `chart_title_acts`: which
    acts charted with each title (`Catalog.chart_title_acts()`), the evidence for wrong artist tags."""
    groups = song_groups(((t.id, t.artist, t.album_artist, t.title, t.duration, t.tempo, t.energy) for t in tracks),
                         known_artists, chart_title_acts)
    for t in tracks:
        k = groups.get(t.id)
        if k:
            t.song_key = k
    return groups


def group_sizes(groups: dict[int, str]) -> Counter:
    return Counter(groups.values())
