"""Tkinter desktop GUI for the portable build. A thin adapter: every action calls jobs.py.

Long operations run in a worker thread; progress reaches the widgets through a queue.
"""
from __future__ import annotations

import os
import queue
import subprocess
import sys
import threading
import traceback
from datetime import date
from pathlib import Path
from typing import Any, Callable, Optional

import tkinter as tk
from tkinter import filedialog, messagebox, scrolledtext, ttk

from . import __version__
from .jobs import open_catalog
from .config import Config, app_dir, detect_library_roots, find_config, load_config, write_config
from .models import ProgressEvent
from .theme import MODES, ThemedText, ThemeMixin, font

GENRES = ["", "Classic Rock", "Southern Rock", "Pop", "Dance", "Country", "Modern Country", "R&B/Soul", "Motown",
          "Funk", "Hip-Hop", "Yacht Rock", "Alternative", "Blues", "Other"]
def MONO_FONT() -> tuple:
    return font("mono", 10)


# Patterns coloured in the plan / playlist panes: (tag, regex)
_PLAN_MARKS = (
    ("head", r"^== .*$|^HOUSE MUSIC PLAN.*$|^PLAYLIST .*$"),
    ("ok", r"\[OK\]"),
    ("warn", r"\[SHORT\]|\[LONG\]|!LEVEL|^WARNINGS:|^UNRESOLVED.*$|^  - .*$"),
)


def open_folder(path: Path) -> None:
    try:
        if sys.platform.startswith("win"):
            os.startfile(str(path))  # type: ignore[attr-defined]
        elif sys.platform == "darwin":
            subprocess.Popen(["open", str(path)])
        else:
            subprocess.Popen(["xdg-open", str(path)])
    except Exception:
        pass


class AutocompleteEntry(ttk.Entry):
    """An Entry that suggests catalog values as you type (artists, titles, albums).
    `values_fn` returns the current list; matches that start with the text come first."""

    def __init__(self, master, values_fn: Callable[[], list[str]], **kw) -> None:
        super().__init__(master, **kw)
        self._values_fn = values_fn
        self._popup: Optional[tk.Toplevel] = None
        self._listbox: Optional[tk.Listbox] = None
        self.bind("<KeyRelease>", self._on_key)
        self.bind("<Down>", self._focus_list)
        self.bind("<Escape>", lambda _e: self._hide())
        self.bind("<FocusOut>", self._on_focus_out)

    def _on_key(self, event) -> None:
        if event.keysym in ("Up", "Down", "Return", "Escape", "Tab", "Shift_L", "Shift_R", "Control_L", "Control_R"):
            return
        text = self.get().strip().lower()
        if len(text) < 2:
            self._hide()
            return
        try:
            values = self._values_fn()
        except Exception:
            values = []
        starts = [v for v in values if v.lower().startswith(text)]
        contains = [v for v in values if text in v.lower() and not v.lower().startswith(text)]
        matches = (starts + contains)[:12]
        if not matches:
            self._hide()
            return
        self._show(matches)

    def _show(self, matches: list[str]) -> None:
        if self._popup is None:
            self._popup = tk.Toplevel(self)
            self._popup.wm_overrideredirect(True)
            self._popup.attributes("-topmost", True)
            self._listbox = tk.Listbox(self._popup, font=("Segoe UI", 10), activestyle="dotbox", exportselection=False)
            self._listbox.pack(fill="both", expand=True)
            self._listbox.bind("<ButtonRelease-1>", self._choose)
            self._listbox.bind("<Return>", self._choose)
            self._listbox.bind("<Escape>", lambda _e: self._hide())
            self._listbox.bind("<FocusOut>", lambda _e: self.after(120, self._hide_if_unfocused))
        lb = self._listbox
        lb.delete(0, "end")
        for m in matches:
            lb.insert("end", m)
        lb.configure(height=min(12, len(matches)))
        x, y = self.winfo_rootx(), self.winfo_rooty() + self.winfo_height()
        w = max(self.winfo_width(), 320)
        self._popup.geometry(f"{w}x{min(12, len(matches)) * 22 + 4}+{x}+{y}")
        self._popup.deiconify()
        self._popup.lift()

    def _hide(self) -> None:
        if self._popup is not None:
            self._popup.withdraw()

    def _hide_if_unfocused(self) -> None:
        try:
            if self.focus_get() not in (self, self._listbox):
                self._hide()
        except Exception:
            self._hide()

    def _on_focus_out(self, _event) -> None:
        self.after(120, self._hide_if_unfocused)

    def _focus_list(self, _event) -> None:
        if self._popup is not None and self._popup.state() == "normal" and self._listbox is not None:
            self._listbox.focus_set()
            self._listbox.selection_clear(0, "end")
            self._listbox.selection_set(0)
            self._listbox.activate(0)

    def _choose(self, _event=None) -> None:
        if self._listbox is None:
            return
        sel = self._listbox.curselection()
        if sel:
            value = self._listbox.get(sel[0])
            self.delete(0, "end")
            self.insert(0, value)
        self._hide()
        self.focus_set()
        self.icursor("end")
        self.event_generate("<<AutocompleteChosen>>")


