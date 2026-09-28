"""Setlist parsing (text / PDF) and fuzzy matching against the catalog.

Unresolved entries are always returned, never dropped: an entry we can't resolve is
exactly the song that stays eligible and gets played before the band covers it.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Optional

from rapidfuzz import fuzz, process

from .catalog import Catalog
from .config import Config
from .models import SetlistEntry, SetlistMatch, SetlistParse, Track
from .textnorm import norm_artist, norm_basic, norm_title

_SET_HEADER = re.compile(
    r"^\s*(?:set\s*(?:\d+|one|two|three|four|five|[ivx]+)\b|set\s*[:\-]|(?:first|second|third|fourth|1st|2nd|3rd|4th)\s+set\b|"
    r"encore\b|break\b|set\s*break\b|intermission\b|doors\b|load.?out\b|pre.?show\b|post.?show\b)",
    re.IGNORECASE,
)
_SKIP_LINE = re.compile(
    r"^\s*(?:\d{1,2}:\d{2}\s*(?:am|pm)?|page \d+|\d+\s*/\s*\d+|setlist|set list|[-=_*]{3,})\s*$",
    re.IGNORECASE,
)
_LEADING_NUM = re.compile(r"^\s*(?:\d{1,3}|[a-z])\s*[.):\-–]\s+", re.IGNORECASE)
# "*SHINE" / "+ZOMBIE": vocalist or arrangement markers on stage sheets.
_LEADING_MARK = re.compile(r"^[\*\+#!•●~^]+\s*")
_TRAILING_MARK = re.compile(r"\s*[\*\+#!~^]+$")
# "D - THE MIDDLE" / "Am: Zombie": a key written before the title.
_LEADING_KEY = re.compile(r"^(?:key(?: of)?\s*[:\-]?\s*)?[A-G][#b♯♭]?(?:m|min|maj)?\s*[-–:]\s+", re.IGNORECASE)
_TRAILING_NOTES = re.compile(
    r"(?:\s*[\(\[](?:[a-g][#b]?m?|capo\s*\d+|\d{2,3}\s*bpm|key(?: of)?\s*[a-g][#b]?m?|drop d|acoustic|open)\s*[\)\]])+\s*$",
    re.IGNORECASE,
)
_TRAILING_KEY = re.compile(r"\s+[-–]\s+(?:key(?: of)?\s+)?[A-G][#b]?m?\s*$")
_BPM = re.compile(r"\s*\b\d{2,3}\s*bpm\b\s*", re.IGNORECASE)
_SPLIT = re.compile(r"\s+[-–—|/]\s+|\t+|\s{3,}|\s+by\s+", re.IGNORECASE)


SETLIST_EXTS = (".txt", ".pdf", ".docx")
_KEY_CELL = re.compile(r"^(?:[A-G][#b♯♭]?(?:m|min|maj)?|\d{2,3}\s*bpm|capo\s*\d+|\d{1,3}\.?)$", re.IGNORECASE)


def _natural_key(s: str) -> list:
    return [int(x) if x.isdigit() else x.lower() for x in re.split(r"(\d+)", s)]


def _read_docx_lines(path: Path) -> list[str]:
    """Paragraphs and table rows from a Word document. No dependencies: a .docx is a zip of XML."""
    import zipfile
    from xml.etree import ElementTree as ET

    W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
    with zipfile.ZipFile(str(path)) as z:
        root = ET.fromstring(z.read("word/document.xml"))
    body = root.find(f"{W}body")
    if body is None:
        return []

    def para_text(p) -> str:
        parts: list[str] = []
        for node in p.iter():
            if node.tag == f"{W}t":
                parts.append(node.text or "")
            elif node.tag == f"{W}tab":
                parts.append("\t")
            elif node.tag in (f"{W}br", f"{W}cr"):
                parts.append("\n")
        return "".join(parts)

    lines: list[str] = []
    for child in body:
        if child.tag == f"{W}p":
            lines.extend(para_text(child).split("\n"))
        elif child.tag == f"{W}tbl":
            for tr in child.iter(f"{W}tr"):
                cells = []
                for tc in tr.findall(f"{W}tc"):
                    cell = " ".join(para_text(p) for p in tc.findall(f"{W}p")).strip()
                    if cell and not _KEY_CELL.match(cell):  # drop index / key / bpm columns
                        cells.append(cell)
                if cells:
                    lines.append(" - ".join(cells[:2]))
    return lines


def read_lines(path: Path) -> list[str]:
    """Lines of a setlist file. A directory means one file per set ("SET 1.docx", "SET 2.docx" ...):
    files are read in natural order and each contributes its name as a set header."""
    if path.is_dir():
        files = sorted(
            (p for p in path.iterdir() if p.is_file() and p.suffix.lower() in SETLIST_EXTS),
            key=lambda p: _natural_key(p.stem),
        )
        lines: list[str] = []
        for p in files:
            stem = p.stem.strip()
            lines.append(stem if _SET_HEADER.match(stem) else f"Set: {stem}")
            lines.extend(read_lines(p))
        return lines
    suffix = path.suffix.lower()
    if suffix == ".pdf":
        import pdfplumber

        lines = []
        with pdfplumber.open(str(path)) as pdf:
            for page in pdf.pages:
                text = page.extract_text() or ""
                lines.extend(text.splitlines())
        return lines
    if suffix == ".docx":
        return _read_docx_lines(path)
    for enc in ("utf-8-sig", "utf-8", "cp1252", "latin-1"):
        try:
            return path.read_text(encoding=enc).splitlines()
        except UnicodeDecodeError:
            continue
    return path.read_text(errors="replace").splitlines()


def clean_line(line: str) -> str:
    s = line.strip().strip("﻿")
    s = _LEADING_NUM.sub("", s)
    s = _LEADING_MARK.sub("", s)
    s = _LEADING_NUM.sub("", s)  # "* 3. Song" - number after the marker
    s = _LEADING_KEY.sub("", s)
    s = _TRAILING_MARK.sub("", s)
    s = _BPM.sub(" ", s)
    s = _TRAILING_NOTES.sub("", s)
    s = _TRAILING_KEY.sub("", s)
    return re.sub(r"\s+", " ", s).strip(" -–—*•")


def parse_entries(lines: list[str]) -> list[SetlistEntry]:
    entries: list[SetlistEntry] = []
    current_set = ""
    for i, raw in enumerate(lines, 1):
        if not raw.strip() or _SKIP_LINE.match(raw):
            continue
        if _SET_HEADER.match(raw.strip()):
            current_set = raw.strip()
            continue
        s = clean_line(raw)
        if not s or len(s) < 2:
            continue
        parts = [p.strip() for p in _SPLIT.split(s, maxsplit=1) if p and p.strip()]
        if len(parts) >= 2:
            entries.append(SetlistEntry(raw=raw.strip(), line_no=i, set_name=current_set, part_a=parts[0], part_b=parts[1]))
        else:
            entries.append(SetlistEntry(raw=raw.strip(), line_no=i, set_name=current_set, part_a=s, part_b=""))
    return entries


# ---------------------------------------------------------------- overrides


def load_overrides(path: Path) -> dict[str, dict[str, Any]]:
    if not path.is_file():
        return {}
    import tomllib

    with open(path, "rb") as f:
        data = tomllib.load(f)
    out: dict[str, dict[str, Any]] = {}
    for item in data.get("override", []):
        key = norm_basic(str(item.get("entry", "")))
        if key:
            out[key] = dict(item)
    return out


def save_override(
    path: Path,
    entry_raw: str,
    track_id: Optional[int] = None,
    not_in_library: bool = False,
    note: str = "",
    track_ids: Optional[list[int]] = None,
) -> None:
    """Pin a setlist line. Several track ids pin a medley line to every song in it."""
    import tomli_w

    existing = load_overrides(path)
    key = norm_basic(entry_raw)
    item: dict[str, Any] = {"entry": entry_raw}
    ids = [int(t) for t in (track_ids or [])] + ([int(track_id)] if track_id is not None else [])
    if len(ids) == 1:
        item["track_id"] = ids[0]
    elif ids:
        item["track_ids"] = ids
    if not_in_library:
        item["not_in_library"] = True
    if note:
        item["note"] = note
    existing[key] = item
    payload = {"override": [v for v in existing.values()]}
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "wb") as f:
        f.write(b"# Hand mappings from setlist lines to catalog tracks. Written by `housekit setlist resolve`.\n")
        tomli_w.dump(payload, f)


# ---------------------------------------------------------------- matching

_NEGATIONS = {"not", "no", "never", "dont", "cant", "wont", "aint", "isnt", "without"}
_STOPWORDS = {
    "a", "an", "the", "to", "of", "in", "on", "and", "for", "you", "me", "my", "i", "im", "it", "its", "is",
    "at", "by", "with", "your", "this", "that", "be", "do", "so", "oh", "yeah", "la", "da", "na", "some",
} | _NEGATIONS


def _has_negation(tokens: list[str]) -> bool:
    return any(t in _NEGATIONS for t in tokens)


def _stem(t: str) -> str:
    return t[:-1] if len(t) > 3 and t.endswith("s") else t


def _content_tokens(tokens: list[str]) -> set[str]:
    return {_stem(t) for t in tokens if t not in _STOPWORDS}


def _in_order(sheet_tokens: list[str], title_tokens: list[str]) -> bool:
    """The sheet's content words appear in the library title in the same order
    ("JUST A GIRL" is not "Girls Just Wanna Have Fun")."""
    stems = [_stem(t) for t in title_tokens]
    pos = 0
    for t in sheet_tokens:
        if t in _STOPWORDS:
            continue
        st = _stem(t)
        try:
            pos = stems.index(st, pos) + 1
        except ValueError:
            return False
    return True


def _title_score(nt: str, nt_tokens: list[str], nt_content: set[str], nt_neg: bool, choice: str) -> float:
    """0..100 likelihood that library title `choice` is the song written as `nt` on the sheet.

    Three signals, best one wins:
      exact        normalized titles are identical
      edit ratio   typos and small wording differences ("ALL THE SMALL THINGYS")
      abbreviation the sheet line is a shortened form of the library title: either a literal
                   substring of it ("SWEET CHILD" in "Sweet Child O' Mine") or every content
                   word of the line appears in the title ("FIGHT 4 YOUR RIGHT" in "(You Gotta)
                   Fight for Your Right (To Party)"). Never the other way round: a library title
                   shorter than the sheet line ("Dig" for "NO DIGGITY") gets no such credit.
    """
    if choice == nt:
        return 100.0
    score = float(fuzz.ratio(nt, choice))
    longer = len(choice) - len(nt)
    if longer > 0:
        length_pen = min(8.0, 0.4 * longer)
        if len(nt) >= 8:
            score = max(score, 0.9 * fuzz.partial_ratio(nt, choice) - length_pen)
        c_tokens = choice.split()
        c_content = _content_tokens(c_tokens)
        if len(nt_content) >= 2 and nt_content <= c_content and _in_order(nt_tokens, c_tokens):
            score = max(score, 92.0 - length_pen)
    if _has_negation(choice.split()) != nt_neg:
        score -= 15.0  # "I'm the Only One" is not "I'm Not the Only One"
    return score


class _Index:
    def __init__(self, tracks: list[Track]):
        self.tracks = tracks
        self.titles: list[str] = []
        self.by_title: dict[str, list[int]] = {}
        for i, t in enumerate(tracks):
            nt = norm_title(t.title)
            if nt not in self.by_title:
                self.by_title[nt] = []
                self.titles.append(nt)
            self.by_title[nt].append(i)
        self.artists = [norm_artist(t.artist) for t in tracks]

    def candidates(self, title: str, artist: str, limit: int = 8) -> list[tuple[int, float]]:
        """(track index, composite score 0..100) sorted best first."""
        nt = norm_title(title)
        if not nt:
            return []
        # WRatio is only the recall step (it is >= every scorer used below); the score itself is
        # `_title_score`, which knows that a stage sheet abbreviates a title but never pads it.
        hits = process.extract(nt, self.titles, scorer=fuzz.WRatio, limit=80, score_cutoff=55)
        na = norm_artist(artist)
        nt_tokens = nt.split()
        nt_content = _content_tokens(nt_tokens)
        nt_neg = _has_negation(nt_tokens)
        scored: dict[int, float] = {}
        for choice, _w, _ in hits:
            tscore = _title_score(nt, nt_tokens, nt_content, nt_neg, choice)
            for idx in self.by_title[choice]:
                t = self.tracks[idx]
                if na:
                    ta = self.artists[idx]
                    ascore = fuzz.token_set_ratio(na, ta) if ta else 45.0
                    # short artist strings inflate token_set_ratio; temper with plain ratio
                    ascore = 0.7 * ascore + 0.3 * fuzz.ratio(na, ta)
                    total = 0.65 * tscore + 0.35 * ascore
                    if tscore >= 95:
                        # Exact title, different artist: almost always a cover of the same song
                        # (Wagon Wheel by Darius Rucker vs Old Crow Medicine Show). Keep it resolvable.
                        total = max(total, 0.85 * tscore + 0.15 * ascore)
                else:
                    total = tscore
                # Tiebreakers: prefer studio, non-junk, well-tagged copies
                total -= 1.5 if t.live else 0.0
                total -= 1.5 if t.junk else 0.0
                total -= (1.0 - t.tag_confidence) * 2.0
                scored[idx] = max(scored.get(idx, 0.0), total)
        best = sorted(scored.items(), key=lambda kv: kv[1], reverse=True)
        return best[:limit]


def _orientation_score(idx: _Index, entries: list[SetlistEntry]) -> tuple[float, float]:
    """Total best-score across entries for (title-artist, artist-title) orderings."""
    ta = at = 0.0
    for e in entries:
        if not e.part_b:
            continue
        c1 = idx.candidates(e.part_a, e.part_b, limit=1)
        c2 = idx.candidates(e.part_b, e.part_a, limit=1)
        ta += c1[0][1] if c1 else 0.0
        at += c2[0][1] if c2 else 0.0
    return ta, at


def match_setlist(
    entries: list[SetlistEntry],
    tracks: list[Track],
    threshold: float,
    overrides: Optional[dict[str, dict[str, Any]]] = None,
    source: str = "",
) -> SetlistParse:
    idx = _Index(tracks)
    by_id = {t.id: t for t in tracks}
    overrides = overrides or {}
    two_part = [e for e in entries if e.part_b]
    if two_part:
        ta, at = _orientation_score(idx, entries)
        orientation = "title-artist" if ta >= at else "artist-title"
    else:
        orientation = "title-only"

    out: list[SetlistMatch] = []
    for e in entries:
        ov = overrides.get(norm_basic(e.raw)) or overrides.get(norm_basic(clean_line(e.raw)))
        if orientation == "artist-title" and e.part_b:
            title, artist = e.part_b, e.part_a
        else:
            title, artist = e.part_a, e.part_b

        if ov is not None:
            if ov.get("not_in_library"):
                out.append(SetlistMatch(e, title, artist, None, "", 100.0, "not_in_library"))
                continue
            tids = list(ov.get("track_ids") or [])
            if ov.get("track_id") is not None:
                tids.append(ov["track_id"])
            pinned = [by_id[int(t)] for t in tids if int(t) in by_id]
            if pinned:
                for t in pinned:  # a medley line pins several songs: one match row per song
                    out.append(SetlistMatch(e, t.title, t.artist, t.id, t.display, 100.0, "override"))
                continue
            # override points at a track that no longer exists: fall through to matching

        cands = idx.candidates(title, artist)
        alt: list[tuple[int, float]] = []
        if e.part_b:
            alt = idx.candidates(artist, title)  # the other ordering, in case this line is flipped
        best_list = cands
        best = cands[0] if cands else None
        if e.part_b and alt and (best is None or (alt[0][1] >= threshold and alt[0][1] > best[1] + 5)):
            best = alt[0]
            best_list = alt
            title, artist = artist, title
        cand_view = [(tracks[i].id, tracks[i].display, round(s, 1)) for i, s in best_list[:5]]
        if best is not None and best[1] >= threshold:
            t = tracks[best[0]]
            out.append(SetlistMatch(e, t.title, t.artist, t.id, t.display, round(best[1], 1), "matched", cand_view))
        else:
            out.append(SetlistMatch(e, title, artist, None, "", round(best[1], 1) if best else 0.0, "unresolved", cand_view))
    return SetlistParse(source=source, orientation=orientation, entries=out, threshold=threshold)


def parse_setlist(path: Path, catalog: Catalog, cfg: Config, tracks: Optional[list[Track]] = None) -> SetlistParse:
    lines = read_lines(path)
    entries = parse_entries(lines)
    tracks = tracks if tracks is not None else catalog.all_tracks()
    overrides = load_overrides(cfg.overrides_path)
    return match_setlist(entries, tracks, cfg.match_threshold, overrides, source=str(path))


_MEDLEY_SPLIT = re.compile(r"\s*[-/+&–]\s*|\s+into\s+|\s+w/\s+", re.IGNORECASE)


def excluded_titles(parse: SetlistParse) -> set[str]:
    """Every normalized title the band is playing, including both halves of unresolved lines.

    Unresolved lines also contribute each hyphen/slash-separated segment: a medley written as
    "HANDS 2 YERSF-SLOW RIDE" must still keep "Slow Ride" off the PA even before anyone pins it."""
    titles: set[str] = set()
    for m in parse.entries:
        if m.title:
            titles.add(norm_title(m.title))
        if m.status in ("unresolved", "not_in_library"):
            for part in (m.entry.part_a, m.entry.part_b):
                if not part:
                    continue
                titles.add(norm_title(part))
                for seg in _MEDLEY_SPLIT.split(part):
                    seg = norm_title(seg)
                    if len(seg) >= 5 and " " in seg:
                        titles.add(seg)
    return {t for t in titles if t}
