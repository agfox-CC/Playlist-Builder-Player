"""Audio conversion through ffmpeg: files or folders in, one format out, playlists rewritten.

Pure functions plus one worker pool. Two modes: with an output folder the sources are left alone
and a converted copy is written there; without one (the default) the files are converted **in
place**, so a gig or playlist folder ends up holding the converted files and its playlists point
at them. Either way nothing is ever written or changed inside a music library root.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

from .fsutil import longpath
from .models import ProgressCallback, ProgressEvent
from .scoring import lufs_gain
from .tags import AUDIO_EXTS

_SUBPROC_FLAGS = getattr(subprocess, "CREATE_NO_WINDOW", 0)

# Output presets. `args` are the ffmpeg encoder settings; `cover` says whether the container can carry
# embedded artwork; `lossy` decides whether high sample rates get brought down for compatibility.
FORMATS: dict[str, dict] = {
    "mp3-320": {"label": "MP3 320 kbps", "ext": ".mp3", "args": ["-c:a", "libmp3lame", "-b:a", "320k"], "cover": True, "lossy": True},
    "mp3-256": {"label": "MP3 256 kbps", "ext": ".mp3", "args": ["-c:a", "libmp3lame", "-b:a", "256k"], "cover": True, "lossy": True},
    "mp3-192": {"label": "MP3 192 kbps", "ext": ".mp3", "args": ["-c:a", "libmp3lame", "-b:a", "192k"], "cover": True, "lossy": True},
    "mp3-128": {"label": "MP3 128 kbps", "ext": ".mp3", "args": ["-c:a", "libmp3lame", "-b:a", "128k"], "cover": True, "lossy": True},
    "m4a-256": {"label": "AAC 256 kbps (.m4a)", "ext": ".m4a", "args": ["-c:a", "aac", "-b:a", "256k"], "cover": True, "lossy": True},
    "flac": {"label": "FLAC (lossless)", "ext": ".flac", "args": ["-c:a", "flac"], "cover": True, "lossy": False},
    "wav": {"label": "WAV 16-bit", "ext": ".wav", "args": ["-c:a", "pcm_s16le"], "cover": False, "lossy": False},
    "ogg": {"label": "OGG Vorbis (q6)", "ext": ".ogg", "args": ["-c:a", "libvorbis", "-q:a", "6"], "cover": False, "lossy": True},
}
MAX_LOSSY_RATE = 48000


@dataclass
class ConvertOptions:
    fmt: str = "mp3-320"
    match_loudness: Optional[float] = None  # target LUFS, e.g. -16; None = leave levels alone
    keep_structure: bool = True  # mirror the source folder layout under the output folder
    overwrite: bool = False
    jobs: int = 0  # 0 = half the CPUs


@dataclass
class ConvertItem:
    src: Path
    dst: Path
    status: str = "pending"  # converted | skipped | error
    detail: str = ""
    bytes: int = 0


@dataclass
class ConvertResult:
    out_dir: str
    items: list[ConvertItem]
    playlists: list[str] = field(default_factory=list)
    interrupted: bool = False

    @property
    def converted(self) -> int:
        return sum(1 for i in self.items if i.status == "converted")

    @property
    def skipped(self) -> int:
        return sum(1 for i in self.items if i.status == "skipped")

    @property
    def errors(self) -> list[ConvertItem]:
        return [i for i in self.items if i.status == "error"]

    @property
    def bytes_written(self) -> int:
        return sum(i.bytes for i in self.items)


def ffmpeg_available() -> Optional[str]:
    return shutil.which("ffmpeg")


def collect_inputs(paths: list[Path]) -> tuple[list[tuple[Path, Optional[Path]]], list[Path]]:
    """Expand files and folders into (file, base_folder) pairs; base_folder is the folder the file was
    found under (None for a file given directly). Also returns the folders, for playlist rewriting."""
    files: list[tuple[Path, Optional[Path]]] = []
    folders: list[Path] = []
    seen: set[str] = set()
    for p in paths:
        p = Path(p)
        if p.is_dir():
            folders.append(p)
            for dirpath, dirnames, filenames in os.walk(p):
                dirnames.sort()
                for fn in sorted(filenames):
                    f = Path(dirpath) / fn
                    if f.suffix.lower() in AUDIO_EXTS and str(f).lower() not in seen:
                        seen.add(str(f).lower())
                        files.append((f, p))
        elif p.is_file() and p.suffix.lower() in AUDIO_EXTS and str(p).lower() not in seen:
            seen.add(str(p).lower())
            files.append((p, None))
    return files, folders


def probe_rate(path: Path) -> Optional[int]:
    try:
        r = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "a:0", "-show_entries", "stream=sample_rate", "-of", "json", longpath(path)],
            capture_output=True, text=True, timeout=30, stdin=subprocess.DEVNULL, creationflags=_SUBPROC_FLAGS, encoding="utf-8", errors="replace",
        )
        streams = json.loads(r.stdout or "{}").get("streams") or []
        return int(streams[0]["sample_rate"]) if streams and streams[0].get("sample_rate") else None
    except Exception:
        return None


def build_command(
    src: Path,
    dst: Path,
    fmt: str,
    with_cover: bool = True,
    audio_filter: Optional[str] = None,
    sample_rate: Optional[int] = None,
) -> list[str]:
    """The ffmpeg command line for one file. Tags are carried over; artwork too where the format allows."""
    spec = FORMATS[fmt]
    cmd = ["ffmpeg", "-v", "error", "-nostdin", "-y", "-i", longpath(src), "-map_metadata", "0", "-map", "0:a:0"]
    if spec["cover"] and with_cover:
        cmd += ["-map", "0:v?", "-c:v", "copy"]
        if spec["ext"] == ".m4a":
            cmd += ["-disposition:v:0", "attached_pic"]
    else:
        cmd += ["-vn"]
    cmd += spec["args"]
    if spec["ext"] == ".mp3":
        cmd += ["-id3v2_version", "3"]
    if audio_filter:
        cmd += ["-af", audio_filter]
    if sample_rate:
        cmd += ["-ar", str(sample_rate)]
    cmd += [longpath(dst)]
    return cmd


def loudness_filter(target: float, measured: Optional[tuple[Optional[float], Optional[float]]]) -> str:
    """Plain gain when the catalog knows the track's loudness (transparent, no dynamics processing),
    single-pass loudnorm for anything unmeasured."""
    if measured and measured[0] is not None:
        gain = lufs_gain(measured[0], target, measured[1])
        if gain is None or abs(gain) < 0.25:  # not worth re-encoding for a quarter of a dB
            return ""
        return f"volume={gain:+.1f}dB"
    return f"loudnorm=I={target:g}:TP=-1.5:LRA=11"


def _convert_one(src: Path, dst: Path, opts: ConvertOptions, measured, catalog_rate: Optional[int], in_place: bool = False) -> ConvertItem:
    item = ConvertItem(src=src, dst=dst)
    spec = FORMATS[opts.fmt]
    same = in_place and str(src).lower() == str(dst).lower()  # already in the target format
    if same and not opts.overwrite:
        item.status, item.detail = "skipped", f"already {spec['ext']}"
        return item
    if not same and dst.exists() and not opts.overwrite:
        item.status, item.detail = "skipped", "exists"
        return item
    dst.parent.mkdir(parents=True, exist_ok=True)
    af = loudness_filter(opts.match_loudness, measured) if opts.match_loudness is not None else ""
    rate = catalog_rate or probe_rate(src)
    out_rate: Optional[int] = None
    if rate and spec["lossy"] and rate > MAX_LOSSY_RATE:
        out_rate = MAX_LOSSY_RATE
    elif af.startswith("loudnorm"):
        out_rate = rate or 44100  # loudnorm would otherwise emit 192 kHz
    tmp = dst.with_name(dst.stem + ".part" + dst.suffix)
    try:
        for with_cover in (True, False):
            cmd = build_command(src, tmp, opts.fmt, with_cover=with_cover, audio_filter=af or None, sample_rate=out_rate)
            r = subprocess.run(cmd, capture_output=True, timeout=1800, stdin=subprocess.DEVNULL, creationflags=_SUBPROC_FLAGS)
            if r.returncode == 0 and tmp.exists() and tmp.stat().st_size > 0:
                break
            err = r.stderr.decode("utf-8", "replace").strip().splitlines()
            item.detail = err[-1][:200] if err else f"ffmpeg exit {r.returncode}"
            if not with_cover:
                raise RuntimeError(item.detail)
        os.replace(tmp, dst)
        item.status, item.detail, item.bytes = "converted", "", dst.stat().st_size
        if in_place and not same:
            try:
                os.remove(longpath(src))  # the converted file takes the original's place
            except OSError as e:
                item.detail = f"converted, but the original could not be removed: {e}"
    except subprocess.TimeoutExpired:
        item.status, item.detail = "error", "ffmpeg timeout"
    except Exception as e:
        item.status, item.detail = "error", item.detail or f"{type(e).__name__}: {e}"
    finally:
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                pass
    return item


def plan_outputs(files: list[tuple[Path, Optional[Path]]], out_dir: Optional[Path], opts: ConvertOptions) -> list[tuple[Path, Path]]:
    """Where each source goes. out_dir=None means in place: next to the source, same name, new extension.
    Sources already in the target format claim their own name first, so "X.mp3" is left as it is
    and "X.flac" beside it becomes "X (2).mp3" rather than replacing it."""
    ext = FORMATS[opts.fmt]["ext"]
    used: set[str] = set()
    plan: list[tuple[Path, Path]] = []
    for src, base in sorted(files, key=lambda fb: fb[0].suffix.lower() != ext):
        if out_dir is None:
            dst = src.with_suffix(ext)
        elif opts.keep_structure and base is not None:
            rel = src.relative_to(base)
            dst = out_dir / base.name / rel.with_suffix(ext)
        else:
            dst = out_dir / (src.stem + ext)
        n = 2
        while str(dst).lower() in used:
            dst = dst.with_name(f"{src.stem} ({n}){ext}")
            n += 1
        used.add(str(dst).lower())
        plan.append((src, dst))
    return plan


def rewrite_playlists(folders: list[Path], out_dir: Optional[Path], mapping: dict[str, Path], opts: ConvertOptions) -> list[str]:
    """Point every .m3u8 found under the source folders at the converted files: written next to the
    converted copies when there is an output folder, rewritten where they are when converting in place."""
    written: list[str] = []
    for base in folders:
        for dirpath, _, filenames in os.walk(base):
            for fn in filenames:
                if not fn.lower().endswith((".m3u8", ".m3u")):
                    continue
                src_pl = Path(dirpath) / fn
                try:
                    lines = src_pl.read_text(encoding="utf-8-sig").splitlines()
                except OSError:
                    continue
                if out_dir is None:
                    dst_pl, dst_pl_dir = src_pl, src_pl.parent
                elif opts.keep_structure:
                    dst_pl = out_dir / base.name / src_pl.relative_to(base)
                    dst_pl_dir = dst_pl.parent
                else:
                    dst_pl, dst_pl_dir = out_dir / src_pl.name, out_dir
                out_lines = []
                for line in lines:
                    if line.startswith("#") or not line.strip():
                        out_lines.append(line)
                        continue
                    ref = (src_pl.parent / line.strip()).resolve()
                    dst = mapping.get(str(ref).lower())
                    if dst is None:
                        out_lines.append(line)
                    elif out_dir is None or opts.keep_structure:
                        out_lines.append(os.path.relpath(dst, dst_pl_dir).replace("\\", "/"))
                    else:
                        out_lines.append(dst.name)
                if out_dir is None and out_lines == lines:
                    continue  # nothing in this playlist changed
                dst_pl.parent.mkdir(parents=True, exist_ok=True)
                dst_pl.write_text("\n".join(out_lines) + "\n", encoding="utf-8")
                written.append(str(dst_pl))
    return written


def _inside(p: Path, roots: list[Path]) -> bool:
    try:
        rp = p.resolve()
    except OSError:
        return False
    for r in roots:
        try:
            fr = Path(r).resolve()
        except OSError:
            continue
        if rp == fr or fr in rp.parents:
            return True
    return False


def convert_files(
    inputs: list[Path],
    out_dir: Optional[Path],
    opts: ConvertOptions,
    loudness_lookup: Optional[Callable[[Path], Optional[tuple]]] = None,
    rate_lookup: Optional[Callable[[Path], Optional[int]]] = None,
    on_progress: Optional[ProgressCallback] = None,
    should_stop: Optional[Callable[[], bool]] = None,
    forbidden_dirs: Optional[list[Path]] = None,
) -> ConvertResult:
    """Convert every audio file in `inputs` (files and/or folders). With `out_dir` the converted
    copies go there; with out_dir=None the files are converted in place (the original is removed
    once its replacement is written). Inputs inside a library root are refused in place."""
    emit = on_progress or (lambda e: None)
    stop = should_stop or (lambda: False)
    if opts.fmt not in FORMATS:
        raise ValueError(f"Unknown format {opts.fmt!r}; choose one of {', '.join(FORMATS)}")
    if not ffmpeg_available():
        raise RuntimeError("ffmpeg was not found. Put ffmpeg.exe and ffprobe.exe in the app's ffmpeg\\ folder or on the PATH.")
    out_dir = Path(out_dir) if out_dir is not None else None
    roots = list(forbidden_dirs or [])
    if out_dir is not None and _inside(out_dir, roots):
        raise ValueError(f"Refusing to write converted files inside the music library: {out_dir}")
    if out_dir is None:
        for p in inputs:
            if _inside(Path(p), roots):
                raise ValueError(f"Refusing to convert in place inside the music library: {p}. Choose an output folder instead.")
    files, folders = collect_inputs(inputs)
    plan = plan_outputs(files, out_dir, opts)
    first = Path(inputs[0]) if inputs else Path(".")
    result = ConvertResult(out_dir=str(out_dir) if out_dir is not None else str(first if first.is_dir() else first.parent), items=[])
    if not plan:
        return result
    if out_dir is not None:
        out_dir.mkdir(parents=True, exist_ok=True)
    workers = opts.jobs if opts.jobs > 0 else max(1, (os.cpu_count() or 2) // 2)
    total = len(plan)
    done = 0
    mapping: dict[str, Path] = {}
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futures = {}
        for src, dst in plan:
            measured = loudness_lookup(src) if loudness_lookup else None
            rate = rate_lookup(src) if rate_lookup else None
            futures[ex.submit(_convert_one, src, dst, opts, measured, rate, out_dir is None)] = (src, dst)
        for fut in as_completed(futures):
            item = fut.result()
            result.items.append(item)
            done += 1
            if item.status != "error":
                mapping[str(item.src.resolve()).lower()] = item.dst
            emit(ProgressEvent("convert", done, total, f"{item.status}: {item.src.name}"))
            if stop():
                result.interrupted = True
                for f in futures:
                    f.cancel()
                break
    result.items.sort(key=lambda i: str(i.src).lower())
    if folders and not result.interrupted:
        result.playlists = rewrite_playlists(folders, out_dir, mapping, opts)
    return result
