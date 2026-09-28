"""Looks for the tkinter windows: the Sun Valley (Windows 11 style) ttk theme in light or dark,
following the system when asked to, matching colours for classic tk widgets, the fonts, and a
dark title bar.

The preference (system | dark | light) lives in ui.json next to the app so both the builder and
the player share it. Dark mode restyles ttk through Sun Valley (or "clam" when the package is not
there), gives classic tk widgets matching colours (existing ones by walking the widget tree,
future ones through option defaults), and asks Windows for a dark title bar.
"""
from __future__ import annotations

import json
import subprocess
import sys
import tkinter as tk
import tkinter.font as tkfont
from pathlib import Path
from tkinter import ttk
from typing import Optional

from .config import app_dir

PREF_FILE = "ui.json"
MODES = ("system", "dark", "light")

# Colours that match the Sun Valley theme's own, for the parts ttk does not draw.
DARK = {
    "bg": "#1c1c1c", "surface": "#2b2b2b", "field": "#2f2f2f", "fg": "#fafafa", "muted": "#8b8b8b",
    "accent": "#4cc2ff", "sel": "#2f60d8", "border": "#3b3b3b", "hover": "#3a3a3a", "err": "#ff6b6b",
    "ok": "#3fb950", "warn": "#e0a000", "wave": "#3d8f98", "meter_bg": "#141414",
    "tag_playing": "#1f4a2a", "tag_standby": "#4a3b10", "tag_queued": "#1f3550", "zebra": "#232323",
}
LIGHT = {
    "bg": "#fafafa", "surface": "#ffffff", "field": "#ffffff", "fg": "#1c1c1c", "muted": "#6b6b6b",
    "accent": "#005fb8", "sel": "#cce4ff", "border": "#e0e0e0", "hover": "#f0f0f0", "err": "#c42b1c",
    "ok": "#0f7b0f", "warn": "#9d5d00", "wave": "#4a9aa5", "meter_bg": "#e8e8e8",
    "tag_playing": "#d4edda", "tag_standby": "#fff1c2", "tag_queued": "#dbe8f8", "zebra": "#f3f3f3",
}

# Font families chosen at start (see setup_fonts): display, body, mono, glyph.
FONTS: dict[str, str] = {}

# Segoe Fluent Icons / MDL2 code points for the transport, with plain-text fallbacks.
GLYPHS = {
    "play": ("", "▶"), "pause": ("", "❚❚"), "stop": ("", "■"),
    "prev": ("", "⏮"), "next": ("", "⏭"), "folder": ("", "…"),
    "list": ("", "≡"), "display": ("", "⤢"), "volume": ("", "♪"),
}


def system_prefers_dark() -> bool:
    """Windows: the 'app mode' in Settings > Personalization > Colors. macOS: the appearance."""
    if sys.platform.startswith("win"):
        try:
            import winreg

            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize") as k:
                v, _ = winreg.QueryValueEx(k, "AppsUseLightTheme")
            return int(v) == 0
        except OSError:
            return False
    if sys.platform == "darwin":
        try:
            r = subprocess.run(["defaults", "read", "-g", "AppleInterfaceStyle"], capture_output=True, text=True, timeout=3)
            return "dark" in r.stdout.lower()
        except Exception:
            return False
    return False


def resolve(pref: Optional[str]) -> str:
    p = (pref or "system").lower()
    if p in ("dark", "light"):
        return p
    return "dark" if system_prefers_dark() else "light"


def load_pref(base: Optional[Path] = None) -> str:
    try:
        v = json.loads(((base or app_dir()) / PREF_FILE).read_text(encoding="utf-8")).get("theme", "system")
    except (OSError, ValueError, AttributeError):
        return "system"
    return v if v in MODES else "system"


def save_pref(pref: str, base: Optional[Path] = None) -> Path:
    p = (base or app_dir()) / PREF_FILE
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            data = {}
    except (OSError, ValueError):
        data = {}
    data["theme"] = pref if pref in MODES else "system"
    p.write_text(json.dumps(data, indent=2), encoding="utf-8")
    return p


def palette(mode: str) -> dict:
    return dict(DARK if mode == "dark" else LIGHT)


