"""Typer CLI. Thin adapter: loads config, calls core, prints. `--json` on every command."""
from __future__ import annotations

import csv
import json
import re
import sys
from datetime import date
from pathlib import Path
from typing import Any, Optional

import typer
from rich.console import Console
from rich.progress import BarColumn, Progress, TextColumn, TimeElapsedColumn, TimeRemainingColumn
from rich.table import Table

from . import __version__
from .catalog import FLAG_FIELDS, Catalog
from .charts import pop_label
from .config import Config, load_config
from .models import ProgressEvent, SlotSpec, to_jsonable

app = typer.Typer(help="housekit: build venue house-music playlists from your library and the band's setlist.", no_args_is_help=True)
setlist_app = typer.Typer(help="Parse a setlist and pin hand-mappings.", no_args_is_help=True)
app.add_typer(setlist_app, name="setlist")
charts_app = typer.Typer(help="Popularity data: Billboard Hot 100 / 200, radio spins and year-end genre charts. Fetched once, then used offline.", no_args_is_help=True)
app.add_typer(charts_app, name="charts")

console = Console()
if not console.is_terminal:
    console = Console(width=160)  # piped/redirected output: don't wrap tables at 80 columns
err = Console(stderr=True)


class State:
    cfg: Config
    json: bool = False


state = State()


def _version_callback(value: bool) -> None:
    if value:
        typer.echo(f"housekit {__version__}")
        raise typer.Exit()


@app.callback()
def _main(
    config: Optional[Path] = typer.Option(None, "--config", "-c", help="Path to housekit.toml (default: next to the app, then upward from cwd)."),
    json_out: bool = typer.Option(False, "--json", help="Emit structured JSON to stdout and suppress human output."),
    version: bool = typer.Option(False, "--version", help="Print version and exit.", callback=_version_callback, is_eager=True),
) -> None:
    state.cfg = load_config(config)
    state.json = json_out


def _emit(obj: object) -> None:
    typer.echo(json.dumps(to_jsonable(obj), indent=2, ensure_ascii=False))


def _catalog() -> Catalog:
    from .jobs import open_catalog

    return open_catalog(state.cfg)


def _mmss(seconds: Optional[float]) -> str:
    s = int(round(seconds or 0))
    return f"{s // 60}:{s % 60:02d}"


def _progress_printer(progress: Progress) -> "callable":
    tasks: dict[str, int] = {}
    labels = {"scan": "Walking", "tags": "Reading tags", "analyze": "Analyzing audio", "copy": "Copying"}

    def on_event(e: ProgressEvent) -> None:
        if e.phase not in tasks:
            tasks[e.phase] = progress.add_task(labels.get(e.phase, e.phase), total=e.total or None, message="")
        progress.update(tasks[e.phase], completed=e.done, total=e.total or None, message=e.message[:60])

    return on_event


# ------------------------------------------------------------------ index / status / export


@app.command()
def index(
    jobs: Optional[int] = typer.Option(None, "--jobs", "-j", help="Analysis worker processes (default: CPU count - 1)."),
    rescan: bool = typer.Option(False, "--rescan", help="Re-read tags for every file, even unchanged ones (picks up rule edits)."),
    limit: Optional[int] = typer.Option(None, "--limit", help="Only process an evenly spaced sample of N files (for a first look)."),
    no_analyze: bool = typer.Option(False, "--no-analyze", help="Tags only; skip audio analysis."),
    retry_errors: bool = typer.Option(False, "--retry-errors", help="Re-attempt files whose analysis failed before."),
) -> None:
    """Scan the library roots into the catalog. Incremental: re-running only touches new or changed files."""
    from .indexer import scan_library

    cfg = state.cfg
    missing_roots = [r for r in cfg.library_roots if not r.is_dir()]
    if missing_roots:
        err.print(f"[red]Library root not found:[/red] {', '.join(str(r) for r in missing_roots)}")
        raise typer.Exit(2)
    with _catalog() as cat:
        if state.json:
            res = scan_library(cat, cfg, None, rescan=rescan, limit=limit, analyze=not no_analyze, retry_errors=retry_errors, jobs=jobs)
            _emit(res)
            return
        console.print(f"Library roots: {', '.join(str(r) for r in cfg.library_roots)}")
        console.print(f"Catalog: {cfg.db_path}   workers: {jobs or cfg.effective_jobs}")
        with Progress(
            TextColumn("[bold]{task.description}"), BarColumn(), TextColumn("{task.completed}/{task.total}"),
            TimeElapsedColumn(), TimeRemainingColumn(), TextColumn("{task.fields[message]}", style="dim"),
            console=console, transient=False,
        ) as progress:
            res = scan_library(cat, cfg, _progress_printer(progress), rescan=rescan, limit=limit, analyze=not no_analyze, retry_errors=retry_errors, jobs=jobs)
        if res.interrupted:
            console.print("[yellow]Interrupted. Progress so far is saved; run `housekit index` again to resume.[/yellow]")
        console.print(
            f"Files seen {res.files_seen} | tags read {res.tags_read}, unchanged {res.tags_skipped}, errors {res.tag_errors} | "
            f"analyzed {res.analyzed}, errors {res.analysis_errors} | missing now {res.marked_missing} | dupe groups {res.dupe_groups}"
        )
        if res.errors:
            console.print(f"[dim]{len(res.errors)} problem file(s), first few:[/dim]")
            for line in res.errors[:8]:
                console.print(f"  [dim]{line}[/dim]")
        _print_status(cat)


