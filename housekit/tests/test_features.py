import math
import shutil
import wave
from pathlib import Path

import numpy as np
import pytest

from housekit.features import analyze_one, compute_features

SR = 44100
needs_ffmpeg = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not on PATH")


def write_wav(path: Path, audio: np.ndarray) -> None:
    pcm = (np.clip(audio, -1, 1) * 32767).astype("<i2")
    with wave.open(str(path), "wb") as w:
        w.setnchannels(2)
        w.setsampwidth(2)
        w.setframerate(SR)
        w.writeframes(pcm.tobytes())


def ballad(seconds: float = 40.0, gain_db: float = -20.0) -> np.ndarray:
    """Slow sustained tones, quiet."""
    t = np.arange(int(seconds * SR)) / SR
    x = 0.5 * np.sin(2 * math.pi * 220 * t) + 0.3 * np.sin(2 * math.pi * 330 * t)
    env = 0.6 + 0.4 * np.sin(2 * math.pi * 0.25 * t)  # slow swell
    x = x * env
    x = x / np.sqrt(np.mean(x ** 2)) * 10 ** (gain_db / 20)
    return np.stack([x, x], axis=1).astype(np.float32)


def banger(seconds: float = 40.0, gain_db: float = -10.0, bpm: float = 128.0) -> np.ndarray:
    """Noise bursts on every beat plus bright hats: dense and percussive."""
    rng = np.random.default_rng(0)
    n = int(seconds * SR)
    x = 0.05 * rng.standard_normal(n)
    beat = int(SR * 60 / bpm)
    for i in range(0, n, beat):
        L = min(4000, n - i)
        x[i: i + L] += rng.standard_normal(L) * np.exp(-np.arange(L) / 800) * 1.5
    for i in range(beat // 2, n, beat // 2):
        L = min(1500, n - i)
        x[i: i + L] += rng.standard_normal(L) * np.exp(-np.arange(L) / 200) * 0.6
    x = x / np.sqrt(np.mean(x ** 2)) * 10 ** (gain_db / 20)
    return np.stack([x, x], axis=1).astype(np.float32)


def test_loudness_measured_and_energy_orders_correctly():
    calm = compute_features(ballad(gain_db=-20))
    hot = compute_features(banger(gain_db=-10))
    assert hot["energy"] > calm["energy"] + 0.25
    assert hot["lufs"] > calm["lufs"] + 6
    assert hot["onset_rate"] > calm["onset_rate"]
    assert hot["perc_ratio"] > calm["perc_ratio"]
    assert 118 < hot["tempo"] < 138 or 60 < hot["tempo"] < 68  # 128 or its half


def test_energy_is_loudness_invariant():
    """The same material at two mastering levels should land within a small margin (only the LUFS term differs)."""
    quiet = compute_features(banger(gain_db=-22))
    loud = compute_features(banger(gain_db=-8))
    assert abs(loud["energy"] - quiet["energy"]) < 0.15
    assert loud["lufs"] - quiet["lufs"] > 10


@needs_ffmpeg
def test_analyze_one_via_ffmpeg(tmp_path: Path):
    p = tmp_path / "hot.wav"
    write_wav(p, banger(seconds=50, gain_db=-14))
    f = analyze_one((1, str(p), 50.0, 30.0, 2, p.stat().st_size, p.stat().st_mtime))
    assert f.error == ""
    assert f.energy is not None and f.lufs is not None
    # mono material at -14 dBFS RMS, duplicated to both channels, K-weighted: lands around -10 LUFS
    assert -14 < f.lufs < -6
    assert f.peak_dbfs <= 0.0


@needs_ffmpeg
def test_corrupt_file_reports_error_not_exception(tmp_path: Path):
    p = tmp_path / "junk.flac"
    p.write_bytes(b"not audio at all" * 100)
    f = analyze_one((1, str(p), 100.0, 30.0, 2, p.stat().st_size, p.stat().st_mtime))
    assert f.error and f.energy is None
