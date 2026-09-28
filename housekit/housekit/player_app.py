"""HouseKit Player: a standalone window that plays the folders HouseKit builds, cue-list style.

    python -m housekit.player_app [gig folder | playlist folder | folder of audio | .m3u8]

Space = GO, P = pause, Esc = stop, Shift+Esc = panic (fade everything out), Up/Down = standby cue,
1-8 = pads, M = mono/stereo, F11 = show display. Needs no catalog and no config: it reads the
folder it is given.
"""
from __future__ import annotations

import math
import subprocess
import sys
import traceback
from pathlib import Path
from typing import Optional

import numpy as np
import tkinter as tk
from tkinter import filedialog, messagebox, simpledialog, ttk

from housekit import __version__
from housekit.player.cues import Cue, Show, classify_drop, cues_from_paths, load_show, save_show

QUICK_LIST = "Quick List"
from housekit.player.engine import Engine
from housekit.player.show import Player
from housekit.theme import MODES, ThemeMixin, font, glyph, set_dark_titlebar

ENDING_WORD = {"fade": "fade", "ring": "ring", "cold": "cold"}


def mmss(seconds: Optional[float]) -> str:
    s = int(round(seconds or 0))
    return f"{s // 60}:{s % 60:02d}"


def _fnum(s: str, default: Optional[float] = None) -> Optional[float]:
    s = (s or "").strip()
    if not s:
        return default
    return float(s)


class Meter:
    """A segmented level meter with peak hold, drawn on a canvas: green, amber in the last third, red at the top."""

    def __init__(self, parent: tk.Misc, pal: dict, width: int = 220, height: int = 14, segments: int = 28) -> None:
        self.canvas = tk.Canvas(parent, width=width, height=height, highlightthickness=0, bd=0)
        self.canvas.keep_colors = True
        self.n = segments
        self.w, self.h = width, height
        self.pal = pal
        self.hold = 0.0
        self.hold_age = 0
        self.rects: list[int] = []
        gap = 2
        seg_w = (width - gap * (segments - 1)) / segments
        for i in range(segments):
            x0 = i * (seg_w + gap)
            self.rects.append(self.canvas.create_rectangle(x0, 0, x0 + seg_w, height, outline="", fill=pal["meter_bg"]))
        self.hold_line = self.canvas.create_line(0, 0, 0, height, fill=pal["fg"], width=2, state="hidden")
        self.set_palette(pal)

    def set_palette(self, pal: dict) -> None:
        self.pal = pal
        self.canvas.configure(bg=pal["bg"])
        self.update(0.0)

    def _colour(self, i: int) -> str:
        frac = (i + 1) / self.n
        if frac > 0.93:
            return "#e5484d"
        if frac > 0.72:
            return self.pal["warn"]
        return self.pal["ok"]

    def update(self, peak: float) -> None:
        db = 20 * math.log10(max(peak, 1e-6))
        frac = max(0.0, min(1.0, (db + 60) / 60))
        lit = int(round(frac * self.n))
        for i, r in enumerate(self.rects):
            self.canvas.itemconfigure(r, fill=self._colour(i) if i < lit else self.pal["meter_bg"])
        if frac >= self.hold:
            self.hold, self.hold_age = frac, 0
        else:
            self.hold_age += 1
            if self.hold_age > 12:
                self.hold = max(frac, self.hold - 0.02)
        if self.hold > 0.02:
            x = self.hold * self.w
            self.canvas.coords(self.hold_line, x, 0, x, self.h)
            self.canvas.itemconfigure(self.hold_line, state="normal", fill=self.pal["fg"])
        else:
            self.canvas.itemconfigure(self.hold_line, state="hidden")


class Waveform:
    """The playing track's waveform overview with the trimmed ends dimmed, the crossfade region marked
    and a play head; the played part is tinted with the accent colour."""

    def __init__(self, parent: tk.Misc, pal: dict, height: int = 58, on_seek=None) -> None:
        self.canvas = tk.Canvas(parent, height=height, highlightthickness=0, bd=0, cursor="hand2")
        self.canvas.keep_colors = True
        self.pal = pal
        self.h = height
        self.env: Optional[np.ndarray] = None
        self.in_frac = self.out_frac = 0.0
        self.xfade_frac = 1.0
        self.head_frac = 0.0
        self.on_seek = on_seek  # called with a fraction 0..1 of the whole track when the user picks a spot
        self._scrub: Optional[float] = None
        self.canvas.bind("<Configure>", lambda _e: self.redraw())
        self.canvas.bind("<Button-1>", self._press)
        self.canvas.bind("<B1-Motion>", self._drag)
        self.canvas.bind("<ButtonRelease-1>", self._release)
        self.set_palette(pal)

    def _frac_at(self, x: float) -> float:
        w = self.canvas.winfo_width() or 600
        return max(0.0, min(1.0, x / w))

    def _press(self, event) -> None:
        if self.env is None:
            return
        self._scrub = self._frac_at(event.x)
        self._draw_scrub()

    def _drag(self, event) -> None:
        if self._scrub is None:
            return
        self._scrub = self._frac_at(event.x)
        self._draw_scrub()

    def _release(self, event) -> None:
        if self._scrub is None:
            return
        frac = self._frac_at(event.x)
        self._scrub = None
        self.canvas.delete("scrub")
        if self.on_seek is not None:
            self.on_seek(frac)

    def _draw_scrub(self) -> None:
        c = self.canvas
        c.delete("scrub")
        if self._scrub is None:
            return
        x = self._scrub * (c.winfo_width() or 600)
        c.create_line(x, 0, x, self.h, fill=self.pal["warn"], width=2, dash=(3, 2), tags=("scrub",))

    def set_palette(self, pal: dict) -> None:
        self.pal = pal
        self.canvas.configure(bg=pal["meter_bg"])
        self.redraw()

    def set_track(self, env: Optional[np.ndarray], in_frac: float = 0.0, out_frac: float = 1.0, xfade_frac: float = 1.0) -> None:
        self.env, self.in_frac, self.out_frac, self.xfade_frac = env, in_frac, out_frac, xfade_frac
        self.head_frac = in_frac
        self.redraw()

    def set_head(self, frac: float) -> None:
        self.head_frac = max(0.0, min(1.0, frac))
        c = self.canvas
        w = c.winfo_width() or 600
        x = self.head_frac * w
        c.coords("head", x, 0, x, self.h)
        c.coords("played", 0, 0, x, self.h)

    def redraw(self) -> None:
        c = self.canvas
        c.delete("all")
        w = c.winfo_width() or 600
        h = self.h
        mid = h / 2
        if self.env is None or len(self.env) == 0:
            c.create_line(0, mid, w, mid, fill=self.pal["border"])
            return
        n = len(self.env)
        for i, v in enumerate(self.env):
            x = (i + 0.5) * w / n
            y = max(1.0, float(v) * (mid - 2))
            c.create_line(x, mid - y, x, mid + y, fill=self.pal["wave"], width=max(1, w / n))
        if self.in_frac > 0:
            c.create_rectangle(0, 0, self.in_frac * w, h, fill=self.pal["meter_bg"], stipple="gray50", outline="")
        if self.out_frac < 1:
            c.create_rectangle(self.out_frac * w, 0, w, h, fill=self.pal["meter_bg"], stipple="gray50", outline="")
        if self.xfade_frac < self.out_frac:
            c.create_rectangle(self.xfade_frac * w, 0, self.out_frac * w, h, fill=self.pal["warn"], stipple="gray25", outline="")
        x = self.head_frac * w
        c.create_rectangle(0, 0, x, h, fill=self.pal["accent"], stipple="gray25", outline="", tags=("played",))
        c.create_line(x, 0, x, h, fill=self.pal["accent"], width=2, tags=("head",))


