"""Metadata extraction and normalization. One file in, one TagResult out. Never writes."""
from __future__ import annotations

import json
import os
import re
import subprocess
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any, Optional

from .config import load_toml
from .fsutil import longpath
from .models import TagResult
from .textnorm import norm_artist, norm_basic, norm_title, song_key, split_version

AUDIO_EXTS = {".mp3", ".flac", ".m4a", ".aac", ".wav", ".aiff", ".aif", ".ogg", ".oga", ".opus", ".wma"}

_SUBPROC_FLAGS = getattr(subprocess, "CREATE_NO_WINDOW", 0)

# Candidate keys per field, across ID3 / Vorbis / MP4 / ASF / RIFF, all lowercased.
_KEYS = {
    "title": ["title", "tit2", "\xa9nam", "inam"],
    "artist": ["artist", "tpe1", "\xa9art", "author", "iart"],
    "album_artist": ["albumartist", "album_artist", "album artist", "tpe2", "aart", "wm/albumartist", "band"],
    "album": ["album", "talb", "\xa9alb", "wm/albumtitle", "iprd"],
    "genre": ["genre", "tcon", "\xa9gen", "gnre", "wm/genre", "ignr"],
    # Original release date first: remasters and compilations put the reissue year in DATE/TDRC.
    "date": ["originaldate", "originalyear", "tdor", "tory", "txxx:originalyear", "date", "year", "tdrc", "tyer", "\xa9day", "wm/year", "wm/originalreleaseyear", "icrd"],
    "track": ["tracknumber", "track", "trck", "trkn", "wm/tracknumber", "itrk"],
    "comment": ["comment", "\xa9cmt", "description", "wm/description", "icmt"],
    "advisory": ["rtng", "itunesadvisory", "txxx:itunesadvisory", "wm/parentalrating"],
}


@dataclass
class ContentRules:
    junk_title: list[re.Pattern]
    junk_genre: list[re.Pattern]
    junk_album: list[re.Pattern]
    holiday_title: list[re.Pattern]
    holiday_album: list[re.Pattern]
    holiday_genre: list[re.Pattern]
    live: list[re.Pattern]
    live_genre: list[re.Pattern]
    instrumental: list[re.Pattern]
    instrumental_genre: list[re.Pattern]
    explicit_markers: list[re.Pattern]
    clean_markers: list[re.Pattern]
    explicit_known: set[str]  # song keys


@dataclass
class GenreRule:
    pattern: re.Pattern
    genre: str
    min_year: Optional[int]
    max_year: Optional[int]


@dataclass
class GenreRules:
    vocabulary: list[str]
    rules: list[GenreRule]
    similarity: dict[tuple[str, str], float]

    def similarity_of(self, a: str, b: str) -> float:
        if a == b:
            return 1.0
        return self.similarity.get((a, b), self.similarity.get((b, a), 0.0))


def _compile(patterns: list[str]) -> list[re.Pattern]:
    return [re.compile(p, re.IGNORECASE) for p in patterns]


@lru_cache(maxsize=4)
def load_content_rules(path: str) -> ContentRules:
    d = load_toml(Path(path))
    junk = d.get("junk", {})
    hol = d.get("holiday", {})
    live = d.get("live", {})
    ins = d.get("instrumental", {})
    exp = d.get("explicit", {})
    known = {song_key(e.get("artist", ""), e.get("title", "")) for e in exp.get("known", [])}
    return ContentRules(
        junk_title=_compile(junk.get("title_patterns", [])),
        junk_genre=_compile(junk.get("genre_patterns", [])),
        junk_album=_compile(junk.get("album_patterns", [])),
        holiday_title=_compile(hol.get("title_patterns", [])),
        holiday_album=_compile(hol.get("album_patterns", [])),
        holiday_genre=_compile(hol.get("genre_patterns", [])),
        live=_compile(live.get("patterns", [])),
        live_genre=_compile(live.get("genre_patterns", [])),
        instrumental=_compile(ins.get("patterns", [])),
        instrumental_genre=_compile(ins.get("genre_patterns", [])),
        explicit_markers=_compile(exp.get("patterns", [])),
        clean_markers=_compile(exp.get("clean_patterns", [])),
        explicit_known=known,
    )