class App(ThemeMixin, tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title(f"HouseKit {__version__}")
        self.geometry("1100x760")
        self.minsize(900, 600)
        self.theme_init()  # before the widgets exist, so classic ones pick up the defaults
        self.q: queue.Queue = queue.Queue()
        self.busy = False
        self.stop_flag = False
        self.cfg: Optional[Config] = None
        self.last_gig_dir: Optional[Path] = None
        self.last_playlist_dir: Optional[Path] = None
        self.last_convert_dir: Optional[Path] = None
        self.ac: dict[str, list[str]] = {"artists": [], "titles": [], "albums": []}  # type-ahead values

        self.nb = ttk.Notebook(self)
        self.tab_gig = ttk.Frame(self.nb)
        self.tab_pl = ttk.Frame(self.nb)
        self.tab_lib = ttk.Frame(self.nb)
        self.tab_set = ttk.Frame(self.nb)
        self.tab_conv = ttk.Frame(self.nb)
        self.tab_setup = ttk.Frame(self.nb)
        for tab, name in ((self.tab_gig, "Gig"), (self.tab_pl, "Playlist"), (self.tab_lib, "Library"),
                          (self.tab_set, "Setlist"), (self.tab_conv, "Convert"), (self.tab_setup, "Setup")):
            self.nb.add(tab, text=f"  {name}  ")
        self.text_panes: list[ThemedText] = []
        self._build_status_bar()  # packed at the bottom first, so it always keeps its space
        self.nb.pack(fill="both", expand=True, padx=10, pady=(8, 0))
        self._build_gig_tab()
        self._build_playlist_tab()
        self._build_library_tab()
        self._build_setlist_tab()
        self._build_convert_tab()
        self._build_setup_tab()
        self._enable_dnd()
        self.theme_refresh(force=True)  # recolour everything just built

        self.after(100, self._poll)
        self._load_config(first=True)

    # ------------------------------------------------------------------ plumbing

    def _build_status_bar(self) -> None:
        bar = ttk.Frame(self)
        bar.pack(side="bottom", fill="x", padx=10, pady=(4, 8))
        self.progress = ttk.Progressbar(bar, mode="determinate", length=260)
        self.progress.pack(side="left")
        self.status_var = tk.StringVar(value="Ready")
        ttk.Label(bar, textvariable=self.status_var).pack(side="left", padx=10)
        ttk.Button(bar, text="Cancel", command=self._cancel).pack(side="right")
        self.log = ThemedText(self, height=5, font=MONO_FONT(), state="disabled")
        self.log.pack(side="bottom", fill="x", padx=10, pady=(6, 0))

    def on_theme_applied(self) -> None:
        """Colours outside the ttk style: zebra rows and the coloured marks in the text panes."""
        p = self.pal
        for tree in (getattr(self, "lib_tree", None), getattr(self, "set_tree", None)):
            if tree is not None:
                tree.tag_configure("odd", background=p["zebra"], foreground=p["fg"])
                tree.tag_configure("even", background=p["field"], foreground=p["fg"])
        for pane in getattr(self, "text_panes", []):
            pane.tag_configure("head", foreground=p["accent"], font=font("mono", 10, "bold"))
            pane.tag_configure("ok", foreground=p["ok"])
            pane.tag_configure("warn", foreground=p["warn"])

    def _set_pane(self, pane: ThemedText, text: str) -> None:
        """Replace a text pane's contents and colour the plan markers."""
        pane.delete("1.0", "end")
        pane.insert("end", text)
        if pane not in self.text_panes:
            self.text_panes.append(pane)
            self.on_theme_applied()
        for tag, pattern in _PLAN_MARKS:
            pane.tag_remove(tag, "1.0", "end")
            idx = "1.0"
            count = tk.IntVar()
            while True:
                idx = pane.search(pattern, idx, stopindex="end", regexp=True, count=count)
                if not idx or count.get() == 0:
                    break
                end = f"{idx}+{count.get()}c"
                pane.tag_add(tag, idx, end)
                idx = end

    def _log(self, text: str) -> None:
        self.log.configure(state="normal")
        self.log.insert("end", text.rstrip() + "\n")
        self.log.see("end")
        self.log.configure(state="disabled")

    def _post(self, kind: str, payload: Any = None) -> None:
        self.q.put((kind, payload))

    def _progress(self, e: ProgressEvent) -> None:
        self._post("progress", e)

    def _poll(self) -> None:
        try:
            while True:
                kind, payload = self.q.get_nowait()
                if kind == "progress":
                    e: ProgressEvent = payload
                    labels = {"scan": "Walking library", "tags": "Reading tags", "analyze": "Analyzing audio", "copy": "Copying"}
                    if e.total:
                        self.progress.configure(mode="determinate", maximum=e.total, value=e.done)
                        self.status_var.set(f"{labels.get(e.phase, e.phase)} {e.done}/{e.total}  {e.message}")
                    else:
                        self.status_var.set(f"{labels.get(e.phase, e.phase)}  {e.message}")
                elif kind == "log":
                    self._log(str(payload))
                elif kind == "status":
                    self.status_var.set(str(payload))
                elif kind == "done":
                    fn, result = payload
                    self.busy = False
                    self.progress.configure(value=0)
                    if fn is not None:
                        try:
                            fn(result)
                        except Exception:
                            self._log(traceback.format_exc())
        except queue.Empty:
            pass
        self.after(100, self._poll)

    def run_bg(self, label: str, work: Callable[[], Any], on_done: Optional[Callable[[Any], None]] = None) -> None:
        if self.busy:
            messagebox.showinfo("Busy", "Something is already running. Wait for it to finish or press Cancel.")
            return
        if self.cfg is None:
            messagebox.showinfo("Setup needed", "Choose your music folders on the Setup tab first.")
            self.nb.select(self.tab_setup)
            return
        self.busy = True
        self.stop_flag = False
        self.status_var.set(label + "...")
        self._log(f"== {label}")

        def runner() -> None:
            try:
                result = work()
                self._post("done", (on_done, result))
            except Exception as e:  # never crash the window
                self._post("log", f"ERROR: {type(e).__name__}: {e}")
                self._post("status", f"{label} failed")
                self._post("done", (None, None))

        threading.Thread(target=runner, daemon=True).start()

    def _cancel(self) -> None:
        if self.busy:
            self.stop_flag = True
            self.status_var.set("Cancelling after the current file...")

    # ------------------------------------------------------------------ drag and drop from Explorer

    def _enable_dnd(self) -> None:
        """Files or folders dropped from Explorer: onto the Convert tab they join the convert list,
        onto the Setup tab folders become music folders."""
        self.dnd_ok = False
        try:
            from tkinterdnd2 import DND_FILES, TkinterDnD

            TkinterDnD._require(self)
        except Exception:
            return
        self.dnd_ok = True
        for widget, handler in ((self.conv_list, self._drop_convert), (self.tab_conv, self._drop_convert),
                                (self.roots_list, self._drop_roots), (self.tab_setup, self._drop_roots)):
            widget.drop_target_register(DND_FILES)
            widget.dnd_bind("<<Drop>>", handler)

    def _dropped(self, event) -> list[Path]:
        try:
            return [Path(p) for p in self.tk.splitlist(event.data)]
        except Exception:
            return []

    def _drop_convert(self, event) -> None:
        paths = [p for p in self._dropped(event) if p.exists()]
        if not paths:
            return
        for p in paths:
            self.conv_list.insert("end", str(p))
        self.nb.select(self.tab_conv)
        self._log(f"Added {len(paths)} item(s) to the convert list")

    def _drop_roots(self, event) -> None:
        folders = [p for p in self._dropped(event) if p.is_dir()]
        if not folders:
            self._log("Drop a folder on the Setup tab to add it as a music folder")
            return
        have = {self.roots_list.get(i).lower() for i in range(self.roots_list.size())}
        for p in folders:
            if str(p).lower() not in have:
                self.roots_list.insert("end", str(p))
        self.nb.select(self.tab_setup)
        self._log(f"Added {len(folders)} music folder(s); press Save settings, then Reindex")

    def _play(self, folder: Optional[Path]) -> None:
        """Open a built folder in the standalone player (its own process, so this window stays free)."""
        from .player_app import launch_detached

        if folder is None or not Path(folder).is_dir():
            messagebox.showinfo("Nothing to play yet", "Build a gig or a playlist first; the player opens the folder you built.")
            return
        try:
            launch_detached(Path(folder))
            self._log(f"Player opened on {folder}")
        except Exception as e:
            messagebox.showerror("Player", f"Could not start the player: {e}")

    def _should_stop(self) -> bool:
        return self.stop_flag

    def _load_config(self, first: bool = False) -> None:
        path = find_config(app_dir())
        if path is None:
            self.cfg = None
            self._log("No housekit.toml yet. Choose your music folders on the Setup tab and press Save.")
            self._fill_setup_defaults()
            self.nb.select(self.tab_setup)
            return
        self.cfg = load_config(path)
        self._fill_setup_from_cfg()
        self._fill_gig_defaults()
        self.setlist_path_var.set(str(self.cfg.setlist_path))
        self._refresh_status()
        if first:
            self._maybe_update_charts()

    # ------------------------------------------------------------------ Gig tab

    def _build_gig_tab(self) -> None:
        f = self.tab_gig
        form = ttk.Frame(f)
        form.pack(fill="x", padx=10, pady=10)
        self.gig_date = tk.StringVar(value=date.today().isoformat())
        self.gig_venue = tk.StringVar()
        self.gig_clean = tk.BooleanVar(value=False)
        self.gig_top = tk.StringVar(value="0")
        self.gig_seed = tk.StringVar()
        ttk.Label(form, text="Date").grid(row=0, column=0, sticky="w")
        ttk.Entry(form, textvariable=self.gig_date, width=12).grid(row=0, column=1, sticky="w", padx=(4, 16))
        ttk.Label(form, text="Venue").grid(row=0, column=2, sticky="w")
        ttk.Entry(form, textvariable=self.gig_venue, width=28).grid(row=0, column=3, sticky="w", padx=(4, 16))
        ttk.Checkbutton(form, text="Family-safe (no explicit / unknown)", variable=self.gig_clean).grid(row=0, column=4, sticky="w", padx=(0, 16))
        ttk.Label(form, text="Hits only, top").grid(row=0, column=5, sticky="w")
        ttk.Entry(form, textvariable=self.gig_top, width=5).grid(row=0, column=6, sticky="w", padx=(4, 16))
        ttk.Label(form, text="Seed").grid(row=0, column=7, sticky="w")
        ttk.Entry(form, textvariable=self.gig_seed, width=6).grid(row=0, column=8, sticky="w", padx=4)

        self.slot_vars: list[tuple[tk.StringVar, tk.StringVar]] = []
        slots = ttk.LabelFrame(f, text="Slots (minutes)")
        slots.pack(fill="x", padx=10)
        self.slot_frame = slots

        btns = ttk.Frame(f)
        btns.pack(fill="x", padx=10, pady=8)
        ttk.Button(btns, text="Preview (nothing written)", command=lambda: self._do_build(dry_run=True)).pack(side="left")
        ttk.Button(btns, text="Build gig", style="Accent.TButton", command=lambda: self._do_build(dry_run=False)).pack(side="left", padx=8)
        ttk.Button(btns, text="Open gig folder", command=lambda: self.last_gig_dir and open_folder(self.last_gig_dir)).pack(side="left")
        ttk.Button(btns, text="Open gigs directory", command=lambda: self.cfg and open_folder(self.cfg.output_dir)).pack(side="left", padx=8)
        ttk.Button(btns, text="Play in HouseKit Player", command=lambda: self._play(self.last_gig_dir)).pack(side="left")
        self.gig_out = ThemedText(f, font=MONO_FONT(), wrap="none")
        self.gig_out.pack(fill="both", expand=True, padx=10, pady=(0, 10))

    def _fill_gig_defaults(self) -> None:
        for w in self.slot_frame.winfo_children():
            w.destroy()
        self.slot_vars = []
        if not self.cfg:
            return
        for i, s in enumerate(self.cfg.slots):
            name = tk.StringVar(value=s.name)
            mins = tk.StringVar(value=f"{s.minutes:g}")
            ttk.Label(self.slot_frame, textvariable=name).grid(row=0, column=2 * i, sticky="w", padx=(10, 2), pady=6)
            ttk.Entry(self.slot_frame, textvariable=mins, width=6).grid(row=0, column=2 * i + 1, sticky="w", padx=(0, 10))
            self.slot_vars.append((name, mins))
        self.gig_top.set(str(self.cfg.top_default))
        self.gig_clean.set(self.cfg.clean_default)

    def _do_build(self, dry_run: bool) -> None:
        from .jobs import BuildRequest, run_build
        from .output import plan_text

        if not self.cfg:
            return
        venue = self.gig_venue.get().strip()
        gdate = self.gig_date.get().strip() or date.today().isoformat()
        name = f"{gdate} {venue}".strip()
        mins: list[Optional[float]] = []
        for _, v in self.slot_vars:
            try:
                mins.append(float(v.get()))
            except ValueError:
                mins.append(None)
        try:
            top = int(self.gig_top.get() or 0)
        except ValueError:
            top = 0
        seed = int(self.gig_seed.get()) if self.gig_seed.get().strip().isdigit() else None
        out_dir = self.cfg.output_dir / name
        overwrite = False
        if not dry_run and out_dir.exists() and any(out_dir.iterdir()):
            if not messagebox.askyesno("Folder exists", f"{out_dir}\nalready has files. Replace its slot folders and playlists?"):
                return
            overwrite = True
        req = BuildRequest(out=Path(name), venue=venue, gig_date=gdate, clean=self.gig_clean.get(), slot_minutes=mins,
                           top=top, seed=seed, dry_run=dry_run, overwrite=overwrite)
        cfg = self.cfg

        def work():
            return run_build(cfg, req, self._progress)

        def done(res):
            plan, written, out_path = res
            self._set_pane(self.gig_out, plan_text(plan, cfg))
            if dry_run:
                self._log("Preview only, nothing written.")
                self.status_var.set("Preview ready")
            elif written:
                self.last_gig_dir = Path(written.out_dir)
                total = sum(len(sp.tracks) for sp in plan.slots)
                self._log(f"Copied {written.copied} of {total} files ({written.bytes_copied/1e9:.2f} GB) to {written.out_dir}")
                for s in written.skipped:
                    self._log(f"  SKIPPED {s}")
                self.status_var.set(f"Gig built: {written.out_dir}")
                self._refresh_status()
                if written.skipped:
                    messagebox.showwarning(
                        "Some files were not found",
                        f"{len(written.skipped)} of {total} songs could not be copied because their files were not where the "
                        "catalog last saw them. If the music folders moved, check the Setup tab and run Reindex on the Library tab.",
                    )
            if not plan.all_filled:
                self._log("One or more slots are outside tolerance; see WARNINGS in the plan.")

        self.run_bg("Preview" if dry_run else "Build gig", work, done)

    # ------------------------------------------------------------------ Playlist tab

    def _build_playlist_tab(self) -> None:
        f = self.tab_pl
        form = ttk.Frame(f)
        form.pack(fill="x", padx=10, pady=10)
        from .playlists import DECADES, decade_label, energy_level_names

        self.pl_name = tk.StringVar(value="My Playlist")
        self.pl_artist = tk.StringVar()
        self.pl_years = tk.StringVar()
        self.pl_energy = tk.StringVar(value="Any")
        self.pl_top = tk.StringVar(value="0")
        self.pl_search = tk.StringVar()
        self.pl_count = tk.StringVar(value="20")
        self.pl_minutes = tk.StringVar()
        self.pl_order = tk.StringVar(value="rank")
        self.pl_clean = tk.BooleanVar(value=False)
        self.pl_live = tk.BooleanVar(value=False)
        self.pl_nocopy = tk.BooleanVar(value=False)
        self.pl_fill = tk.BooleanVar(value=False)
        self.pl_genre_vars: dict[str, tk.BooleanVar] = {}
        self.pl_decade_vars: dict[int, tk.BooleanVar] = {}
        rows = [
            ("Name", self.pl_name, 30), ("Artist (contains)", self.pl_artist, 30),
            ("Words in title/album", self.pl_search, 30), ("Top N on the Hot 100 (0 = any)", self.pl_top, 6),
            ("How many songs", self.pl_count, 6), ("...or minutes", self.pl_minutes, 6),
        ]
        suggest = {id(self.pl_artist): self._ac("artists"), id(self.pl_search): self._ac("titles", "albums")}
        for i, (label, var, width) in enumerate(rows):
            r, c = divmod(i, 2)
            ttk.Label(form, text=label).grid(row=r, column=2 * c, sticky="w", pady=3)
            if id(var) in suggest:
                AutocompleteEntry(form, suggest[id(var)], textvariable=var, width=width).grid(row=r, column=2 * c + 1, sticky="w", padx=(4, 24))
            else:
                ttk.Entry(form, textvariable=var, width=width).grid(row=r, column=2 * c + 1, sticky="w", padx=(4, 24))
        ttk.Label(form, text="Energy").grid(row=3, column=0, sticky="w", pady=3)
        ttk.Combobox(form, textvariable=self.pl_energy, values=energy_level_names(), width=30, state="readonly").grid(row=3, column=1, sticky="w", padx=(4, 24))
        ttk.Label(form, text="Order").grid(row=3, column=2, sticky="w", pady=3)
        ttk.Combobox(form, textvariable=self.pl_order, values=["rank", "energy", "year", "alpha", "random"], width=10, state="readonly").grid(row=3, column=3, sticky="w", padx=(4, 24))
        era = ttk.LabelFrame(f, text="Era (tick any decades and/or type a custom range; nothing = any year)")
        era.pack(fill="x", padx=10, pady=(0, 4))
        for i, d in enumerate(DECADES):
            var = tk.BooleanVar(value=False)
            self.pl_decade_vars[d] = var
            ttk.Checkbutton(era, text=decade_label(d), variable=var).grid(row=0, column=i, sticky="w", padx=8, pady=3)
        ttk.Label(era, text="   Custom years").grid(row=0, column=len(DECADES), sticky="w", padx=(12, 2))
        ttk.Entry(era, textvariable=self.pl_years, width=12).grid(row=0, column=len(DECADES) + 1, sticky="w", padx=(0, 4))
        ttk.Label(era, text="(e.g. 1995-1999)").grid(row=0, column=len(DECADES) + 2, sticky="w", padx=(0, 8))
        self.genre_frame = ttk.LabelFrame(f, text="Genres (tick any; none ticked = all)")
        self.genre_frame.pack(fill="x", padx=10, pady=(0, 4))
        ttk.Label(self.genre_frame, text="Index the library to see its genres.").grid(row=0, column=0, padx=8, pady=4)
        opts = ttk.Frame(f)
        opts.pack(fill="x", padx=10)
        ttk.Checkbutton(opts, text="Family-safe", variable=self.pl_clean).pack(side="left")
        ttk.Checkbutton(opts, text="Include live recordings", variable=self.pl_live).pack(side="left", padx=12)
        ttk.Checkbutton(opts, text="Fill with similar songs when short (artist / word lists)", variable=self.pl_fill).pack(side="left", padx=(0, 12))
        ttk.Checkbutton(opts, text="Don't copy files (playlist points at the library)", variable=self.pl_nocopy).pack(side="left")
        btns = ttk.Frame(f)
        btns.pack(fill="x", padx=10, pady=8)
        ttk.Button(btns, text="Preview", command=lambda: self._do_playlist(dry_run=True)).pack(side="left")
        ttk.Button(btns, text="Build playlist", style="Accent.TButton", command=lambda: self._do_playlist(dry_run=False)).pack(side="left", padx=8)
        ttk.Button(btns, text="Open playlist folder", command=lambda: self.last_playlist_dir and open_folder(self.last_playlist_dir)).pack(side="left")
        ttk.Button(btns, text="Play in HouseKit Player", command=lambda: self._play(self.last_playlist_dir)).pack(side="left", padx=8)
        self.pl_out = ThemedText(f, font=MONO_FONT(), wrap="none")
        self.pl_out.pack(fill="both", expand=True, padx=10, pady=(0, 10))

    def _do_playlist(self, dry_run: bool) -> None:
        from .jobs import run_playlist
        from .output import playlist_text
        from .playlists import PlaylistSpec, parse_energy, parse_range

        if not self.cfg:
            return
        name = self.pl_name.get().strip() or "Playlist"
        try:
            y_lo, y_hi = parse_range(self.pl_years.get() or None, as_int=True)
            e_lo, e_hi = parse_energy(self.pl_energy.get())
            top = int(self.pl_top.get() or 0)
            count = int(self.pl_count.get()) if self.pl_count.get().strip() else None
            minutes = float(self.pl_minutes.get()) if self.pl_minutes.get().strip() else None
        except ValueError:
            messagebox.showerror("Check the fields", "Years look like 1980-1989; counts are whole numbers.")
            return
        genres = [g for g, v in self.pl_genre_vars.items() if v.get()]
        decades = [d for d, v in self.pl_decade_vars.items() if v.get()]
        spec = PlaylistSpec(
            name=name, artists=[self.pl_artist.get()] if self.pl_artist.get().strip() else [],
            genres=genres, decades=decades, year_min=y_lo, year_max=y_hi,
            energy_min=e_lo, energy_max=e_hi, top=top, search=self.pl_search.get(),
            count=count if minutes is None else None, minutes=minutes, order=self.pl_order.get(),
            clean=self.pl_clean.get(), allow_live=self.pl_live.get(), fill=self.pl_fill.get(),
        )
        if spec.count is None and spec.minutes is None:
            spec.count = 20
        out_dir = self.cfg.playlists_dir / name
        overwrite = False
        if not dry_run and out_dir.exists() and any(out_dir.iterdir()):
            if not messagebox.askyesno("Folder exists", f"{out_dir}\nalready has files. Replace them?"):
                return
            overwrite = True
        cfg, copy = self.cfg, not self.pl_nocopy.get()

        def work():
            return run_playlist(cfg, spec, copy=copy, dry_run=dry_run, overwrite=overwrite, on_progress=self._progress)

        def done(res):
            result, written, out_path = res
            self._set_pane(self.pl_out, playlist_text(result))
            if dry_run:
                self.status_var.set("Preview ready")
            elif written:
                self.last_playlist_dir = Path(written.out_dir)
                self._log(("Copied %d of %d files (%.2f GB) to %s" % (written.copied, len(result.tracks), written.bytes_copied / 1e9, written.out_dir)) if copy else f"Wrote playlist into {written.out_dir}")
                self.status_var.set(f"Playlist built: {written.out_dir}")
                if written.skipped:
                    for s in written.skipped:
                        self._log(f"  SKIPPED {s}")
                    messagebox.showwarning(
                        "Some files were not found",
                        f"{len(written.skipped)} of {len(result.tracks)} songs could not be copied because their files were not "
                        "where the catalog last saw them. If the music folders moved, check the Setup tab and run Reindex on the Library tab.",
                    )
            elif not result.tracks:
                self._log("No songs matched those filters.")

        self.run_bg("Preview playlist" if dry_run else "Build playlist", work, done)

    # ------------------------------------------------------------------ Library tab

    def _build_library_tab(self) -> None:
        f = self.tab_lib
        top = ttk.Frame(f)
        top.pack(fill="x", padx=10, pady=10)
        self.lib_status = tk.StringVar(value="No catalog yet.")
        ttk.Label(top, textvariable=self.lib_status, wraplength=1000, justify="left").pack(anchor="w")
        btns = ttk.Frame(f)
        btns.pack(fill="x", padx=10)
        ttk.Button(btns, text="Reindex library (new/changed files)", style="Accent.TButton", command=self._do_index).pack(side="left")
        ttk.Button(btns, text="Re-read all tags", command=lambda: self._do_index(rescan=True)).pack(side="left", padx=8)
        ttk.Button(btns, text="Update chart data online", command=lambda: self._do_charts(force=True)).pack(side="left")
        ttk.Button(btns, text="Load chart files (offline)", command=self._do_chart_load).pack(side="left", padx=8)
        ttk.Button(btns, text="Export CSV", command=self._do_export).pack(side="left")
        srch = ttk.Frame(f)
        srch.pack(fill="x", padx=10, pady=(10, 4))
        ttk.Label(srch, text="Search").pack(side="left")
        self.lib_query = tk.StringVar()
        e = AutocompleteEntry(srch, self._ac("artists", "titles", "albums"), textvariable=self.lib_query, width=40)
        e.pack(side="left", padx=6)
        e.bind("<Return>", lambda _e: self._do_search())
        e.bind("<<AutocompleteChosen>>", lambda _e: self._do_search())
        ttk.Button(srch, text="Find", command=self._do_search).pack(side="left")
        cols = ("id", "artist", "title", "album", "year", "yr src", "genre", "len", "energy", "pop", "flags")
        self.lib_tree = ttk.Treeview(f, columns=cols, show="headings", height=14)
        widths = {"id": 60, "artist": 170, "title": 230, "album": 190, "year": 50, "yr src": 70, "genre": 110, "len": 50, "energy": 60, "pop": 80, "flags": 110}
        for c in cols:
            self.lib_tree.heading(c, text=c)
            self.lib_tree.column(c, width=widths[c], anchor="w")
        self.lib_tree.pack(fill="both", expand=True, padx=10)
        fix = ttk.LabelFrame(f, text="Fix the selected track (kept through re-indexing)")
        fix.pack(fill="x", padx=10, pady=8)
        for label, flag, val in (("Explicit", "explicit", True), ("Not explicit", "explicit", False), ("Junk", "junk", True),
                                 ("Not junk", "junk", False), ("Live", "live", True), ("Not live", "live", False)):
            ttk.Button(fix, text=label, command=lambda fl=flag, v=val: self._do_flag(fl, v)).pack(side="left", padx=3, pady=4)
        ttk.Label(fix, text="   Genre").pack(side="left")
        self.fix_genre = tk.StringVar()
        ttk.Combobox(fix, textvariable=self.fix_genre, values=GENRES[1:], width=16, state="readonly").pack(side="left", padx=4)
        ttk.Button(fix, text="Set genre", command=self._do_genre).pack(side="left")
        ttk.Label(fix, text="   Year").pack(side="left")
        self.fix_year = tk.StringVar()
        ttk.Entry(fix, textvariable=self.fix_year, width=6).pack(side="left", padx=4)
        ttk.Button(fix, text="Set year", command=self._do_year).pack(side="left")
        ttk.Button(fix, text="Auto year", command=lambda: self._do_year(clear=True)).pack(side="left", padx=4)

    def _refresh_status(self) -> None:
        if not self.cfg:
            return
        from .jobs import library_status

        try:
            st = library_status(self.cfg)
        except Exception as e:
            self.lib_status.set(f"Catalog unavailable: {e}")
            return
        fl = st.flags
        self.lib_status.set(
            f"Library: {', '.join(st.roots)}\n"
            f"{st.tracks} tracks, {st.analyzed} analyzed, {st.missing} missing, last scan {st.last_scan or 'never'}.  "
            f"Explicit {fl.get('explicit', 0)} / unknown {fl.get('explicit_unknown', 0)}, junk {fl.get('junk', 0)}, live {fl.get('live', 0)}, "
            f"popularity known {fl.get('popular', 0)} (Hot 100 run {fl.get('charted', 0)}), hand corrections {fl.get('hand_corrections', 0)}."
        )
        self._fill_genres()
        self._load_values()

    def _load_values(self) -> None:
        """Type-ahead lists for the artist / title / album fields, from the catalog."""
        if not self.cfg:
            return
        try:
            with open_catalog(self.cfg) as cat:
                self.ac = cat.distinct_values()
        except Exception:
            self.ac = {"artists": [], "titles": [], "albums": []}

    def _ac(self, *keys: str) -> Callable[[], list[str]]:
        return lambda: [v for k in keys for v in self.ac.get(k, [])]

    def _fill_genres(self) -> None:
        """Genre checkboxes on the Playlist tab, from what the scan actually found (with counts)."""
        if not self.cfg:
            return
        try:
            with open_catalog(self.cfg) as cat:
                counts = cat.genre_counts()
        except Exception:
            counts = []
        keep = {g: v.get() for g, v in self.pl_genre_vars.items()}
        for w in self.genre_frame.winfo_children():
            w.destroy()
        self.pl_genre_vars = {}
        if not counts:
            ttk.Label(self.genre_frame, text="Index the library to see its genres.").grid(row=0, column=0, padx=8, pady=4)
            return
        per_row = 7
        for i, (genre, n) in enumerate(counts):
            var = tk.BooleanVar(value=keep.get(genre, False))
            self.pl_genre_vars[genre] = var
            ttk.Checkbutton(self.genre_frame, text=f"{genre} ({n})", variable=var).grid(
                row=i // per_row, column=i % per_row, sticky="w", padx=8, pady=2
            )

    def _do_index(self, rescan: bool = False) -> None:
        from .jobs import run_index

        cfg = self.cfg

        def work():
            # retry_errors: a file that failed once (e.g. a transient WinError 1450 during decode) gets another go
            return run_index(cfg, self._progress, rescan=rescan, retry_errors=True, should_stop=self._should_stop)

        def done(res):
            self._log(
                f"Files seen {res.files_seen} | tags read {res.tags_read}, unchanged {res.tags_skipped}, errors {res.tag_errors} | "
                f"analyzed {res.analyzed}, errors {res.analysis_errors} | missing {res.marked_missing}"
                + (" | interrupted, run again to resume" if res.interrupted else "")
            )
            for line in res.errors[:5]:
                self._log("  " + line)
            self.status_var.set("Index complete" if not res.interrupted else "Index cancelled (progress kept)")
            self._refresh_status()

        self.run_bg("Re-read all tags" if rescan else "Reindex", work, done)

    def _chart_summary(self, res: dict) -> str:
        return (f"{res['popular_tracks']} library tracks now have known popularity ({res['charted_tracks']} with a Hot 100 run, "
                f"{res['top40_tracks']} top-40); {res['chart_songs']} songs and {res['chart_albums']} albums stored.")

    def _do_charts(self, force: bool) -> None:
        from .jobs import run_chart_update

        cfg = self.cfg

        def work():
            return run_chart_update(cfg, force=force)

        def done(res):
            if res is None:
                self._log("Chart data is up to date.")
                self.status_var.set("Chart data up to date")
            else:
                for s in res.get("sources", []):
                    self._log(f"  {s['source']}: {s['error']}" if "error" in s else f"  {s['source']}: {s['records']} records downloaded")
                self._log("Chart data refreshed. " + self._chart_summary(res))
                self.status_var.set("Chart data refreshed")
                self._refresh_status()

        self.run_bg("Update chart data", work, done)

    def _do_chart_load(self) -> None:
        """Import whatever chart files are in the charts folder, without the network."""
        from .jobs import run_chart_load

        cfg = self.cfg

        def done(res):
            if not res["sources"]:
                self._log("No chart files in the charts folder. 'Update chart data online' downloads them (needs the network once).")
                self.status_var.set("No chart files")
                return
            for s in res["sources"]:
                self._log(f"  {s['source']}: {s['error']}" if "error" in s else f"  {s['source']}: {s['records']} records from {Path(s['file']).name}")
            self._log("Chart files loaded. " + self._chart_summary(res))
            self.status_var.set("Chart files loaded")
            self._refresh_status()

        self.run_bg("Load chart files", lambda: run_chart_load(cfg), done)

    def _maybe_update_charts(self) -> None:
        """On launch: load the shipped chart files if the catalog has none, then refresh online if due.
        Runs in the background; offline is fine."""
        from .jobs import chart_startup_plan, run_chart_startup

        if not self.cfg:
            return
        cfg = self.cfg
        try:
            plan = chart_startup_plan(cfg)
        except Exception:
            return
        if not plan["load"] and not plan["update"]:
            return
        if plan["load"]:
            self._log("Re-reading the chart files under the current song-matching rules (no network needed)." if plan["stale"]
                      else "Loading the chart files that came with the app (no network needed).")
        elif plan["update"]:
            self._log("Chart data is older than a week; refreshing in the background (skipped if offline).")

        def done(res):
            if res.get("loaded"):
                self._log("Chart files loaded. " + self._chart_summary(res["loaded"]))
                self._refresh_status()
            if res.get("updated"):
                self._log("Chart data refreshed online. " + self._chart_summary(res["updated"]))
                self._refresh_status()
            elif res.get("error"):
                self._log(f"Online refresh skipped ({res['error'].split(':')[0]}); the loaded chart data is used as is.")
            self.status_var.set("Ready")

        self.run_bg("Chart data", lambda: run_chart_startup(cfg), done)

    def _do_export(self) -> None:
        if not self.cfg:
            return
        path = filedialog.asksaveasfilename(defaultextension=".csv", initialfile="library.csv", filetypes=[("CSV", "*.csv")])
        if not path:
            return
        import csv

        cfg = self.cfg

        def work():
            with open_catalog(cfg) as cat:
                rows = cat.export_rows()
            with open(path, "w", newline="", encoding="utf-8-sig") as fh:
                w = csv.DictWriter(fh, fieldnames=list(rows[0].keys()) if rows else ["id"])
                w.writeheader()
                w.writerows(rows)
            return len(rows)

        self.run_bg("Export CSV", work, lambda n: self._log(f"Wrote {n} rows to {path}"))

    def _do_search(self) -> None:
        if not self.cfg:
            return
        q = self.lib_query.get().strip()
        if not q:
            return
        from .charts import pop_label

        with open_catalog(self.cfg) as cat:
            tracks = cat.search(q, limit=200)
        self.lib_tree.delete(*self.lib_tree.get_children())
        for row_i, t in enumerate(tracks):
            flags = []
            if t.explicit is True:
                flags.append("EXPLICIT")
            elif t.explicit is None:
                flags.append("expl?")
            flags += [f for f in ("junk", "holiday", "live", "instrumental") if getattr(t, f)]
            secs = int(round(t.duration or 0))
            self.lib_tree.insert("", "end", tags=("odd" if row_i % 2 else "even",), values=(
                t.id, t.artist, t.title + (f" [{t.version}]" if t.version else ""), t.album, t.year or "",
                t.year_source if t.year_source != "tag" else "", t.genre_norm,
                f"{secs // 60}:{secs % 60:02d}", f"{t.energy:.2f}" if t.energy is not None else "-",
                pop_label(t.chart_peak, t.chart_weeks, t.pop_source, t.popularity), " ".join(flags),
            ))
        self.status_var.set(f"{len(tracks)} result(s)")

    def _selected_track_id(self) -> Optional[int]:
        sel = self.lib_tree.selection()
        if not sel:
            messagebox.showinfo("Select a track", "Click a row in the search results first.")
            return None
        return int(self.lib_tree.item(sel[0], "values")[0])

    def _do_flag(self, flag: str, value: bool) -> None:
        tid = self._selected_track_id()
        if tid is None or not self.cfg:
            return
        with open_catalog(self.cfg) as cat:
            cat.set_flag(tid, flag, value)
        self._log(f"Track {tid}: {flag} = {'yes' if value else 'no'}")
        self._do_search()

    def _do_genre(self) -> None:
        tid = self._selected_track_id()
        if tid is None or not self.cfg or not self.fix_genre.get():
            return
        with open_catalog(self.cfg) as cat:
            cat.set_genre(tid, self.fix_genre.get())
        self._log(f"Track {tid}: genre = {self.fix_genre.get()}")
        self._do_search()

    def _do_year(self, clear: bool = False) -> None:
        tid = self._selected_track_id()
        if tid is None or not self.cfg:
            return
        if clear:
            year = None
        else:
            v = self.fix_year.get().strip()
            if not (v.isdigit() and 1900 <= int(v) <= 2100):
                messagebox.showerror("Check the year", "Type a four-digit year, e.g. 1977.")
                return
            year = int(v)
        with open_catalog(self.cfg) as cat:
            cat.set_year(tid, year)
        self._log(f"Track {tid}: year = {year if year else 'auto'}")
        self._do_search()

    # ------------------------------------------------------------------ Setlist tab

    def _build_setlist_tab(self) -> None:
        f = self.tab_set
        top = ttk.Frame(f)
        top.pack(fill="x", padx=10, pady=10)
        self.setlist_path_var = tk.StringVar(value="")
        ttk.Label(top, text="Standing setlist folder:").pack(side="left")
        ttk.Label(top, textvariable=self.setlist_path_var).pack(side="left", padx=6)
        ttk.Button(top, text="Open folder", command=lambda: self.cfg and open_folder(self.cfg.setlist_path)).pack(side="left", padx=8)
        ttk.Button(top, text="Check matches", command=self._do_parse).pack(side="left")
        ttk.Label(f, text="Drop the band's set files (SET 1.docx, SET 2.docx ...) in that folder. Every line should be matched, "
                          "override or not_in_library. Select an unresolved line, search for the song, and pin it.",
                  wraplength=1000, justify="left").pack(anchor="w", padx=10)
        cols = ("line", "set", "entry", "status", "score", "matched", "id")
        self.set_tree = ttk.Treeview(f, columns=cols, show="headings", height=12)
        for c, w in zip(cols, (50, 70, 260, 100, 50, 320, 60)):
            self.set_tree.heading(c, text=c)
            self.set_tree.column(c, width=w, anchor="w")
        self.set_tree.pack(fill="both", expand=True, padx=10, pady=(6, 4))
        pin = ttk.LabelFrame(f, text="Resolve the selected line")
        pin.pack(fill="x", padx=10, pady=(0, 10))
        self.pin_query = tk.StringVar()
        e = AutocompleteEntry(pin, self._ac("titles", "artists"), textvariable=self.pin_query, width=36)
        e.grid(row=0, column=0, padx=6, pady=6)
        e.bind("<Return>", lambda _e: self._do_pin_search())
        e.bind("<<AutocompleteChosen>>", lambda _e: self._do_pin_search())
        ttk.Button(pin, text="Search library", command=self._do_pin_search).grid(row=0, column=1)
        self.pin_pick = ttk.Combobox(pin, width=60, state="readonly")
        self.pin_pick.grid(row=0, column=2, padx=6)
        ttk.Button(pin, text="Pin to this track", command=self._do_pin).grid(row=0, column=3)
        ttk.Button(pin, text="Not in library", command=lambda: self._do_pin(not_in_library=True)).grid(row=0, column=4, padx=6)
        self.pin_results: list[tuple[int, str]] = []

    def _do_parse(self) -> None:
        from .setlist import parse_setlist

        if not self.cfg:
            return
        cfg = self.cfg

        def work():
            with open_catalog(cfg) as cat:
                return parse_setlist(cfg.setlist_path, cat, cfg)

        def done(parse):
            self.set_tree.delete(*self.set_tree.get_children())
            for row_i, m in enumerate(parse.entries):
                self.set_tree.insert("", "end", tags=("odd" if row_i % 2 else "even",),
                                     values=(m.entry.line_no, m.entry.set_name[:10], m.entry.raw, m.status, f"{m.score:.0f}", m.track_display, m.track_id or ""))
            n = len(parse.unresolved)
            self.status_var.set(f"{len(parse.entries)} lines, {n} unresolved" if n else f"{len(parse.entries)} lines, all resolved")

        self.run_bg("Check setlist", work, done)

    def _do_pin_search(self) -> None:
        if not self.cfg:
            return
        q = self.pin_query.get().strip()
        if not q:
            return
        with open_catalog(self.cfg) as cat:
            tracks = cat.search(q, limit=40)
        self.pin_results = [(t.id, f"{t.id}  {t.display}  ({t.album}{', ' + str(t.year) if t.year else ''})") for t in tracks]
        self.pin_pick["values"] = [d for _, d in self.pin_results]
        if self.pin_results:
            self.pin_pick.current(0)

    def _do_pin(self, not_in_library: bool = False) -> None:
        from .setlist import save_override

        if not self.cfg:
            return
        sel = self.set_tree.selection()
        if not sel:
            messagebox.showinfo("Select a line", "Click a setlist line first.")
            return
        raw = self.set_tree.item(sel[0], "values")[2]
        if not_in_library:
            save_override(self.cfg.overrides_path, raw, not_in_library=True)
            self._log(f"'{raw}' marked not in library")
        else:
            idx = self.pin_pick.current()
            if idx < 0 or idx >= len(self.pin_results):
                messagebox.showinfo("Pick a track", "Search and choose a track first.")
                return
            tid = self.pin_results[idx][0]
            save_override(self.cfg.overrides_path, raw, track_ids=[tid])
            self._log(f"'{raw}' pinned to track {tid}")
        self._do_parse()

    # ------------------------------------------------------------------ Convert tab

    def _build_convert_tab(self) -> None:
        from .convert import FORMATS

        f = self.tab_conv
        ttk.Label(f, text="Convert audio with the bundled ffmpeg, in place: the files in a gig or playlist folder are replaced by "
                          "the converted ones and its playlists are updated to match. Tags and artwork carry over. Nothing in the "
                          "music library is ever touched (give an output folder to make a separate copy instead). "
                          "Files and folders can be dropped here from Explorer.",
                  wraplength=1000, justify="left").pack(anchor="w", padx=10, pady=(10, 4))
        src = ttk.LabelFrame(f, text="What to convert")
        src.pack(fill="x", padx=10)
        self.conv_list = tk.Listbox(src, height=6, width=100)
        self.conv_list.pack(side="left", fill="x", expand=True, padx=6, pady=6)
        b = ttk.Frame(src)
        b.pack(side="left", padx=6)
        ttk.Button(b, text="Add files...", command=self._conv_add_files).pack(fill="x")
        ttk.Button(b, text="Add folder...", command=self._conv_add_folder).pack(fill="x", pady=3)
        ttk.Button(b, text="Last gig folder", command=lambda: self._conv_add_path(self.last_gig_dir)).pack(fill="x")
        ttk.Button(b, text="Last playlist folder", command=lambda: self._conv_add_path(self.last_playlist_dir)).pack(fill="x", pady=3)
        ttk.Button(b, text="Remove", command=lambda: [self.conv_list.delete(i) for i in reversed(self.conv_list.curselection())]).pack(fill="x")
        ttk.Button(b, text="Clear", command=lambda: self.conv_list.delete(0, "end")).pack(fill="x", pady=3)

        opts = ttk.Frame(f)
        opts.pack(fill="x", padx=10, pady=8)
        self.conv_fmt = tk.StringVar(value=FORMATS["mp3-320"]["label"])
        self.conv_labels = {v["label"]: k for k, v in FORMATS.items()}
        ttk.Label(opts, text="Format").grid(row=0, column=0, sticky="w")
        ttk.Combobox(opts, textvariable=self.conv_fmt, values=list(self.conv_labels), width=22, state="readonly").grid(row=0, column=1, sticky="w", padx=(4, 20))
        self.conv_match = tk.BooleanVar(value=False)
        self.conv_lufs = tk.StringVar(value="-16")
        ttk.Checkbutton(opts, text="Match loudness to", variable=self.conv_match).grid(row=0, column=2, sticky="w")
        ttk.Entry(opts, textvariable=self.conv_lufs, width=6).grid(row=0, column=3, sticky="w", padx=(4, 2))
        ttk.Label(opts, text="LUFS").grid(row=0, column=4, sticky="w", padx=(0, 20))
        self.conv_keep = tk.BooleanVar(value=True)
        ttk.Checkbutton(opts, text="Keep folder layout (copy mode)", variable=self.conv_keep).grid(row=0, column=5, sticky="w", padx=(0, 20))
        self.conv_overwrite = tk.BooleanVar(value=False)
        ttk.Checkbutton(opts, text="Redo files already in that format", variable=self.conv_overwrite).grid(row=0, column=6, sticky="w")
        ttk.Label(opts, text="Copy to folder (blank = convert in place)").grid(row=1, column=0, columnspan=2, sticky="w", pady=(8, 0))
        self.conv_out = tk.StringVar()
        ttk.Entry(opts, textvariable=self.conv_out, width=60).grid(row=1, column=2, columnspan=4, sticky="w", padx=4, pady=(8, 0))
        ttk.Button(opts, text="Browse...", command=lambda: self._browse_into(self.conv_out)).grid(row=1, column=6, sticky="w", pady=(8, 0))

        btns = ttk.Frame(f)
        btns.pack(fill="x", padx=10)
        ttk.Button(btns, text="Convert", style="Accent.TButton", command=self._do_convert).pack(side="left")
        ttk.Button(btns, text="Open output folder", command=lambda: self.last_convert_dir and open_folder(self.last_convert_dir)).pack(side="left", padx=8)
        self.conv_out_text = ThemedText(f, font=MONO_FONT(), wrap="none")
        self.conv_out_text.pack(fill="both", expand=True, padx=10, pady=10)

    def _conv_add_files(self) -> None:
        files = filedialog.askopenfilenames(title="Choose audio files", filetypes=[("Audio", "*.mp3 *.flac *.m4a *.aac *.wav *.aiff *.aif *.ogg *.opus *.wma"), ("All files", "*.*")])
        for p in files:
            self.conv_list.insert("end", p)

    def _conv_add_folder(self) -> None:
        d = filedialog.askdirectory(title="Choose a folder of audio")
        if d:
            self.conv_list.insert("end", d)

    def _conv_add_path(self, p: Optional[Path]) -> None:
        if p is None:
            messagebox.showinfo("Nothing yet", "Build a gig or playlist first, or use Add folder.")
            return
        self.conv_list.insert("end", str(p))

    def _do_convert(self) -> None:
        from .convert import ConvertOptions, ffmpeg_available
        from .jobs import run_convert

        if not self.cfg:
            return
        inputs = [Path(self.conv_list.get(i)) for i in range(self.conv_list.size())]
        if not inputs:
            messagebox.showinfo("Nothing to convert", "Add files or a folder first.")
            return
        if not ffmpeg_available():
            messagebox.showerror("ffmpeg missing", "ffmpeg was not found. Put ffmpeg.exe and ffprobe.exe in the app's ffmpeg folder.")
            return
        try:
            target = float(self.conv_lufs.get()) if self.conv_match.get() else None
        except ValueError:
            messagebox.showerror("Check the LUFS value", "Use a number like -16.")
            return
        out_dir = Path(self.conv_out.get().strip()) if self.conv_out.get().strip() else None
        if out_dir is None and not messagebox.askyesno(
            "Convert in place",
            f"Replace the files in\n" + "\n".join(str(p) for p in inputs[:6]) + ("\n..." if len(inputs) > 6 else "")
            + f"\n\nwith {self.conv_fmt.get()} versions? The originals in those folders are removed once converted "
            "(the music library is never touched). Fill in 'Copy to folder' to keep them instead.",
        ):
            return
        opts = ConvertOptions(fmt=self.conv_labels[self.conv_fmt.get()], match_loudness=target,
                              keep_structure=self.conv_keep.get(), overwrite=self.conv_overwrite.get())
        cfg = self.cfg

        def work():
            return run_convert(cfg, inputs, opts, out_dir, on_progress=self._progress, should_stop=self._should_stop)

        def done(res):
            self.last_convert_dir = Path(res.out_dir)
            lines = [f"Converted {res.converted} file(s), {res.bytes_written/1e6:.0f} MB, {'in place in' if out_dir is None else 'into'} {res.out_dir}"]
            if res.skipped:
                lines.append(f"Skipped {res.skipped} file(s) already in that format or already converted (tick 'Redo' to re-encode them).")
            for p in res.playlists:
                lines.append(f"Playlist rewritten: {p}")
            for i in res.errors:
                lines.append(f"FAILED {i.src.name}: {i.detail}")
            if res.interrupted:
                lines.append("Cancelled; finished files were kept.")
            lines.append("")
            for i in res.items:
                lines.append(f"  {i.status:<9} {i.src.name}  ->  {i.dst.name}" + (f"  ({i.detail})" if i.detail else ""))
            self.conv_out_text.delete("1.0", "end")
            self.conv_out_text.insert("end", "\n".join(lines) + "\n")
            self._log(lines[0])
            self.status_var.set("Conversion done" if not res.errors else f"Conversion done with {len(res.errors)} failure(s)")

        self.run_bg("Convert", work, done)

    # ------------------------------------------------------------------ Setup tab

    def _build_setup_tab(self) -> None:
        f = self.tab_setup
        ttk.Label(f, text="Music folders (read-only; HouseKit never changes anything in them). Folders can be dropped here from Explorer.").pack(anchor="w", padx=10, pady=(12, 2))
        row = ttk.Frame(f)
        row.pack(fill="x", padx=10)
        self.roots_list = tk.Listbox(row, height=5, width=90)
        self.roots_list.pack(side="left", fill="x", expand=True)
        b = ttk.Frame(row)
        b.pack(side="left", padx=8)
        ttk.Button(b, text="Add folder...", command=self._add_root).pack(fill="x")
        ttk.Button(b, text="Remove", command=lambda: [self.roots_list.delete(i) for i in reversed(self.roots_list.curselection())]).pack(fill="x", pady=4)
        grid = ttk.Frame(f)
        grid.pack(fill="x", padx=10, pady=10)
        self.setup_gigs = tk.StringVar()
        self.setup_pl = tk.StringVar()
        self.setup_sets = tk.StringVar()
        self.setup_days = tk.StringVar(value="7")
        for i, (label, var) in enumerate((("Gig output folder", self.setup_gigs), ("Playlists folder", self.setup_pl), ("Setlist folder", self.setup_sets))):
            ttk.Label(grid, text=label).grid(row=i, column=0, sticky="w", pady=3)
            ttk.Entry(grid, textvariable=var, width=70).grid(row=i, column=1, sticky="w", padx=6)
            ttk.Button(grid, text="Browse...", command=lambda v=var: self._browse_into(v)).grid(row=i, column=2)
        ttk.Label(grid, text="Refresh chart data every N days (0 = never)").grid(row=3, column=0, sticky="w", pady=3)
        ttk.Spinbox(grid, from_=0, to=90, textvariable=self.setup_days, width=5).grid(row=3, column=1, sticky="w", padx=6)
        ttk.Label(grid, text="Look").grid(row=4, column=0, sticky="w", pady=3)
        self.theme_var = tk.StringVar(value=self.theme_pref.capitalize())
        tb = ttk.Combobox(grid, textvariable=self.theme_var, values=[m.capitalize() for m in MODES], width=10, state="readonly")
        tb.grid(row=4, column=1, sticky="w", padx=6)
        tb.bind("<<ComboboxSelected>>", lambda _e: self.theme_set(self.theme_var.get()))
        ttk.Label(grid, text="System follows the Windows app colour setting; applies to the player too.").grid(row=4, column=2, sticky="w")
        ttk.Button(f, text="Save settings", style="Accent.TButton", command=self._save_setup).pack(anchor="w", padx=10, pady=6)
        self.setup_note = tk.StringVar(value="")
        ttk.Label(f, textvariable=self.setup_note, wraplength=1000, justify="left").pack(anchor="w", padx=10)
        ttk.Label(f, text=f"Config file: {app_dir() / 'housekit.toml'}   (edit it for slot lengths, weights and rules)").pack(anchor="w", padx=10, pady=(20, 0))
        clear = ttk.LabelFrame(f, text="Moving HouseKit to a different music library")
        clear.pack(fill="x", padx=10, pady=(20, 0))
        ttk.Label(clear, text="Clear catalog forgets every indexed track, its analysis and the play history, so the next Reindex starts "
                              "fresh on the new folders. Chart data, setlist files and pins are kept. Change the music folders above "
                              "and Save first, then clear, then Reindex on the Library tab.",
                  wraplength=950, justify="left").pack(anchor="w", padx=8, pady=(4, 2))
        ttk.Button(clear, text="Clear catalog...", command=self._do_clear_catalog).pack(anchor="w", padx=8, pady=(2, 8))

    def _add_root(self) -> None:
        d = filedialog.askdirectory(title="Choose a music folder")
        if d:
            self.roots_list.insert("end", d)

    def _browse_into(self, var: tk.StringVar) -> None:
        d = filedialog.askdirectory(title="Choose a folder")
        if d:
            var.set(d)

    def _fill_setup_defaults(self) -> None:
        base = app_dir()
        self.roots_list.delete(0, "end")
        for r in detect_library_roots(base):
            self.roots_list.insert("end", str(r))
        self.setup_gigs.set(str(base / "gigs"))
        self.setup_pl.set(str(base / "playlists"))
        self.setup_sets.set(str(base / "setlists"))
        self.setup_note.set("First run: these folders were guessed from what's around the app. Adjust them and press Save, "
                            "then use Reindex on the Library tab.")

    def _fill_setup_from_cfg(self) -> None:
        if not self.cfg:
            return
        self.roots_list.delete(0, "end")
        for r in self.cfg.library_roots:
            self.roots_list.insert("end", str(r))
        self.setup_gigs.set(str(self.cfg.output_dir))
        self.setup_pl.set(str(self.cfg.playlists_dir))
        self.setup_sets.set(str(self.cfg.setlist_path))
        self.setup_days.set(str(self.cfg.chart_update_days))

    def _do_clear_catalog(self) -> None:
        from .jobs import run_clear_catalog

        if not self.cfg:
            messagebox.showinfo("Nothing to clear", "There is no catalog yet.")
            return
        if self.busy:
            messagebox.showinfo("Busy", "Wait for the current job to finish first.")
            return
        if not messagebox.askyesno(
            "Clear catalog",
            f"Forget every indexed track in\n{self.cfg.db_path}\n\nAnalysis and play history go with it; chart data and setlist pins "
            "are kept. You will need to Reindex afterwards. Continue?",
            icon="warning", default="no",
        ):
            return
        cfg = self.cfg

        def done(res):
            self._log(f"Catalog cleared: {res['tracks_removed']} tracks removed; {res['chart_songs_kept']} chart songs kept.")
            self.status_var.set("Catalog cleared; run Reindex")
            self.last_gig_dir = self.last_playlist_dir = None
            self.lib_tree.delete(*self.lib_tree.get_children())
            self._refresh_status()
            self.setup_note.set("Catalog cleared. Check the music folders above, then Reindex on the Library tab.")

        self.run_bg("Clear catalog", lambda: run_clear_catalog(cfg), done)

    def _save_setup(self) -> None:
        roots = [Path(self.roots_list.get(i)) for i in range(self.roots_list.size())]
        if not roots:
            messagebox.showerror("No music folders", "Add at least one music folder.")
            return
        bad = [str(r) for r in roots if not r.is_dir()]
        if bad:
            messagebox.showerror("Folder not found", "\n".join(bad))
            return
        try:
            days = int(self.setup_days.get() or 0)
        except ValueError:
            days = 7
        path = write_config(app_dir(), roots, Path(self.setup_gigs.get()), Path(self.setup_pl.get()), Path(self.setup_sets.get()), days)
        self._log(f"Saved {path}")
        self._load_config()
        self.setup_note.set("Saved. Next: Library tab, Reindex. The first full index of a large library takes a while; you can close and resume later.")
        self.nb.select(self.tab_lib)


def run() -> None:
    app = App()
    app.mainloop()


def selftest() -> None:
    """Build the window, pump events briefly, close. Used to verify the packaged build starts."""
    app = App()
    app.after(1500, app.destroy)
    app.mainloop()