# ---------------------------------------------------------------- fonts


def pick_family(root: tk.Misc, candidates: list[str], fallback: str) -> str:
    try:
        fams = set(tkfont.families(root))
    except tk.TclError:
        return fallback
    for c in candidates:
        if c in fams:
            return c
    return fallback


def setup_fonts(root: tk.Misc) -> dict[str, str]:
    """Pick the families once and point Tk's named fonts (and Sun Valley's) at them."""
    if not FONTS:
        FONTS["display"] = pick_family(root, ["Bahnschrift", "Segoe UI Variable Display", "Segoe UI Semibold", "Segoe UI"], "Helvetica")
        FONTS["body"] = pick_family(root, ["Segoe UI Variable Text", "Segoe UI Variable", "Segoe UI"], "Helvetica")
        FONTS["mono"] = pick_family(root, ["Cascadia Mono", "Consolas", "Menlo", "Courier New"], "Courier")
        FONTS["glyph"] = pick_family(root, ["Segoe Fluent Icons", "Segoe MDL2 Assets"], "")
    body, mono, display = FONTS["body"], FONTS["mono"], FONTS["display"]
    named = {
        "TkDefaultFont": (body, 10), "TkTextFont": (body, 10), "TkMenuFont": (body, 10), "TkHeadingFont": (body, 10, "bold"),
        "TkFixedFont": (mono, 10), "TkTooltipFont": (body, 9), "TkCaptionFont": (body, 10), "TkSmallCaptionFont": (body, 9), "TkIconFont": (body, 10),
        "SunValleyCaptionFont": (body, 9), "SunValleyBodyFont": (body, 10), "SunValleyBodyStrongFont": (body, 10, "bold"),
        "SunValleyBodyLargeFont": (body, 12), "SunValleySubtitleFont": (display, 14, "bold"), "SunValleyTitleFont": (display, 20, "bold"),
        "SunValleyTitleLargeFont": (display, 28, "bold"), "SunValleyDisplayFont": (display, 48, "bold"),
    }
    existing = set(tkfont.names(root))
    for name, spec in named.items():
        if name not in existing:
            continue
        try:
            f = tkfont.nametofont(name, root)
            f.configure(family=spec[0], size=spec[1], weight="bold" if len(spec) > 2 else "normal")
        except tk.TclError:
            pass
    return FONTS


def font(kind: str, size: int, weight: str = "normal") -> tuple:
    """A font tuple for the chosen family: kind = display | body | mono."""
    fam = FONTS.get(kind) or {"display": "Segoe UI", "body": "Segoe UI", "mono": "Consolas"}[kind]
    return (fam, size, weight) if weight != "normal" else (fam, size)


def glyph(name: str) -> tuple[str, bool]:
    """(text, is_icon_font): the Fluent icon when the font exists, else a plain-text stand-in."""
    icon, plain = GLYPHS[name]
    return (icon, True) if FONTS.get("glyph") else (plain, False)


# ---------------------------------------------------------------- applying


