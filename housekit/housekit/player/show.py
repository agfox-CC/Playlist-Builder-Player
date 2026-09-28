"""The transport: which cue is on standby, what is playing, how the next one follows.

GO starts the standby cue (crossfading over anything playing). With auto-follow, the next cue is
decoded ahead and scheduled on the sample clock to start `overlap` seconds before the current one
ends, the overlap chosen from how the track ends: under a fade-out, a breath after a cold stop.
Pads play on top and duck the bed. All decoding happens on a worker thread; `tick` is called
from the UI a few times a second to attach follow-ons and keep the prefetch going."""
from __future__ import annotations

import threading
from collections import OrderedDict
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

import numpy as np

from .cues import Cue, CueList, Pad, Show
from .decode import Edges, analyze_edges, db_to_gain, decode, envelope
from .engine import Engine, Voice


@dataclass
class Decoded:
    pcm: np.ndarray
    edges: Edges
    overview: Optional[np.ndarray] = None  # waveform overview for the display, 0..1 per column


class Player:
    def __init__(self, show: Show, engine: Engine, decoder: Callable[[Path, int], np.ndarray] = decode,
                 analyzer: Callable[[np.ndarray, int], Edges] = analyze_edges) -> None:
        self.show = show
        self.engine = engine
        self.decoder = decoder
        self.analyzer = analyzer
        self.list_index = 0
        self.standby = 0
        self.current: Optional[tuple[Cue, Voice, Decoded]] = None
        self.queued: Optional[tuple[Cue, Voice]] = None
        self.state = "stopped"  # stopped | playing | paused
        self.error = ""
        self._cache: OrderedDict[str, Decoded] = OrderedDict()
        self._pending: dict[str, Future] = {}
        self._lock = threading.Lock()
        self._pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="decode")
        self._pad_voices: dict[str, Voice] = {}
        self._audition: Optional[tuple[int, float]] = None  # (cue index to return to, seconds to play into the next cue)
        self._audition_stop: Optional[int] = None  # engine clock at which the audition stops
        engine.duck_level = db_to_gain(float(show.settings.get("duck_db", -12.0)))
        engine.set_master_db(float(show.settings.get("master_db", 0.0)))
        engine.mono = bool(show.settings.get("mono", False))

    def set_mono(self, on: bool) -> None:
        self.show.settings["mono"] = bool(on)
        self.engine.mono = bool(on)

    # ------------------------------------------------------------------ lists / cues

    @property
    def cue_list(self) -> Optional[CueList]:
        return self.show.lists[self.list_index] if 0 <= self.list_index < len(self.show.lists) else None

    def cue_at(self, i: int) -> Optional[Cue]:
        cl = self.cue_list
        return cl.cues[i] if cl and 0 <= i < len(cl.cues) else None

    @property
    def standby_cue(self) -> Optional[Cue]:
        return self.cue_at(self.standby)

    def select_list(self, i: int) -> None:
        if i != self.list_index and 0 <= i < len(self.show.lists):
            self.list_index = i
            self.select(0)

    def select(self, i: int) -> None:
        cl = self.cue_list
        if not cl:
            return
        i = max(0, min(i, len(cl.cues)))  # len(cues) = past the end: nothing on standby
        if i != self.standby:
            self.standby = i
            self._drop_queued()
        self.prefetch()

    # ------------------------------------------------------------------ decoding

    def _key(self, path: str) -> str:
        return str(self.show.abs_path(path)).lower()

    def ready(self, cue: Cue) -> Optional[Decoded]:
        with self._lock:
            d = self._cache.get(self._key(cue.path))
            if d is not None:
                self._cache.move_to_end(self._key(cue.path))
            return d

    def _decode_job(self, path: str) -> Decoded:
        p = self.show.abs_path(path)
        pcm = self.decoder(p, self.engine.rate)
        d = Decoded(pcm=pcm, edges=self.analyzer(pcm, self.engine.rate), overview=envelope(pcm))
        with self._lock:
            self._cache[self._key(path)] = d
            while len(self._cache) > 6:
                self._cache.popitem(last=False)
            self._pending.pop(self._key(path), None)
        return d

    def _request(self, path: str) -> Optional[Future]:
        k = self._key(path)
        with self._lock:
            if k in self._cache or k in self._pending:
                return self._pending.get(k)
            fut = self._pool.submit(self._decode_job, path)
            self._pending[k] = fut
            return fut

    def _decode_now(self, cue: Cue) -> Decoded:
        d = self.ready(cue)
        if d is not None:
            return d
        fut = self._request(cue.path)
        if fut is not None:
            return fut.result()
        d = self.ready(cue)
        if d is None:
            raise RuntimeError(f"could not decode {cue.path}")
        return d

    def prefetch(self) -> None:
        """Decode the standby cue and the one after it ahead of time."""
        for i in (self.standby, self.standby + 1):
            c = self.cue_at(i)
            if c is not None:
                self._request(c.path)

    # ------------------------------------------------------------------ voices

    def _points(self, cue: Cue, d: Decoded) -> tuple[int, int]:
        rate = self.engine.rate
        trim = bool(self.show.settings.get("auto_trim", True))
        in_pt = int(cue.in_point * rate) if cue.in_point is not None else (d.edges.in_sample if trim else 0)
        out_pt = int(cue.out_point * rate) if cue.out_point is not None else (d.edges.out_sample if trim else len(d.pcm))
        return in_pt, out_pt

    def overlap_for(self, cue: Cue, d: Decoded) -> float:
        """Seconds the next cue overlaps this one, from the cue's own setting or the way it ends."""
        if cue.crossfade is not None:
            return max(0.0, float(cue.crossfade))
        default = float(self.show.settings.get("crossfade", 6.0))
        e = d.edges
        if e.ending == "fade":
            return max(0.5, min(default, e.fade_seconds if e.fade_seconds > 0 else default))
        if e.ending == "ring":
            return min(default, 1.5)
        return float(self.show.settings.get("cold_gap", 0.25))

    def _voice(self, cue: Cue, d: Decoded, fade_in: float = 0.0) -> Voice:
        rate = self.engine.rate
        in_pt, out_pt = self._points(cue, d)
        return Voice(d.pcm, rate, in_pt=in_pt, out_pt=out_pt, gain=db_to_gain(cue.gain_db),
                     fade_in=int(max(cue.fade_in, fade_in, 0.008) * rate),  # at least 8 ms: no click on a hot start
                     fade_out=0 if cue.loop else int(cue.fade_out * rate), tag=cue, loop=cue.loop)

    # ------------------------------------------------------------------ transport

    def go(self, preroll: float = 0.0) -> Optional[Cue]:
        """Start the standby cue now. Over a playing cue that is a crossfade; a queued follow-on for
        the same cue is simply brought forward. `preroll` > 0 starts that many seconds before the
        cue's end instead of at its start (used to audition a transition)."""
        cue = self.standby_cue
        if cue is None:
            return None
        self.error = ""
        self._audition = None
        self._audition_stop = None
        try:
            d = self._decode_now(cue)
        except Exception as e:
            self.error = f"{cue.display}: {e}"
            self.select(self.standby + 1)
            return None
        xf = float(self.show.settings.get("go_crossfade", 2.0))
        was_playing = self.current is not None and not self.current[1].done
        self._drop_queued()
        if was_playing:
            self.engine.fade_out(self.current[1], xf)
        voice = self._voice(cue, d, fade_in=xf if was_playing else 0.0)
        if preroll > 0:
            voice.in_pt = max(voice.in_pt, voice.out_pt - int(preroll * self.engine.rate))
            voice.pos = voice.in_pt
            voice.fade_in = min(voice.fade_in, int(0.5 * self.engine.rate))
        self.engine.resume()
        self.engine.play(voice)
        self.current = (cue, voice, d)
        self.state = "playing"
        self.standby = self.standby + 1
        self.prefetch()
        self._attach_follow()
        return cue

    def _drop_queued(self) -> None:
        if self.queued is not None:
            self.engine.cancel(self.queued[1])
            self.queued = None

    def _attach_follow(self) -> None:
        """Schedule the standby cue to follow the current one, once its audio is decoded."""
        if self.current is None or self.queued is not None:
            return
        cue, voice, d = self.current
        if not cue.auto_follow or cue.loop or voice.done:
            return
        nxt = self.standby_cue
        if nxt is None:
            return
        nd = self.ready(nxt)
        if nd is None:
            self._request(nxt.path)
            return
        rate = self.engine.rate
        overlap = self.overlap_for(cue, d)
        ov = int(overlap * rate)
        with self.engine.lock:
            voice.fade_out = max(voice.fade_out, ov)
            remaining = voice.out_pt - voice.pos
            start = self.engine.clock + max(0, remaining - ov) + int(max(0.0, nxt.pre_wait) * rate)
        nv = self._voice(nxt, nd, fade_in=overlap if nxt.pre_wait <= 0 else 0.0)
        self.engine.play(nv, at_clock=start)
        self.queued = (nxt, nv)

    def tick(self) -> None:
        """Called from the UI several times a second."""
        if self._audition_stop is not None and self.engine.clock >= self._audition_stop:
            back_to = self._audition[0] if self._audition else self.standby
            self.stop(0.5)
            self.select(back_to)
        if self.current is not None:
            cue, voice, d = self.current
            if self.queued is not None and self.queued[1].started:
                nc, nv = self.queued
                self.current = (nc, nv, self.ready(nc) or d)
                self.queued = None
                self.standby = self.standby + 1
                self.prefetch()
                if self._audition is not None:  # heard the join: stop a few seconds into the next cue
                    self._audition_stop = self.engine.clock + int(self._audition[1] * self.engine.rate)
            elif voice.done:
                if self.queued is None:
                    if cue.auto_follow and self.standby_cue is not None and not self.engine.paused:
                        self.go()  # decoding ran late: start the next cue now
                    else:
                        self.current = None
                        self.state = "stopped"
        self._attach_follow()
        self.prefetch()
        for k, v in list(self._pad_voices.items()):
            if v.done:
                self._pad_voices.pop(k, None)

    def seek(self, sample: int, xfade: Optional[float] = None) -> bool:
        """Jump the playing cue to `sample` (absolute, within the track) with a short crossfade:
        the old position fades out while the new one fades in, so nothing clicks or jumps."""
        if self.current is None or self.current[1].done:
            return False
        cue, old, d = self.current
        xf = float(self.show.settings.get("seek_crossfade", 0.4)) if xfade is None else xfade
        rate = self.engine.rate
        sample = max(old.in_pt, min(int(sample), old.out_pt - 1))
        new = Voice(d.pcm, rate, in_pt=old.in_pt, out_pt=old.out_pt, gain=old.gain, fade_in=0,
                    fade_out=0 if old.loop else int(cue.fade_out * rate), tag=cue, loop=old.loop)
        new.pos = sample
        new.ramp_from, new.ramp_len = sample, max(1, int(xf * rate))
        self._drop_queued()  # the follow-on was timed from the old position; tick re-attaches it
        self.engine.fade_out(old, xf)
        self.engine.play(new)
        self.current = (cue, new, d)
        return True

    def seek_seconds(self, delta: float) -> bool:
        """Nudge the playing cue by `delta` seconds (negative = back)."""
        if self.current is None:
            return False
        v = self.current[1]
        return self.seek(v.pos + int(delta * self.engine.rate))

    def pause(self) -> None:
        if self.state == "playing":
            self.engine.pause()
            self.state = "paused"
        elif self.state == "paused":
            self.engine.resume()
            self.state = "playing"

    def stop(self, seconds: Optional[float] = None) -> None:
        self._drop_queued()
        self._audition = None
        self._audition_stop = None
        self.engine.stop_all(0.05 if seconds is None else seconds, pads_too=True)
        self.engine.resume()
        self.current = None
        self.state = "stopped"

    def audition(self, i: int, tail: float = 15.0, into: float = 8.0) -> bool:
        """Play the last `tail` seconds of cue i into cue i+1, stop `into` seconds after the join, and
        put the standby back on cue i. The way to check a crossfade before doors."""
        if self.cue_at(i) is None or self.cue_at(i + 1) is None:
            return False
        self.select(i)
        if self.go(preroll=tail) is None:
            return False
        self._audition = (i, into)
        self._audition_stop = None
        return True

    # ------------------------------------------------------------------ editing lists

    def add_cues(self, cues: list[Cue], at: Optional[int] = None) -> int:
        """Insert cues into the current list at `at` (default: the end). Returns the index used."""
        cl = self.cue_list
        if cl is None:
            return 0
        at = len(cl.cues) if at is None else max(0, min(at, len(cl.cues)))
        cl.cues[at:at] = list(cues)
        if self.standby >= at and cues and self.standby < len(cl.cues) - len(cues) + (1 if self.standby > at else 0):
            pass
        self._drop_queued()
        self.prefetch()
        return at

    def remove_cue(self, i: int) -> Optional[Cue]:
        cl = self.cue_list
        if cl is None or not 0 <= i < len(cl.cues):
            return None
        cue = cl.cues.pop(i)
        if self.standby > i:
            self.standby -= 1
        self.standby = max(0, min(self.standby, len(cl.cues)))
        self._drop_queued()
        self.prefetch()
        return cue

    def move_cue(self, i: int, j: int) -> None:
        cl = self.cue_list
        if cl is None or not 0 <= i < len(cl.cues):
            return
        j = max(0, min(j, len(cl.cues) - 1))
        if i == j:
            return
        standby_cue = self.cue_at(self.standby)
        cue = cl.cues.pop(i)
        cl.cues.insert(j, cue)
        if standby_cue is not None:
            self.standby = cl.cues.index(standby_cue)
        self._drop_queued()
        self.prefetch()

    def new_list(self, name: str, cues: Optional[list[Cue]] = None) -> int:
        self.show.lists.append(CueList(name=name or "List", cues=list(cues or []), source=""))
        return len(self.show.lists) - 1

    def duplicate_list(self, i: int, name: Optional[str] = None) -> int:
        src = self.show.lists[i]
        copy = [Cue(**{k: v for k, v in c.__dict__.items()}) for c in src.cues]
        return self.new_list(name or f"{src.name} copy", copy)

    def rename_list(self, i: int, name: str) -> None:
        if 0 <= i < len(self.show.lists) and name.strip():
            self.show.lists[i].name = name.strip()

    def delete_list(self, i: int) -> None:
        if not 0 <= i < len(self.show.lists):
            return
        del self.show.lists[i]
        if self.list_index >= len(self.show.lists):
            self.list_index = max(0, len(self.show.lists) - 1)
        elif self.list_index > i:
            self.list_index -= 1
        self.standby = 0
        self._drop_queued()
        self.prefetch()

    def panic(self) -> None:
        self.stop(float(self.show.settings.get("panic_fade", 2.0)))

    def play_pad(self, i: int) -> Optional[Pad]:
        if not 0 <= i < len(self.show.pads):
            return None
        pad = self.show.pads[i]
        if not pad.path:
            return None
        v = self._pad_voices.get(pad.key)
        if v is not None and not v.done:
            self.engine.fade_out(v, 0.3)  # pressing again stops it
            return pad
        fake = Cue(path=pad.path, title=pad.name)
        try:
            d = self._decode_now(fake)
        except Exception as e:
            self.error = f"pad {pad.key}: {e}"
            return None
        voice = Voice(d.pcm, self.engine.rate, in_pt=d.edges.in_sample, out_pt=d.edges.out_sample,
                      gain=db_to_gain(pad.gain_db), fade_in=int(0.02 * self.engine.rate), tag=pad, is_pad=True)
        self.engine.resume()
        self.engine.play(voice)
        self._pad_voices[pad.key] = voice
        return pad

    def set_master_db(self, db: float) -> None:
        self.show.settings["master_db"] = float(db)
        self.engine.set_master_db(float(db))

    # ------------------------------------------------------------------ status

    def current_decoded(self) -> Optional[Decoded]:
        return self.current[2] if self.current is not None else None

    def current_points(self) -> Optional[tuple[int, int, int]]:
        """(in, out, total) samples of the playing voice, for the waveform display."""
        if self.current is None:
            return None
        v = self.current[1]
        return v.in_pt, v.out_pt, len(v.pcm)

    def status(self) -> dict:
        cur = self.current
        st = {"state": self.state, "list": self.cue_list.name if self.cue_list else "", "error": self.error,
              "audition": self._audition is not None, "looping": cur is not None and not cur[1].done and cur[1].loop,
              "current": None, "elapsed": 0.0, "remaining": 0.0, "length": 0.0, "next": self.standby_cue,
              "next_ready": self.standby_cue is not None and self.ready(self.standby_cue) is not None,
              "queued": self.queued[0] if self.queued else None, "peak": self.engine.peak, "pads": [k for k, v in self._pad_voices.items() if not v.done]}
        if cur is not None and not cur[1].done:
            cue, voice, _ = cur
            st.update({"current": cue, "elapsed": voice.elapsed, "remaining": voice.remaining, "length": voice.length})
        return st

    def close(self) -> None:
        self.engine.stop_all(0.02)
        self._pool.shutdown(wait=False, cancel_futures=True)