@lru_cache(maxsize=4)
def load_genre_rules(path: str) -> GenreRules:
    d = load_toml(Path(path))
    rules = [
        GenreRule(
            pattern=re.compile(r["pattern"], re.IGNORECASE),
            genre=r["genre"],
            min_year=r.get("min_year"),
            max_year=r.get("max_year"),
        )
        for r in d.get("rule", [])
    ]
    sim: dict[tuple[str, str], float] = {}
    for k, v in d.get("similarity", {}).items():
        a, _, b = k.partition("|")
        sim[(a.strip(), b.strip())] = float(v)
    return GenreRules(vocabulary=list(d.get("vocabulary", [])), rules=rules, similarity=sim)


def normalize_genre(raw: str, year: Optional[int], rules: GenreRules) -> str:
    if not raw:
        return "Other"
    for r in rules.rules:
        if r.min_year is not None or r.max_year is not None:
            if year is None:
                continue  # year-gated rules need a known year
            if r.min_year is not None and year < r.min_year:
                continue
            if r.max_year is not None and year > r.max_year:
                continue
        if r.pattern.search(raw):
            return r.genre
    return "Other"


# ---------------------------------------------------------------- raw tag reading


def _stringify(v: Any) -> str:
    """Coerce a mutagen tag value (frame, attribute, list, tuple) to a plain string."""
    if v is None:
        return ""
    if isinstance(v, (list, tuple)):
        if len(v) == 0:
            return ""
        if isinstance(v[0], tuple):  # MP4 trkn: [(n, total)]
            return str(v[0][0])
        if all(isinstance(x, int) for x in v):  # MP4 rtng: [1]
            return str(v[0])
        return _stringify(v[0])
    if hasattr(v, "text"):  # ID3 frame
        t = v.text
        if isinstance(t, (list, tuple)):
            return "; ".join(str(x) for x in t if str(x))
        return str(t)
    if hasattr(v, "value"):  # ASF attribute
        return str(v.value)
    return str(v)


def _collect(tags: Any) -> dict[str, str]:
    """Flatten mutagen tags into {lowercase key: string value}."""
    out: dict[str, str] = {}
    if tags is None:
        return out
    try:
        items = list(tags.items()) if hasattr(tags, "items") else []
    except Exception:
        items = []
    if not items and hasattr(tags, "keys"):
        try:
            items = [(k, tags[k]) for k in tags.keys()]
        except Exception:
            items = []
    for k, v in items:
        key = str(k).lower()
        # ID3 COMM frames are keyed like "COMM::eng"; TXXX like "TXXX:desc"
        if key.startswith("comm"):
            key = "comment"
        s = _stringify(v)
        if s and key not in out:
            out[key] = s
    return out


def _get(tags: dict[str, str], field: str) -> str:
    for k in _KEYS[field]:
        v = tags.get(k)
        if v:
            return v.strip()
    return ""


def _parse_year(s: str) -> Optional[int]:
    m = re.search(r"(19|20)\d{2}", s or "")
    return int(m.group(0)) if m else None


def _parse_track_no(s: str) -> Optional[int]:
    m = re.match(r"\s*(\d+)", s or "")
    return int(m.group(1)) if m else None


def _ffprobe(path: Path) -> dict[str, Any]:
    try:
        r = subprocess.run(
            ["ffprobe", "-v", "error", "-print_format", "json", "-show_format", "-show_streams", longpath(path)],
            capture_output=True, text=True, timeout=60, stdin=subprocess.DEVNULL, creationflags=_SUBPROC_FLAGS,
            encoding="utf-8", errors="replace",
        )
        if r.returncode != 0:
            return {}
        return json.loads(r.stdout or "{}")
    except Exception:
        return {}


# ---------------------------------------------------------------- filename / folder fallback