def _print_status(cat: Catalog) -> None:
    st = cat.status([str(r) for r in state.cfg.library_roots])
    f = st.flags
    console.print(
        f"Catalog: {st.tracks} tracks ({st.analyzed} analyzed, {st.tracks - st.analyzed} not yet), {st.missing} missing, "
        f"last scan {st.last_scan or 'never'}"
    )
    console.print(
        f"Flags: explicit {f.get('explicit',0)} / unknown {f.get('explicit_unknown',0)} | junk {f.get('junk',0)} | holiday {f.get('holiday',0)} | "
        f"live {f.get('live',0)} | instrumental {f.get('instrumental',0)} | dupe groups {f.get('dupe_groups',0)} | hand corrections {f.get('hand_corrections',0)} | "
        f"popularity known {f.get('popular',0)} (Hot 100 run {f.get('charted',0)})"
    )


@app.command()
def status() -> None:
    """Catalog summary: counts, analysis coverage, flag totals."""
    with _catalog() as cat:
        if state.json:
            _emit(cat.status([str(r) for r in state.cfg.library_roots]))
        else:
            _print_status(cat)


@app.command()
def export(
    path: Path = typer.Argument(Path("library.csv"), help="Output CSV path."),
) -> None:
    """Export the whole catalog as CSV (artist, title, genre, year, energy, LUFS, flags, ...)."""
    with _catalog() as cat:
        rows = cat.export_rows()
    if state.cfg.is_inside_library(path):
        err.print("[red]Refusing to write inside a library root.[/red]")
        raise typer.Exit(2)
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()) if rows else ["id"])
        w.writeheader()
        w.writerows(rows)
    if state.json:
        _emit({"path": str(path), "rows": len(rows)})
    else:
        console.print(f"Wrote {len(rows)} rows to {path}")


# ------------------------------------------------------------------ search / flag


def _track_table(tracks, title: str = "") -> Table:
    t = Table(title=title or None, show_lines=False, pad_edge=False)
    for col in ("id", "artist", "title", "album", "year", "genre", "len", "energy", "bpm", "LUFS", "pop", "flags"):
        t.add_column(col, overflow="fold" if col in ("artist", "title", "album") else "ellipsis")
    for tr in tracks:
        flags = []
        if tr.explicit is True:
            flags.append("EXPLICIT")
        elif tr.explicit is None:
            flags.append("expl?")
        for f in ("junk", "holiday", "live", "instrumental"):
            if getattr(tr, f):
                flags.append(f)
        if tr.missing:
            flags.append("MISSING")
        t.add_row(
            str(tr.id), tr.artist, tr.title + (f" [{tr.version}]" if tr.version else ""), tr.album, str(tr.year or ""),
            tr.genre_norm, _mmss(tr.duration), f"{tr.energy:.2f}" if tr.energy is not None else "-",
            f"{tr.tempo:.0f}" if tr.tempo is not None else "-",
            f"{tr.lufs:.1f}" if tr.lufs is not None else "-", pop_label(tr.chart_peak, tr.chart_weeks, tr.pop_source, tr.popularity), " ".join(flags),
        )
    return t


@app.command()
def search(
    query: str = typer.Argument(..., help="Words to find in artist, title, or album (all must match)."),
    limit: int = typer.Option(40, "--limit", "-n"),
) -> None:
    """Search the catalog. Use it to find track ids for `flag` and `setlist resolve`."""
    with _catalog() as cat:
        tracks = cat.search(query, limit=limit)
    if state.json:
        _emit(tracks)
    else:
        console.print(_track_table(tracks, f"{len(tracks)} result(s) for '{query}'"))


@app.command()
def flag(
    track_id: int = typer.Argument(..., help="Track id (see `search`)."),
    settings: list[str] = typer.Argument(..., help="One or more of explicit=yes|no|unknown junk=yes|no holiday=yes|no live=yes|no instrumental=yes|no genre=<Name>|auto year=1977|auto"),
) -> None:
    """Hand-correct content flags, genre or original year on a track. Corrections survive re-indexing."""
    yes = {"yes", "y", "true", "1", "on"}
    no = {"no", "n", "false", "0", "off"}
    with _catalog() as cat:
        if cat.get_track(track_id) is None:
            err.print(f"[red]No track with id {track_id}[/red]")
            raise typer.Exit(2)
        for s in settings:
            k, _, v = s.partition("=")
            k, v = k.strip().lower(), v.strip()
            if k == "genre":
                cat.set_genre(track_id, None if v.lower() in ("auto", "") else v)
            elif k == "year":
                if v.lower() in ("auto", ""):
                    cat.set_year(track_id, None)
                elif v.isdigit() and 1900 <= int(v) <= 2100:
                    cat.set_year(track_id, int(v))
                else:
                    err.print(f"[red]Bad year: {v}[/red]")
                    raise typer.Exit(2)
            elif k in FLAG_FIELDS:
                vl = v.lower()
                if vl in ("unknown", "auto", ""):
                    cat.set_flag(track_id, k, None)
                elif vl in yes:
                    cat.set_flag(track_id, k, True)
                elif vl in no:
                    cat.set_flag(track_id, k, False)
                else:
                    err.print(f"[red]Bad value for {k}: {v}[/red]")
                    raise typer.Exit(2)
            else:
                err.print(f"[red]Unknown setting {k}. Use one of: {', '.join(FLAG_FIELDS)}, genre, year[/red]")
                raise typer.Exit(2)
        tr = cat.get_track(track_id)
    if state.json:
        _emit(tr)
    else:
        console.print(_track_table([tr]))


# ------------------------------------------------------------------ setlist