def _style_clam_dark(style: ttk.Style, p: dict) -> None:
    """Fallback dark styling for installs without the Sun Valley package."""
    style.configure(".", background=p["bg"], foreground=p["fg"], fieldbackground=p["field"], bordercolor=p["border"],
                    lightcolor=p["surface"], darkcolor=p["bg"], troughcolor=p["surface"], selectbackground=p["sel"],
                    selectforeground=p["fg"], insertcolor=p["fg"], focuscolor=p["accent"])
    for s in ("TFrame", "Card.TFrame"):
        style.configure(s, background=p["bg"])
    style.configure("TLabel", background=p["bg"], foreground=p["fg"])
    style.configure("TLabelframe", background=p["bg"], bordercolor=p["border"], lightcolor=p["border"], darkcolor=p["border"])
    style.configure("TLabelframe.Label", background=p["bg"], foreground=p["fg"])
    style.configure("TButton", background=p["surface"], foreground=p["fg"], bordercolor=p["border"], lightcolor=p["surface"], darkcolor=p["surface"], padding=(8, 3))
    style.map("TButton", background=[("pressed", p["sel"]), ("active", p["hover"]), ("disabled", p["bg"])], foreground=[("disabled", p["muted"])])
    style.configure("Accent.TButton", background=p["accent"], foreground=p["bg"])
    style.configure("TCheckbutton", background=p["bg"], foreground=p["fg"], indicatorbackground=p["field"], indicatorforeground=p["fg"])
    style.map("TCheckbutton", background=[("active", p["bg"])], indicatorbackground=[("selected", p["accent"]), ("pressed", p["hover"])])
    style.configure("TRadiobutton", background=p["bg"], foreground=p["fg"], indicatorbackground=p["field"], indicatorforeground=p["fg"])
    style.configure("TEntry", fieldbackground=p["field"], foreground=p["fg"], insertcolor=p["fg"], bordercolor=p["border"], lightcolor=p["field"], darkcolor=p["field"])
    style.configure("TCombobox", fieldbackground=p["field"], foreground=p["fg"], background=p["surface"], arrowcolor=p["fg"], bordercolor=p["border"], lightcolor=p["field"], darkcolor=p["field"], insertcolor=p["fg"])
    style.map("TCombobox", fieldbackground=[("readonly", p["field"])], foreground=[("readonly", p["fg"])], selectbackground=[("readonly", p["field"])], selectforeground=[("readonly", p["fg"])])
    style.configure("TSpinbox", fieldbackground=p["field"], foreground=p["fg"], background=p["surface"], arrowcolor=p["fg"], bordercolor=p["border"], lightcolor=p["field"], darkcolor=p["field"], insertcolor=p["fg"])
    style.configure("TNotebook", background=p["bg"], bordercolor=p["border"], lightcolor=p["bg"], darkcolor=p["bg"])
    style.configure("TNotebook.Tab", background=p["surface"], foreground=p["fg"], bordercolor=p["border"], lightcolor=p["surface"])
    style.map("TNotebook.Tab", background=[("selected", p["bg"]), ("active", p["hover"])], foreground=[("selected", p["fg"])])
    style.configure("Treeview", background=p["field"], fieldbackground=p["field"], foreground=p["fg"], bordercolor=p["border"], lightcolor=p["field"], darkcolor=p["field"])
    style.map("Treeview", background=[("selected", p["sel"])], foreground=[("selected", p["fg"])])
    style.configure("Treeview.Heading", background=p["surface"], foreground=p["fg"], bordercolor=p["border"], lightcolor=p["surface"], darkcolor=p["surface"], relief="flat")
    style.configure("TProgressbar", background=p["accent"], troughcolor=p["surface"], bordercolor=p["border"], lightcolor=p["accent"], darkcolor=p["accent"])
    style.configure("TScrollbar", background=p["surface"], troughcolor=p["bg"], bordercolor=p["border"], arrowcolor=p["fg"], lightcolor=p["surface"], darkcolor=p["surface"])
    style.configure("TScale", background=p["bg"], troughcolor=p["surface"], bordercolor=p["border"], lightcolor=p["surface"], darkcolor=p["surface"])
    style.configure("Switch.TCheckbutton", background=p["bg"], foreground=p["fg"])


def _option_defaults(root: tk.Misc, p: dict) -> None:
    """Colours for classic tk widgets created from now on (popups, listboxes, text panes, canvases)."""
    add = root.option_add
    for cls in ("Listbox", "Text"):
        add(f"*{cls}.background", p["field"])
        add(f"*{cls}.foreground", p["fg"])
        add(f"*{cls}.selectBackground", p["sel"])
        add(f"*{cls}.selectForeground", p["fg"])
        add(f"*{cls}.highlightBackground", p["border"])
        add(f"*{cls}.highlightColor", p["accent"])
        add(f"*{cls}.borderWidth", "0")
    add("*Text.insertBackground", p["fg"])
    add("*Scale.background", p["bg"])
    add("*Scale.foreground", p["fg"])
    add("*Scale.troughColor", p["surface"])
    add("*Scale.activeBackground", p["hover"])
    add("*Scale.highlightThickness", "0")
    add("*Toplevel.background", p["bg"])
    add("*Frame.background", p["bg"])
    add("*Label.background", p["bg"])
    add("*Label.foreground", p["fg"])
    add("*Canvas.background", p["bg"])
    add("*Canvas.highlightThickness", "0")
    add("*Button.background", p["surface"])
    add("*Button.foreground", p["fg"])
    add("*Button.activeBackground", p["hover"])
    add("*Button.activeForeground", p["fg"])
    add("*TCombobox*Listbox.background", p["field"])
    add("*TCombobox*Listbox.foreground", p["fg"])
    add("*TCombobox*Listbox.selectBackground", p["sel"])
    add("*TCombobox*Listbox.selectForeground", p["fg"])
    add("*Scrollbar.background", p["surface"])
    add("*Scrollbar.troughColor", p["bg"])
    add("*Scrollbar.activeBackground", p["hover"])
    add("*Scrollbar.highlightThickness", "0")
    add("*Menu.background", p["surface"])
    add("*Menu.foreground", p["fg"])
    add("*Menu.activeBackground", p["sel"])
    add("*Menu.activeForeground", p["fg"])