_FOLDER_YEAR_ARTIST_ALBUM = re.compile(r"^\(?(?P<year>(?:19|20)\d{2})\)?\s*[-–.]?\s*(?P<artist>.+?)\s+[-–]\s+(?P<album>.+?)\s*(?:\[.*?\])?\s*$")
_FOLDER_ARTIST_ALBUM = re.compile(r"^(?P<artist>.+?)\s+[-–]\s+(?P<album>.+?)\s*(?:\[.*?\])?\s*$")
_FOLDER_ALBUM_YEAR = re.compile(r"^(?P<album>.+?)\s*[\(\[](?P<year>(?:19|20)\d{2})[\)\]]\s*(?:\[.*?\])?\s*$")
_FILE_NUM_ARTIST_TITLE = re.compile(r"^\s*(?P<num>\d{1,3})\s*[-.)]\s*(?P<artist>.+?)\s+[-–]\s+(?P<title>.+)$")
_FILE_NUM_TITLE = re.compile(r"^\s*(?P<num>\d{1,3})\s*[-.)]\s*(?P<title>.+)$")
_FILE_ARTIST_TITLE = re.compile(r"^(?P<artist>.+?)\s+[-–]\s+(?P<title>.+)$")


def _clean_folder_album(album: str) -> str:
    album = re.sub(r"\s*\[(flac|mp3|wav|tracks|cue|log|\d+ ?bit|\d+ ?khz|lossless|web|cd|vinyl|deluxe)[^\]]*\]", "", album, flags=re.I)
    return album.strip(" -_")


def guess_from_path(path: Path, root: Path) -> dict[str, Any]:
    """Best-effort artist/album/title/year/track from folder and file names."""
    try:
        rel = path.resolve().relative_to(root.resolve())
    except Exception:
        rel = Path(path.name)
    parts = list(rel.parts)
    stem = path.stem
    guess: dict[str, Any] = {"artist": "", "album": "", "title": "", "year": None, "year_strict": False, "track_no": None, "source": "filename"}

    folders = parts[:-1]
    # "Album/CD1/track.mp3": the disc folder carries nothing, look at its parent instead.
    if folders and re.fullmatch(r"\s*(disc|cd|disk)\s*\d+\s*", folders[-1], re.I):
        folders = folders[:-1]
    if folders:
        last = folders[-1]
        m = _FOLDER_YEAR_ARTIST_ALBUM.match(last)
        m_alb_year = _FOLDER_ALBUM_YEAR.match(last)
        m_art_alb = _FOLDER_ARTIST_ALBUM.match(last)
        if m:
            guess.update(artist=m["artist"].strip(), album=_clean_folder_album(m["album"]), year=int(m["year"]), year_strict=True, source="folder")
        elif m_alb_year and len(folders) >= 2:
            guess.update(artist=folders[-2].strip(), album=_clean_folder_album(m_alb_year["album"]), year=int(m_alb_year["year"]), year_strict=True, source="folder")
        elif len(folders) >= 2:
            # Artist/Album/file layout
            guess.update(artist=folders[-2].strip(), album=_clean_folder_album(last), source="folder")
        elif m_art_alb:
            # Root/"Artist - Album"/file
            guess.update(artist=m_art_alb["artist"].strip(), album=_clean_folder_album(m_art_alb["album"]), source="folder")
        else:
            # A single folder level under the root ("MP3/Toto/01 - Africa.mp3") is the artist.
            guess.update(artist=last.strip(), source="folder")
        if guess["year"] is None:
            for f in reversed(folders):
                y = _parse_year(f)
                if y:
                    guess["year"] = y
                    break

    folder_artist = guess["artist"]
    folder_is_compilation = bool(folder_artist) and bool(_COMPILATION_FOLDER.search(folder_artist))
    m = _FILE_NUM_ARTIST_TITLE.match(stem)
    if m:
        guess["track_no"] = int(m["num"])
        fa, ft = m["artist"].strip(), m["title"].strip()
        if folder_artist and not folder_is_compilation and norm_artist(fa) != norm_artist(folder_artist):
            # "01 - Title - Subtitle" under a known artist folder: keep it all as the title
            guess["title"] = f"{fa} - {ft}"
        else:
            guess["artist"] = fa
            guess["title"] = ft
        return guess
    m = _FILE_NUM_TITLE.match(stem)
    if m:
        guess["track_no"] = int(m["num"])
        guess["title"] = m["title"].strip()
        return guess
    m = _FILE_ARTIST_TITLE.match(stem)
    if m:
        # Un-numbered "A - B" files are far more often "Artist - Title" than "Title - Subtitle".
        guess["artist"] = m["artist"].strip()
        guess["title"] = m["title"].strip()
        return guess
    guess["title"] = stem
    return guess


