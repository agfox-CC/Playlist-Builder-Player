"""Audio analysis: energy, tempo and loudness from a short excerpt decoded through ffmpeg.

`analyze_one` is a pure function designed to run inside a multiprocessing pool.
It never raises; failures come back as Features(error=...).

Energy is built from loudness-invariant cues (the excerpt is level-normalised first) so a
loud 2024 remaster of a ballad doesn't outscore a quiet 1975 rocker:
  onset rate      how many rhythmic hits per second
  percussive      share of energy that is percussive (median-filter HPSS)
  centroid        spectral brightness (distorted guitars, cymbals)
  flux            how fast the spectrum changes
  tempo           from the onset-envelope autocorrelation
  density         inverse of frame-to-frame RMS swing (dense mixes vs sparse arrangements)
  loudness        the measured LUFS, small weight
Each component is mapped linearly onto 0..1 using library-derived 5th/95th percentiles.
"""
from __future__ import annotations

import math
import subprocess
import warnings
from pathlib import Path
from typing import Optional

import numpy as np

from .fsutil import longpath
from .models import Features

SAMPLE_RATE = 44100
FRAME = 2048
HOP = 1024
N_BANDS = 64
ONSET_MIN_DB = 0.5  # mean positive log-band difference a frame must show to count as an onset
_SUBPROC_FLAGS = getattr(subprocess, "CREATE_NO_WINDOW", 0)

# Component -> (low, high) mapped to 0..1, and weight. Tune here only.
CAL = {
    "onset_rate": (2.3, 4.4, 0.20),
    "perc_ratio": (0.30, 0.80, 0.20),
    "flatness": (0.015, 0.11, 0.15),  # spectral flatness of band power: wall-of-sound vs sparse
    "centroid_log": (math.log2(1500.0), math.log2(4500.0), 0.10),
    "flux": (0.20, 0.31, 0.10),
    "tempo": (65.0, 165.0, 0.10),
    "density": (-6.0, -1.5, 0.05),  # negative dyn_db: less swing = denser = higher
    "lufs": (-20.0, -6.0, 0.10),
}


def decode_excerpt(path: Path, start: float, seconds: float, sr: int = SAMPLE_RATE) -> np.ndarray:
    """Decode `seconds` of stereo float32 audio starting at `start`. Returns shape (n, 2)."""
    cmd = [
        "ffmpeg", "-v", "error", "-nostdin",
        "-ss", f"{max(0.0, start):.3f}", "-t", f"{seconds:.3f}",
        "-i", longpath(path),
        "-vn", "-sn", "-dn", "-map", "0:a:0",
        "-ac", "2", "-ar", str(sr), "-f", "f32le", "-",
    ]
    r = subprocess.run(cmd, capture_output=True, timeout=180, stdin=subprocess.DEVNULL, creationflags=_SUBPROC_FLAGS)
    buf = r.stdout
    if len(buf) < 8 * sr:  # less than a second: treat as failure
        err = r.stderr.decode("utf-8", "replace").strip().splitlines()
        raise RuntimeError(err[-1] if err else "ffmpeg produced no audio")
    n = len(buf) // 8
    return np.frombuffer(buf[: n * 8], dtype=np.float32).reshape(-1, 2)


def _band_matrix(sr: int, n_fft: int, n_bands: int = N_BANDS, fmin: float = 80.0, fmax: float = 16000.0) -> tuple[np.ndarray, np.ndarray]:
    freqs = np.fft.rfftfreq(n_fft, 1.0 / sr)
    edges = np.geomspace(fmin, fmax, n_bands + 1)
    W = np.zeros((n_bands, len(freqs)), dtype=np.float32)
    for b in range(n_bands):
        idx = np.where((freqs >= edges[b]) & (freqs < edges[b + 1]))[0]
        if len(idx) == 0:
            idx = np.array([np.argmin(np.abs(freqs - (edges[b] + edges[b + 1]) / 2))])
        W[b, idx] = 1.0 / len(idx)
    return W, edges


