"""The mixer. Voices are decoded tracks with a play position, gain and fades; `render` sums the
active ones into a block of float32 stereo. Starts can be scheduled to a sample clock, which is
what makes follow-ons gapless: the next cue begins on the exact sample the previous one hands over,
inside the same output block, no timer involved. Fades are equal-power (sin/cos), so a crossfade
of two similar tracks holds its level. Pads (stingers) duck everything else while they play.

`render` is pure numpy and runs without a sound card, so the whole thing is testable offline;
`start` wraps it in a PortAudio output stream via sounddevice."""
from __future__ import annotations

import math
import threading
from typing import Optional

import numpy as np

from .decode import FULL_SCALE


class Voice:
    def __init__(self, pcm: np.ndarray, rate: int, in_pt: int = 0, out_pt: Optional[int] = None, gain: float = 1.0,
                 fade_in: int = 0, fade_out: int = 0, tag=None, is_pad: bool = False, loop: bool = False) -> None:
        self.pcm = pcm
        self.rate = rate
        n = len(pcm)
        self.in_pt = max(0, min(int(in_pt), n))
        self.out_pt = n if out_pt is None else max(self.in_pt, min(int(out_pt), n))
        self.pos = self.in_pt
        self.gain = float(gain)
        self.fade_in = max(0, int(fade_in))  # samples, from in_pt
        self.fade_out = max(0, int(fade_out))  # samples, up to out_pt
        self.manual_len = 0  # a fade-out started by hand (stop, panic, GO-skip)
        self.manual_pos = 0
        self.ramp_from = 0  # a fade-in starting at an arbitrary sample (a seek into the track)
        self.ramp_len = 0
        self.done = False
        self.started = False
        self.tag = tag
        self.is_pad = is_pad
        self.loop = loop  # wrap from out_pt back to in_pt, seamlessly, until faded by hand

    @property
    def length(self) -> float:
        return (self.out_pt - self.in_pt) / self.rate

    @property
    def elapsed(self) -> float:
        return (self.pos - self.in_pt) / self.rate

    @property
    def remaining(self) -> float:
        return max(0.0, (self.out_pt - self.pos) / self.rate)

    def fade(self, seconds: float) -> None:
        """Start fading this voice out by hand; it is done when the fade completes."""
        if self.manual_len == 0:
            self.manual_len = max(1, int(seconds * self.rate))
            self.manual_pos = 0


def _approach(cur: float, target: float, step: float) -> float:
    if cur < target:
        return min(target, cur + step)
    if cur > target:
        return max(target, cur - step)
    return cur