_COMPILATION_FOLDER = re.compile(
    r"various|compilation|compliation|\btop ?\d+|\bhits\b|\bmix\b|collection|soundtrack|\bost\b|\bnow\b|billboard|best of|greatest|"
    r"\bva\b|unknown|singles|playlist|jukebox|party|wedding|classics",
    re.IGNORECASE,
)


# ---------------------------------------------------------------- content flags


def _any(patterns: list[re.Pattern], *texts: str) -> bool:
    for t in texts:
        if not t:
            continue
        for p in patterns:
            if p.search(t):
                return True
    return False


def content_flags(
    title_full: str,
    title_clean: str,
    version: str,
    artist: str,
    album: str,
    genre_raw: str,
    duration: Optional[float],
    advisory: str,
    rules: ContentRules,
    min_seconds: float = 90.0,
) -> dict[str, Any]:
    key = song_key(artist, title_clean)

    explicit: Optional[bool] = None
    adv = (advisory or "").strip().lower()
    if adv in ("1", "4", "explicit", "true"):
        explicit = True
    elif adv in ("2", "clean"):
        explicit = False
    if _any(rules.explicit_markers, title_full, album):
        explicit = True
    elif key in rules.explicit_known:
        explicit = True
    elif explicit is None and _any(rules.clean_markers, title_full, album):
        explicit = False

    junk = (
        _any(rules.junk_title, title_full)
        or _any(rules.junk_genre, genre_raw)
        or _any(rules.junk_album, album)
        or (duration is not None and duration < min_seconds)
    )
    holiday = _any(rules.holiday_title, title_full) or _any(rules.holiday_album, album) or _any(rules.holiday_genre, genre_raw)
    live = _any(rules.live, title_full, version, album) or _any(rules.live_genre, genre_raw)
    instrumental = _any(rules.instrumental, title_full, version) or _any(rules.instrumental_genre, genre_raw)
    return {
        "explicit_auto": explicit,
        "junk_auto": bool(junk),
        "holiday_auto": bool(holiday),
        "live_auto": bool(live),
        "instrumental_auto": bool(instrumental),
    }


# ---------------------------------------------------------------- main entry