def _print_parse(parse) -> None:
    t = Table(title=f"Setlist: {parse.source}  (orientation: {parse.orientation}, threshold {parse.threshold:.0f})", pad_edge=False)
    t.add_column("line"); t.add_column("set"); t.add_column("setlist entry", overflow="fold"); t.add_column("status")
    t.add_column("score"); t.add_column("matched library track", overflow="fold"); t.add_column("id")
    for m in parse.entries:
        style = {"matched": "green", "override": "cyan", "unresolved": "bold red", "not_in_library": "yellow"}[m.status]
        t.add_row(str(m.entry.line_no), m.entry.set_name[:10], m.entry.raw, f"[{style}]{m.status}[/{style}]",
                  f"{m.score:.0f}", m.track_display, str(m.track_id or ""))
    console.print(t)
    if parse.unresolved:
        console.print(f"[bold red]{len(parse.unresolved)} unresolved.[/bold red] Their titles are still excluded by text, but pin them so the whole song family is:")
        for m in parse.unresolved:
            console.print(f"  line {m.entry.line_no}: '{m.entry.raw}'")
            for tid, disp, sc in m.candidates[:4]:
                console.print(f"      {sc:5.1f}  id {tid:<6} {disp}")
            console.print(f"      -> housekit setlist resolve \"{m.entry.raw}\" --track <id>     (or --not-in-library)")


@setlist_app.command("parse")
def setlist_parse(
    path: Optional[Path] = typer.Argument(None, help="Setlist .pdf, .docx or .txt, or a folder with one file per set. Default: the standing setlist from housekit.toml."),
) -> None:
    """Match every setlist line to the library and show confidence; unresolved lines are listed with candidates."""
    from .setlist import parse_setlist

    path = path or state.cfg.setlist_path
    if not path.exists():
        err.print(f"[red]No setlist at {path}. Drop the set files there or pass a path.[/red]")
        raise typer.Exit(2)
    with _catalog() as cat:
        parse = parse_setlist(path, cat, state.cfg)
    if state.json:
        _emit(parse)
    else:
        _print_parse(parse)


@setlist_app.command("resolve")
def setlist_resolve(
    entry: str = typer.Argument(..., help="The setlist line, exactly as shown by `setlist parse`."),
    track: Optional[list[int]] = typer.Option(None, "--track", "-t", help="Catalog track id this line refers to. Repeat for a medley line."),
    not_in_library: bool = typer.Option(False, "--not-in-library", help="The band plays this but you don't own it; stop flagging it."),
    note: str = typer.Option("", "--note"),
) -> None:
    """Pin a hand-mapping for a setlist line into setlist_overrides.toml."""
    from .setlist import save_override

    tracks = list(track or [])
    if not tracks and not not_in_library:
        err.print("[red]Give --track <id> (repeatable) or --not-in-library.[/red]")
        raise typer.Exit(2)
    disps = []
    if tracks:
        with _catalog() as cat:
            for tid in tracks:
                tr = cat.get_track(tid)
                if tr is None:
                    err.print(f"[red]No track with id {tid}[/red]")
                    raise typer.Exit(2)
                disps.append(tr.display)
    else:
        disps.append("(not in library)")
    save_override(state.cfg.overrides_path, entry, track_ids=tracks, not_in_library=not_in_library, note=note)
    if state.json:
        _emit({"entry": entry, "track_ids": tracks, "not_in_library": not_in_library, "file": str(state.cfg.overrides_path)})
    else:
        console.print(f"Pinned '{entry}' -> {' + '.join(disps)}  ({state.cfg.overrides_path})")


# ------------------------------------------------------------------ charts


def _print_chart_stats(stats: dict) -> None:
    srcs = ", ".join(f"{k} {v}" for k, v in sorted(stats.get("per_source", {}).items())) or "none"
    console.print(
        f"Popularity data: {stats['chart_songs']} songs + {stats['chart_albums']} albums stored ({srcs}). "
        f"Library tracks with known popularity: {stats['popular_tracks']} ({stats['charted_tracks']} with a Hot 100 run, "
        f"{stats['top40_tracks']} top-40)."
    )


@charts_app.command("import")
def charts_import(
    path: Path = typer.Argument(..., exists=True, help="A chart file: a Billboard Hot 100 / Billboard 200 weekly export, a per-song CSV with title, artist and peak columns, a radio spins file, or the scraped year-end file."),
    source: Optional[str] = typer.Option(None, "--source", help="Dataset name: hot100, hot100-archive, billboard200, radio, yearend, or your own. Re-importing a name replaces it. Default: guessed from the file name, else hot100."),
    kind: Optional[str] = typer.Option(None, "--kind", help="How to read the file: hot100 | billboard200 | radio | yearend. Default: the source's kind, else hot100."),
) -> None:
    """Load a popularity dataset from a file and attach it to every matching library track."""
    from datetime import datetime

    from .charts import META_UPDATED, SOURCES, guess_source, read_source

    name = source or guess_source(path)
    kind = kind or (SOURCES[name]["kind"] if name in SOURCES else "hot100")
    with _catalog() as cat:
        try:
            n = cat.import_chart_rows(read_source(kind, path), source=name, kind=kind)
        except (ValueError, KeyError) as e:
            err.print(f"[red]{e}[/red]")
            raise typer.Exit(2)
        matched = cat.apply_chart_data()
        if name == "hot100":
            cat.set_meta(META_UPDATED, datetime.now().isoformat(timespec="seconds"))
        cat.compute_era_years()
        stats = cat.chart_stats()
    if state.json:
        _emit({"source": name, "kind": kind, "records": n, "songs_imported": n, "tracks_matched": matched, **stats})
    else:
        console.print(f"Imported {n} {'albums' if kind == 'billboard200' else 'songs'} from {path.name} as '{name}'.")
        _print_chart_stats(stats)