class Engine:
    def __init__(self, rate: int = 48000, blocksize: int = 1024, duck_db: float = -12.0) -> None:
        self.rate = rate
        self.blocksize = blocksize
        self.voices: list[Voice] = []
        self._scheduled: list[tuple[int, Voice]] = []
        self.lock = threading.Lock()
        self.master = 1.0
        self._master_cur = 1.0
        self.duck_level = 10 ** (duck_db / 20)
        self._duck_cur = 1.0
        self.paused = False
        self.mono = False  # sum to mono on both outputs, for a single input on the board
        self.peak = 0.0
        self.clock = 0  # samples rendered while not paused
        self.stream = None
        self.device_error = ""

    # ------------------------------------------------------------------ device

    @staticmethod
    def output_devices() -> list[tuple[int, str]]:
        import sounddevice as sd

        apis = sd.query_hostapis()
        out = []
        for i, d in enumerate(sd.query_devices()):
            if d["max_output_channels"] >= 2:
                out.append((i, f"{d['name']}  [{apis[d['hostapi']]['name']}]"))
        return out

    @staticmethod
    def default_output() -> Optional[int]:
        import sounddevice as sd

        d = sd.default.device
        return int(d[1]) if d is not None and d[1] is not None and d[1] >= 0 else None

    def start(self, device: Optional[int] = None) -> None:
        import sounddevice as sd

        self.stop()
        # A deep buffer: the mixer runs in Python, so a moment of contention (a decode finishing,
        # the window redrawing) must not be audible. A third of a second is nothing for house music.
        self.stream = sd.OutputStream(samplerate=self.rate, channels=2, dtype="float32", blocksize=self.blocksize,
                                      device=device, latency=0.35, callback=self._callback)
        self.stream.start()
        self.device_error = ""

    def stop(self) -> None:
        if self.stream is not None:
            try:
                self.stream.stop()
                self.stream.close()
            except Exception:
                pass
            self.stream = None

    def _callback(self, outdata, frames, time_info, status) -> None:
        outdata[:] = self.render(frames)

    # ------------------------------------------------------------------ control

    def play(self, voice: Voice, at_clock: Optional[int] = None) -> None:
        """Start a voice now, or at a sample clock value (see `clock`)."""
        with self.lock:
            if at_clock is None or at_clock <= self.clock:
                voice.started = True
                if voice.pos < voice.in_pt:
                    voice.pos = voice.in_pt  # a pos set beyond in_pt is deliberate (a seek)
                self.voices.append(voice)
            else:
                self._scheduled.append((int(at_clock), voice))

    def cancel(self, voice: Voice) -> bool:
        with self.lock:
            before = len(self._scheduled)
            self._scheduled = [(t, v) for (t, v) in self._scheduled if v is not voice]
            return len(self._scheduled) < before

    def fade_out(self, voice: Voice, seconds: float) -> None:
        with self.lock:
            voice.fade(seconds)

    def stop_all(self, seconds: float = 0.05, pads_too: bool = True) -> None:
        with self.lock:
            self._scheduled = []
            for v in self.voices:
                if pads_too or not v.is_pad:
                    v.fade(seconds)

    def pause(self) -> None:
        self.paused = True

    def resume(self) -> None:
        self.paused = False

    def set_master_db(self, db: float) -> None:
        self.master = 10 ** (db / 20)

    def active(self, pads: bool = False) -> list[Voice]:
        with self.lock:
            return [v for v in self.voices if not v.done and (pads or not v.is_pad)]

    def scheduled(self) -> list[tuple[int, Voice]]:
        with self.lock:
            return list(self._scheduled)

    # ------------------------------------------------------------------ rendering

    def render(self, frames: int) -> np.ndarray:
        out = np.zeros((frames, 2), dtype=np.float32)
        if self.paused:
            self.peak = 0.0
            return out
        with self.lock:
            block_end = self.clock + frames
            todo = [(v, 0) for v in self.voices if not v.done]
            due = [(t, v) for (t, v) in self._scheduled if t < block_end]
            if due:
                self._scheduled = [(t, v) for (t, v) in self._scheduled if t >= block_end]
                for t, v in sorted(due, key=lambda tv: tv[0]):
                    v.started = True
                    if v.pos < v.in_pt:
                        v.pos = v.in_pt
                    self.voices.append(v)
                    todo.append((v, max(0, t - self.clock)))
            any_pad = any(v.is_pad and not v.done for v in self.voices)
            self._duck_cur = _approach(self._duck_cur, self.duck_level if any_pad else 1.0, frames / (0.3 * self.rate))
            self._master_cur = _approach(self._master_cur, self.master, frames / (0.05 * self.rate))
            for v, off in todo:
                self._mix(v, out, off, frames - off)
            self.voices = [v for v in self.voices if not v.done]
            self.clock += frames
        if self.mono:
            m = out.mean(axis=1)  # (L+R)/2: centred material keeps its level, wide mixes fold in
            out[:, 0] = m
            out[:, 1] = m
        out *= self._master_cur
        np.clip(out, -1.0, 1.0, out=out)
        self.peak = float(np.max(np.abs(out))) if frames else 0.0
        return out

    def _envelope(self, v: Voice, n: int) -> np.ndarray:
        """Gain per sample for the next n samples of v (fades, a hand fade, ducking); advances the hand fade."""
        idx = v.pos + np.arange(n)
        env = np.full(n, v.gain, dtype=np.float32)
        if v.fade_in > 0:
            x = np.clip((idx - v.in_pt) / v.fade_in, 0.0, 1.0)
            env *= np.sin(0.5 * math.pi * x).astype(np.float32)
        if v.ramp_len > 0:
            x = np.clip((idx - v.ramp_from) / v.ramp_len, 0.0, 1.0)
            env *= np.sin(0.5 * math.pi * x).astype(np.float32)
        if v.fade_out > 0:
            x = np.clip((v.out_pt - idx) / v.fade_out, 0.0, 1.0)
            env *= np.sin(0.5 * math.pi * x).astype(np.float32)
        if v.manual_len > 0:
            x = np.clip(1.0 - (v.manual_pos + np.arange(n)) / v.manual_len, 0.0, 1.0)
            env *= np.sin(0.5 * math.pi * x).astype(np.float32)
            v.manual_pos += n
        if not v.is_pad:
            env *= self._duck_cur
        return env

    def _mix(self, v: Voice, out: np.ndarray, off: int, n: int) -> None:
        pos_out = off
        while n > 0:
            avail = v.out_pt - v.pos
            if avail <= 0:
                if v.loop and v.out_pt > v.in_pt and v.manual_len == 0:
                    v.pos = v.in_pt  # seamless wrap: the rest of this block comes from the top of the loop
                    avail = v.out_pt - v.pos
                else:
                    v.done = True
                    return
            m = min(n, avail)
            env = self._envelope(v, m)
            chunk = v.pcm[v.pos: v.pos + m].astype(np.float32) * (env / FULL_SCALE)[:, None]
            out[pos_out: pos_out + m] += chunk
            v.pos += m
            pos_out += m
            n -= m
            if v.manual_len > 0 and v.manual_pos >= v.manual_len:
                v.done = True
                return
        if v.pos >= v.out_pt and not v.loop:
            v.done = True