def _recolour(w: tk.Misc, p: dict, dark: bool) -> None:
    """Recolour an existing classic widget (ttk ones follow the style). Widgets marked keep_colors are left alone."""
    if getattr(w, "keep_colors", False):
        return
    cls = w.winfo_class()
    try:
        if cls == "Listbox":
            w.configure(bg=p["field"], fg=p["fg"], selectbackground=p["sel"], selectforeground=p["fg"],
                        highlightbackground=p["border"], highlightcolor=p["accent"])
        elif cls == "Text":
            w.configure(bg=p["field"], fg=p["fg"], selectbackground=p["sel"], selectforeground=p["fg"],
                        highlightbackground=p["border"], highlightcolor=p["accent"], insertbackground=p["fg"])
        elif cls == "Scale":
            w.configure(bg=p["bg"], fg=p["fg"], troughcolor=p["surface"], activebackground=p["hover"], highlightthickness=0)
        elif cls == "Button":
            w.configure(bg=p["surface"], fg=p["fg"], activebackground=p["hover"], activeforeground=p["fg"])
        elif cls in ("Frame", "Toplevel", "Labelframe"):
            w.configure(bg=p["bg"])
        elif cls == "Canvas":
            w.configure(bg=p["bg"], highlightthickness=0)
        elif cls in ("Label", "Message"):
            w.configure(bg=p["bg"], fg=p["fg"])
        elif cls in ("Checkbutton", "Radiobutton"):
            w.configure(bg=p["bg"], fg=p["fg"], activebackground=p["bg"], activeforeground=p["fg"], selectcolor=p["field"])
        elif cls in ("Entry", "Spinbox"):
            w.configure(bg=p["field"], fg=p["fg"], insertbackground=p["fg"], highlightbackground=p["border"])
        elif cls == "Menu":
            w.configure(bg=p["surface"], fg=p["fg"], activebackground=p["sel"], activeforeground=p["fg"])
        elif cls == "Scrollbar":
            w.configure(bg=p["surface"], troughcolor=p["bg"], activebackground=p["hover"], highlightthickness=0, bd=0)
    except tk.TclError:
        pass
    if cls == "Toplevel":
        set_dark_titlebar(w, dark)


def _walk(w: tk.Misc, p: dict, dark: bool) -> None:
    for c in w.winfo_children():
        _recolour(c, p, dark)
        _walk(c, p, dark)


def set_dark_titlebar(window: tk.Misc, dark: bool) -> None:
    """Ask Windows for a dark (or light) title bar on this toplevel. Harmless elsewhere."""
    if not sys.platform.startswith("win"):
        return
    try:
        import ctypes

        window.update_idletasks()
        hwnd = ctypes.windll.user32.GetParent(window.winfo_id())
        value = ctypes.c_int(1 if dark else 0)
        for attr in (20, 19):  # DWMWA_USE_IMMERSIVE_DARK_MODE, and its pre-20H1 number
            if ctypes.windll.dwmapi.DwmSetWindowAttribute(hwnd, attr, ctypes.byref(value), ctypes.sizeof(value)) == 0:
                break
    except Exception:
        pass