@charts_app.command("fetch")
def charts_fetch(
    names: Optional[list[str]] = typer.Argument(None, help="Sources to download: hot100, hot100-archive, billboard200, radio, or 'all' (default). The year-end charts are scraped instead: `charts scrape`."),
) -> None:
    """Download popularity sources into the charts folder and import them. Needs the network once; builds never do."""
    from datetime import datetime

    from .charts import META_UPDATED, SOURCES, fetch_source

    wanted = list(names or ["all"])
    if "all" in wanted:
        wanted = [n for n, s in SOURCES.items() if s["url"]]
    unknown = [n for n in wanted if n not in SOURCES or not SOURCES[n]["url"]]
    if unknown:
        err.print(f"[red]Unknown or non-downloadable source(s): {', '.join(unknown)}. Choose from {', '.join(n for n, s in SOURCES.items() if s['url'])}.[/red]")
        raise typer.Exit(2)
    cfg = state.cfg
    results = []
    with _catalog() as cat:
        for n in wanted:
            if not state.json:
                console.print(f"Downloading {SOURCES[n]['label']} ...")
            try:
                if n == "hot100":
                    results.append(fetch_source(cat, n, cfg.base_dir, url=cfg.chart_url, dest=cfg.chart_file))
                else:
                    results.append(fetch_source(cat, n, cfg.base_dir))
            except Exception as e:
                results.append({"source": n, "error": f"{type(e).__name__}: {e}"})
        matched = cat.apply_chart_data()
        if any(r.get("source") == "hot100" and "error" not in r for r in results):
            cat.set_meta(META_UPDATED, datetime.now().isoformat(timespec="seconds"))
        cat.compute_era_years()
        stats = cat.chart_stats()
    if state.json:
        _emit({"sources": results, "tracks_matched": matched, **stats})
        return
    for r in results:
        if "error" in r:
            console.print(f"  [yellow]{r['source']}: {r['error']}[/yellow]")
        else:
            console.print(f"  {r['source']}: {r['records']} records, {r['bytes']/1e6:.1f} MB -> {r['file']}")
    _print_chart_stats(stats)
    if any("error" in r for r in results):
        raise typer.Exit(1)


@charts_app.command("load")
def charts_load() -> None:
    """Import every chart file already in the charts folder. No network: this is what a copied HouseKit folder uses."""
    from .jobs import run_chart_load

    res = run_chart_load(state.cfg)
    if state.json:
        _emit(res)
        return
    if not res["sources"]:
        console.print("No chart files in the charts folder. `housekit charts fetch` (and `charts scrape`) download them.")
        return
    for r in res["sources"]:
        console.print(f"  [yellow]{r['source']}: {r['error']}[/yellow]" if "error" in r else f"  {r['source']}: {r['records']} records from {Path(r['file']).name}")
    _print_chart_stats(res)


@charts_app.command("scrape")
def charts_scrape(
    years: Optional[str] = typer.Option(None, "--years", help="Year range, e.g. 1970-1999 (default: 1958 to this year)."),
    chart: Optional[list[str]] = typer.Option(None, "--chart", help="billboard.com year-end chart slug, e.g. hot-country-songs (repeatable). Default: every known genre chart."),
    delay: float = typer.Option(1.5, "--delay", help="Seconds between page requests."),
    no_import: bool = typer.Option(False, "--no-import", help="Only write the CSV; don't import it."),
) -> None:
    """Pull Billboard year-end genre charts (country, rock, alternative, R&B/hip-hop, dance, adult contemporary, pop airplay)
    from billboard.com into the charts folder, then import them. Resumable; needs the network once."""
    from datetime import date

    from .charts import SOURCES, YEAREND_CHARTS, import_source, scrape_yearend
    from .playlists import parse_range

    cfg = state.cfg
    charts_sel = YEAREND_CHARTS
    if chart:
        bad = [c for c in chart if c not in YEAREND_CHARTS]
        if bad:
            err.print(f"[red]Unknown chart(s): {', '.join(bad)}. Known: {', '.join(YEAREND_CHARTS)}[/red]")
            raise typer.Exit(2)
        charts_sel = {c: YEAREND_CHARTS[c] for c in chart}
    y_lo, y_hi = parse_range(years, as_int=True) if years else (None, None)
    yr = range(int(y_lo or 1958), int(y_hi or date.today().year) + 1)
    dest = cfg.base_dir / SOURCES["yearend"]["file"]

    def prog(done: int, total: int, msg: str) -> None:
        if not state.json:
            console.print(f"  {done}/{total}  {msg}", highlight=False)

    res = scrape_yearend(dest, charts_sel, yr, delay=delay, on_progress=prog)
    out = {"scrape": res}
    if not no_import and dest.exists():
        with _catalog() as cat:
            out["import"] = import_source(cat, "yearend", dest)
            out["tracks_matched"] = cat.apply_chart_data()
            cat.compute_era_years()
            out.update(cat.chart_stats())
    if state.json:
        _emit(out)
        return
    console.print(f"Fetched {res['fetched']} pages, {res['rows']} rows written, {res['errors']} problem pages -> {res['file']}")
    if "import" in out:
        _print_chart_stats(out)