class Marquee:
    """A single line of text that stays put when it fits its box and glides left, with a pause at
    the start of each pass, when it is wider than the box."""

    GAP = 60  # pixels between the end of the text and its repeat
    SPEED = 45.0  # pixels per second
    PAUSE = 2.0  # seconds to hold the start before scrolling

    def __init__(self, parent: tk.Misc, pal: dict, font_spec: tuple, colour_key: str = "fg", height: Optional[int] = None) -> None:
        import tkinter.font as tkfont

        self.font = tkfont.Font(font=font_spec)
        h = height or (self.font.metrics("linespace") + 4)
        self.canvas = tk.Canvas(parent, height=h, width=10, highlightthickness=0, bd=0)
        self.canvas.keep_colors = True
        self.pal = pal
        self.colour_key = colour_key
        self.text = ""
        self.text_w = 0
        self.offset = 0.0
        self.hold_until = 0.0
        self.last = 0.0
        self.canvas.bind("<Configure>", lambda _e: self._draw())
        self.set_palette(pal)
        self._tick()

    def set_palette(self, pal: dict) -> None:
        self.pal = pal
        self.canvas.configure(bg=pal["bg"])
        self._draw()

    def set(self, text: str) -> None:
        import time

        if text == self.text:
            return
        self.text = text
        self.text_w = self.font.measure(text) if text else 0
        self.offset = 0.0
        self.hold_until = time.monotonic() + self.PAUSE
        self._draw()

    def _draw(self) -> None:
        c = self.canvas
        c.delete("all")
        if not self.text:
            return
        w = c.winfo_width() or 400
        y = int(c.winfo_height() or self.font.metrics("linespace")) // 2
        colour = self.pal[self.colour_key]
        if self.text_w <= w:
            c.create_text(0, y, text=self.text, anchor="w", font=self.font, fill=colour)
            return
        x = -int(self.offset)
        c.create_text(x, y, text=self.text, anchor="w", font=self.font, fill=colour)
        c.create_text(x + self.text_w + self.GAP, y, text=self.text, anchor="w", font=self.font, fill=colour)

    def _tick(self) -> None:
        import time

        try:
            if not self.canvas.winfo_exists():
                return
            now = time.monotonic()
            dt = min(0.1, now - self.last) if self.last else 0.0
            self.last = now
            w = self.canvas.winfo_width() or 400
            if self.text and self.text_w > w and now >= self.hold_until:
                self.offset += self.SPEED * dt
                if self.offset >= self.text_w + self.GAP:  # the repeat has reached the start: hold there again
                    self.offset = 0.0
                    self.hold_until = now + self.PAUSE
                self._draw()
        except tk.TclError:
            return
        self.canvas.after(40, self._tick)


