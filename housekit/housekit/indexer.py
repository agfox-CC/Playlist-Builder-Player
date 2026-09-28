"""Library scan orchestration: walk roots, read tags, analyze audio in a process pool.

Incremental and resumable: tag rows are committed in batches during the walk, feature
rows in batches during analysis, so an interrupted run simply resumes on the next call.
"""
from __future__ import annotations

import os
import time
from concurrent.futures import FIRST_COMPLETED, Future, ProcessPoolExecutor, wait
from datetime import datetime
from pathlib import Path
from typing import Callable, Optional

from .catalog import Catalog
from .config import Config
from .features import analyze_one
from .models import Features, ProgressCallback, ProgressEvent, ScanResult
from .tags import is_audio, load_content_rules, load_genre_rules, read_tags
from .textnorm import NORM_VERSION

TAG_BATCH = 200
FEATURE_BATCH = 50


def walk_roots(roots: list[Path], skip: Optional[list[Path]] = None) -> list[tuple[Path, Path]]:
    """(file, root) for every audio file under the roots, sorted for determinism.
    `skip` folders (the app's own folder, gig and playlist output) are never entered, so a
    portable install dropped inside the music folder doesn't catalog its own copies."""
    skip_set = {str(Path(s).resolve()).lower() for s in (skip or []) if s}
    out: list[tuple[Path, Path]] = []
    for root in roots:
        if not root.is_dir():
            continue
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = sorted(d for d in dirnames if str((Path(dirpath) / d).resolve()).lower() not in skip_set)
            for fn in sorted(filenames):
                p = Path(dirpath) / fn
                if is_audio(p):
                    out.append((p, root))
    return out


def _sample(items: list, limit: int) -> list:
    if limit <= 0 or len(items) <= limit:
        return items
    step = len(items) / limit
    return [items[int(i * step)] for i in range(limit)]


