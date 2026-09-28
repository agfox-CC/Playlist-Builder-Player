import tkinter as tk
from pathlib import Path
from tkinter import ttk

import pytest

from housekit.theme import DARK, FONTS, LIGHT, MODES, apply_theme, font, glyph, load_pref, palette, resolve, save_pref, system_prefers_dark


def test_font_and_glyph_helpers_have_fallbacks():
    saved = dict(FONTS)
    try:
        FONTS.clear()
        assert font("mono", 10) == ("Consolas", 10) and font("display", 20, "bold") == ("Segoe UI", 20, "bold")
        assert glyph("play") == ("▶", False)
        FONTS.update(display="Bahnschrift", body="Segoe UI", mono="Cascadia Mono", glyph="Segoe Fluent Icons")
        assert font("display", 30, "bold")[0] == "Bahnschrift" and glyph("pause") == ("", True)
    finally:
        FONTS.clear()
        FONTS.update(saved)


def test_apply_theme_restyles_ttk_and_classic_widgets():
    try:
        root = tk.Tk()
    except tk.TclError:
        pytest.skip("no display")
    root.withdraw()
    frame = ttk.Frame(root)
    frame.pack()
    lb = tk.Listbox(frame)
    lb.pack()
    txt = tk.Text(frame)
    txt.pack()
    keep = tk.Button(frame, bg="#2e7d32", fg="white")
    keep.keep_colors = True
    keep.pack()
    tree = ttk.Treeview(frame)
    tree.pack()
    try:
        p = apply_theme(root, "dark")
        style = ttk.Style(root)
        assert style.theme_use() in ("sun-valley-dark", "clam")
        assert style.lookup("Treeview", "fieldbackground") or style.lookup("Treeview", "background")
        assert lb.cget("bg") == DARK["field"] and txt.cget("fg") == DARK["fg"] and keep.cget("bg") == "#2e7d32"
        assert root.cget("bg") == DARK["bg"] and p == DARK
        late = tk.Listbox(frame)  # created after: picks the defaults up
        assert late.cget("bg") == DARK["field"]
        assert FONTS["body"] and FONTS["mono"] and font("mono", 10)[0] == FONTS["mono"]
        p = apply_theme(root, "light")
        assert ttk.Style(root).theme_use() in ("sun-valley-light", "vista", "xpnative", "default", "aqua")
        assert lb.cget("bg") == LIGHT["field"] and root.cget("bg") == LIGHT["bg"] and p == LIGHT
    finally:
        root.destroy()


def test_resolve_and_palettes():
    assert resolve("dark") == "dark" and resolve("light") == "light" and resolve("DARK") == "dark"
    assert resolve("system") in ("dark", "light") and resolve(None) == resolve("system") == resolve("nonsense")
    assert isinstance(system_prefers_dark(), bool)
    assert set(DARK) == set(LIGHT) and palette("dark") == DARK and palette("light") == LIGHT and palette("system") == LIGHT
    assert MODES == ("system", "dark", "light")


def test_pref_file_roundtrip(tmp_path: Path):
    assert load_pref(tmp_path) == "system"  # nothing saved yet
    p = save_pref("dark", tmp_path)
    assert p.name == "ui.json" and load_pref(tmp_path) == "dark"
    save_pref("bogus", tmp_path)
    assert load_pref(tmp_path) == "system"
    p.write_text('{"theme": "light", "other": 1}', encoding="utf-8")
    assert load_pref(tmp_path) == "light"
    save_pref("dark", tmp_path)
    assert '"other": 1' in p.read_text(encoding="utf-8")  # other keys survive
    p.write_text("not json", encoding="utf-8")
    assert load_pref(tmp_path) == "system" and load_pref(tmp_path / "missing") == "system"