@charts_app.command("status")
def charts_status() -> None:
    """What popularity data is loaded, and how much of the library it covers."""
    from .charts import local_chart_files

    with _catalog() as cat:
        stats = cat.chart_stats()
        stats["updated_at"] = cat.get_meta("charts_updated_at")
    stats["files"] = {n: str(p) for n, p in local_chart_files(state.cfg.base_dir, state.cfg.chart_file).items()}
    if state.json:
        _emit(stats)
    else:
        _print_chart_stats(stats)
        console.print(f"Chart files on disk: {', '.join(stats['files']) or 'none'} | last online refresh: {stats['updated_at'] or 'never'}")


@charts_app.command("update")
def charts_update(
    force: bool = typer.Option(False, "--force", help="Refresh even if the data is newer than the configured interval."),
) -> None:
    """Download the latest Hot 100 and Billboard 200 files and re-match the library. Needs network; skip it when offline."""
    from .jobs import run_chart_update

    try:
        res = run_chart_update(state.cfg, force=force)
    except Exception as e:
        err.print(f"[red]Chart refresh failed: {type(e).__name__}: {e}[/red]")
        raise typer.Exit(1)
    if state.json:
        _emit(res or {"updated": False})
    elif res is None:
        console.print(f"Chart data is up to date (refresh interval {state.cfg.chart_update_days} days; use --force to refresh anyway).")
    else:
        for r in res.get("sources", []):
            console.print(f"  [yellow]{r['source']}: {r['error']}[/yellow]" if "error" in r else f"  {r['source']}: {r['records']} records")
        _print_chart_stats(res)


# ------------------------------------------------------------------ build


@app.command()
def build(
    setlist: Optional[Path] = typer.Option(None, "--setlist", "-s", help="Setlist .pdf, .docx or .txt, or a folder with one file per set. Default: the standing setlist from housekit.toml ([setlist] path)."),
    out: Optional[Path] = typer.Option(None, "--out", "-o", help="Gig folder name or path (bare names go under the gigs dir). Default: '<today> Gig'."),
    venue: str = typer.Option("", "--venue"),
    gig_date: Optional[str] = typer.Option(None, "--date", help="YYYY-MM-DD (default today)."),
    clean: Optional[bool] = typer.Option(None, "--clean/--no-clean", help="Family-safe: drop explicit AND unknown-rating tracks."),
    allow_holiday: Optional[bool] = typer.Option(None, "--allow-holiday/--no-holiday", help="Allow holiday songs outside the holiday months."),
    doors: Optional[float] = typer.Option(None, "--doors", help="Minutes for slot 1."),
    break1: Optional[float] = typer.Option(None, "--break1", help="Minutes for slot 2."),
    break2: Optional[float] = typer.Option(None, "--break2", help="Minutes for slot 3."),
    post: Optional[float] = typer.Option(None, "--post", help="Minutes for slot 4."),
    exclude_last: Optional[int] = typer.Option(None, "--exclude-last", help="Skip songs used at the last N gigs (0 = off)."),
    top: Optional[int] = typer.Option(None, "--top", help="Only songs that peaked at this Hot 100 position or better (needs `charts import`). 0 = off."),
    seed: Optional[int] = typer.Option(None, "--seed", help="Random seed; change it to get a different but equally valid plan."),
    dry_run: bool = typer.Option(False, "--dry-run", help="Print the plan; copy nothing, log nothing."),
    overwrite: bool = typer.Option(False, "--overwrite", help="Replace an existing gig folder's slot folders."),
    no_log: bool = typer.Option(False, "--no-log", help="Don't record this build in play history."),
) -> None:
    """Pick tracks for every slot, copy them into per-slot folders, and write the playlists."""
    from .jobs import BuildRequest, run_build
    from .output import plan_text

    cfg = state.cfg
    req = BuildRequest(
        setlist=setlist, out=out, venue=venue, gig_date=gig_date, clean=clean, allow_holiday=allow_holiday,
        slot_minutes=[doors, break1, break2, post], exclude_last=exclude_last, top=top, seed=seed,
        dry_run=dry_run, overwrite=overwrite, log=not no_log,
    )
    try:
        if state.json or dry_run:
            plan, result, out_path = run_build(cfg, req)
        else:
            with Progress(TextColumn("[bold]{task.description}"), BarColumn(), TextColumn("{task.completed}/{task.total}"),
                          TextColumn("{task.fields[message]}", style="dim"), console=console) as progress:
                plan, result, out_path = run_build(cfg, req, _progress_printer(progress))
    except (FileNotFoundError, FileExistsError, ValueError, RuntimeError) as e:
        err.print(f"[red]{e}[/red]")
        raise typer.Exit(2)

    if state.json:
        _emit({"plan": plan, "output": result, "dry_run": dry_run})
        return
    console.print(plan_text(plan, cfg), highlight=False, markup=False)
    if dry_run:
        console.print("[yellow]Dry run: nothing copied, nothing logged.[/yellow]")
    elif result:
        console.print(f"[green]Copied {result.copied} files ({result.bytes_copied/1e9:.2f} GB) to {result.out_dir}[/green]")
        console.print(f"Master playlist: {result.master_playlist}")
        if result.skipped:
            console.print(f"[yellow]{len(result.skipped)} file(s) skipped:[/yellow]")
            for s in result.skipped:
                console.print(f"  {s}")
    if not plan.all_filled:
        console.print("[bold yellow]One or more slots are outside tolerance. See WARNINGS above.[/bold yellow]")


# ------------------------------------------------------------------ custom playlists


