"""Popularity sources: chart files, radio spins, scraped year-end charts, and the score they yield.

Everything here is imported once into the catalog; builds never touch the network. Sources:

  hot100         Billboard Hot 100, weekly since 1958 (csv, refreshed weekly when online)
  hot100-archive the same chart 1958-2021 from a second export (fills any gaps)
  billboard200   Billboard 200 album chart, weekly since 1963 (album-level: dates and lifts album tracks)
  radio          FiveThirtyEight classic-rock radio spins (25 stations, one week in 2014, ~2,200 songs)
  yearend        Billboard year-end genre charts scraped from billboard.com (country, rock, alternative,
                 R&B/hip-hop, dance, adult contemporary, pop airplay ...), one page per chart per year

Each source gets a 0..1 score on the same scale as a Hot 100 run; a track's popularity is the best
score across the sources that mention it (album charts at a discount).
"""
from __future__ import annotations

import csv
import html
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterator, Optional

from .textnorm import norm_artist, norm_basic, norm_title

DEFAULT_CHART_URL = "https://raw.githubusercontent.com/utdata/rwd-billboard-data/main/data-out/hot-100-current.csv"
META_UPDATED = "charts_updated_at"
META_SOURCE_URL = "charts_source_url"

# name -> how to get it and how to read it. `auto` sources are refreshed by `charts update`.
SOURCES: dict[str, dict] = {
    "hot100": {"label": "Billboard Hot 100 (weekly since 1958)", "kind": "hot100", "auto": True,
               "url": DEFAULT_CHART_URL, "file": "charts/hot-100-current.csv"},
    "hot100-archive": {"label": "Billboard Hot 100 archive 1958-2021", "kind": "hot100", "auto": False,
                       "url": "https://raw.githubusercontent.com/utdata/rwd-billboard-data/main/data-out/hot100_archive_1958_2021.csv",
                       "file": "charts/hot100_archive_1958_2021.csv"},
    "billboard200": {"label": "Billboard 200 albums (weekly since 1963)", "kind": "billboard200", "auto": True,
                     "url": "https://raw.githubusercontent.com/utdata/rwd-billboard-data/main/data-out/billboard-200-current.csv",
                     "file": "charts/billboard-200-current.csv"},
    "radio": {"label": "Classic rock radio spins (FiveThirtyEight, 2014)", "kind": "radio", "auto": False,
              "url": "https://raw.githubusercontent.com/fivethirtyeight/data/master/classic-rock/classic-rock-song-list.csv",
              "file": "charts/classic-rock-song-list.csv"},
    "yearend": {"label": "Billboard year-end genre charts (scraped)", "kind": "yearend", "auto": False,
                "url": None, "file": "charts/yearend-genre-charts.csv"},
}

# Year-end charts worth scraping: billboard.com slug -> (short label, first year with a page).
YEAREND_CHARTS: dict[str, tuple[str, int]] = {
    "hot-100-songs": ("hot100 year-end", 1958),
    "hot-country-songs": ("country", 1958),
    "hot-rock-songs": ("rock", 2009),
    "hot-mainstream-rock-tracks": ("mainstream rock", 1981),
    "hot-alternative-songs": ("alternative", 1988),
    "hot-r-and-b-hip-hop-songs": ("r&b/hip-hop", 1958),
    "hot-dance-electronic-songs": ("dance", 2013),
    "dance-club-play-songs": ("dance club", 1976),
    "adult-contemporary": ("adult contemporary", 1961),
    "pop-songs": ("pop airplay", 1992),
    "adult-pop-songs": ("adult pop", 1996),
}

_COLS = {
    "title": ["title", "song", "track", "song_title", "song title", "song clean"],
    "artist": ["performer", "artist", "artists", "artist_name", "artist name", "artist clean"],
    "peak": ["peak_pos", "peak-rank", "peak_rank", "peak", "peak_position", "peakpos", "peak position"],
    "weeks": ["wks_on_chart", "weeks-on-board", "weeks_on_chart", "weeks_on_board", "weeks", "wks", "weeks on chart"],
    "date": ["chart_week", "date", "week", "chart_date", "weekid", "week_id", "chart date"],
    "rank": ["current_week", "current_position", "rank", "position", "week_position", "this_week", "this week"],
    "spins": ["playcount", "spins", "plays"],
    "year": ["release year", "release_year", "year"],
    "chart": ["chart"],
}


