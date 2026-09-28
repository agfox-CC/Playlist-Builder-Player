"""Loads housekit.toml. Everything tunable lives there; modules read it through the Config object."""
from __future__ import annotations

import os
import sys
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from .models import SlotSpec

CONFIG_FILENAME = "housekit.toml"
DATA_DIR = Path(__file__).parent / "data"

DEFAULT_SLOTS = [
    {"name": "01_Doors", "minutes": 45, "energy": [0.25, 0.50]},
    {"name": "02_Break1", "minutes": 20, "energy": [0.45, 0.65]},
    {"name": "03_Break2", "minutes": 20, "energy": [0.55, 0.80]},
    {"name": "04_PostShow", "minutes": 30, "energy": [0.25, 0.50]},
]


@dataclass
class Weights:
    energy: float = 0.35
    era: float = 0.20
    genre: float = 0.20
    familiarity: float = 0.25  # chart peak/weeks when imported, else copies/compilation hints


@dataclass
class Penalties:
    live: float = 0.6
    instrumental: float = 0.7
    alt_version: float = 0.85  # acoustic / demo / remix / alternate takes
    low_tag_confidence: float = 0.9
    unanalyzed: float = 0.9


@dataclass
class Config:
    base_dir: Path
    config_path: Optional[Path]
    library_roots: list[Path]
    db_path: Path
    output_dir: Path
    playlists_dir: Path = Path("playlists")
    converted_dir: Path = Path("converted")
    convert_jobs: int = 0  # parallel ffmpeg encodes; 0 = half the CPUs
    target_lufs: float = -16.0
    max_track_minutes: float = 7.0
    min_track_seconds: float = 90.0
    tolerance_seconds: float = 60.0
    level_flag_lu: float = 3.0
    jobs: int = 0
    excerpt_seconds: float = 30.0
    analysis_version: int = 1
    match_threshold: float = 80.0
    setlist_path: Path = Path("setlists")  # standing setlist: a file, or a folder with one file per set
    overrides_path: Path = Path("setlist_overrides.toml")
    exclude_last_gigs: int = 3
    slots: list[SlotSpec] = field(default_factory=list)
    weights: Weights = field(default_factory=Weights)
    penalties: Penalties = field(default_factory=Penalties)
    jitter: float = 0.05
    clean_default: bool = False
    clean_trust_before_year: int = 1985
    allow_holiday_default: bool = False
    top_default: int = 0  # >0: only songs that peaked at this chart position or better
    holiday_months: list[int] = field(default_factory=lambda: [12])
    artist_cap_slot: int = 1
    artist_cap_night: int = 2
    album_cap_slot: int = 1
    energy_falloff: float = 0.20
    genres_file: Path = DATA_DIR / "genres.toml"
    content_rules_file: Path = DATA_DIR / "content_rules.toml"
    chart_url: str = "https://raw.githubusercontent.com/utdata/rwd-billboard-data/main/data-out/hot-100-current.csv"
    chart_file: Path = Path("charts/hot-100-current.csv")
    chart_update_days: int = 7  # 0 disables the automatic weekly refresh
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def effective_jobs(self) -> int:
        if self.jobs and self.jobs > 0:
            return self.jobs
        return max(1, (os.cpu_count() or 2) - 1)

    def is_inside_library(self, path: Path) -> bool:
        p = path.resolve()
        for root in self.library_roots:
            r = root.resolve()
            if p == r or r in p.parents:
                return True
        return False

    def settings_summary(self) -> dict[str, Any]:
        return {
            "weights": vars(self.weights),
            "penalties": vars(self.penalties),
            "jitter": self.jitter,
            "target_lufs": self.target_lufs,
            "tolerance_seconds": self.tolerance_seconds,
            "max_track_minutes": self.max_track_minutes,
            "min_track_seconds": self.min_track_seconds,
            "clean_trust_before_year": self.clean_trust_before_year,
            "top_default": self.top_default,
            "artist_cap_slot": self.artist_cap_slot,
            "artist_cap_night": self.artist_cap_night,
            "album_cap_slot": self.album_cap_slot,
            "energy_falloff": self.energy_falloff,
            "match_threshold": self.match_threshold,
            "analysis_version": self.analysis_version,
        }


