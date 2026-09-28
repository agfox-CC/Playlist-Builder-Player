"""Model objects returned by core functions. Core never prints; adapters serialize these."""
from __future__ import annotations

from dataclasses import dataclass, field, asdict, is_dataclass
from typing import Any, Callable, Optional


@dataclass
class ProgressEvent:
    phase: str  # "scan" | "tags" | "analyze" | "copy"
    done: int
    total: int
    message: str = ""


ProgressCallback = Callable[[ProgressEvent], None]


@dataclass
class TagResult:
    """Everything the tag pass learns about one file. Auto flags only; overrides live in the catalog."""

    path: str
    root: str
    size: int
    mtime: float
    title: str = ""
    artist: str = ""
    album_artist: str = ""
    album: str = ""
    version: str = ""
    genre_raw: str = ""
    genre_norm: str = "Other"
    year: Optional[int] = None
    track_no: Optional[int] = None
    comment: str = ""
    tag_confidence: float = 0.0
    duration: Optional[float] = None
    bitrate: Optional[int] = None
    sample_rate: Optional[int] = None
    channels: Optional[int] = None
    codec: str = ""
    explicit_auto: Optional[bool] = None  # None = unknown
    junk_auto: bool = False
    holiday_auto: bool = False
    live_auto: bool = False
    instrumental_auto: bool = False
    song_key: str = ""
    error: str = ""


@dataclass
class Features:
    track_id: int
    analysis_version: int
    size: int
    mtime: float
    energy: Optional[float] = None
    lufs: Optional[float] = None
    peak_dbfs: Optional[float] = None
    rms_db: Optional[float] = None
    centroid_hz: Optional[float] = None
    flux: Optional[float] = None
    tempo: Optional[float] = None
    onset_rate: Optional[float] = None
    perc_ratio: Optional[float] = None
    dyn_db: Optional[float] = None
    flatness: Optional[float] = None
    error: str = ""


@dataclass
class Track:
    """Effective view of a catalog row: overrides already applied to the flag fields."""

    id: int
    path: str
    root: str
    title: str
    artist: str
    album_artist: str
    album: str
    version: str
    genre_raw: str
    genre_norm: str
    year: Optional[int]
    track_no: Optional[int]
    tag_confidence: float
    duration: Optional[float]
    bitrate: Optional[int]
    codec: str
    explicit: Optional[bool]  # None = unknown
    junk: bool
    holiday: bool
    live: bool
    instrumental: bool
    song_key: str
    dupe_group: Optional[str]
    energy: Optional[float]
    lufs: Optional[float]
    peak_dbfs: Optional[float]
    missing: bool = False
    analysis_error: str = ""
    tempo: Optional[float] = None
    chart_peak: Optional[int] = None  # best Hot 100 position, from an imported chart dataset
    chart_weeks: Optional[int] = None
    chart_year: Optional[int] = None
    year_tag: Optional[int] = None  # the year as tagged; `year` is the inferred original year
    year_source: str = ""  # chart | tag | album chart | copy | album | artist | tag? | hand
    popularity: Optional[float] = None  # 0..1, best score across popularity sources
    pop_source: str = ""  # hot100 | radio | billboard200 | yearend | ...
    album_year: Optional[int] = None  # year the album first charted (Billboard 200)

    @property
    def analyzed(self) -> bool:
        return self.energy is not None

    @property
    def display(self) -> str:
        return f"{self.artist} - {self.title}" if self.artist else self.title


@dataclass
class LibraryStatus:
    roots: list[str]
    db_path: str
    tracks: int
    missing: int
    analyzed: int
    analysis_errors: int
    tag_errors: int
    last_scan: Optional[str]
    flags: dict[str, int] = field(default_factory=dict)


@dataclass
class ScanResult:
    files_seen: int
    tags_read: int
    tags_skipped: int
    tag_errors: int
    analyzed: int
    analysis_errors: int
    analysis_skipped: int
    marked_missing: int
    dupe_groups: int
    interrupted: bool = False
    errors: list[str] = field(default_factory=list)