class PlayerWindow(ThemeMixin, tk.Tk):
    def __init__(self, folder: Optional[Path] = None) -> None:
        super().__init__()
        self.title(f"HouseKit Player {__version__}")
        self.geometry("1240x800")
        self.minsize(980, 640)
        self.theme_init()
        self.engine = Engine()
        self.player: Optional[Player] = None
        self.show: Optional[Show] = None
        self.show_win: Optional[tk.Toplevel] = None
        self.devices: list[tuple[int, str]] = []
        self._rows: list[str] = []
        self._row_tags: list[str] = []
        self._ending_done: set[int] = set()
        self._wave_cue: Optional[Cue] = None
        self._drag_iid: Optional[str] = None
        self._drag_moved = False
        self.dnd_ok = False
        self._flash: Optional[tuple[str, float]] = None  # a status message and when it expires
        self._build()
        self._enable_dnd()
        self.theme_refresh(force=True)
        self.protocol("WM_DELETE_WINDOW", self._close)
        self._start_audio()
        if folder is not None:
            self.open_folder(Path(folder))
        self.after(100, self._poll)
        self.after(60, self._meter_tick)

    # ------------------------------------------------------------------ layout

    def _glyph_button(self, parent, name: str, text: str, command, **kw) -> ttk.Button:
        g, is_icon = glyph(name)
        if is_icon:
            return ttk.Button(parent, text=g, style="Glyph.TButton", command=command, **kw)
        return ttk.Button(parent, text=text, command=command, **kw)

    def _build(self) -> None:
        menubar = tk.Menu(self)
        view = tk.Menu(menubar, tearoff=False)
        self.theme_choice = tk.StringVar(value=self.theme_pref)
        for m in MODES:
            view.add_radiobutton(label={"system": "Follow system", "dark": "Dark", "light": "Light"}[m], value=m,
                                 variable=self.theme_choice, command=lambda: self.theme_set(self.theme_choice.get()))
        view.add_separator()
        view.add_command(label="Show display   F11", command=self._toggle_show)
        menubar.add_cascade(label="View", menu=view)
        self.configure(menu=menubar)

        # header: name, folder, output
        hdr = ttk.Frame(self, padding=(14, 10, 14, 6))
        hdr.pack(fill="x")
        ttk.Label(hdr, text="HOUSEKIT PLAYER", style="Title.TLabel").pack(side="left")
        self.folder_var = tk.StringVar(value="Open a gig or playlist folder, drop a folder here, or drop audio files to start a Quick List.")
        ttk.Label(hdr, textvariable=self.folder_var, style="Muted.TLabel").pack(side="left", padx=(16, 0))
        right = ttk.Frame(hdr)
        right.pack(side="right")
        ttk.Label(right, text="Output", style="Muted.TLabel").pack(side="left", padx=(0, 6))
        self.device_var = tk.StringVar()
        self.device_box = ttk.Combobox(right, textvariable=self.device_var, width=38, state="readonly")
        self.device_box.pack(side="left")
        self.device_box.bind("<<ComboboxSelected>>", lambda _e: self._pick_device())
        ttk.Button(right, text="Open folder", command=self._ask_folder).pack(side="left", padx=(14, 4))
        ttk.Button(right, text="Playlist file", command=self._ask_m3u).pack(side="left", padx=4)
        self._glyph_button(right, "display", "Show display", self._toggle_show).pack(side="left", padx=(10, 0))
        self.audio_var = tk.StringVar(value="")
        self.audio_label = ttk.Label(hdr, textvariable=self.audio_var)
        self.audio_label.pack(side="right", padx=12)
        ttk.Separator(self).pack(fill="x")

        # body
        body = ttk.Frame(self, padding=(14, 8, 14, 4))
        body.pack(fill="both", expand=True)
        left = ttk.Frame(body)
        left.pack(side="left", fill="y", padx=(0, 14))
        ttk.Label(left, text="CUE LISTS", style="Muted.TLabel").pack(anchor="w")
        self.lists_box = tk.Listbox(left, width=22, height=7, exportselection=False, font=font("body", 10), activestyle="none")
        self.lists_box.pack(fill="x", pady=(4, 0))
        self.lists_box.bind("<<ListboxSelect>>", lambda _e: self._pick_list())
        lt = ttk.Frame(left)
        lt.pack(fill="x", pady=(4, 0))
        for text, cmd in (("New", self._list_new), ("Copy", self._list_copy), ("Rename", self._list_rename), ("Delete", self._list_delete)):
            ttk.Button(lt, text=text, width=7, command=cmd).pack(side="left", padx=(0, 3))
        ttk.Label(left, text="PADS   keys 1-8, right-click to assign", style="Muted.TLabel").pack(anchor="w", pady=(16, 4))
        pads = ttk.Frame(left)
        pads.pack(fill="x")
        self.pad_buttons: list[ttk.Button] = []
        for i in range(8):
            b = ttk.Button(pads, text=f"{i + 1}", width=12, command=lambda i=i: self._pad(i))
            b.grid(row=i // 2, column=i % 2, sticky="ew", padx=2, pady=2)
            b.bind("<Button-3>", lambda _e, i=i: self._assign_pad(i))
            self.pad_buttons.append(b)
        pads.columnconfigure(0, weight=1)
        pads.columnconfigure(1, weight=1)
        opts = ttk.Frame(left)
        opts.pack(fill="x", pady=(18, 0))
        self.trim_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(opts, text="Trim silence", style="Switch.TCheckbutton", variable=self.trim_var, command=self._settings_changed).pack(anchor="w", pady=2)
        xf = ttk.Frame(opts)
        xf.pack(anchor="w", pady=(8, 0))
        ttk.Label(xf, text="Crossfade up to").pack(side="left")
        self.xfade_var = tk.StringVar(value="6")
        sp = ttk.Spinbox(xf, from_=0, to=30, increment=0.5, textvariable=self.xfade_var, width=5, command=self._settings_changed)
        sp.pack(side="left", padx=6)
        sp.bind("<FocusOut>", lambda _e: self._settings_changed())
        ttk.Label(xf, text="s").pack(side="left")

        center = ttk.Frame(body)
        center.pack(side="left", fill="both", expand=True)
        lh = ttk.Frame(center)
        lh.pack(fill="x", pady=(0, 4))
        self.list_title = tk.StringVar(value="")
        ttk.Label(lh, textvariable=self.list_title, style="Title.TLabel").pack(side="left")
        self.list_info = tk.StringVar(value="")
        ttk.Label(lh, textvariable=self.list_info, style="Muted.TLabel").pack(side="left", padx=12)
        tools = ttk.Frame(lh)
        tools.pack(side="right")
        for text, cmd, w in (("Add files", self._add_files, 0), ("Add folder", self._add_folder, 0), ("Remove", self._remove_cue, 0),
                             ("▲", lambda: self._nudge(-1), 3), ("▼", lambda: self._nudge(1), 3), ("Audition join", self._audition, 0)):
            ttk.Button(tools, text=text, command=cmd, **({"width": w} if w else {})).pack(side="left", padx=2)
        cols = ("#", "artist", "title", "len", "end", "in", "out", "xfade", "gain", "fade in", "fade out", "follow")
        self.tree = ttk.Treeview(center, columns=cols, show="headings", selectmode="browse")
        widths = {"#": 52, "artist": 190, "title": 280, "len": 56, "end": 50, "in": 52, "out": 52, "xfade": 56, "gain": 56, "fade in": 62, "fade out": 66, "follow": 58}
        for c in cols:
            self.tree.heading(c, text=c)
            self.tree.column(c, width=widths[c], anchor="w", stretch=(c in ("artist", "title")))
        sb = ttk.Scrollbar(center, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=sb.set)
        self.tree.pack(side="left", fill="both", expand=True)
        sb.pack(side="left", fill="y")
        self.tree.bind("<<TreeviewSelect>>", lambda _e: self._pick_cue())
        self.tree.bind("<Double-1>", lambda _e: self.go())
        self.tree.bind("<ButtonPress-1>", self._drag_start, add="+")
        self.tree.bind("<B1-Motion>", self._drag_motion, add="+")
        self.tree.bind("<ButtonRelease-1>", self._drag_end, add="+")

        # cue editor
        ed = ttk.Frame(self, padding=(14, 6, 14, 4))
        ed.pack(fill="x")
        ttk.Label(ed, text="SELECTED CUE", style="Muted.TLabel").grid(row=0, column=0, sticky="w", pady=(0, 2))
        self.edit_note = tk.StringVar(value="")
        ttk.Label(ed, textvariable=self.edit_note, style="Muted.TLabel").grid(row=0, column=1, columnspan=20, sticky="w", padx=(12, 0))
        self.ed_vars: dict[str, tk.StringVar] = {}
        fields = [("gain dB", "gain_db", 5), ("pre-wait", "pre_wait", 5), ("fade in", "fade_in", 5), ("fade out", "fade_out", 5),
                  ("in", "in_point", 6), ("out", "out_point", 6), ("crossfade", "crossfade", 5)]
        for i, (label, key, w) in enumerate(fields):
            ttk.Label(ed, text=label).grid(row=1, column=2 * i, sticky="w", padx=(0 if i == 0 else 8, 3))
            v = tk.StringVar()
            e = ttk.Entry(ed, textvariable=v, width=w)
            e.grid(row=1, column=2 * i + 1, sticky="w")
            e.bind("<Return>", lambda _e: self._apply_cue())
            self.ed_vars[key] = v
        ttk.Label(ed, text="(seconds)", style="Muted.TLabel").grid(row=1, column=2 * len(fields), sticky="w", padx=(6, 0))
        self.follow_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(ed, text="follow", style="Switch.TCheckbutton", variable=self.follow_var).grid(row=1, column=2 * len(fields) + 1, padx=(12, 4))
        self.loop_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(ed, text="loop", style="Switch.TCheckbutton", variable=self.loop_var).grid(row=1, column=2 * len(fields) + 2, padx=(0, 4))
        ttk.Button(ed, text="Apply", style="Accent.TButton", command=self._apply_cue).grid(row=1, column=2 * len(fields) + 3, padx=3)
        ttk.Button(ed, text="Reset", command=self._clear_cue).grid(row=1, column=2 * len(fields) + 4, padx=3)
        ttk.Separator(self).pack(fill="x", pady=(4, 0))

        # transport deck
        deck = ttk.Frame(self, padding=(14, 10, 14, 10))
        deck.pack(fill="x")
        deck.columnconfigure(1, weight=1)
        tr = ttk.Frame(deck)
        tr.grid(row=0, column=0, sticky="ns", padx=(0, 18))
        self.go_btn = tk.Button(tr, text="GO", font=font("display", 26, "bold"), width=6, bg="#2ea043", fg="white",
                                activebackground="#3fb950", activeforeground="white", relief="flat", bd=0, cursor="hand2", command=self.go)
        self.go_btn.keep_colors = True
        self.go_btn.grid(row=0, column=0, rowspan=2, sticky="ns", padx=(0, 10))
        tb = ttk.Frame(tr)
        tb.grid(row=0, column=1, sticky="w")
        self._glyph_button(tb, "prev", "Prev", lambda: self._move(-1)).pack(side="left", padx=2)
        self._glyph_button(tb, "pause", "Pause", self.pause).pack(side="left", padx=2)
        self._glyph_button(tb, "stop", "Stop", self.stop).pack(side="left", padx=2)
        self._glyph_button(tb, "next", "Next", lambda: self._move(1)).pack(side="left", padx=2)
        tb2 = ttk.Frame(tr)
        tb2.grid(row=1, column=1, sticky="w", pady=(6, 0))
        ttk.Button(tb2, text="PANIC", style="Big.TButton", command=self.panic).pack(side="left", padx=2)
        ttk.Label(tb2, text="Space GO   P pause   Esc stop   Shift+Esc panic   ← → 5 s   click the wave to jump", style="Muted.TLabel").pack(side="left", padx=(10, 0))

        now = ttk.Frame(deck)
        now.grid(row=0, column=1, sticky="nsew")
        now.columnconfigure(1, weight=1)
        self.state_var = tk.StringVar(value="STOPPED")
        ttk.Label(now, textvariable=self.state_var, style="Muted.TLabel").grid(row=0, column=0, sticky="w")
        self.now_marquee = Marquee(now, self.pal, font("display", 16, "bold"))
        self.now_marquee.canvas.grid(row=1, column=0, columnspan=2, sticky="ew")
        tc = ttk.Frame(now)
        tc.grid(row=2, column=0, columnspan=2, sticky="ew", pady=(2, 4))
        tc.columnconfigure(1, weight=1)
        self.elapsed_var = tk.StringVar(value="0:00")
        self.length_var = tk.StringVar(value="")
        self.remaining_var = tk.StringVar(value="")
        ttk.Label(tc, textvariable=self.elapsed_var, font=font("display", 30, "bold")).grid(row=0, column=0, sticky="w")
        ttk.Label(tc, textvariable=self.length_var, style="Muted.TLabel").grid(row=0, column=1, sticky="sw", padx=8, pady=(0, 6))
        self.remaining_label = ttk.Label(tc, textvariable=self.remaining_var, font=font("display", 30, "bold"), style="Accent.TLabel")
        self.remaining_label.grid(row=0, column=2, sticky="e")
        self.wave = Waveform(now, self.pal, on_seek=self._seek_frac)
        self.wave.canvas.grid(row=3, column=0, columnspan=2, sticky="ew")
        self.next_marquee = Marquee(now, self.pal, font("body", 10), colour_key="muted")
        self.next_marquee.canvas.grid(row=4, column=0, columnspan=2, sticky="ew", pady=(4, 0))

        lv = ttk.Frame(deck)
        lv.grid(row=0, column=2, sticky="ns", padx=(18, 0))
        ttk.Label(lv, text="LEVEL", style="Muted.TLabel").grid(row=0, column=0, sticky="w")
        self.meter = Meter(lv, self.pal)
        self.meter.canvas.grid(row=1, column=0, sticky="w", pady=(2, 8))
        mf = ttk.Frame(lv)
        mf.grid(row=2, column=0, sticky="ew")
        self.master_text = tk.StringVar(value="Master  0.0 dB")
        ttk.Label(mf, textvariable=self.master_text).pack(anchor="w")
        self.master_var = tk.DoubleVar(value=0.0)
        self.master_scale = ttk.Scale(mf, from_=-40, to=6, orient="horizontal", length=220, variable=self.master_var, command=lambda _v: self._set_master())
        self.master_scale.pack(anchor="w", pady=(2, 8))
        self.mono_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(lv, text="MONO out  (M)", style="Switch.TCheckbutton", variable=self.mono_var, command=self._apply_mono).grid(row=3, column=0, sticky="w")

        # status line
        st = ttk.Frame(self, padding=(14, 2, 14, 8))
        st.pack(fill="x")
        self.status_var = tk.StringVar(value="Ready")
        ttk.Label(st, textvariable=self.status_var, style="Mono.TLabel").pack(side="left")
        self._drop_frames = [hdr, body, ed, deck, st]  # the root itself cannot be a drop target; its top-level frames can

        for seq, fn in (("<space>", self.go), ("<Escape>", self.stop), ("<Shift-Escape>", self.panic), ("<F11>", self._toggle_show),
                        ("<Up>", lambda: self._move(-1)), ("<Down>", lambda: self._move(1)), ("<Delete>", self._remove_cue),
                        ("<Control-Up>", lambda: self._nudge(-1)), ("<Control-Down>", lambda: self._nudge(1)),
                        ("<Left>", lambda: self._seek_by(-5.0)), ("<Right>", lambda: self._seek_by(5.0)),
                        ("<Shift-Left>", lambda: self._seek_by(-30.0)), ("<Shift-Right>", lambda: self._seek_by(30.0))):
            self.bind_all(seq, lambda e, fn=fn: self._hotkey(e, fn))
        self.bind_all("<KeyPress>", self._keypress)

    # ------------------------------------------------------------------ seeking in the playing cue

    def _seek_frac(self, frac: float) -> None:
        """A spot picked on the waveform: jump there with a short crossfade."""
        if self.player is None:
            return
        pts = self.player.current_points()
        if pts is None:
            return
        in_pt, out_pt, total = pts
        sample = int(frac * total)
        if self.player.seek(sample):
            v = self.player.current[1]
            self.flash(f"Jumped to {mmss(v.elapsed)}", 3)

    def _seek_by(self, seconds: float) -> None:
        if self.player is not None and self.player.seek_seconds(seconds):
            v = self.player.current[1]
            self.flash(f"{'+' if seconds > 0 else '-'}{abs(seconds):.0f} s  ->  {mmss(v.elapsed)}", 3)

    # ------------------------------------------------------------------ drag and drop from Explorer

    def _enable_dnd(self) -> None:
        try:
            from tkinterdnd2 import DND_FILES, TkinterDnD

            TkinterDnD._require(self)
        except Exception as e:
            self.dnd_ok = False
            self.dnd_error = f"{type(e).__name__}: {e}"
            self.after(500, lambda: self.flash(f"Drag-and-drop from Explorer is unavailable ({self.dnd_error})", 20))
            return
        self.dnd_ok = True
        self.dnd_error = ""
        targets = [(self.tree, self._drop_on_tree), (self.lists_box, self._drop_on_lists)] + [(f, self._drop_on_window) for f in self._drop_frames]
        for widget, handler in targets:
            widget.drop_target_register(DND_FILES)
            widget.dnd_bind("<<Drop>>", handler)
        for i, b in enumerate(self.pad_buttons):
            b.drop_target_register(DND_FILES)
            b.dnd_bind("<<Drop>>", lambda e, i=i: self._drop_on_pad(e, i))

    def _dropped(self, event) -> list[Path]:
        try:
            return [Path(p) for p in self.tk.splitlist(event.data)]
        except Exception:
            return []

    # ------------------------------------------------------------------ nothing open yet

    def _playlists_dir(self) -> Path:
        """HouseKit's playlists folder (from housekit.toml when there is one), home of the Quick List."""
        from housekit.config import app_dir, find_config, load_config

        try:
            cfg_path = find_config(app_dir())
            if cfg_path is not None:
                return Path(load_config(cfg_path).playlists_dir)
        except Exception:
            pass
        return app_dir() / "playlists"

    def _open_quick_list(self) -> bool:
        """Open (creating on first use) the Quick List: a list under the playlists folder for things
        dropped or added while nothing else is open. Its cues.json lives there, never beside the music."""
        try:
            base = self._playlists_dir() / QUICK_LIST
            base.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            messagebox.showerror("Quick List", f"Could not create the Quick List folder:\n{e}")
            return False
        self.open_folder(base)
        return self.show is not None

    def _ensure_show(self, paths: list[Path]) -> list[Path]:
        """With nothing open: a dropped folder or playlist opens as the show, loose audio files start the
        Quick List. Returns the items still to be added to the current list."""
        if self.show is not None:
            return paths
        first, rest = classify_drop(paths)
        if first is not None:
            self.open_folder(first)
            return rest
        if not rest:
            self.flash("Nothing playable in what was dropped", 6)
            return []
        return rest if self._open_quick_list() else []

    def _drop_on_window(self, event) -> None:
        paths = self._dropped(event)
        if not paths:
            return
        rest = self._ensure_show(paths)
        if rest:
            self._add_paths(rest, at=None)

    def _drop_on_tree(self, event) -> None:
        paths = self._dropped(event)
        if not paths:
            return
        if self.show is None:
            self._drop_on_window(event)
            return
        y = event.y_root - self.tree.winfo_rooty()
        row = self.tree.identify_row(y)
        at = self._rows.index(row) + 1 if row in self._rows else None
        self._add_paths(paths, at=at)

    def _drop_on_lists(self, event) -> None:
        """A folder or playlist dropped on the list of lists becomes a new list."""
        paths = self._dropped(event)
        if not paths:
            return
        if self.show is None:
            self._drop_on_window(event)
            return
        cues = cues_from_paths(self.show, paths)
        if not cues:
            return
        name = paths[0].stem if paths[0].suffix else paths[0].name
        i = self.player.new_list(name, cues)
        self._save()
        self._refresh_lists()
        self.lists_box.selection_clear(0, "end")
        self.lists_box.selection_set(i)
        self._pick_list()
        self.flash(f"New list '{name}' with {len(cues)} cue(s)")

    def _drop_on_pad(self, event, i: int) -> None:
        paths = [p for p in self._dropped(event) if p.is_file()]
        if not paths:
            return
        if self.show is None and not self._open_quick_list():
            return
        pad = self.show.pads[i]
        pad.path, pad.name = str(paths[0]), paths[0].stem
        self._fill_pads()
        self._save()
        self.flash(f"Pad {i + 1} is now {pad.name}", 6)

    # ------------------------------------------------------------------ editing the list

    def _add_paths(self, paths: list[Path], at: Optional[int]) -> None:
        if self.player is None or self.show is None:
            return
        cues = cues_from_paths(self.show, paths)
        if not cues:
            self.flash("Nothing playable in what was dropped")
            return
        idx = self.player.add_cues(cues, at)
        self._save()
        self._fill_tree()
        self._select_row(idx)
        where = "at the end of" if at is None or at >= len(self.player.cue_list.cues) - len(cues) else f"at #{idx + 1} in"
        self.flash(f"Added {len(cues)} cue(s) {where} {self.player.cue_list.name}: " + ", ".join(c.display for c in cues[:3]) + (" ..." if len(cues) > 3 else ""), 8)

    def _after_selected(self) -> Optional[int]:
        i = self._selected_index()
        return None if i is None else i + 1

    def _add_files(self) -> None:
        files = filedialog.askopenfilenames(title="Add audio files", filetypes=[("Audio", "*.mp3 *.flac *.wav *.m4a *.aac *.ogg *.aiff *.wma *.m3u8 *.m3u"), ("All files", "*.*")])
        if not files:
            return
        paths = [Path(f) for f in files]
        if self.show is None and not self._open_quick_list():
            return
        self._add_paths(paths, at=self._after_selected())

    def _add_folder(self) -> None:
        d = filedialog.askdirectory(title="Add every audio file in a folder")
        if not d:
            return
        if self.show is None and not self._open_quick_list():
            return
        self._add_paths([Path(d)], at=self._after_selected())

    def _remove_cue(self) -> None:
        i = self._selected_index()
        if i is None or self.player is None:
            return
        cue = self.player.remove_cue(i)
        if cue is None:
            return
        self._save()
        self._fill_tree()
        self._select_row(min(i, len(self._rows) - 1))
        self.flash(f"Removed {cue.display} from the list (the file is untouched)")

    def _nudge(self, delta: int) -> None:
        i = self._selected_index()
        if i is None or self.player is None:
            return
        j = i + delta
        if not 0 <= j < len(self._rows):
            return
        self.player.move_cue(i, j)
        self._save()
        self._fill_tree()
        self._select_row(j)

    def _drag_start(self, event) -> None:
        self._drag_iid, self._drag_moved = None, False
        if self.tree.identify_region(event.x, event.y) in ("cell", "tree"):
            self._drag_iid = self.tree.identify_row(event.y) or None

    def _drag_motion(self, event) -> None:
        if not self._drag_iid or self.player is None:
            return
        target = self.tree.identify_row(event.y)
        if not target or target == self._drag_iid or target not in self._rows:
            return
        i, j = self._rows.index(self._drag_iid), self._rows.index(target)
        self.player.move_cue(i, j)
        self._rows.insert(j, self._rows.pop(i))
        self._row_tags.insert(j, self._row_tags.pop(i))
        self.tree.move(self._drag_iid, "", j)
        self._drag_moved = True

    def _drag_end(self, _event) -> None:
        if self._drag_iid and self._drag_moved and self.player is not None:
            self._save()
            self._fill_tree()
            self._select_row(self.player.standby)
        self._drag_iid, self._drag_moved = None, False

    def _audition(self) -> None:
        i = self._selected_index()
        if i is None or self.player is None:
            return
        if not self.player.audition(i):
            self.flash("Nothing follows that cue to audition into")
            return
        self.flash("Auditioning the join: last 15 s of this cue into the next, then stopping")

    # ------------------------------------------------------------------ the lists

    def _refresh_lists(self) -> None:
        self.lists_box.delete(0, "end")
        if self.show is None:
            return
        for cl in self.show.lists:
            self.lists_box.insert("end", f"  {cl.name}   {len(cl.cues)}")
        if self.player is not None and self.show.lists:
            self.lists_box.selection_clear(0, "end")
            self.lists_box.selection_set(self.player.list_index)

    def _list_new(self) -> None:
        if self.player is None and not self._open_quick_list():
            return
        name = simpledialog.askstring("New list", "Name for the new list:", parent=self)
        if not name:
            return
        i = self.player.new_list(name.strip())
        self._save()
        self._refresh_lists()
        self.lists_box.selection_clear(0, "end")
        self.lists_box.selection_set(i)
        self._pick_list()

    def _list_copy(self) -> None:
        if self.player is None or self.player.cue_list is None:
            return
        i = self.player.duplicate_list(self.player.list_index)
        self._save()
        self._refresh_lists()
        self.lists_box.selection_clear(0, "end")
        self.lists_box.selection_set(i)
        self._pick_list()

    def _list_rename(self) -> None:
        if self.player is None or self.player.cue_list is None:
            return
        name = simpledialog.askstring("Rename list", "New name:", initialvalue=self.player.cue_list.name, parent=self)
        if not name:
            return
        self.player.rename_list(self.player.list_index, name)
        self._save()
        self._refresh_lists()
        self.list_title.set(self.player.cue_list.name)

    def _list_delete(self) -> None:
        if self.player is None or self.player.cue_list is None:
            return
        cl = self.player.cue_list
        if not messagebox.askyesno("Delete list", f"Remove the list '{cl.name}' ({len(cl.cues)} cues) from this folder's cues?\n\nThe audio files stay where they are."):
            return
        self.player.delete_list(self.player.list_index)
        self._save()
        self._refresh_lists()
        self._fill_tree()
        self._select_row(self.player.standby)

    def on_theme_applied(self) -> None:
        """Colours that live outside the ttk style: row highlights, the drawn meter and waveform, the error label."""
        p = self.pal
        if hasattr(self, "tree"):
            self.tree.tag_configure("playing", background=p["tag_playing"], foreground=p["fg"])
            self.tree.tag_configure("standby", background=p["tag_standby"], foreground=p["fg"])
            self.tree.tag_configure("queued", background=p["tag_queued"], foreground=p["fg"])
            self.tree.tag_configure("odd", background=p["zebra"], foreground=p["fg"])
            self.tree.tag_configure("even", background=p["field"], foreground=p["fg"])
        if hasattr(self, "audio_label"):
            self.audio_label.configure(foreground=p["err"])
        if hasattr(self, "theme_choice"):
            self.theme_choice.set(self.theme_pref)
        if hasattr(self, "meter"):
            self.meter.set_palette(p)
        if hasattr(self, "wave"):
            self.wave.set_palette(p)
        for m in (getattr(self, "now_marquee", None), getattr(self, "next_marquee", None)):
            if m is not None:
                m.set_palette(p)

    def flash(self, text: str, seconds: float = 5.0) -> None:
        """Show a message on the status line for a while (the poll otherwise repaints it every tick)."""
        import time

        self._flash = (text, time.monotonic() + seconds)
        self.status_var.set(text)

    def _hotkey(self, event, fn) -> Optional[str]:
        w = event.widget
        if isinstance(w, (ttk.Entry, tk.Entry, ttk.Spinbox, tk.Spinbox, ttk.Combobox)):
            return None  # typing in a field
        fn()
        return "break"

    def _keypress(self, event) -> None:
        w = event.widget
        if isinstance(w, (ttk.Entry, tk.Entry, ttk.Spinbox, tk.Spinbox, ttk.Combobox)):
            return
        if event.char and event.char in "12345678":
            self._pad(int(event.char) - 1)
        elif event.char and event.char.lower() == "p":
            self.pause()
        elif event.char and event.char.lower() == "m":
            self.mono_var.set(not self.mono_var.get())
            self._apply_mono()

    # ------------------------------------------------------------------ audio device

    def _start_audio(self) -> None:
        try:
            self.devices = Engine.output_devices()
        except Exception as e:
            self.devices = []
            self.audio_var.set(f"No audio output: {e}")
            return
        self.device_box["values"] = [name for _, name in self.devices]
        wanted = self.show.settings.get("device") if self.show is not None else None
        idx = None
        if wanted:
            for i, name in self.devices:
                if name == wanted:
                    idx = i
        if idx is None:
            try:
                idx = Engine.default_output()
            except Exception:
                idx = None
        try:
            self.engine.start(idx)
            self.audio_var.set("")
        except Exception as e:
            self.audio_var.set(f"Could not open the output: {e}")
        for i, name in self.devices:
            if i == idx:
                self.device_var.set(name)

    def _pick_device(self) -> None:
        name = self.device_var.get()
        for i, n in self.devices:
            if n == name:
                try:
                    self.engine.start(i)
                    self.audio_var.set("")
                except Exception as e:
                    self.audio_var.set(f"Could not open {name}: {e}")
                if self.show is not None:
                    self.show.settings["device"] = name
                    self._save()
                return

    # ------------------------------------------------------------------ folder / lists

    def _ask_folder(self) -> None:
        d = filedialog.askdirectory(title="Open a gig or playlist folder")
        if d:
            self.open_folder(Path(d))

    def _ask_m3u(self) -> None:
        f = filedialog.askopenfilename(title="Open a playlist", filetypes=[("Playlists", "*.m3u8 *.m3u"), ("All files", "*.*")])
        if f:
            self.open_folder(Path(f))

    def open_folder(self, folder: Path) -> None:
        if self.player is not None:
            self.player.close()
            self.player = None
        try:
            self.show = load_show(folder)
        except Exception as e:
            messagebox.showerror("Can't open", f"{folder}\n\n{e}")
            return
        self.player = Player(self.show, self.engine)
        self.folder_var.set(str(folder))
        self.title(f"HouseKit Player  -  {Path(self.show.folder).name}")
        self.master_var.set(float(self.show.settings.get("master_db", 0.0)))
        self._set_master()
        self.xfade_var.set(f"{float(self.show.settings.get('crossfade', 6.0)):g}")
        self.trim_var.set(bool(self.show.settings.get("auto_trim", True)))
        self.mono_var.set(bool(self.engine.mono))
        want = self.show.settings.get("device")
        if want and want != self.device_var.get() and any(n == want for _, n in self.devices):
            self.device_var.set(want)
            self._pick_device()
        self._refresh_lists()
        self._fill_pads()
        self._fill_tree()
        self._select_row(0)
        self._wave_cue = None
        self.wave.set_track(None)
        n = sum(len(cl.cues) for cl in self.show.lists)
        hint = "  Drop files or folders from Explorer onto the list to add them." if self.dnd_ok else ""
        self.flash(f"{n} cues in {len(self.show.lists)} list(s).  Space = GO.{hint}", 12)

    def _pick_list(self) -> None:
        sel = self.lists_box.curselection()
        if sel and self.player is not None:
            self.player.select_list(sel[0])
            self._fill_tree()
            self._select_row(self.player.standby)

    def _fill_tree(self) -> None:
        self.tree.delete(*self.tree.get_children())
        self._rows, self._row_tags, self._ending_done = [], [], set()
        cl = self.player.cue_list if self.player else None
        if cl is None:
            self.list_title.set("")
            self.list_info.set("")
            return
        for i, c in enumerate(cl.cues):
            tag = "odd" if i % 2 else "even"
            iid = self.tree.insert("", "end", values=self._row(i, c), tags=(tag,))
            self._rows.append(iid)
            self._row_tags.append(tag)
        total = sum(c.duration or 0 for c in cl.cues)
        self.list_title.set(cl.name)
        self.list_info.set(f"{len(cl.cues)} cues  ·  {mmss(total)}" if total else f"{len(cl.cues)} cues")

    def _row(self, i: int, c: Cue, marker: str = "", ending: str = "") -> tuple:
        f = lambda v: "" if v is None else f"{v:g}"  # noqa: E731
        return (f"{marker}{i + 1}", c.artist, c.title, mmss(c.duration), ending, f(c.in_point), f(c.out_point), f(c.crossfade),
                f"{c.gain_db:+.1f}" if c.gain_db else "0", f(c.fade_in or None), f(c.fade_out or None),
                "loop" if c.loop else ("yes" if c.auto_follow else "no"))

    def _refresh_row(self, i: int, marker: str = "") -> None:
        c = self.player.cue_at(i) if self.player else None
        if c is not None and i < len(self._rows):
            ending = self.tree.set(self._rows[i], "end")
            self.tree.item(self._rows[i], values=self._row(i, c, marker, ending))

    def _select_row(self, i: int) -> None:
        if 0 <= i < len(self._rows):
            self.tree.selection_set(self._rows[i])
            self.tree.see(self._rows[i])
            self._load_editor(i)

    def _pick_cue(self) -> None:
        sel = self.tree.selection()
        if not sel or self.player is None:
            return
        i = self._rows.index(sel[0])
        self.player.select(i)
        self._load_editor(i)

    def _move(self, delta: int) -> None:
        if self.player is not None:
            self.player.select(self.player.standby + delta)
            self._select_row(self.player.standby)

    # ------------------------------------------------------------------ cue editor

    def _load_editor(self, i: int) -> None:
        c = self.player.cue_at(i) if self.player else None
        if c is None:
            return
        vals = {"gain_db": c.gain_db, "pre_wait": c.pre_wait or None, "fade_in": c.fade_in or None, "fade_out": c.fade_out or None,
                "in_point": c.in_point, "out_point": c.out_point, "crossfade": c.crossfade}
        for k, v in self.ed_vars.items():
            x = vals.get(k)
            v.set("" if x is None else f"{x:g}")
        self.follow_var.set(c.auto_follow)
        self.loop_var.set(c.loop)
        self.edit_note.set(f"{i + 1}.  {c.display}     {c.path}")

    def _selected_index(self) -> Optional[int]:
        sel = self.tree.selection()
        return self._rows.index(sel[0]) if sel else None

    def _apply_cue(self) -> None:
        i = self._selected_index()
        c = self.player.cue_at(i) if (self.player and i is not None) else None
        if c is None:
            return
        try:
            c.gain_db = _fnum(self.ed_vars["gain_db"].get(), 0.0) or 0.0
            c.pre_wait = _fnum(self.ed_vars["pre_wait"].get(), 0.0) or 0.0
            c.fade_in = _fnum(self.ed_vars["fade_in"].get(), 0.0) or 0.0
            c.fade_out = _fnum(self.ed_vars["fade_out"].get(), 0.0) or 0.0
            c.in_point = _fnum(self.ed_vars["in_point"].get())
            c.out_point = _fnum(self.ed_vars["out_point"].get())
            c.crossfade = _fnum(self.ed_vars["crossfade"].get())
        except ValueError:
            messagebox.showerror("Check the numbers", "Use seconds and dB as plain numbers, e.g. 2.5")
            return
        c.auto_follow = self.follow_var.get()
        c.loop = self.loop_var.get()
        self._refresh_row(i)
        self._save()
        self.edit_note.set(f"Saved: {c.display}")

    def _clear_cue(self) -> None:
        for v in self.ed_vars.values():
            v.set("")
        self.ed_vars["gain_db"].set("0")
        self.follow_var.set(True)
        self.loop_var.set(False)
        self._apply_cue()

    def _settings_changed(self) -> None:
        if self.show is None:
            return
        try:
            self.show.settings["crossfade"] = float(self.xfade_var.get() or 6)
        except ValueError:
            pass
        self.show.settings["auto_trim"] = bool(self.trim_var.get())
        self._save()

    def _set_master(self) -> None:
        db = round(float(self.master_var.get()) * 2) / 2
        self.master_text.set(f"Master  {db:+.1f} dB" if db else "Master  0.0 dB")
        if self.player is not None:
            self.player.set_master_db(db)
        else:
            self.engine.set_master_db(db)

    def _apply_mono(self) -> None:
        """Fold the output to mono (one cable into one board channel) or back to stereo. Live, and remembered with the folder."""
        on = bool(self.mono_var.get())
        if self.player is not None:
            self.player.set_mono(on)
        else:
            self.engine.mono = on
        if self.show is not None:
            self.show.settings["mono"] = on
            self._save()
        self.flash("Output: MONO (left and right summed onto both outputs)" if on else "Output: stereo")

    def _save(self) -> None:
        if self.show is None:
            return
        try:
            save_show(self.show)
        except OSError as e:
            self.flash(f"Could not save cues.json: {e}")

    # ------------------------------------------------------------------ pads

    def _fill_pads(self) -> None:
        if self.show is None:
            return
        for i, b in enumerate(self.pad_buttons):
            p = self.show.pads[i] if i < len(self.show.pads) else None
            b.configure(text=f"{i + 1}  {(p.name or Path(p.path).stem)[:14]}" if p and p.path else f"{i + 1}")

    def _assign_pad(self, i: int) -> None:
        if self.show is None:
            return
        f = filedialog.askopenfilename(title=f"Sound for pad {i + 1}", filetypes=[("Audio", "*.mp3 *.flac *.wav *.m4a *.ogg *.aiff"), ("All files", "*.*")])
        if not f:
            return
        p = self.show.pads[i]
        p.path, p.name = f, Path(f).stem
        self._fill_pads()
        self._save()

    def _pad(self, i: int) -> None:
        if self.player is not None:
            self.player.play_pad(i)

    # ------------------------------------------------------------------ transport

    def go(self) -> None:
        if self.player is None:
            return
        cue = self.player.go()
        if cue is None and self.player.error:
            self.flash(self.player.error)
        self._select_row(self.player.standby)
        self.tree.focus_set()

    def pause(self) -> None:
        if self.player is not None:
            self.player.pause()

    def stop(self) -> None:
        if self.player is not None:
            self.player.stop()

    def panic(self) -> None:
        if self.player is not None:
            self.player.panic()

    # ------------------------------------------------------------------ show display

    def _toggle_show(self) -> None:
        if self.show_win is not None and self.show_win.winfo_exists():
            self.show_win.destroy()
            self.show_win = None
            return
        w = tk.Toplevel(self)
        w.title("HouseKit Show")
        w.configure(bg="black")
        w.keep_colors = True  # always black, whatever the theme
        w.geometry("1100x640")
        self.show_win = w
        self.sw_list = tk.Label(w, text="", font=font("display", 22, "bold"), fg="#8a8a8a", bg="black")
        self.sw_list.pack(anchor="w", padx=36, pady=(30, 0))
        self.sw_now = tk.Label(w, text="", font=font("display", 34, "bold"), fg="white", bg="black", wraplength=1020, justify="left")
        self.sw_now.pack(anchor="w", padx=36, pady=(8, 0))
        self.sw_time = tk.Label(w, text="", font=font("display", 110, "bold"), fg="#7fd67f", bg="black")
        self.sw_time.pack(anchor="w", padx=36, pady=(0, 0))
        self.sw_next = tk.Label(w, text="", font=font("display", 26, "bold"), fg="#ffd54f", bg="black", wraplength=1020, justify="left")
        self.sw_next.pack(anchor="w", padx=36, pady=(6, 0))
        for lab in (self.sw_list, self.sw_now, self.sw_time, self.sw_next):
            lab.keep_colors = True
        w.bind("<F11>", lambda _e: self._toggle_show())
        w.bind("<space>", lambda _e: self.go())
        w.bind("<Escape>", lambda _e: self.stop())
        set_dark_titlebar(w, True)

    # ------------------------------------------------------------------ polling

    def _meter_tick(self) -> None:
        try:
            self.meter.update(self.engine.peak if not self.engine.paused else 0.0)
        except Exception:
            pass
        self.after(60, self._meter_tick)

    def _poll(self) -> None:
        try:
            if self.player is not None:
                self.player.tick()
                st = self.player.status()
                self._render_status(st)
        except Exception:
            self.flash(traceback.format_exc().strip().splitlines()[-1])
        self.after(100, self._poll)

    def _render_status(self, st: dict) -> None:
        cur = st["current"]
        nxt = st["next"]
        p = self.player
        # transport readouts
        label = {"playing": "NOW PLAYING", "paused": "PAUSED", "stopped": "STOPPED"}[st["state"]]
        if st["state"] == "playing" and st.get("audition"):
            label = "AUDITIONING THE JOIN"
        elif st["state"] == "playing" and st.get("looping"):
            label = "LOOPING"
        self.state_var.set(label)
        if cur is not None:
            self.now_marquee.set(cur.display)
            self.elapsed_var.set(mmss(st["elapsed"]))
            self.length_var.set(f"of {mmss(st['length'])}")
            self.remaining_var.set(f"-{mmss(st['remaining'])}")
        else:
            self.now_marquee.set("")
            self.elapsed_var.set("0:00")
            self.length_var.set("")
            self.remaining_var.set("")
        if nxt is not None:
            self.next_marquee.set(f"NEXT   {nxt.display}" + ("" if st["next_ready"] else "   (loading)"))
        else:
            self.next_marquee.set("NEXT   end of list")
        # waveform: new track?
        if cur is not None and cur is not self._wave_cue:
            d = p.current_decoded()
            pts = p.current_points()
            if d is not None and pts is not None and pts[2] > 0:
                in_pt, out_pt, total = pts
                overlap = p.overlap_for(cur, d)
                xf = max(in_pt, out_pt - int(overlap * self.engine.rate))
                self.wave.set_track(d.overview, in_pt / total, out_pt / total, xf / total)
            else:
                self.wave.set_track(None)
            self._wave_cue = cur
        elif cur is None and self._wave_cue is not None:
            self._wave_cue = None
            self.wave.set_track(None)
        if cur is not None:
            pts = p.current_points()
            if pts and pts[2] > 0:
                in_pt, out_pt, total = pts
                frac = (in_pt + st["elapsed"] * self.engine.rate) / total
                self.wave.set_head(frac)
        # status line
        text = f"{st['state']}"
        if st.get("looping"):
            text += "   loop"
        if st["pads"]:
            text += "   pad " + ",".join(st["pads"])
        if self.engine.mono:
            text += "   MONO"
        if st["error"]:
            text = st["error"]
        if self._flash is not None:
            import time

            if time.monotonic() < self._flash[1]:
                text = self._flash[0]
            else:
                self._flash = None
        self.status_var.set(text)
        # pads lit while playing
        for i, b in enumerate(self.pad_buttons):
            want = "Accent.TButton" if str(i + 1) in st["pads"] else "TButton"
            if b.cget("style") != want:
                b.configure(style=want)
        # row highlights and ending words
        playing_i = queued_i = None
        cl = p.cue_list
        if cl is not None:
            if cur is not None:
                playing_i = next((i for i, c in enumerate(cl.cues) if c is cur), None)
            if st["queued"] is not None:
                queued_i = next((i for i, c in enumerate(cl.cues) if c is st["queued"]), None)
            for i, iid in enumerate(self._rows):
                state = "playing" if i == playing_i else "queued" if i == queued_i else "standby" if i == p.standby else ""
                tag = state or ("odd" if i % 2 else "even")
                if self._row_tags[i] != tag:
                    self.tree.item(iid, tags=(tag,))
                    self._row_tags[i] = tag
                    marker = {"playing": "▶ ", "queued": "… ", "standby": "▷ "}.get(state, "")
                    self._refresh_row(i, marker)
                if i not in self._ending_done:
                    d = p.ready(cl.cues[i])
                    if d is not None:
                        self.tree.set(iid, "end", ENDING_WORD.get(d.edges.ending, ""))
                        self._ending_done.add(i)
        if self.show_win is not None and self.show_win.winfo_exists():
            self.sw_list.configure(text=st["list"])
            self.sw_now.configure(text=cur.display if cur else ("PAUSED" if st["state"] == "paused" else "STOPPED"))
            self.sw_time.configure(text=f"-{mmss(st['remaining'])}" if cur else "")
            self.sw_next.configure(text=f"next: {nxt.display}" if nxt else "next: end of list")

    def _close(self) -> None:
        try:
            if self.player is not None:
                self.player.close()
            self.engine.stop()
        finally:
            self.destroy()


def run(folder: Optional[Path] = None) -> None:
    PlayerWindow(folder).mainloop()


def selftest(folder: Optional[Path] = None) -> None:
    app = PlayerWindow(folder)
    app.after(1500, app._close)
    app.mainloop()


def launch_detached(folder: Path) -> None:
    """Open the player as its own process (from the main HouseKit window)."""
    exe = Path(sys.executable)
    pyw = exe.with_name("pythonw.exe")
    cmd = [str(pyw if pyw.exists() else exe), "-m", "housekit.player_app", str(folder)]
    flags = 0 if pyw.exists() else getattr(subprocess, "CREATE_NO_WINDOW", 0)
    subprocess.Popen(cmd, cwd=str(Path.cwd()), creationflags=flags)


def main() -> None:
    from housekit.app import _install_gui_crash_log, setup_portable_paths

    setup_portable_paths()
    _install_gui_crash_log()
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    if "--selftest" in sys.argv:
        selftest(Path(args[0]) if args else None)
        return
    run(Path(args[0]) if args else None)


if __name__ == "__main__":
    main()