def is_frozen() -> bool:
    return bool(getattr(sys, "frozen", False))


def app_dir() -> Path:
    """The portable app folder: next to the executable when frozen, else the project folder."""
    if is_frozen():
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent


def find_config(start: Optional[Path] = None) -> Optional[Path]:
    """Look for housekit.toml next to the portable executable first, then in start dir and its
    parents, then next to the package."""
    if is_frozen():
        cand = app_dir() / CONFIG_FILENAME
        if cand.is_file():
            return cand
    here = (start or Path.cwd()).resolve()
    for d in [here, *here.parents]:
        cand = d / CONFIG_FILENAME
        if cand.is_file():
            return cand
    pkg_cand = Path(__file__).resolve().parent.parent / CONFIG_FILENAME
    if pkg_cand.is_file():
        return pkg_cand
    return None


def _p(base: Path, value: str | Path) -> Path:
    p = Path(value).expanduser()
    p = p if p.is_absolute() else (base / p)
    return Path(os.path.normpath(p))


def load_config(path: Optional[Path] = None) -> Config:
    cfg_path = path or find_config()
    data: dict[str, Any] = {}
    if cfg_path and cfg_path.is_file():
        with open(cfg_path, "rb") as f:
            data = tomllib.load(f)
        base = cfg_path.resolve().parent
    else:
        cfg_path = None
        base = Path.cwd().resolve()

    lib = data.get("library", {})
    out = data.get("output", {})
    ana = data.get("analysis", {})
    mat = data.get("matching", {})
    hist = data.get("history", {})
    build = data.get("build", {})
    scoring = data.get("scoring", {})
    files = data.get("data_files", {})
    charts = data.get("charts", {})

    roots = [_p(base, r) for r in lib.get("roots", ["library"])]
    slots_raw = data.get("slots") or DEFAULT_SLOTS
    slots = [
        SlotSpec(
            name=str(s["name"]),
            minutes=float(s["minutes"]),
            energy_min=float(s["energy"][0]),
            energy_max=float(s["energy"][1]),
        )
        for s in slots_raw
    ]

    w = scoring.get("weights", {})
    pen = scoring.get("penalties", {})

    cfg = Config(
        base_dir=base,
        config_path=cfg_path,
        library_roots=roots,
        db_path=_p(base, lib.get("db", "library.db")),
        output_dir=_p(base, out.get("dir", "gigs")),
        playlists_dir=_p(base, out.get("playlists", "playlists")),
        converted_dir=_p(base, out.get("converted", "converted")),
        convert_jobs=int(data.get("convert", {}).get("jobs", 0)),
        target_lufs=float(out.get("target_lufs", -16.0)),
        max_track_minutes=float(build.get("max_track_minutes", 7.0)),
        min_track_seconds=float(build.get("min_track_seconds", 90.0)),
        tolerance_seconds=float(build.get("tolerance_seconds", 60.0)),
        level_flag_lu=float(out.get("level_flag_lu", 3.0)),
        jobs=int(ana.get("jobs", 0)),
        excerpt_seconds=float(ana.get("excerpt_seconds", 30.0)),
        analysis_version=int(ana.get("version", 1)),
        match_threshold=float(mat.get("threshold", 80.0)),
        setlist_path=_p(base, data.get("setlist", {}).get("path", "setlists")),
        overrides_path=_p(base, mat.get("overrides", "setlist_overrides.toml")),
        exclude_last_gigs=int(hist.get("exclude_last", 3)),
        slots=slots,
        weights=Weights(**{k: float(v) for k, v in w.items() if k in Weights.__dataclass_fields__}),
        penalties=Penalties(**{k: float(v) for k, v in pen.items() if k in Penalties.__dataclass_fields__}),
        jitter=float(scoring.get("jitter", 0.05)),
        clean_default=bool(build.get("clean", False)),
        clean_trust_before_year=int(build.get("clean_trust_before_year", 1985)),
        allow_holiday_default=bool(build.get("allow_holiday", False)),
        top_default=int(build.get("top", 0)),
        holiday_months=[int(m) for m in build.get("holiday_months", [12])],
        artist_cap_slot=int(build.get("artist_cap_slot", 1)),
        artist_cap_night=int(build.get("artist_cap_night", 2)),
        album_cap_slot=int(build.get("album_cap_slot", 1)),
        energy_falloff=float(scoring.get("energy_falloff", 0.20)),
        genres_file=_p(base, files["genres"]) if "genres" in files else DATA_DIR / "genres.toml",
        content_rules_file=_p(base, files["content_rules"]) if "content_rules" in files else DATA_DIR / "content_rules.toml",
        chart_url=str(charts.get("url", Config.chart_url)),
        chart_file=_p(base, charts.get("file", "charts/hot-100-current.csv")),
        chart_update_days=int(charts.get("auto_update_days", 7)),
        raw=data,
    )
    return cfg