@app.command()
def playlist(
    name: str = typer.Argument(..., help="Playlist name; also the output folder name (under the playlists dir unless a path is given)."),
    artist: Optional[list[str]] = typer.Option(None, "--artist", "-a", help="Artist (substring, repeatable)."),
    genre: Optional[list[str]] = typer.Option(None, "--genre", "-g", help="Normalized genre, e.g. 'Yacht Rock' (repeatable)."),
    decade: Optional[list[str]] = typer.Option(None, "--decade", "-d", help="Decade: 50, 60s, 1970 ... (repeatable). With --years too, a song passes if it is in any decade OR the range."),
    years: Optional[str] = typer.Option(None, "--years", help="Year range: 1975-1985, 1980, -1989, 2000-."),
    energy: Optional[str] = typer.Option(None, "--energy", help="mellow | laid-back | upbeat | 'high energy' | full-on, or a 0-1 range like 0.3-0.6."),
    top: int = typer.Option(0, "--top", help="Only songs that peaked at this Hot 100 position or better."),
    search: str = typer.Option("", "--search", help="Words that must all appear in the title or album."),
    count: Optional[int] = typer.Option(None, "--count", "-n", help="How many songs (default 20 unless --minutes is given)."),
    minutes: Optional[float] = typer.Option(None, "--minutes", "-m", help="Fill to this many minutes instead of a count."),
    order: str = typer.Option("rank", "--order", help="rank (most popular first) | energy | year | alpha | random (shuffled, no two songs by the same act in a row where the mix allows)."),
    clean: bool = typer.Option(False, "--clean", help="Family-safe: no explicit or unknown-rating tracks (pre-1985 trusted)."),
    allow_live: bool = typer.Option(False, "--allow-live", help="Include live recordings (excluded by default)."),
    allow_holiday: bool = typer.Option(False, "--allow-holiday"),
    fill: bool = typer.Option(False, "--fill", help="If an artist or search list comes up short, top it up with similar songs by other artists (same era, genres and energy, best known first; marked '(similar)')."),
    no_dedupe: bool = typer.Option(False, "--no-dedupe", help="Keep every copy of a song instead of the best one."),
    seed: Optional[int] = typer.Option(None, "--seed", help="Seed for --order random."),
    no_copy: bool = typer.Option(False, "--no-copy", help="Don't copy files; write an M3U8 that points at the library files in place."),
    dry_run: bool = typer.Option(False, "--dry-run", help="Print the list; write nothing."),
    overwrite: bool = typer.Option(False, "--overwrite", help="Replace an existing playlist folder."),
) -> None:
    """Build a custom playlist from the catalog, e.g. the top 20 Steely Dan songs, ranked by chart popularity."""
    from .jobs import run_playlist
    from .output import playlist_text
    from .playlists import ORDERS, PlaylistSpec, parse_decade, parse_energy, parse_range

    cfg = state.cfg
    if order not in ORDERS:
        err.print(f"[red]--order must be one of {', '.join(ORDERS)}[/red]")
        raise typer.Exit(2)
    try:
        y_lo, y_hi = parse_range(years, as_int=True)
        e_lo, e_hi = parse_energy(energy)
        decades = sorted({parse_decade(d) for d in (decade or [])})
    except ValueError:
        err.print("[red]Bad --years, --decade or --energy. Examples: --years 1975-1985, --decade 70s, --energy upbeat, --energy 0.3-0.6[/red]")
        raise typer.Exit(2)
    spec = PlaylistSpec(
        name=name, artists=list(artist or []), genres=list(genre or []), decades=decades, year_min=y_lo, year_max=y_hi,
        energy_min=e_lo, energy_max=e_hi, top=top, search=search,
        count=count if (count is not None or minutes is None) else None, minutes=minutes,
        order=order, clean=clean, allow_live=allow_live, allow_holiday=allow_holiday, dedupe=not no_dedupe, seed=seed,
        fill=fill,
    )
    if spec.count is None and spec.minutes is None:
        spec.count = 20

    try:
        if state.json or dry_run:
            result, written, out_path = run_playlist(cfg, spec, Path(name), copy=not no_copy, dry_run=dry_run, overwrite=overwrite)
        else:
            with Progress(TextColumn("[bold]{task.description}"), BarColumn(), TextColumn("{task.completed}/{task.total}"),
                          TextColumn("{task.fields[message]}", style="dim"), console=console) as progress:
                result, written, out_path = run_playlist(cfg, spec, Path(name), copy=not no_copy, dry_run=dry_run,
                                                         overwrite=overwrite, on_progress=_progress_printer(progress))
    except (FileExistsError, ValueError, RuntimeError) as e:
        err.print(f"[red]{e}[/red]")
        raise typer.Exit(2)
    if state.json:
        _emit({"playlist": result, "output": written, "dry_run": dry_run})
        return
    console.print(playlist_text(result), highlight=False, markup=False)
    if dry_run:
        console.print("[yellow]Dry run: nothing written.[/yellow]")
    elif not result.tracks:
        console.print("[yellow]No songs matched, nothing written.[/yellow]")
    elif written:
        what = f"Copied {written.copied} files ({written.bytes_copied/1e9:.2f} GB) to" if not no_copy else "Wrote playlist pointing at the library into"
        console.print(f"[green]{what} {written.out_dir}[/green]")
        console.print(f"Playlist: {written.master_playlist}")
        for s in written.skipped:
            console.print(f"  [yellow]skipped {s}[/yellow]")


# ------------------------------------------------------------------ convert