def apply_theme(root: tk.Misc, mode: str) -> dict:
    """Restyle a whole window (root and everything in it). Returns the palette in use."""
    mode = "dark" if mode == "dark" else "light"
    p = palette(mode)
    setup_fonts(root)
    style = ttk.Style(root)
    try:
        import sv_ttk

        sv_ttk.set_theme(mode, root)
        # Sun Valley calls tk_setPalette, which recolours classic widgets on the next idle pass;
        # let that happen now so the recolouring below is the last word.
        root.update_idletasks()
        setup_fonts(root)  # Sun Valley's named fonts exist now: point them at our families
    except Exception:
        names = style.theme_names()
        if mode == "dark":
            if "clam" in names:
                style.theme_use("clam")
            _style_clam_dark(style, p)
        else:
            native = next((n for n in ("vista", "aqua", "xpnative", "default") if n in names), None)
            if native:
                style.theme_use(native)
    # a little more air than the theme's defaults
    style.configure("Treeview", rowheight=26)
    style.configure("Treeview.Heading", font=font("body", 10, "bold"))
    style.configure("TNotebook.Tab", padding=(16, 7))
    style.configure("Glyph.TButton", font=(FONTS.get("glyph") or FONTS.get("body", "Segoe UI"), 13), padding=(10, 5))
    style.configure("Big.TButton", font=font("body", 11, "bold"), padding=(14, 8))
    style.configure("Title.TLabel", font=font("display", 15, "bold"))
    style.configure("Muted.TLabel", foreground=p["muted"])
    style.configure("Accent.TLabel", foreground=p["accent"])
    style.configure("Mono.TLabel", font=font("mono", 10))
    _option_defaults(root, p)
    try:
        root.configure(bg=p["bg"])
    except tk.TclError:
        pass
    _walk(root, p, mode == "dark")
    set_dark_titlebar(root, mode == "dark")
    # Sun Valley's tk_setPalette pass lands during the idle work above and repaints classic widgets
    # with the plain window colour; recolour once more so our field colours are the last word.
    _walk(root, p, mode == "dark")
    return p


class ThemedText(tk.Text):
    """A Text with a ttk scrollbar (Windows draws classic tk scrollbars natively and ignores colours,
    so a themed one is the only way to keep a dark pane dark). Pack/grid it like ScrolledText."""

    def __init__(self, master=None, **kw) -> None:
        self.frame = ttk.Frame(master)
        self.vbar = ttk.Scrollbar(self.frame, orient="vertical")
        kw.update(yscrollcommand=self.vbar.set)
        super().__init__(self.frame, **kw)
        self.vbar.configure(command=self.yview)
        self.vbar.pack(side="right", fill="y")
        tk.Text.pack(self, side="left", fill="both", expand=True)
        text_meths = vars(tk.Text).keys()
        methods = (vars(tk.Pack).keys() | vars(tk.Grid).keys() | vars(tk.Place).keys()) - text_meths
        for m in methods:
            if m[0] != "_" and m not in ("config", "configure"):
                setattr(self, m, getattr(self.frame, m))

    def __str__(self) -> str:
        return str(self.frame)


class ThemeMixin:
    """For a Tk window: keeps the preference, applies it, follows the system while set to 'system'."""

    theme_pref: str = "system"
    theme_mode: str = "light"
    pal: dict = LIGHT

    def theme_init(self) -> None:
        self.theme_pref = load_pref()
        self.theme_mode = resolve(self.theme_pref)
        self.pal = apply_theme(self, self.theme_mode)
        self.after(4000, self._theme_follow)

    def theme_set(self, pref: str) -> None:
        pref = pref.lower() if pref and pref.lower() in MODES else "system"
        self.theme_pref = pref
        save_pref(pref)
        self.theme_refresh(force=True)

    def theme_refresh(self, force: bool = False) -> None:
        mode = resolve(self.theme_pref)
        if force or mode != self.theme_mode:
            self.theme_mode = mode
            self.pal = apply_theme(self, mode)
            hook = getattr(self, "on_theme_applied", None)
            if hook:
                hook()

    def _theme_follow(self) -> None:
        try:
            if self.theme_pref == "system":
                self.theme_refresh()
        finally:
            self.after(4000, self._theme_follow)