def scan_library(
    catalog: Catalog,
    cfg: Config,
    on_progress: Optional[ProgressCallback] = None,
    rescan: bool = False,
    limit: Optional[int] = None,
    analyze: bool = True,
    retry_errors: bool = False,
    jobs: Optional[int] = None,
    should_stop: Optional[Callable[[], bool]] = None,
) -> ScanResult:
    """Scan every configured root. With `limit`, an evenly spaced sample of N files is
    scanned instead (and nothing is marked missing). `should_stop` is polled so a GUI can
    cancel; progress so far is kept either way."""
    emit = on_progress or (lambda e: None)
    stop = should_stop or (lambda: False)
    result = ScanResult(0, 0, 0, 0, 0, 0, 0, 0, 0)
    content_rules = load_content_rules(str(cfg.content_rules_file))
    genre_rules = load_genre_rules(str(cfg.genres_file))

    # Song keys are stored with the normalization rules of the day; if those rules changed since the
    # last scan, recompute every key first so old and new copies of a song agree again.
    if catalog.get_meta("norm_version") != str(NORM_VERSION):
        emit(ProgressEvent("scan", 0, 0, "updating song keys"))
        catalog.recompute_song_keys()
        catalog.set_meta("norm_version", str(NORM_VERSION))
    if catalog.chart_stats()["chart_songs"] and catalog.get_meta("charts_norm_version") != str(NORM_VERSION):
        # Chart artist keys were stored under older rules: re-import the files so matching agrees again.
        from .charts import load_local_charts

        emit(ProgressEvent("scan", 0, 0, "re-reading chart files"))
        load_local_charts(catalog, cfg.base_dir, hot100_file=cfg.chart_file)

    emit(ProgressEvent("scan", 0, 0, "walking library"))
    files = walk_roots(cfg.library_roots, skip=[cfg.base_dir, cfg.output_dir, cfg.playlists_dir])
    if limit:
        files = _sample(files, limit)
    result.files_seen = len(files)
    known = catalog.known_files()

    # ---- pass 1: tags
    to_read: list[tuple[Path, Path]] = []
    unchanged: list[str] = []
    for p, root in files:
        s = str(p)
        k = known.get(s)
        if k is not None and not rescan:
            try:
                st = p.stat()
            except OSError:
                to_read.append((p, root))
                continue
            if k[0] == st.st_size and abs((k[1] or 0) - st.st_mtime) < 1.0:
                unchanged.append(s)
                continue
        to_read.append((p, root))
    result.tags_skipped = len(unchanged)
    for i in range(0, len(unchanged), 1000):
        catalog.touch_seen(unchanged[i : i + 1000])

    total = len(to_read)
    batch = []
    interrupted = False
    try:
        for i, (p, root) in enumerate(to_read, 1):
            if stop():
                raise KeyboardInterrupt
            tr = read_tags(p, root, content_rules, genre_rules, cfg.min_track_seconds)
            if tr.error:
                result.tag_errors += 1
                result.errors.append(f"{p}: {tr.error}")
            batch.append(tr)
            result.tags_read += 1
            if len(batch) >= TAG_BATCH:
                catalog.upsert_tags(batch)
                batch = []
            if i % 25 == 0 or i == total:
                emit(ProgressEvent("tags", i, total, p.name))
    except KeyboardInterrupt:
        interrupted = True
    finally:
        if batch:
            catalog.upsert_tags(batch)

    if interrupted:
        result.interrupted = True
        return result

    if not limit:
        seen = {str(p) for p, _ in files}
        result.marked_missing = catalog.mark_missing(seen, [str(r) for r in cfg.library_roots])
    catalog.inherit_overrides_from_missing()
    catalog.fill_genres_from_artist()
    result.dupe_groups = catalog.rebuild_dupe_groups()
    if result.tags_read and catalog.chart_stats()["chart_songs"]:
        catalog.apply_chart_data()  # new or retagged tracks pick up their chart peak
    catalog.compute_era_years()  # original years from chart / copies / album evidence (remasters lie)
    catalog.set_meta("last_scan", datetime.now().isoformat(timespec="seconds"))
    catalog.set_meta("library_roots", os.pathsep.join(str(r) for r in cfg.library_roots))

    if not analyze:
        return result

    # ---- pass 2: analysis
    jobs_all = catalog.analysis_jobs(cfg.analysis_version, retry_errors=retry_errors)
    if limit:
        wanted = {str(p) for p, _ in files}
        jobs_all = [j for j in jobs_all if j[1] in wanted]
    job_list = [(tid, path, dur, cfg.excerpt_seconds, cfg.analysis_version, size, mtime) for (tid, path, dur, size, mtime) in jobs_all]
    result.analysis_skipped = max(0, len(files) - len(job_list))
    total = len(job_list)
    if total == 0:
        emit(ProgressEvent("analyze", 0, 0, "nothing to analyze"))
        return result

    workers = jobs or cfg.effective_jobs
    workers = max(1, min(workers, total))
    done = 0
    pending: set[Future] = set()
    feats: list[Features] = []
    it = iter(job_list)
    window = workers * 4
    t0 = time.time()

    def flush() -> None:
        nonlocal feats
        if feats:
            catalog.store_features(feats)
            feats = []

    executor = ProcessPoolExecutor(max_workers=workers)
    try:
        for job in it:
            pending.add(executor.submit(analyze_one, job))
            if len(pending) >= window:
                break
        while pending:
            if stop():
                raise KeyboardInterrupt
            finished, pending = wait(pending, return_when=FIRST_COMPLETED, timeout=2.0)
            for fut in finished:
                f = fut.result()
                feats.append(f)
                done += 1
                if f.error:
                    result.analysis_errors += 1
                    result.errors.append(f"{f.track_id}: {f.error}")
                else:
                    result.analyzed += 1
                if len(feats) >= FEATURE_BATCH:
                    flush()
                if done % 10 == 0 or done == total:
                    rate = done / max(1e-6, time.time() - t0)
                    eta = (total - done) / rate if rate > 0 else 0
                    emit(ProgressEvent("analyze", done, total, f"{rate:.1f}/s, ~{int(eta // 60)}m{int(eta % 60):02d}s left"))
                for job in it:
                    pending.add(executor.submit(analyze_one, job))
                    break
        flush()
    except KeyboardInterrupt:
        result.interrupted = True
        flush()
        executor.shutdown(wait=False, cancel_futures=True)
        return result
    finally:
        executor.shutdown(wait=True, cancel_futures=True)
    return result