def load_toml(path: Path) -> dict[str, Any]:
    with open(path, "rb") as f:
        return tomllib.load(f)


# ---------------------------------------------------------------- first-run setup

AUDIO_EXTS_QUICK = {".mp3", ".flac", ".m4a", ".aac", ".wav", ".aiff", ".aif", ".ogg", ".opus", ".wma"}


def _has_audio(folder: Path, depth: int = 2, budget: int = 400) -> bool:
    """Cheap probe: does this folder (a couple of levels deep) hold audio files?"""
    seen = 0
    try:
        stack = [(folder, 0)]
        while stack:
            d, lvl = stack.pop()
            with os.scandir(d) as it:
                for e in it:
                    seen += 1
                    if seen > budget:
                        return False
                    if e.is_file() and Path(e.name).suffix.lower() in AUDIO_EXTS_QUICK:
                        return True
                    if e.is_dir() and lvl < depth and not e.name.startswith("."):
                        stack.append((Path(e.path), lvl + 1))
    except OSError:
        return False
    return False


def detect_library_roots(base: Path) -> list[Path]:
    """Guess the music folders around the app: sibling folders of the app dir that contain audio,
    or the parent itself when the music sits directly in it."""
    base = Path(base).resolve()
    parent = base.parent
    out: list[Path] = []
    try:
        for e in sorted(os.scandir(parent), key=lambda x: x.name.lower()):
            p = Path(e.path)
            if not e.is_dir() or p == base or e.name.startswith(".") or e.name.lower() in ("gigs", "playlists", "setlists", "housekit"):
                continue
            if _has_audio(p):
                out.append(p)
    except OSError:
        pass
    if not out:
        try:
            direct = any(Path(e.name).suffix.lower() in AUDIO_EXTS_QUICK for e in os.scandir(parent) if e.is_file())
        except OSError:
            direct = False
        if direct:
            out.append(parent)
    return out


def _toml_path(base: Path, p: Path) -> str:
    """Relative when possible (so the folder stays portable), forward slashes, TOML-safe."""
    p = Path(p).resolve()
    try:
        rel = os.path.relpath(p, base)
        if not rel.startswith(".."):
            s = rel
        elif rel.count("..") <= 2:
            s = rel
        else:
            s = str(p)
    except ValueError:  # different drive
        s = str(p)
    return s.replace("\\", "/")


def write_config(
    base: Path,
    library_roots: list[Path],
    gigs_dir: Optional[Path] = None,
    playlists_dir: Optional[Path] = None,
    setlist_dir: Optional[Path] = None,
    chart_update_days: int = 7,
) -> Path:
    """Write housekit.toml for a portable install from the packaged template."""
    base = Path(base).resolve()
    template = (DATA_DIR / "housekit.default.toml").read_text(encoding="utf-8")
    roots = ", ".join(f'"{_toml_path(base, r)}"' for r in library_roots) or '"library"'
    text = (
        template.replace("@ROOTS@", roots)
        .replace("@GIGS@", _toml_path(base, gigs_dir or base / "gigs"))
        .replace("@PLAYLISTS@", _toml_path(base, playlists_dir or base / "playlists"))
        .replace("@SETLISTS@", _toml_path(base, setlist_dir or base / "setlists"))
        .replace("@CHART_DAYS@", str(int(chart_update_days)))
    )
    path = base / CONFIG_FILENAME
    path.write_text(text, encoding="utf-8")
    for d in (gigs_dir or base / "gigs", playlists_dir or base / "playlists", setlist_dir or base / "setlists", base / "charts"):
        Path(d).mkdir(parents=True, exist_ok=True)
    return path