@app.command()
def convert(
    inputs: list[Path] = typer.Argument(..., exists=True, help="Audio files and/or folders (a gig or playlist folder works)."),
    to: str = typer.Option("mp3-320", "--to", help="mp3-320 | mp3-256 | mp3-192 | mp3-128 | m4a-256 | flac | wav | ogg"),
    out: Optional[Path] = typer.Option(None, "--out", "-o", help="Write converted copies here instead. Default: convert in place, replacing the files in their folder."),
    match_loudness: Optional[float] = typer.Option(None, "--match-loudness", help="Bring every file to this LUFS, e.g. -16."),
    flat: bool = typer.Option(False, "--flat", help="With --out: put everything in one folder instead of mirroring the source layout."),
    overwrite: bool = typer.Option(False, "--overwrite", help="Re-encode files that are already in the target format / whose output already exists."),
    jobs: int = typer.Option(0, "--jobs", "-j", help="Parallel encodes (default: half the CPUs)."),
) -> None:
    """Convert audio with ffmpeg, in place: a gig or playlist folder ends up holding the converted files and its
    .m3u8 playlists point at them. Tags and artwork carry over. Library folders are refused; use --out for a copy."""
    from .convert import FORMATS, ConvertOptions
    from .jobs import run_convert

    if to not in FORMATS:
        err.print(f"[red]--to must be one of {', '.join(FORMATS)}[/red]")
        raise typer.Exit(2)
    opts = ConvertOptions(fmt=to, match_loudness=match_loudness, keep_structure=not flat, overwrite=overwrite, jobs=jobs)
    try:
        if state.json:
            res = run_convert(state.cfg, list(inputs), opts, out)
        else:
            with Progress(TextColumn("[bold]{task.description}"), BarColumn(), TextColumn("{task.completed}/{task.total}"),
                          TextColumn("{task.fields[message]}", style="dim"), console=console) as progress:
                res = run_convert(state.cfg, list(inputs), opts, out, on_progress=_progress_printer(progress))
    except (RuntimeError, ValueError) as e:
        err.print(f"[red]{e}[/red]")
        raise typer.Exit(2)
    if state.json:
        _emit({"out_dir": res.out_dir, "converted": res.converted, "skipped": res.skipped,
               "errors": [{"file": str(i.src), "detail": i.detail} for i in res.errors], "playlists": res.playlists})
        return
    console.print(f"Converted {res.converted} file(s) ({res.bytes_written/1e6:.0f} MB) to {FORMATS[to]['label']} "
                  f"{'in place in' if out is None else 'into'} {res.out_dir}"
                  + (f", skipped {res.skipped} already done" if res.skipped else ""))
    for p in res.playlists:
        console.print(f"  playlist: {p}")
    for i in res.errors:
        console.print(f"  [yellow]failed: {i.src.name}: {i.detail}[/yellow]")


# ------------------------------------------------------------------ setup / gui


@app.command("clear-catalog")
def clear_catalog(
    yes: bool = typer.Option(False, "--yes", "-y", help="Don't ask for confirmation."),
    charts: bool = typer.Option(False, "--charts", help="Also drop the imported chart data (kept by default)."),
) -> None:
    """Forget the indexed library (tracks, analysis, play history) before pointing HouseKit at a different music folder.
    Chart data and setlist pins are kept. Follow with `housekit setup --force --root ...` and `housekit index`."""
    from .jobs import run_clear_catalog

    if not yes and not typer.confirm(f"Forget every indexed track in {state.cfg.db_path}?", default=False):
        raise typer.Exit(1)
    res = run_clear_catalog(state.cfg, keep_charts=not charts)
    if state.json:
        _emit(res)
    else:
        console.print(f"Removed {res['tracks_removed']} tracks. Kept {res['chart_songs_kept']} chart songs and {res['chart_albums_kept']} chart albums.")
        console.print("Next: point the Setup tab (or `housekit setup --force --root <folder>`) at the new music and run `housekit index`.")


@app.command()
def setup(
    root: Optional[list[Path]] = typer.Option(None, "--root", "-r", help="Music folder (repeatable). Default: folders with audio next to the app."),
    gigs: Optional[Path] = typer.Option(None, "--gigs", help="Gig output folder (default: <app>/gigs)."),
    playlists: Optional[Path] = typer.Option(None, "--playlists", help="Playlists folder (default: <app>/playlists)."),
    setlists: Optional[Path] = typer.Option(None, "--setlists", help="Standing setlist folder (default: <app>/setlists)."),
    chart_days: int = typer.Option(7, "--chart-days", help="Refresh chart data every N days when the app opens (0 = never)."),
    force: bool = typer.Option(False, "--force", help="Overwrite an existing housekit.toml."),
) -> None:
    """Write housekit.toml for this folder: point HouseKit at your music and go."""
    from .config import CONFIG_FILENAME, app_dir, detect_library_roots, write_config

    base = app_dir()
    if (base / CONFIG_FILENAME).exists() and not force:
        err.print(f"[yellow]{base / CONFIG_FILENAME} already exists. Use --force to rewrite it.[/yellow]")
        raise typer.Exit(2)
    roots = list(root or []) or detect_library_roots(base)
    if not roots:
        err.print("[red]No music folders found next to the app. Pass --root <folder>.[/red]")
        raise typer.Exit(2)
    missing = [str(r) for r in roots if not r.is_dir()]
    if missing:
        err.print(f"[red]Folder not found: {', '.join(missing)}[/red]")
        raise typer.Exit(2)
    path = write_config(base, roots, gigs, playlists, setlists, chart_days)
    if state.json:
        _emit({"config": str(path), "roots": [str(r) for r in roots]})
    else:
        console.print(f"Wrote {path}")
        console.print("Music folders: " + ", ".join(str(r) for r in roots))
        console.print("Next: `housekit index` to catalog the library (takes a while the first time).")


