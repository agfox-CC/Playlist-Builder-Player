"""Decode any audio file to PCM through ffmpeg, and measure what a player needs to know about it:
where the sound starts and stops (for gapless trimming) and how the track ends (fade, ring-out or
cold stop), which decides how the next cue is brought in."""
from __future__ import annotations

import math
import subprocess
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from ..fsutil import longpath

_FLAGS = getattr(subprocess, "CREATE_NO_WINDOW", 0)
FULL_SCALE = 32768.0


@dataclass
class Edges:
    in_sample: int
    out_sample: int
    ending: str  # fade | ring | cold
    fade_seconds: float  # length of the final fade for "fade" endings, else 0
    rate: int

    @property
    def in_seconds(self) -> float:
        return self.in_sample / self.rate

    @property
    def out_seconds(self) -> float:
        return self.out_sample / self.rate


def decode(path: Path, rate: int = 48000) -> np.ndarray:
    """The whole file as int16 stereo PCM, shape (n, 2), at `rate`. Raises RuntimeError when ffmpeg can't."""
    # pan rather than -ac 2: ffmpeg's default upmix puts a mono file 3 dB down; this keeps mono at
    # unity on both sides, passes stereo through untouched and folds surrounds in for the rare 5.1 file
    cmd = ["ffmpeg", "-v", "error", "-nostdin", "-i", longpath(Path(path)), "-vn", "-map", "0:a:0",
           "-af", "pan=stereo|FL=FL+FC+0.7*BL+0.7*SL|FR=FR+FC+0.7*BR+0.7*SR",
           "-f", "s16le", "-acodec", "pcm_s16le", "-ar", str(rate), "-"]
    r = subprocess.run(cmd, capture_output=True, timeout=900, stdin=subprocess.DEVNULL, creationflags=_FLAGS)
    if r.returncode != 0:
        err = r.stderr.decode("utf-8", "replace").strip().splitlines()
        raise RuntimeError(err[-1][:200] if err else f"ffmpeg exit {r.returncode}")
    data = r.stdout
    n = len(data) // 4 * 4
    if n == 0:
        raise RuntimeError("no audio decoded")
    return np.frombuffer(data[:n], dtype="<i2").reshape(-1, 2)


_CHUNK_FRAMES = 1 << 18  # analysis works on ~a quarter million frames at a time: small temporaries,
# so the decode worker never stalls the audio thread with big allocations


def _block_peaks(pcm: np.ndarray, w: int) -> np.ndarray:
    """Peak absolute sample per block of w frames (both channels). Works on the int16 data directly
    (max and -min per block) instead of converting the track, so it costs almost no memory."""
    n_blocks = len(pcm) // w
    peaks = np.zeros(n_blocks, dtype=np.int32)
    per_chunk = max(1, _CHUNK_FRAMES // w)
    for b0 in range(0, n_blocks, per_chunk):
        b1 = min(n_blocks, b0 + per_chunk)
        seg = pcm[b0 * w: b1 * w].reshape(b1 - b0, w * 2)
        hi = seg.max(axis=1).astype(np.int32)
        lo = seg.min(axis=1).astype(np.int32)
        peaks[b0:b1] = np.maximum(hi, -lo)
    return peaks


def _block_rms_db(pcm: np.ndarray, w: int) -> np.ndarray:
    """RMS per block of w frames, in dBFS, a few blocks at a time."""
    n_blocks = len(pcm) // w
    out = np.zeros(n_blocks, dtype=np.float64)
    per_chunk = max(1, _CHUNK_FRAMES // w)
    for b0 in range(0, n_blocks, per_chunk):
        b1 = min(n_blocks, b0 + per_chunk)
        seg = pcm[b0 * w: b1 * w].astype(np.float32)
        seg *= 1.0 / FULL_SCALE
        np.square(seg, out=seg)
        rms = np.sqrt(seg.reshape(b1 - b0, w * 2).mean(axis=1))
        out[b0:b1] = 20 * np.log10(np.maximum(rms, 1e-9))
    return out


def _body_level_db(pcm: np.ndarray, rate: int, samples: int = 40) -> float:
    """The track's typical level: the median RMS of up to `samples` one-second windows spread across it."""
    n = len(pcm)
    if n < rate:
        return float(_block_rms_db(pcm, max(1, n))[0]) if n else -90.0
    starts = np.linspace(0, n - rate, min(samples, n // rate)).astype(int)
    vals = [float(_block_rms_db(pcm[s: s + rate], rate)[0]) for s in starts]
    return float(np.median(vals))


def analyze_edges(pcm: np.ndarray, rate: int, threshold_db: float = -45.0, window_ms: int = 10) -> Edges:
    """Where the sound starts and stops, and how it ends:
    fade  a smooth decline of 10 dB or more over the last seconds (start the next cue under it)
    ring  the tail is much quieter than the song but not a smooth fade: a last chord ringing out
    cold  the song stops at level (butt-splice the next cue, only a hair of fade to avoid a click)"""
    n = len(pcm)
    if n == 0:
        return Edges(0, 0, "cold", 0.0, rate)
    w = max(1, rate * window_ms // 1000)
    peaks = _block_peaks(pcm, w)
    thr = FULL_SCALE * 10 ** (threshold_db / 20)
    loud = np.nonzero(peaks > thr)[0]
    if len(loud) == 0:
        return Edges(0, n, "cold", 0.0, rate)
    in_s = max(0, int(loud[0]) * w - rate // 50)  # 20 ms before the first sound
    out_s = min(n, (int(loud[-1]) + 1) * w + rate // 20)  # 50 ms after the last
    ending, fade_secs = "cold", 0.0
    tail = pcm[max(in_s, out_s - 12 * rate): out_s]
    nb = len(tail) // rate
    if nb >= 3:
        rms = _block_rms_db(tail[len(tail) - nb * rate:], rate)  # 1 s windows aligned to the end
        top_i = int(np.argmax(rms))
        top = rms[top_i]
        drop = top - rms[-1]
        declining = all(rms[i + 1] <= rms[i] + 1.5 for i in range(top_i, nb - 1))
        body_db = _body_level_db(pcm[in_s:out_s], rate)
        if drop >= 10 and declining and nb - top_i >= 3:
            ending = "fade"
            below = np.nonzero(rms[top_i:] <= top - 3)[0]
            start = top_i + (int(below[0]) if len(below) else 1)
            fade_secs = float(nb - start)
        elif rms[-1] <= body_db - 8:
            ending = "ring"
    return Edges(in_s, out_s, ending, fade_secs, rate)


def envelope(pcm: np.ndarray, cols: int = 600) -> np.ndarray:
    """A waveform overview: `cols` peak values 0..1 across the track, lifted (x^0.6) so quiet
    passages still show."""
    n = len(pcm)
    if n == 0 or cols <= 0:
        return np.zeros(max(cols, 0), dtype=np.float32)
    step = max(1, n // cols)
    peaks = _block_peaks(pcm, step)[:cols].astype(np.float32) / FULL_SCALE
    return np.clip(peaks, 0.0, 1.0) ** 0.6


def db_to_gain(db: float) -> float:
    return float(10 ** (db / 20)) if math.isfinite(db) else 1.0