_W_BANDS, _EDGES = _band_matrix(SAMPLE_RATE, FRAME)


def _median_filter(x: np.ndarray, k: int, axis: int) -> np.ndarray:
    pad = [(0, 0)] * x.ndim
    pad[axis] = (k // 2, k // 2)
    xp = np.pad(x, pad, mode="edge")
    win = np.lib.stride_tricks.sliding_window_view(xp, k, axis=axis)
    return np.median(win, axis=-1)


def _lin(x: float, lo: float, hi: float) -> float:
    return float(min(1.0, max(0.0, (x - lo) / (hi - lo))))


def compute_features(audio: np.ndarray, sr: int = SAMPLE_RATE) -> dict[str, float]:
    """All measured values for a stereo float32 block. See module docstring."""
    peak = float(np.max(np.abs(audio))) if audio.size else 0.0
    peak_dbfs = 20.0 * math.log10(peak + 1e-9)

    lufs: float
    try:
        import pyloudnorm as pyln

        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            lufs = float(pyln.Meter(sr).integrated_loudness(audio.astype(np.float64)))
        if not math.isfinite(lufs):
            lufs = -70.0
    except Exception:
        lufs = -0.691 + 10 * math.log10(float(np.mean(audio ** 2)) + 1e-12)

    mono = audio.mean(axis=1).astype(np.float32)
    rms_all = float(np.sqrt(np.mean(mono ** 2) + 1e-12))
    rms_db = 20.0 * math.log10(rms_all + 1e-12)
    mono = mono * (10 ** (-20 / 20) / max(rms_all, 1e-6))  # level-normalise to -20 dBFS RMS

    if len(mono) < FRAME * 4:
        mono = np.pad(mono, (0, FRAME * 4 - len(mono)))
    n = 1 + (len(mono) - FRAME) // HOP
    idx = np.arange(FRAME)[None, :] + HOP * np.arange(n)[:, None]
    frames = mono[idx]
    win = np.hanning(FRAME).astype(np.float32)
    spec = np.abs(np.fft.rfft(frames * win, axis=1)).astype(np.float32)
    freqs = np.fft.rfftfreq(FRAME, 1.0 / sr)

    frame_rms = np.sqrt(np.mean(frames ** 2, axis=1) + 1e-12)
    active = frame_rms > 10 ** (-40 / 20)
    if active.sum() < 10:
        active = np.ones(n, dtype=bool)

    spec_sum = spec.sum(axis=1) + 1e-9
    centroid = (spec * freqs[None, :]).sum(axis=1) / spec_sum
    centroid_hz = float(np.mean(centroid[active]))

    normed = spec / spec_sum[:, None]
    flux_series = 0.5 * np.abs(np.diff(normed, axis=0)).sum(axis=1)
    flux = float(np.mean(flux_series[active[1:]])) if len(flux_series) else 0.0

    bands = spec @ _W_BANDS.T  # (n, N_BANDS) linear magnitude
    logb = 20 * np.log10(bands + 1e-7)
    logb = np.maximum(logb, logb.max() - 80)

    # Onset envelope: half-wave rectified log-band difference, median-floored.
    onset = np.maximum(np.diff(logb, axis=0), 0).mean(axis=1)
    onset = np.maximum(onset - np.median(onset), 0)
    # Relative threshold, but never below an absolute floor: a sustained tone must not
    # produce "onsets" out of numerical ripple.
    thr = max(onset.mean() + onset.std(), ONSET_MIN_DB)
    peaks = np.where((onset[1:-1] > thr) & (onset[1:-1] >= onset[:-2]) & (onset[1:-1] >= onset[2:]))[0] + 1
    kept = 0
    last = -10
    for p in peaks:
        if p - last >= 3:
            kept += 1
            last = p
    seconds = len(onset) * HOP / sr
    onset_rate = kept / max(seconds, 1e-6)

    # Tempo: autocorrelation of the onset envelope, log-normal prior around 115 BPM.
    o = onset - onset.mean()
    acf = np.correlate(o, o, mode="full")[len(o) - 1:]
    acf = acf / (acf[0] + 1e-9)
    lag_min = int(60 / 200 * sr / HOP)
    lag_max = min(int(60 / 50 * sr / HOP), len(acf) - 1)
    lags = np.arange(lag_min, lag_max + 1)
    bpms = 60.0 * sr / (lags * HOP)
    prior = np.exp(-0.5 * ((np.log2(bpms) - np.log2(115.0)) / 0.9) ** 2)
    best = int(np.argmax(acf[lag_min: lag_max + 1] * prior))
    tempo = float(bpms[best])
    beat_strength = float(max(0.0, acf[lag_min + best]))

    # Percussive share via median-filter HPSS on the band power spectrogram.
    P = bands ** 2
    H = _median_filter(P, 17, axis=0)
    Pc = _median_filter(P, 9, axis=1)
    mask_p = Pc ** 2 / (H ** 2 + Pc ** 2 + 1e-12)
    perc_ratio = float((P * mask_p).sum() / (P.sum() + 1e-9))

    dyn_db = float(np.std(20 * np.log10(frame_rms[active] + 1e-9)))

    # Spectral flatness of the band power (geometric / arithmetic mean): a distorted wall of
    # sound fills every band (high), a voice over a piano leaves most bands empty (low).
    Pa = P[active] if active.sum() >= 10 else P
    flatness = float(np.mean(np.exp(np.mean(np.log(Pa + 1e-12), axis=1)) / (np.mean(Pa, axis=1) + 1e-12)))

    comps = {
        "onset_rate": onset_rate,
        "perc_ratio": perc_ratio,
        "flatness": flatness,
        "centroid_log": math.log2(max(centroid_hz, 1.0)),
        "flux": flux,
        "tempo": tempo,
        "density": -dyn_db,
        "lufs": lufs,
    }
    energy = sum(w * _lin(comps[k], lo, hi) for k, (lo, hi, w) in CAL.items())
    energy /= sum(w for _, _, w in CAL.values())

    return {
        "energy": round(float(min(1.0, max(0.0, energy))), 4),
        "lufs": round(lufs, 2),
        "peak_dbfs": round(peak_dbfs, 2),
        "rms_db": round(rms_db, 2),
        "centroid_hz": round(centroid_hz, 1),
        "flux": round(flux, 4),
        "tempo": round(tempo, 1),
        "onset_rate": round(onset_rate, 3),
        "perc_ratio": round(perc_ratio, 4),
        "beat_strength": round(beat_strength, 3),
        "dyn_db": round(dyn_db, 2),
        "flatness": round(flatness, 4),
    }


def analyze_one(job: tuple) -> Features:
    """Pool worker. job = (track_id, path, duration, excerpt_seconds, analysis_version, size, mtime)."""
    track_id, path_s, duration, excerpt, version, size = job[:6]
    mtime = job[6] if len(job) > 6 else 0.0
    path = Path(path_s)
    feat = Features(track_id=track_id, analysis_version=version, size=size, mtime=mtime)
    try:
        start = duration / 2.0 - excerpt / 2.0 if (duration and duration > excerpt) else 0.0
        audio = decode_excerpt(path, start, excerpt)
        vals = compute_features(audio)
        for k in ("energy", "lufs", "peak_dbfs", "rms_db", "centroid_hz", "flux", "tempo", "onset_rate", "perc_ratio", "dyn_db", "flatness"):
            setattr(feat, k, vals[k])
    except subprocess.TimeoutExpired:
        feat.error = "ffmpeg timeout"
    except Exception as e:
        feat.error = f"{type(e).__name__}: {str(e)[:200]}"
    return feat
