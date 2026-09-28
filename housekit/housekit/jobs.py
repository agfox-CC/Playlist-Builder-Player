"""The operations both the CLI and the GUI run, in one place: index, build, playlist, chart refresh.

Each takes a Config plus plain arguments and returns model objects. No printing, no widgets.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Callable, Optional

from .catalog import Catalog
from .config import Config
from .models import BuildPlan, LibraryStatus, OutputResult, ProgressCallback, ScanResult, SlotSpec
from .playlists import PlaylistResult, PlaylistSpec, select_playlist


def open_catalog(cfg: Config) -> Catalog:
    """Open the catalog and let it follow the music if the folders moved since the last scan
    (a portable bundle copied elsewhere, a library reorganised). Never fails the caller."""
    cat = Catalog(cfg.db_path)
    try:
        rep = cat.relocate_roots(cfg.library_roots)
        if rep["relocated"] or rep["merged"] or rep["marked_missing"]:
            cat.rebuild_dupe_groups()
            cat.set_meta("last_relocation", repr(rep))
    except Exception:
        pass
    return cat


@dataclass
class BuildRequest:
    setlist: Optional[Path] = None  # default: the standing setlist
    out: Optional[Path] = None  # default: "<today> Gig" under the gigs dir
    venue: str = ""
    gig_date: Optional[str] = None
    clean: Optional[bool] = None
    allow_holiday: Optional[bool] = None
    slot_minutes: list[Optional[float]] = field(default_factory=list)  # per slot, None keeps the default
    exclude_last: Optional[int] = None
    top: Optional[int] = None
    seed: Optional[int] = None
    dry_run: bool = False
    overwrite: bool = False
    log: bool = True


def resolve_out(cfg: Config, out: Optional[Path], default_name: str, base: Path) -> Path:
    if out is None:
        out = Path(default_name)
    return out if (out.is_absolute() or len(out.parts) > 1) else base / out


def run_build(cfg: Config, req: BuildRequest, on_progress: Optional[ProgressCallback] = None) -> tuple[BuildPlan, Optional[OutputResult], Path]:
    """Parse the setlist, plan every slot, and (unless dry_run) copy files, write playlists, log history."""
    from .history import log_build, recent_exclusions
    from .output import write_output
    from .setlist import parse_setlist
    from .slots import build_plan

    setlist = req.setlist or cfg.setlist_path
    if not setlist.exists():
        raise FileNotFoundError(f"No setlist at {setlist}. Drop the set files there or choose one.")
    out_path = resolve_out(cfg, req.out, f"{date.today().isoformat()} Gig", cfg.output_dir)
    name = out_path.name
    gig_date = req.gig_date
    if gig_date is None:
        m = re.match(r"^(\d{4}-\d{2}-\d{2})", name)
        if m:
            gig_date = m.group(1)
    slots = [SlotSpec(s.name, s.minutes, s.energy_min, s.energy_max) for s in cfg.slots]
    for i, mins in enumerate(req.slot_minutes):
        if mins is not None and i < len(slots):
            slots[i].minutes = float(mins)

    with open_catalog(cfg) as cat:
        tracks = cat.all_tracks()
        if not tracks:
            raise RuntimeError("The catalog is empty. Index the library first.")
        parse = parse_setlist(setlist, cat, cfg, tracks=tracks)
        n_hist = cfg.exclude_last_gigs if req.exclude_last is None else req.exclude_last
        hist = recent_exclusions(cat, n_hist)
        plan = build_plan(
            tracks, parse, cfg, name=name, venue=req.venue, slots=slots, clean=req.clean, allow_holiday=req.allow_holiday,
            history_keys=hist, seed=req.seed, gig_date=gig_date, top=req.top, known_artists=cat.chart_artist_keys(),
            chart_title_acts=cat.chart_title_acts(),
        )
        result = None
        if not req.dry_run:
            result = write_output(plan, out_path, cfg, on_progress, overwrite=req.overwrite)
            if req.log:
                log_build(cat, plan, str(out_path))
    return plan, result, out_path


def run_playlist(
    cfg: Config,
    spec: PlaylistSpec,
    out: Optional[Path] = None,
    copy: bool = True,
    dry_run: bool = False,
    overwrite: bool = False,
    on_progress: Optional[ProgressCallback] = None,
) -> tuple[PlaylistResult, Optional[OutputResult], Path]:
    from .output import write_playlist

    out_path = resolve_out(cfg, out, spec.name, cfg.playlists_dir)
    with open_catalog(cfg) as cat:
        tracks = cat.all_tracks()
        known = cat.chart_artist_keys()
        acts = cat.chart_title_acts()
    if not tracks:
        raise RuntimeError("The catalog is empty. Index the library first.")
    result = select_playlist(tracks, spec, cfg, known_artists=known, chart_title_acts=acts)
    written = None
    if not dry_run and result.tracks:
        written = write_playlist(result, out_path, cfg, copy=copy, overwrite=overwrite, on_progress=on_progress)
    return result, written, out_path


def run_index(
    cfg: Config,
    on_progress: Optional[ProgressCallback] = None,
    rescan: bool = False,
    limit: Optional[int] = None,
    analyze: bool = True,
    retry_errors: bool = False,
    jobs: Optional[int] = None,
    should_stop: Optional[Callable[[], bool]] = None,
) -> ScanResult:
    from .indexer import scan_library

    missing = [r for r in cfg.library_roots if not r.is_dir()]
    if missing:
        raise FileNotFoundError("Library root not found: " + ", ".join(str(r) for r in missing))
    with open_catalog(cfg) as cat:
        return scan_library(cat, cfg, on_progress, rescan=rescan, limit=limit, analyze=analyze,
                            retry_errors=retry_errors, jobs=jobs, should_stop=should_stop)


def library_status(cfg: Config) -> LibraryStatus:
    with open_catalog(cfg) as cat:
        return cat.status([str(r) for r in cfg.library_roots])


def run_convert(
    cfg: Config,
    inputs: list[Path],
    opts,
    out_dir: Optional[Path] = None,
    on_progress: Optional[ProgressCallback] = None,
    should_stop: Optional[Callable[[], bool]] = None,
):
    """Convert files/folders with ffmpeg: in place unless an output folder is given. Library tracks get
    their measured loudness applied as a plain gain when loudness matching is on; everything else
    goes through loudnorm."""
    from .convert import convert_files

    out_dir = Path(out_dir) if out_dir else None
    with open_catalog(cfg) as cat:
        loud = cat.loudness_map()
        rates = cat.rate_map()
    if opts.jobs <= 0 and cfg.convert_jobs > 0:
        opts.jobs = cfg.convert_jobs
    return convert_files(
        inputs, out_dir, opts,
        loudness_lookup=lambda p: loud.get(str(p).lower()),
        rate_lookup=lambda p: rates.get(str(p).lower()),
        on_progress=on_progress, should_stop=should_stop,
        forbidden_dirs=list(cfg.library_roots),
    )


def run_chart_update(cfg: Config, force: bool = False) -> Optional[dict]:
    """Refresh the chart data if it is due (or forced). Returns None when nothing was done.
    Network errors propagate; callers that run this opportunistically should catch them."""
    from .charts import charts_due, update_charts

    with open_catalog(cfg) as cat:
        if not force and not charts_due(cat, cfg.chart_update_days):
            return None
        res = update_charts(cat, cfg.chart_url, cfg.chart_file, base_dir=cfg.base_dir)
        cat.compute_era_years()  # new chart years may date more tracks
        return res


def run_chart_load(cfg: Config) -> dict:
    """Import every chart file already in the charts folder (no network) and re-match the library.
    This is what makes a copied-along HouseKit folder work offline: the CSVs travel with it."""
    from .charts import load_local_charts

    with open_catalog(cfg) as cat:
        res = load_local_charts(cat, cfg.base_dir, hot100_file=cfg.chart_file)
        if res["sources"]:
            cat.compute_era_years()
        return res


def chart_startup_plan(cfg: Config) -> dict:
    """What opening the app should do about chart data: load the shipped files (nothing loaded yet,
    or loaded under older artist-matching rules) and/or refresh online (due)."""
    from .charts import charts_due, local_chart_files
    from .textnorm import NORM_VERSION

    with open_catalog(cfg) as cat:
        have = cat.chart_stats()["chart_songs"] > 0
        stale = have and cat.get_meta("charts_norm_version") != str(NORM_VERSION)
        due = charts_due(cat, cfg.chart_update_days)
    files = local_chart_files(cfg.base_dir, hot100_file=cfg.chart_file)
    return {"load": bool(files) and (not have or stale), "update": due, "stale": stale}


def run_chart_startup(cfg: Config) -> dict:
    """What the window does when it opens: load the shipped chart files if needed, then refresh
    online if due. Offline is fine; a failed download is reported, not raised."""
    plan = chart_startup_plan(cfg)
    out: dict = {"loaded": None, "updated": None, "error": ""}
    due = plan["update"]
    if plan["load"]:
        out["loaded"] = run_chart_load(cfg)
        due = False  # freshly loaded from files; the weekly refresh can wait for its next turn
    if due:
        try:
            out["updated"] = run_chart_update(cfg, force=False)
        except Exception as e:  # offline, blocked, or the source moved
            out["error"] = f"{type(e).__name__}: {e}"
    return out


def run_clear_catalog(cfg: Config, keep_charts: bool = True) -> dict:
    """Forget the indexed library (tracks, analysis, play history) so the app can be pointed at a
    new one. Chart data stays unless keep_charts is False; setlist pins live in a file and stay."""
    before = None
    with Catalog(cfg.db_path) as cat:
        before = cat.status([str(r) for r in cfg.library_roots]).tracks
        cat.clear_catalog(keep_charts=keep_charts)
        stats = cat.chart_stats()
    return {"tracks_removed": before, "chart_songs_kept": stats["chart_songs"], "chart_albums_kept": stats["chart_albums"]}