@dataclass
class ChartRow:
    artist: str
    title: str  # song title, or album title for album charts
    peak: Optional[int] = None
    weeks: Optional[int] = None  # None when the file has no weeks column (one row per week instead)
    year: Optional[int] = None
    kind: str = "song"  # song | album
    spins: Optional[int] = None  # radio sources
    rank: Optional[int] = None  # year-end rank
    chart: str = ""  # year-end chart label


def _pick(header: list[str], names: list[str]) -> Optional[str]:
    low = {h.strip().lower(): h for h in header}
    for n in names:
        if n in low:
            return low[n]
    return None


def _int(v: Optional[str]) -> Optional[int]:
    if v is None:
        return None
    m = re.search(r"\d+", str(v))
    return int(m.group(0)) if m else None


def read_chart_csv(path: Path, kind: str = "song") -> Iterator[ChartRow]:
    """Billboard-style weekly CSV (one row per entry per week). Raises ValueError without title/artist columns."""
    with open(path, encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        header = reader.fieldnames or []
        c_title, c_artist = _pick(header, _COLS["title"]), _pick(header, _COLS["artist"])
        if not c_title or not c_artist:
            raise ValueError(f"No title/artist columns found in {path.name}; header is {header}")
        c_peak, c_weeks = _pick(header, _COLS["peak"]), _pick(header, _COLS["weeks"])
        c_date, c_rank = _pick(header, _COLS["date"]), _pick(header, _COLS["rank"])
        for row in reader:
            title, artist = (row.get(c_title) or "").strip(), (row.get(c_artist) or "").strip()
            if not title or not artist:
                continue
            peak = _int(row.get(c_peak)) if c_peak else None
            if peak is None:
                peak = _int(row.get(c_rank)) if c_rank else None
            if peak is None:
                continue
            weeks = _int(row.get(c_weeks)) if c_weeks else None
            year = _int((row.get(c_date) or "")[:4]) if c_date else None
            if year is not None and not (1950 <= year <= 2100):
                year = None
            yield ChartRow(artist=artist, title=title, peak=peak, weeks=weeks, year=year, kind=kind)


def read_radio_csv(path: Path) -> Iterator[ChartRow]:
    """FiveThirtyEight classic-rock-song-list.csv: Song Clean, ARTIST CLEAN, Release Year, PlayCount."""
    with open(path, encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        header = reader.fieldnames or []
        c_title, c_artist = _pick(header, _COLS["title"]), _pick(header, _COLS["artist"])
        c_spins, c_year = _pick(header, _COLS["spins"]), _pick(header, _COLS["year"])
        if not c_title or not c_artist or not c_spins:
            raise ValueError(f"Not a radio spins file: {header}")
        for row in reader:
            title, artist = (row.get(c_title) or "").strip(), (row.get(c_artist) or "").strip()
            spins = _int(row.get(c_spins))
            if not title or not artist or not spins:
                continue
            year = _int(row.get(c_year)) if c_year else None
            yield ChartRow(artist=artist, title=title, year=year if year and year >= 1940 else None, spins=spins)


def read_yearend_csv(path: Path) -> Iterator[ChartRow]:
    """Our scraped file: chart,label,year,rank,title,artist."""
    with open(path, encoding="utf-8-sig", newline="") as f:
        for row in csv.DictReader(f):
            try:
                yield ChartRow(artist=row["artist"], title=row["title"], year=int(row["year"]), rank=int(row["rank"]),
                               chart=row.get("label") or row.get("chart") or "")
            except (KeyError, ValueError):
                continue


def read_source(kind: str, path: Path) -> Iterator[ChartRow]:
    if kind == "hot100":
        return read_chart_csv(path, "song")
    if kind == "billboard200":
        return read_chart_csv(path, "album")
    if kind == "radio":
        return read_radio_csv(path)
    if kind == "yearend":
        return read_yearend_csv(path)
    raise ValueError(f"unknown source kind {kind!r}")


# ---------------------------------------------------------------- scoring

def chart_popularity(peak: Optional[int], weeks: Optional[int]) -> float:
    """0..1 from a Hot 100 peak and weeks on chart. #1 for half a year is 1.0; a one-week #100 is ~0.01."""
    if peak is None:
        return 0.0
    peak_score = max(0.0, (101 - min(max(int(peak), 1), 100)) / 100.0)
    weeks_score = min(1.0, (weeks or 1) / 26.0)
    return round(0.65 * peak_score + 0.35 * weeks_score, 3)


def aggregate(rows: Iterator[ChartRow]) -> dict[tuple[str, str], dict]:
    """Collapse rows into one record per song (or album): best peak, most weeks, first/last year,
    total spins, best year-end rank."""
    agg: dict[tuple[str, str], dict] = {}
    for r in rows:
        key = (norm_artist(r.artist), norm_basic(r.title) if r.kind == "album" else norm_title(r.title))
        if not key[0] or not key[1]:
            continue
        a = agg.get(key)
        if a is None:
            a = agg[key] = {"artist": r.artist, "title": r.title, "peak": r.peak, "weeks": r.weeks if r.weeks is not None else (1 if r.peak else None),
                            "first_year": r.year, "last_year": r.year, "spins": r.spins or 0, "rank": r.rank, "chart": r.chart, "n": 0,
                            "debuts": set()}
        a["n"] += 1
        if r.kind == "album" and r.weeks == 1 and r.year is not None:
            a["debuts"].add(r.year)  # a first week on the chart: a new album of that name (self-titled records recur)
        if r.peak is not None:
            a["peak"] = r.peak if a["peak"] is None else min(a["peak"], r.peak)
            if r.weeks is None:
                a["weeks"] = (a["weeks"] or 0) + 1  # one row per week: count them
            else:
                a["weeks"] = max(a["weeks"] or 0, r.weeks)
        if r.year is not None:
            a["first_year"] = r.year if a["first_year"] is None else min(a["first_year"], r.year)
            a["last_year"] = r.year if a["last_year"] is None else max(a["last_year"], r.year)
        if r.spins:
            a["spins"] += r.spins
        if r.rank is not None and (a["rank"] is None or r.rank < a["rank"]):
            a["rank"], a["chart"] = r.rank, r.chart
    return agg


def score_for(kind: str, a: dict, spins_max: int = 1) -> float:
    if kind == "hot100":
        return chart_popularity(a["peak"], a["weeks"])
    if kind == "billboard200":
        return round(0.85 * chart_popularity(a["peak"], a["weeks"]), 3)  # about the album, not the song
    if kind == "radio":
        import math

        return round(0.5 + 0.5 * (math.log(a["spins"] + 1) / math.log(spins_max + 1)), 3)
    if kind == "yearend":
        rank = a["rank"] or 100
        return round(0.9 * (1.0 - (min(rank, 100) - 1) / 100.0 * 0.6), 3)  # #1 -> 0.9, #100 -> 0.36
    return 0.0


def pop_label(chart_peak: Optional[int], chart_weeks: Optional[int], pop_source: str, popularity: Optional[float]) -> str:
    """Short text for tables: '#3/22w' for a Hot 100 run, otherwise the source that made it popular."""
    if chart_peak is not None:
        return f"#{chart_peak}/{chart_weeks}w" if chart_weeks else f"#{chart_peak}"
    if not pop_source or popularity is None:
        return ""
    return {"radio": "radio", "billboard200": "album"}.get(pop_source, pop_source)


def chart_label(peak: Optional[int], weeks: Optional[int]) -> str:
    if peak is None:
        return ""
    return f"#{peak}/{weeks}w" if weeks else f"#{peak}"


# ---------------------------------------------------------------- fetching / importing

def download_file(url: str, dest: Path, timeout: float = 120.0, expect_csv: bool = True) -> int:
    """Fetch to `dest` atomically. Raises on network/HTTP error or an implausible file."""
    import shutil
    import tempfile
    import urllib.request

    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    req = urllib.request.Request(url, headers={"User-Agent": "housekit/0.1 (+chart refresh)"})
    with urllib.request.urlopen(req, timeout=timeout) as resp, tempfile.NamedTemporaryFile("wb", delete=False, dir=str(dest.parent), suffix=".part") as tmp:
        shutil.copyfileobj(resp, tmp)
        tmp_path = Path(tmp.name)
    if tmp_path.stat().st_size < 64:
        tmp_path.unlink(missing_ok=True)
        raise RuntimeError("downloaded file is empty")
    if expect_csv:
        with open(tmp_path, encoding="utf-8-sig", newline="") as f:
            header = f.readline().strip().split(",")
        if not (_pick(header, _COLS["title"]) and _pick(header, _COLS["artist"])):
            tmp_path.unlink(missing_ok=True)
            raise RuntimeError(f"downloaded file doesn't look like a chart CSV (header: {','.join(header)[:80]})")
    tmp_path.replace(dest)
    return dest.stat().st_size


download_chart_csv = download_file  # older name


def import_source(catalog, name: str, path: Path) -> dict:
    """Read a source file and store its scored records. Returns counts."""
    spec = SOURCES.get(name)
    kind = spec["kind"] if spec else name
    n = catalog.import_chart_rows(read_source(kind, Path(path)), source=name, kind=kind)
    return {"source": name, "kind": kind, "records": n}


def guess_source(path: Path) -> str:
    """The source a file belongs to, by its name (hot-100-current.csv -> hot100); else hot100."""
    base = Path(path).name.lower()
    for name, spec in SOURCES.items():
        if Path(spec["file"]).name.lower() == base:
            return name
    return "hot100"


def fetch_source(catalog, name: str, base_dir: Path, url: Optional[str] = None, dest: Optional[Path] = None) -> dict:
    spec = SOURCES[name]
    url = url or spec["url"]
    if not url:
        raise ValueError(f"{name} has no download URL (it is scraped with `charts scrape`)")
    dest = Path(dest) if dest else Path(base_dir) / spec["file"]
    size = download_file(url, dest)
    res = import_source(catalog, name, dest)
    res.update({"bytes": size, "file": str(dest)})
    return res


def local_chart_files(base_dir: Path, hot100_file: Optional[Path] = None) -> dict[str, Path]:
    """Source name -> file, for every source whose file is present in the charts folder."""
    out: dict[str, Path] = {}
    for name, spec in SOURCES.items():
        p = Path(hot100_file) if (name == "hot100" and hot100_file) else Path(base_dir) / spec["file"]
        if p.is_file() and p.stat().st_size > 0:
            out[name] = p
    return out


def load_local_charts(catalog, base_dir: Path, hot100_file: Optional[Path] = None) -> dict:
    """Import every chart file present in the charts folder and re-match the library. No network:
    this is the offline path, used on first launch of a copied folder and by `charts load`."""
    from datetime import datetime

    out: dict = {"sources": [], "tracks_matched": 0}
    for name, path in local_chart_files(base_dir, hot100_file).items():
        try:
            out["sources"].append(import_source(catalog, name, path) | {"file": str(path)})
        except Exception as e:
            out["sources"].append({"source": name, "file": str(path), "error": f"{type(e).__name__}: {e}"})
    if out["sources"]:
        out["tracks_matched"] = catalog.apply_chart_data()
        if not catalog.get_meta(META_UPDATED):
            # Never refreshed online: date the data by the files so the weekly refresh runs when due
            newest = max(Path(s["file"]).stat().st_mtime for s in out["sources"] if "error" not in s and Path(s["file"]).exists())
            catalog.set_meta(META_UPDATED, datetime.fromtimestamp(newest).isoformat(timespec="seconds"))
    out.update(catalog.chart_stats())
    return out


def charts_due(catalog, every_days: int) -> bool:
    """True when the chart data has never been imported or is older than `every_days`."""
    from datetime import datetime, timedelta

    if every_days <= 0:
        return False
    stamp = catalog.get_meta(META_UPDATED)
    if not stamp:
        return True
    try:
        return datetime.now() - datetime.fromisoformat(stamp) > timedelta(days=every_days)
    except ValueError:
        return True


def update_charts(catalog, url: str, dest: Path, source: str = "Billboard Hot 100", base_dir: Optional[Path] = None) -> dict:
    """Refresh the auto-updating sources (Hot 100 and Billboard 200), re-match the library, stamp the time.
    `url`/`dest` keep the older single-file call working for the Hot 100."""
    from datetime import datetime

    out: dict = {"sources": []}
    size = download_file(url, dest)
    out["sources"].append(import_source(catalog, "hot100", Path(dest)) | {"bytes": size, "file": str(dest)})
    if base_dir is not None:  # the other weekly sources live in <base>/charts; without a base only the Hot 100 is refreshed
        for name, spec in SOURCES.items():
            if name == "hot100" or not spec.get("auto"):
                continue
            try:
                out["sources"].append(fetch_source(catalog, name, Path(base_dir)))
            except Exception as e:  # a secondary source failing must not spoil the refresh
                out["sources"].append({"source": name, "error": f"{type(e).__name__}: {e}"})
    out["tracks_matched"] = catalog.apply_chart_data()
    catalog.set_meta(META_UPDATED, datetime.now().isoformat(timespec="seconds"))
    catalog.set_meta(META_SOURCE_URL, url)
    out.update(catalog.chart_stats())
    out["bytes"] = size
    out["songs"] = out.get("chart_songs", 0)
    return out


# ---------------------------------------------------------------- year-end scraping

_ROW_SPLIT = "o-chart-results-list-row-container"
_RANK_RE = re.compile(r'c-label[^"]*"\s*>\s*(\d{1,3})\s*<')
_TITLE_RE = re.compile(r'id="title-of-a-story"[^>]*>\s*(.*?)\s*</h3>', re.S)
_ARTIST_RE = re.compile(r'a-no-trucate[^"]*"\s*>\s*(.*?)\s*</span>', re.S)
_TAG_RE = re.compile(r"<[^>]+>")


def parse_yearend_page(html_text: str) -> list[tuple[int, str, str]]:
    """(rank, title, artist) for every row on a billboard.com year-end chart page."""
    out = []
    for chunk in html_text.split(_ROW_SPLIT)[1:]:
        m_rank, m_title, m_artist = _RANK_RE.search(chunk), _TITLE_RE.search(chunk), _ARTIST_RE.search(chunk)
        if not (m_rank and m_title and m_artist):
            continue
        title = html.unescape(_TAG_RE.sub("", m_title.group(1))).strip()
        artist = html.unescape(_TAG_RE.sub("", m_artist.group(1))).strip()
        artist = re.sub(r"\s+", " ", artist)
        if title and artist:
            out.append((int(m_rank.group(1)), title, artist))
    return out


def fetch_yearend_page(slug: str, year: int, timeout: float = 30.0) -> Optional[str]:
    """The page's HTML, or None when that chart has no page for that year (404)."""
    import urllib.error
    import urllib.request

    url = f"https://www.billboard.com/charts/year-end/{year}/{slug}/"
    req = urllib.request.Request(url, headers={
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36",
        "Accept": "text/html", "Accept-Language": "en-US,en;q=0.8",
    })
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return None
        raise


def scrape_yearend(
    dest: Path,
    charts: Optional[dict[str, tuple[str, int]]] = None,
    years: Optional[range] = None,
    delay: float = 1.5,
    on_progress: Optional[Callable] = None,
    should_stop: Optional[Callable[[], bool]] = None,
    fetch: Callable[[str, int], Optional[str]] = fetch_yearend_page,
) -> dict:
    """Pull year-end charts into one CSV (chart,label,year,rank,title,artist), resuming what is
    already there. Polite: one request every `delay` seconds.

    billboard.com answers a year that has no page with the nearest later year's chart (status 200,
    same markup), so years are fetched newest first and a page whose rows match a year already
    seen for that chart is a stand-in and is skipped, never written under the wrong year."""
    from datetime import date

    charts = charts or YEAREND_CHARTS
    years = years or range(1958, date.today().year + 1)
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    done: set[tuple[str, int]] = set()
    seen: dict[str, dict[tuple, int]] = {}  # chart -> rows -> year, for the stand-in check across resumes
    existing: dict[tuple[str, int], list[tuple[int, str, str]]] = {}
    if dest.exists():
        with open(dest, encoding="utf-8-sig", newline="") as f:
            for row in csv.DictReader(f):
                try:
                    key = (row["chart"], int(row["year"]))
                    existing.setdefault(key, []).append((int(row["rank"]), row["title"], row["artist"]))
                except (KeyError, ValueError):
                    pass
        for (chart, y), rows in existing.items():
            done.add((chart, y))
            seen.setdefault(chart, {})[tuple(rows)] = y
    new_file = not dest.exists()
    order = sorted(years, reverse=True)
    total = sum(1 for slug, (_, first) in charts.items() for y in order if y >= first and (slug, y) not in done)
    fetched = rows_written = errors = standins = 0
    stop = should_stop or (lambda: False)
    with open(dest, "a", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        if new_file:
            w.writerow(["chart", "label", "year", "rank", "title", "artist"])
        for slug, (label, first) in charts.items():
            chart_seen = seen.setdefault(slug, {})
            for y in order:
                if y < first or (slug, y) in done:
                    continue
                if stop():
                    return {"fetched": fetched, "rows": rows_written, "errors": errors, "standins": standins, "stopped": True, "file": str(dest)}
                try:
                    page = fetch(slug, y)
                except Exception:
                    errors += 1
                    page = None
                fetched += 1
                if on_progress:
                    on_progress(fetched, total, f"{label} {y}")
                if page is None:
                    time.sleep(delay)
                    continue
                rows = parse_yearend_page(page)
                key = tuple(rows)
                if not rows:
                    errors += 1
                elif key in chart_seen:
                    standins += 1  # no page for this year; billboard showed us year chart_seen[key] again
                else:
                    chart_seen[key] = y
                    for rank, title, artist in rows:
                        w.writerow([slug, label, y, rank, title, artist])
                        rows_written += 1
                    f.flush()
                time.sleep(delay)
    return {"fetched": fetched, "rows": rows_written, "errors": errors, "standins": standins, "stopped": False, "file": str(dest)}
