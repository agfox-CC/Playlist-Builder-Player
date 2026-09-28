"""Text normalization shared by tag parsing, setlist matching, and exclusion.

One implementation: if two modules need to agree on whether "Don't Stop Believin'"
and "Dont Stop Believing" are the same song, they both go through `norm_title`.
"""
from __future__ import annotations

import re
import unicodedata

# Bump whenever norm_title / norm_artist change: the catalog stores song keys computed with
# these rules, and a scan recomputes them when this number differs from the stored one.
NORM_VERSION = 3

_APOSTROPHES = "'‘’ʼ`´"
_ARTICLES = ("the ", "a ", "an ")
# "Doobie Brothers, The" -> "The Doobie Brothers" (library-sorting style)
TRAILING_ARTICLE_RE = re.compile(r"^(.+?),\s*(the|a|an)\s*$", re.IGNORECASE)

# Parenthetical / bracketed suffixes that describe a *version*, not a different song.
_VERSION_WORDS = (
    r"live|remaster(ed)?|remix|mix|edit|version|mono|stereo|single|album|deluxe|bonus|"
    r"explicit|clean|acoustic|demo|instrumental|karaoke|radio|extended|original|"
    r"re-?recorded|rerecorded|anniversary|\d{4}|feat\.?|featuring|ft\.?|with |from |"
    r"soundtrack|ost|take \d+|alternate|outtake|unplugged|session|reprise|part \d+|pt\.? \d+|"
    r"taylor's version|sped up|slowed"
)
_PAREN_VERSION_RE = re.compile(
    r"\s*[\(\[\{]\s*(?:[^)\]}]*\b(?:" + _VERSION_WORDS + r")\b[^)\]}]*)\s*[\)\]\}]",
    re.IGNORECASE,
)
_DASH_VERSION_RE = re.compile(
    r"\s+[-–—]\s+(?:(?:\d{4}\s+)?(?:" + _VERSION_WORDS + r")\b.*)$",
    re.IGNORECASE,
)
_FEAT_RE = re.compile(r"\s+(?:feat\.?|featuring|ft\.?|f/)\s+.*$", re.IGNORECASE)
_PUNCT_RE = re.compile(r"[^\w\s]", re.UNICODE)
_WS_RE = re.compile(r"\s+")


def strip_accents(s: str) -> str:
    nfkd = unicodedata.normalize("NFKD", s)
    return "".join(c for c in nfkd if not unicodedata.combining(c))


def split_version(title: str) -> tuple[str, str]:
    """Return (clean_title, version_text). Version text is whatever parenthetical or
    dash suffix described a live/remaster/remix/etc. variant."""
    if not title:
        return "", ""
    versions: list[str] = []
    t = title

    def _grab(m: re.Match) -> str:
        versions.append(m.group(0).strip(" -–—()[]{}"))
        return ""

    # Repeat: "(Live) (Remastered 2011)"
    for _ in range(3):
        new = _PAREN_VERSION_RE.sub(_grab, t)
        if new == t:
            break
        t = new
    t2 = _DASH_VERSION_RE.sub(_grab, t)
    if t2.strip():
        t = t2
    m = _FEAT_RE.search(t)
    if m:
        versions.append(m.group(0).strip())
        t = t[: m.start()]
    return _WS_RE.sub(" ", t).strip(" -–—"), "; ".join(v for v in versions if v)


def norm_basic(s: str) -> str:
    """Lowercase, strip accents, drop apostrophes entirely, & -> and, drop punctuation."""
    if not s:
        return ""
    s = strip_accents(s).lower()
    for ch in _APOSTROPHES:
        s = s.replace(ch, "")
    s = s.replace("&", " and ").replace("+", " and ")
    s = s.replace("$", "s")
    s = _PUNCT_RE.sub(" ", s)
    return _WS_RE.sub(" ", s).strip()


# Stage-sheet shorthand: "HATE MYSELF 4 LOVIN U", "7 NATION ARMY", "FIGHT 4 YOUR RIGHT".
# Applied to both sides of a comparison, so real titles with digits still match themselves.
_TEXTSPEAK = {
    "2": "to", "4": "for", "u": "you", "ur": "your", "n": "and", "r": "are", "luv": "love", "w": "with",
    "0": "zero", "1": "one", "3": "three", "5": "five", "6": "six", "7": "seven", "8": "eight", "9": "nine",
    "10": "ten", "yer": "your", "ya": "you", "thru": "through", "nite": "night", "lil": "little", "xmas": "christmas",
}


def norm_title(title: str) -> str:
    """Aggressive normalization for song-identity comparison."""
    base, _ = split_version(title or "")
    s = norm_basic(base)
    s = " ".join(_TEXTSPEAK.get(w, w) for w in s.split())
    for a in _ARTICLES:
        if s.startswith(a):
            s = s[len(a):]
            break
    # "believin" vs "believing": collapse the dropped-g ending so both compare equal.
    s = re.sub(r"(\w)in\b", r"\1ing", s)
    return s


def strip_article(s: str) -> str:
    for a in _ARTICLES:
        if s.startswith(a):
            return s[len(a):]
    return s


def norm_artist(artist: str) -> str:
    """The act's name for identity purposes: no featured credits, no backing band, no leading
    article, and no connector words, so "Earth, Wind & Fire", "Earth Wind and Fire" and
    "Doobie Brothers, The" / "The Doobie Brothers" compare equal."""
    raw = (artist or "").strip()
    m = TRAILING_ARTICLE_RE.match(raw)
    if m:
        raw = f"{m.group(2)} {m.group(1)}"
    s = norm_basic(raw)
    s = re.sub(r"\b(feat|featuring|ft|with|and the [\w ]+|and his [\w ]+|and her [\w ]+)\b.*$", "", s).strip()
    s = strip_article(s)
    s = re.sub(r"\band\b", " ", s)
    return _WS_RE.sub(" ", s).strip()


def song_key(artist: str, title: str) -> str:
    return f"{norm_artist(artist)}|{norm_title(title)}"


def decade_of(year: int | None) -> int | None:
    if not year or year < 1900:
        return None
    return (year // 10) * 10