@app.command()
def gui(
    selftest: bool = typer.Option(False, "--selftest", hidden=True, help="Open the window, close it after two seconds, report errors."),
) -> None:
    """Open the desktop window."""
    from .gui import run, selftest as gui_selftest

    if selftest:
        gui_selftest()
        typer.echo("GUI selftest OK")
        return
    run()


@app.command()
def player(
    folder: Optional[Path] = typer.Argument(None, help="A gig folder, a playlist folder, any folder of audio, or an .m3u8 file. Default: choose in the window."),
    selftest: bool = typer.Option(False, "--selftest", hidden=True),
) -> None:
    """Open the standalone player: cue lists from the folder, GO / pause / stop / panic, gapless follow-ons with
    crossfades, pads, a big show display. Needs no catalog."""
    from .player_app import run, selftest as player_selftest

    if selftest:
        player_selftest(folder)
        typer.echo("Player selftest OK")
        return
    run(folder)


@app.command()
def doctor() -> None:
    """Check the install: ffmpeg, config, catalog, chart data. Run this first when something looks wrong."""
    import shutil
    import subprocess

    from .config import app_dir, is_frozen

    cfg = state.cfg
    info: dict[str, Any] = {
        "version": __version__,
        "frozen": is_frozen(),
        "app_dir": str(app_dir()),
        "python": sys.version.split()[0],
        "config_file": str(cfg.config_path) if cfg.config_path else None,
        "library_roots": {str(r): r.is_dir() for r in cfg.library_roots},
        "db": str(cfg.db_path),
        "db_exists": cfg.db_path.exists(),
    }
    for tool in ("ffmpeg", "ffprobe"):
        path = shutil.which(tool)
        ver = None
        if path:
            try:
                out = subprocess.run([path, "-version"], capture_output=True, text=True, timeout=20,
                                     creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)).stdout
                ver = out.splitlines()[0][:60] if out else "?"
            except Exception as e:
                ver = f"error: {e}"
        info[tool] = {"path": path, "version": ver}
    try:
        with _catalog() as cat:
            st = cat.status([str(r) for r in cfg.library_roots])
            info["tracks"] = st.tracks
            info["analyzed"] = st.analyzed
            info["last_scan"] = st.last_scan
            cs = cat.chart_stats()
            info["chart_songs"] = cs["chart_songs"]
            info["chart_sources"] = cs["per_source"]
            info["charted_tracks"] = cs["charted_tracks"]
            info["popular_tracks"] = cs["popular_tracks"]
            info["charts_updated_at"] = cat.get_meta("charts_updated_at")
    except Exception as e:
        info["catalog_error"] = f"{type(e).__name__}: {e}"
    info["setlist_path"] = {str(cfg.setlist_path): cfg.setlist_path.exists()}
    problems = []
    if not info["ffmpeg"]["path"]:
        problems.append("ffmpeg not found: indexing new music will fail (put ffmpeg.exe in the app's ffmpeg\\ folder or on PATH).")
    if not info["config_file"]:
        problems.append("no housekit.toml: run `housekit setup` or use the Setup tab.")
    for r, ok in info["library_roots"].items():
        if not ok:
            problems.append(f"library folder missing: {r}")
    if not info["db_exists"]:
        problems.append("no catalog yet: run `housekit index`.")
    info["problems"] = problems
    if state.json:
        _emit(info)
        return
    for k, v in info.items():
        if k != "problems":
            console.print(f"{k:>18}: {v}")
    if problems:
        console.print("[yellow]Problems:[/yellow]")
        for p in problems:
            console.print(f"  - {p}")
    else:
        console.print("[green]Everything looks fine.[/green]")


# ------------------------------------------------------------------ history


@app.command()
def history(
    forget: Optional[int] = typer.Option(None, "--forget", help="Delete a gig record by id so its songs stop being excluded."),
) -> None:
    """Show gigs recorded in play history (these feed the recent-repeat exclusion)."""
    from .history import list_gigs

    with _catalog() as cat:
        if forget is not None:
            ok = cat.delete_gig(forget)
            if state.json:
                _emit({"deleted": ok, "id": forget})
            else:
                console.print("Deleted." if ok else f"No gig with id {forget}.")
            return
        gigs = list_gigs(cat)
    if state.json:
        _emit(gigs)
        return
    t = Table(title="Play history (newest first)", pad_edge=False)
    for c in ("id", "date", "name", "venue", "tracks", "logged", "folder"):
        t.add_column(c, overflow="fold")
    for g in gigs:
        t.add_row(str(g.id), g.date, g.name, g.venue, str(g.track_count), g.logged_at, g.out_dir)
    console.print(t)
    console.print(f"The last {state.cfg.exclude_last_gigs} gig(s) are excluded from new builds (config history.exclude_last).")


@app.command()
def config() -> None:
    """Show the resolved configuration (paths, slots, weights)."""
    cfg = state.cfg
    info = {
        "config_file": str(cfg.config_path) if cfg.config_path else None,
        "library_roots": [str(r) for r in cfg.library_roots],
        "db": str(cfg.db_path),
        "gigs_dir": str(cfg.output_dir),
        "playlists_dir": str(cfg.playlists_dir),
        "setlist": str(cfg.setlist_path),
        "overrides": str(cfg.overrides_path),
        "workers": cfg.effective_jobs,
        "slots": [vars(s) for s in cfg.slots],
        **cfg.settings_summary(),
        "exclude_last_gigs": cfg.exclude_last_gigs,
        "clean_default": cfg.clean_default,
    }
    if state.json:
        _emit(info)
    else:
        console.print_json(json.dumps(info))


if __name__ == "__main__":
    app()