def read_tags(
    path: Path,
    root: Path,
    content_rules: ContentRules,
    genre_rules: GenreRules,
    min_track_seconds: float = 90.0,
) -> TagResult:
    """Read one file. Never raises: errors land in TagResult.error and the row is still catalogued."""
    lp = longpath(path)
    try:
        st = os.stat(lp)
    except OSError as e:
        return TagResult(path=str(path), root=str(root), size=0, mtime=0.0, title=path.stem, error=f"stat failed: {e}")

    res = TagResult(path=str(path), root=str(root), size=st.st_size, mtime=st.st_mtime)
    if st.st_size == 0:
        res.title = path.stem
        res.error = "zero-byte file"
        return res

    tags: dict[str, str] = {}
    info: Any = None
    codec = ""
    try:
        import mutagen

        audio = mutagen.File(lp)
        if audio is not None:
            tags = _collect(audio.tags)
            info = audio.info
            codec = type(audio).__name__.replace("Easy", "")
    except Exception as e:  # corrupt / DRM / unsupported
        res.error = f"mutagen: {type(e).__name__}: {e}"

    duration = bitrate = sample_rate = channels = None
    if info is not None:
        duration = float(getattr(info, "length", 0) or 0) or None
        bitrate = int(getattr(info, "bitrate", 0) or 0) or None
        sample_rate = int(getattr(info, "sample_rate", 0) or 0) or None
        channels = int(getattr(info, "channels", 0) or 0) or None

    if duration is None or not tags:
        probe = _ffprobe(path)
        fmt = probe.get("format", {})
        if duration is None and fmt.get("duration"):
            try:
                duration = float(fmt["duration"]) or None
            except ValueError:
                pass
        if bitrate is None and fmt.get("bit_rate"):
            bitrate = int(float(fmt["bit_rate"]))
        for s in probe.get("streams", []):
            if s.get("codec_type") == "audio":
                codec = codec or s.get("codec_name", "")
                sample_rate = sample_rate or int(s.get("sample_rate") or 0) or None
                channels = channels or int(s.get("channels") or 0) or None
                break
        if not tags:
            ptags = {str(k).lower(): str(v) for k, v in (fmt.get("tags") or {}).items()}
            tags = {k: v for k, v in ptags.items() if v}
            if tags:
                res.error = ""  # ffprobe rescued the tags
        if duration is None and not res.error:
            res.error = "no duration (undecodable?)"

    title_raw = _get(tags, "title")
    artist = _get(tags, "artist")
    album_artist = _get(tags, "album_artist")
    album = _get(tags, "album")
    genre_raw = _get(tags, "genre")
    year = _parse_year(_get(tags, "date"))
    track_no = _parse_track_no(_get(tags, "track"))
    comment = _get(tags, "comment")
    advisory = _get(tags, "advisory")

    confidence = 1.0 if (title_raw and artist) else 0.0
    g = guess_from_path(path, root)
    if g["year"] and (year is None or (g["year_strict"] and g["year"] < year)):
        # "(1970) Artist - Album" folder beats a 2011 remaster date in the tag. A loose year found
        # anywhere in a folder name ("Hits of 1980-1989") only fills in a missing year.
        year = g["year"]
    if not title_raw or not artist:
        if not title_raw:
            title_raw = g["title"] or path.stem
        if not artist:
            artist = g["artist"] or album_artist
        album = album or g["album"]
        year = year or g["year"]
        track_no = track_no or g["track_no"]
        if title_raw and artist:
            confidence = 0.6 if g["source"] == "folder" else 0.4
        elif title_raw:
            confidence = 0.2
    if artist and re.match(r"^(various( artists)?|va|unknown( artist)?|soundtrack)$", artist, re.I) and album_artist and not re.match(r"^(various|va)", album_artist, re.I):
        artist = album_artist

    # Strip a leading track number that leaked into a tagged title ("03 - Song")
    title_raw = re.sub(r"^\s*\d{1,3}\s*[-.)]\s+", "", title_raw).strip() or title_raw
    title_clean, version = split_version(title_raw)
    genre_norm = normalize_genre(genre_raw, year, genre_rules)

    flags = content_flags(title_raw, title_clean, version, artist, album, genre_raw, duration, advisory, content_rules, min_track_seconds)

    res.title = title_clean or title_raw
    res.artist = artist
    res.album_artist = album_artist
    res.album = album
    res.version = version
    res.genre_raw = genre_raw
    res.genre_norm = genre_norm
    res.year = year
    res.track_no = track_no
    res.comment = comment[:500]
    res.tag_confidence = confidence
    res.duration = duration
    res.bitrate = bitrate
    res.sample_rate = sample_rate
    res.channels = channels
    res.codec = codec or path.suffix.lstrip(".").upper()
    res.song_key = song_key(artist, res.title)
    for k, v in flags.items():
        setattr(res, k, v)
    return res


def is_audio(path: Path) -> bool:
    return path.suffix.lower() in AUDIO_EXTS


__all__ = [
    "AUDIO_EXTS", "ContentRules", "GenreRules", "load_content_rules", "load_genre_rules",
    "normalize_genre", "read_tags", "guess_from_path", "content_flags", "is_audio", "norm_basic", "norm_title",
]
