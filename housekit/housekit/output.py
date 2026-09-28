"""Gig folder output: numbered copies per slot, M3U8 per slot, a master M3U8, manifest.json.

This is the only module that copies files. It never writes inside a library root.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import unicodedata
from datetime import datetime
from pathlib import Path
from typing import Optional

from . import __version__
from .charts import pop_label
from .config import Config
from .fsutil import longpath
from .models import BuildPlan, OutputResult, ProgressCallback, ProgressEvent, to_jsonable

_ILLEGAL = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_RESERVED = {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))}


def sanitize_filename(name: str, max_len: int = 120) -> str:
    """Safe on Windows and macOS: strips illegal characters, reserved names, trailing dots/spaces."""
    s = unicodedata.normalize("NFC", name or "")
    s = _ILLEGAL.sub("_", s)
    s = re.sub(r"_{2,}", "_", s)
    s = re.sub(r"\s+", " ", s).strip(" .")
    if not s:
        s = "untitled"
    if s.upper().split(".")[0] in _RESERVED:
        s = "_" + s
    if len(s) > max_len:
        s = s[:max_len].rstrip(" .")
    return s


def _m3u_line(dur: Optional[float], display: str, rel: str) -> str:
    return f"#EXTINF:{int(round(dur or 0))},{display}\n{rel}\n"


def _assign_names(plan: BuildPlan) -> None:
    for sp in plan.slots:
        used: set[str] = set()
        for st in sp.tracks:
            ext = Path(st.track.path).suffix.lower()
            base = sanitize_filename(f"{st.position:03d} - {st.track.artist} - {st.track.title}")
            name = base + ext
            n = 2
            while name.lower() in used:
                name = f"{base} ({n}){ext}"
                n += 1
            used.add(name.lower())
            st.out_name = name


def write_output(
    plan: BuildPlan,
    out_dir: Path,
    cfg: Config,
    on_progress: Optional[ProgressCallback] = None,
    overwrite: bool = False,
) -> OutputResult:
    emit = on_progress or (lambda e: None)
    out_dir = Path(out_dir)
    if cfg.is_inside_library(out_dir):
        raise ValueError(f"Refusing to write inside a library root: {out_dir}")
    if out_dir.exists() and any(out_dir.iterdir()) and not overwrite:
        raise FileExistsError(f"{out_dir} already has files in it. Use --overwrite to replace this gig's slot folders.")
    out_dir.mkdir(parents=True, exist_ok=True)
    if overwrite:
        for sp in plan.slots:
            d = out_dir / sp.spec.name
            if d.is_dir():
                shutil.rmtree(d)
        for f in ("housemusic.m3u8", "manifest.json", "plan.txt"):
            p = out_dir / f
            if p.is_file():
                p.unlink()

    _assign_names(plan)
    total = sum(len(sp.tracks) for sp in plan.slots)
    done = copied = 0
    nbytes = 0
    skipped: list[str] = []
    master = ["#EXTM3U\n"]

    for sp in plan.slots:
        slot_dir = out_dir / sp.spec.name
        slot_dir.mkdir(parents=True, exist_ok=True)
        slot_m3u = ["#EXTM3U\n"]
        for st in sp.tracks:
            src = Path(st.track.path)
            dst = slot_dir / st.out_name
            done += 1
            try:
                if not os.path.isfile(longpath(src)):
                    raise FileNotFoundError(str(src))
                shutil.copy2(longpath(src), longpath(dst))
                copied += 1
                nbytes += dst.stat().st_size
            except Exception as e:
                skipped.append(f"{sp.spec.name}/{st.out_name}: {type(e).__name__}: {e}")
                emit(ProgressEvent("copy", done, total, f"SKIPPED {src.name}"))
                continue
            slot_m3u.append(_m3u_line(st.track.duration, st.track.display, st.out_name))
            master.append(_m3u_line(st.track.duration, st.track.display, f"{sp.spec.name}/{st.out_name}"))
            emit(ProgressEvent("copy", done, total, st.out_name))
        (slot_dir / f"{sp.spec.name}.m3u8").write_text("".join(slot_m3u), encoding="utf-8")

    master_path = out_dir / "housemusic.m3u8"
    master_path.write_text("".join(master), encoding="utf-8")

    manifest = {
        "housekit_version": __version__,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "out_dir": str(out_dir),
        "plan": to_jsonable(plan),
        "skipped": skipped,
    }
    manifest_path = out_dir / "manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8")
    (out_dir / "plan.txt").write_text(plan_text(plan, cfg), encoding="utf-8")

    return OutputResult(
        out_dir=str(out_dir), manifest_path=str(manifest_path), master_playlist=str(master_path),
        copied=copied, skipped=skipped, bytes_copied=nbytes,
    )


def _mmss(seconds: Optional[float]) -> str:
    s = int(round(seconds or 0))
    return f"{s // 60}:{s % 60:02d}"


# ---------------------------------------------------------------- custom playlists


def write_playlist(result, out_dir: Path, cfg: Config, copy: bool = True, overwrite: bool = False,
                   on_progress: Optional[ProgressCallback] = None) -> OutputResult:
    """A single-folder playlist: numbered copies (or, with copy=False, an M3U8 that points at the
    library files in place) plus playlist.txt and manifest.json."""
    emit = on_progress or (lambda e: None)
    out_dir = Path(out_dir)
    if cfg.is_inside_library(out_dir):
        raise ValueError(f"Refusing to write inside a library root: {out_dir}")
    if out_dir.exists() and any(out_dir.iterdir()) and not overwrite:
        raise FileExistsError(f"{out_dir} already has files in it. Use --overwrite to replace it.")
    if overwrite and out_dir.exists():
        for p in out_dir.iterdir():
            if p.is_file() and (p.suffix.lower() in {".m3u8", ".json", ".txt"} or re.match(r"^\d{3} - ", p.name)):
                p.unlink()
    out_dir.mkdir(parents=True, exist_ok=True)

    used: set[str] = set()
    for pt in result.tracks:
        ext = Path(pt.track.path).suffix.lower()
        base = sanitize_filename(f"{pt.position:03d} - {pt.track.artist} - {pt.track.title}")
        name, n = base + ext, 2
        while name.lower() in used:
            name = f"{base} ({n}){ext}"
            n += 1
        used.add(name.lower())
        pt.out_name = name

    total = len(result.tracks)
    copied = nbytes = 0
    skipped: list[str] = []
    m3u = ["#EXTM3U\n"]
    for i, pt in enumerate(result.tracks, 1):
        src = Path(pt.track.path)
        if not os.path.isfile(longpath(src)):
            skipped.append(f"{pt.out_name}: missing file")
            emit(ProgressEvent("copy", i, total, f"SKIPPED {src.name}"))
            continue
        if copy:
            dst = out_dir / pt.out_name
            try:
                shutil.copy2(longpath(src), longpath(dst))
                copied += 1
                nbytes += dst.stat().st_size
            except Exception as e:
                skipped.append(f"{pt.out_name}: {type(e).__name__}: {e}")
                emit(ProgressEvent("copy", i, total, f"SKIPPED {src.name}"))
                continue
            m3u.append(_m3u_line(pt.track.duration, pt.track.display, pt.out_name))
        else:
            m3u.append(_m3u_line(pt.track.duration, pt.track.display, str(src)))
        emit(ProgressEvent("copy", i, total, pt.out_name))

    m3u_path = out_dir / (sanitize_filename(result.spec.name) + ".m3u8")
    m3u_path.write_text("".join(m3u), encoding="utf-8")
    (out_dir / "playlist.txt").write_text(playlist_text(result), encoding="utf-8")
    manifest = {
        "housekit_version": __version__,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "out_dir": str(out_dir),
        "copied": copy,
        "playlist": to_jsonable(result),
        "skipped": skipped,
    }
    (out_dir / "manifest.json").write_text(json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8")
    return OutputResult(out_dir=str(out_dir), manifest_path=str(out_dir / "manifest.json"), master_playlist=str(m3u_path),
                        copied=copied, skipped=skipped, bytes_copied=nbytes)


def playlist_text(result) -> str:
    s = result.spec
    filters = []
    if s.artists:
        filters.append("artist " + " / ".join(s.artists))
    if s.genres:
        filters.append("genre " + " / ".join(s.genres))
    if getattr(s, "decades", None):
        filters.append("decades " + " / ".join(f"{d}s" for d in s.decades))
    if s.year_min is not None or s.year_max is not None:
        filters.append(f"years {s.year_min or ''}-{s.year_max or ''}")
    if s.energy_min is not None or s.energy_max is not None:
        filters.append(f"energy {s.energy_min if s.energy_min is not None else ''}-{s.energy_max if s.energy_max is not None else ''}")
    if s.top:
        filters.append(f"top {s.top}")
    if s.search:
        filters.append(f"search '{s.search}'")
    if s.clean:
        filters.append("clean")
    n_fill = sum(1 for pt in result.tracks if getattr(pt, "filled", False))
    lines = [
        f"PLAYLIST  {s.name}",
        f"Filters: {', '.join(filters) or 'none'}   Order: {s.order}   "
        f"{len(result.tracks)} songs{f' ({n_fill} similar)' if n_fill else ''}, {result.runtime/60:.0f} min, from {result.candidates} matching tracks",
        "",
    ]
    for pt in result.tracks:
        t = pt.track
        en = f"{t.energy:.2f}" if t.energy is not None else " -- "
        chart = pop_label(t.chart_peak, t.chart_weeks, t.pop_source, t.popularity)
        lines.append(
            f"  {pt.position:03d}. {_mmss(t.duration):>5}  e{en}  {t.year or '----'}  {chart:>8}  pop {pt.rank_score:.2f}  "
            f"{t.artist} - {t.title}{(' [' + t.version + ']') if t.version else ''}  ({t.album})"
            f"{'  (similar)' if getattr(pt, 'filled', False) else ''}"
        )
    if result.warnings:
        lines += ["", "WARNINGS:"] + [f"  - {w}" for w in result.warnings]
    return "\n".join(lines) + "\n"


def plan_text(plan: BuildPlan, cfg: Config) -> str:
    """Printable slot plan for the gig folder (and for --dry-run)."""
    lines = [f"HOUSE MUSIC PLAN  {plan.name}", f"Venue: {plan.venue or '-'}   Date: {plan.date}   Setlist: {plan.setlist_source}", ""]
    p = plan.profile
    if p.n_tracks:
        dec = ", ".join(f"{d}s {s*100:.0f}%" for d, s in p.decades.items())
        gen = ", ".join(f"{g} {s*100:.0f}%" for g, s in list(p.genres.items())[:6])
        lines.append(f"Band profile from {p.n_tracks} matched setlist songs: {dec or 'no years'} | {gen or 'no genres'}")
        if p.energy_mean is not None:
            lines.append(f"Setlist energy mean {p.energy_mean:.2f}")
        lines.append("")
    e = plan.exclusions
    lines.append(
        f"Library {e.total}: eligible {e.eligible} | setlist {e.setlist} + dupes {e.dupe_of_excluded} | junk {e.junk} | "
        f"holiday {e.holiday} | explicit {e.explicit} + unknown {e.explicit_unknown} | recent gigs {e.history} | "
        f"too long {e.too_long} | too short {e.too_short} | no duration {e.no_duration} | missing {e.missing_file}"
        + (f" | below top {plan.settings.get('top')}: {e.not_top}" if e.not_top else "")
    )
    if plan.settings.get("popular_tracks") or plan.settings.get("charted_tracks"):
        lines.append(
            f"Popularity data: {plan.settings.get('popular_tracks') or plan.settings.get('charted_tracks')} library tracks matched a chart, "
            f"radio or year-end source ({plan.settings.get('charted_tracks', 0)} with a Hot 100 run, shown as #peak/weeks); known songs score higher."
        )
    lines.append("")
    for sp in plan.slots:
        status = "OK" if sp.filled else "SHORT" if sp.delta < 0 else "LONG"
        lines.append(
            f"== {sp.spec.name}  target {sp.spec.minutes:.0f} min  energy {sp.spec.energy_min:.2f}-{sp.spec.energy_max:.2f}  "
            f"runtime {sp.runtime/60:.1f} min ({sp.delta/60:+.1f})  [{status}]"
        )
        if sp.lufs_median is not None:
            lines.append(f"   loudness median {sp.lufs_median:.1f} LUFS, spread {sp.lufs_spread:.1f} LU, target {cfg.target_lufs:.0f}")
        for st in sp.tracks:
            t = st.track
            en = f"{t.energy:.2f}" if t.energy is not None else " -- "
            lu = f"{t.lufs:6.1f}" if t.lufs is not None else "   -- "
            gain = f"{st.gain_db:+.1f}" if st.gain_db is not None else "  --"
            flag = " !LEVEL" if st.level_flag else ""
            yr = t.year or "----"
            chart = pop_label(t.chart_peak, t.chart_weeks, t.pop_source, t.popularity)
            lines.append(
                f"  {st.position:02d}. {_mmss(t.duration):>5}  e{en}  {lu} LUFS  gain {gain:>5}  {yr}  {t.genre_norm:<14} {chart:>8} "
                f"{t.artist} - {t.title}{(' [' + t.version + ']') if t.version else ''}  (score {st.score.total:.2f}){flag}"
            )
        lines.append("")
    if plan.unresolved:
        lines.append("UNRESOLVED SETLIST LINES (titles still excluded by text, but verify):")
        lines += [f"  - {u}" for u in plan.unresolved]
        lines.append("")
    if plan.warnings:
        lines.append("WARNINGS:")
        lines += [f"  - {w}" for w in plan.warnings]
    return "\n".join(lines) + "\n"