# ---------------------------------------------------------------- setlist


@dataclass
class SetlistEntry:
    raw: str
    line_no: int
    set_name: str
    part_a: str  # first half before the separator (or the whole line)
    part_b: str = ""  # second half, empty if no separator found


@dataclass
class SetlistMatch:
    entry: SetlistEntry
    title: str  # the title we settled on for exclusion purposes
    artist: str
    track_id: Optional[int]
    track_display: str
    score: float
    status: str  # "matched" | "override" | "unresolved" | "not_in_library"
    candidates: list[tuple[int, str, float]] = field(default_factory=list)  # (track_id, display, score)


@dataclass
class SetlistParse:
    source: str
    orientation: str  # "title-artist" | "artist-title" | "title-only"
    entries: list[SetlistMatch]
    threshold: float

    @property
    def unresolved(self) -> list[SetlistMatch]:
        return [m for m in self.entries if m.status == "unresolved"]

    @property
    def matched(self) -> list[SetlistMatch]:
        return [m for m in self.entries if m.status in ("matched", "override")]


# ---------------------------------------------------------------- build


@dataclass
class SlotSpec:
    name: str
    minutes: float
    energy_min: float
    energy_max: float

    @property
    def target_seconds(self) -> float:
        return self.minutes * 60.0


@dataclass
class GigProfile:
    decades: dict[int, float]  # decade -> share
    genres: dict[str, float]  # normalized genre -> share
    energy_mean: Optional[float]
    energy_std: Optional[float]
    setlist_artists: list[str]
    setlist_titles: list[str]
    n_tracks: int


@dataclass
class ScoreBreakdown:
    energy: float
    era: float
    genre: float
    familiarity: float
    penalty: float
    jitter: float
    total: float


@dataclass
class SlotTrack:
    track: Track
    score: ScoreBreakdown
    position: int = 0
    gain_db: Optional[float] = None
    level_flag: bool = False  # more than N LU off slot median
    out_name: str = ""


@dataclass
class SlotPlan:
    spec: SlotSpec
    tracks: list[SlotTrack]
    runtime: float
    delta: float  # runtime - target, seconds
    filled: bool
    warnings: list[str] = field(default_factory=list)
    lufs_median: Optional[float] = None
    lufs_spread: Optional[float] = None
    candidates_considered: int = 0


@dataclass
class ExclusionSummary:
    setlist: int = 0
    setlist_unresolved_title: int = 0
    dupe_of_excluded: int = 0
    junk: int = 0
    holiday: int = 0
    explicit: int = 0
    explicit_unknown: int = 0
    history: int = 0
    too_long: int = 0
    too_short: int = 0
    no_duration: int = 0
    missing_file: int = 0
    not_top: int = 0  # never charted high enough for --top N
    eligible: int = 0
    total: int = 0


@dataclass
class BuildPlan:
    name: str
    venue: str
    date: str
    setlist_source: str
    profile: GigProfile
    slots: list[SlotPlan]
    exclusions: ExclusionSummary
    settings: dict[str, Any]
    warnings: list[str] = field(default_factory=list)
    unresolved: list[str] = field(default_factory=list)

    @property
    def all_filled(self) -> bool:
        return all(s.filled for s in self.slots)


@dataclass
class OutputResult:
    out_dir: str
    manifest_path: str
    master_playlist: str
    copied: int
    skipped: list[str]
    bytes_copied: int


@dataclass
class GigRecord:
    id: int
    name: str
    date: str
    venue: str
    out_dir: str
    logged_at: str
    track_count: int


def to_jsonable(obj: Any) -> Any:
    """Recursively convert dataclasses / tuples into JSON-friendly structures."""
    if is_dataclass(obj) and not isinstance(obj, type):
        return {k: to_jsonable(v) for k, v in asdict(obj).items()}
    if isinstance(obj, dict):
        return {str(k): to_jsonable(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [to_jsonable(v) for v in obj]
    return obj
