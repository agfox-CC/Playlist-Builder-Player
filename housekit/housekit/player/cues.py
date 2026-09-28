"""Cue lists for the player, built from what HouseKit writes (a gig folder with one M3U8 per slot
and a manifest with per-track gain, or a playlist folder) or from any folder of audio, and saved
as cues.json next to the files so a copied folder carries its cues."""
from __future__ import annotations

import json
import os
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Iterable, Optional

from ..scoring import lufs_gain
from ..tags import AUDIO_EXTS

CUES_FILE = "cues.json"
TARGET_LUFS = -16.0


@dataclass
class Cue:
    path: str  # relative to the show folder when inside it
    title: str = ""
    artist: str = ""
    duration: Optional[float] = None
    gain_db: float = 0.0
    pre_wait: float = 0.0  # seconds of silence before this cue when it follows on
    fade_in: float = 0.0
    fade_out: float = 0.0
    in_point: Optional[float] = None  # seconds; None = auto (trim silence) or the start
    out_point: Optional[float] = None
    crossfade: Optional[float] = None  # overlap into the next cue; None = decide from how the track ends
    auto_follow: bool = True
    loop: bool = False  # repeat in..out until GO or stop (a walk-in bed)
    notes: str = ""

    @property
    def display(self) -> str:
        return f"{self.artist} - {self.title}" if self.artist else (self.title or Path(self.path).stem)


@dataclass
class Pad:
    key: str
    path: str = ""
    name: str = ""
    gain_db: float = 0.0


@dataclass
class CueList:
    name: str
    cues: list[Cue] = field(default_factory=list)
    source: str = ""  # the playlist file (relative) this list came from; "" for a list made in the player


@dataclass
class Show:
    folder: str
    lists: list[CueList] = field(default_factory=list)
    pads: list[Pad] = field(default_factory=lambda: [Pad(key=str(i)) for i in range(1, 9)])
    settings: dict[str, Any] = field(default_factory=lambda: dict(DEFAULT_SETTINGS))

    def abs_path(self, p: str) -> Path:
        q = Path(p)
        return q if q.is_absolute() else Path(self.folder) / q

    @property
    def cues_path(self) -> Path:
        return Path(self.folder) / CUES_FILE


DEFAULT_SETTINGS: dict[str, Any] = {
    "crossfade": 6.0,  # default overlap into the next cue for tracks that fade out
    "cold_gap": 0.25,  # overlap for tracks that stop dead (a hair of fade against clicks)
    "go_crossfade": 2.0,  # crossfade when GO is pressed over a playing cue
    "seek_crossfade": 0.4,  # crossfade when jumping to a spot in the playing cue
    "auto_trim": True,  # skip leading / trailing silence
    "master_db": 0.0,
    "duck_db": -12.0,  # how far the bed drops under a pad
    "panic_fade": 2.0,
    "device": None,  # output device name
    "mono": False,  # fold to mono on both outputs (one cable into one board channel)
    "target_lufs": TARGET_LUFS,
}


# ---------------------------------------------------------------- reading folders


_NUMBERED = re.compile(r"^\d{1,3}\s*[-.]\s*")


def _split_display(s: str) -> tuple[str, str]:
    s = _NUMBERED.sub("", s.strip())  # "001 - Artist - Title" -> "Artist - Title"
    if " - " in s:
        a, t = s.split(" - ", 1)
        return a.strip(), t.strip()
    return "", s.strip()


_ARTIST_KEYS = ("artist", "albumartist", "Author", "WM/AlbumArtist", "\xa9ART", "TPE1")
_TITLE_KEYS = ("title", "Title", "\xa9nam", "TIT2")


def quick_tags(path: Path) -> tuple[str, str, Optional[float]]:
    """(artist, title, duration) from the file's tags, falling back to the file name."""
    artist = title = ""
    dur: Optional[float] = None
    try:
        import mutagen

        f = mutagen.File(str(path), easy=True)
    except Exception:
        f = None
    if f is not None:
        tags = f.tags or {}

        def first(keys: tuple[str, ...]) -> str:
            for k in keys:
                try:
                    v = tags.get(k)
                except Exception:
                    v = None
                if v:
                    v = v[0] if isinstance(v, (list, tuple)) else v
                    s = str(getattr(v, "value", v)).strip()
                    if s:
                        return s
            return ""

        artist, title = first(_ARTIST_KEYS), first(_TITLE_KEYS)
        dur = getattr(getattr(f, "info", None), "length", None)
    if not title:
        a2, t2 = _split_display(path.stem)
        title = t2 or path.stem
        artist = artist or a2
    return artist, title, dur


def classify_drop(paths: Iterable[Path]) -> tuple[Optional[Path], list[Path]]:
    """For a drop with nothing open: (the folder or playlist to open as the show, the other items to
    add afterwards). No folder or playlist among them means (None, all the audio files)."""
    items = [Path(p) for p in paths]
    first = next((p for p in items if p.is_dir() or p.suffix.lower() in (".m3u8", ".m3u")), None)
    if first is None:
        return None, [p for p in items if p.is_file() and p.suffix.lower() in AUDIO_EXTS]
    return first, [p for p in items if p != first]


def _cue_for_file(base: Path, f: Path) -> Cue:
    artist, title, dur = quick_tags(f)
    return Cue(path=_rel(base, f), title=title, artist=artist, duration=dur)


def cues_from_paths(show: Show, paths: Iterable[Path]) -> list[Cue]:
    """Cues for dropped or chosen items: audio files, folders (their audio in name order), playlists."""
    out: list[Cue] = []
    base = Path(show.folder)
    for p in paths:
        p = Path(p)
        if p.is_dir():
            for f in sorted(p.iterdir()):
                if f.is_file() and f.suffix.lower() in AUDIO_EXTS:
                    out.append(_cue_for_file(base, f))
        elif p.is_file() and p.suffix.lower() in (".m3u8", ".m3u"):
            for f, disp, dur in read_m3u8(p):
                if not f.is_file():
                    continue
                c = _cue_for_file(base, f)
                if disp:
                    a, t = _split_display(disp)
                    c.artist, c.title = a or c.artist, t or c.title
                c.duration = c.duration or dur
                out.append(c)
        elif p.is_file() and p.suffix.lower() in AUDIO_EXTS:
            out.append(_cue_for_file(base, p))
    return out


def read_m3u8(path: Path) -> list[tuple[Path, str, Optional[float]]]:
    """(file, display, duration) per entry; relative entries resolved against the playlist's folder."""
    out = []
    pending_disp, pending_dur = "", None
    try:
        lines = path.read_text(encoding="utf-8-sig").splitlines()
    except OSError:
        return out
    for line in lines:
        s = line.strip()
        if not s:
            continue
        if s.startswith("#EXTINF:"):
            body = s[len("#EXTINF:"):]
            dur_s, _, disp = body.partition(",")
            try:
                pending_dur = float(dur_s)
            except ValueError:
                pending_dur = None
            pending_disp = disp.strip()
            continue
        if s.startswith("#"):
            continue
        p = Path(s.replace("/", os.sep))
        if not p.is_absolute():
            p = path.parent / p
        out.append((p, pending_disp, pending_dur))
        pending_disp, pending_dur = "", None
    return out


def _manifest_info(folder: Path) -> dict[tuple[str, str], dict]:
    """(list name, file name) -> {gain_db, artist, title, duration} from a HouseKit manifest, if any."""
    mp = folder / "manifest.json"
    info: dict[tuple[str, str], dict] = {}
    if not mp.is_file():
        return info
    try:
        m = json.loads(mp.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return info
    plan = m.get("plan")
    if plan:
        for sp in plan.get("slots", []):
            name = (sp.get("spec") or {}).get("name", "")
            for st in sp.get("tracks", []):
                t = st.get("track") or {}
                info[(name, st.get("out_name", ""))] = {"gain_db": st.get("gain_db"), "artist": t.get("artist", ""),
                                                        "title": t.get("title", ""), "duration": t.get("duration")}
    pl = m.get("playlist")
    if pl:
        for pt in pl.get("tracks", []):
            t = pt.get("track") or {}
            g = lufs_gain(t.get("lufs"), TARGET_LUFS, t.get("peak_dbfs"))
            info[(folder.name, pt.get("out_name", ""))] = {"gain_db": g, "artist": t.get("artist", ""),
                                                           "title": t.get("title", ""), "duration": t.get("duration")}
    return info


def _rel(folder: Path, p: Path) -> str:
    try:
        return str(p.resolve().relative_to(folder.resolve()))
    except ValueError:
        return str(p)


def _list_from_m3u8(folder: Path, m3u: Path, name: str, info: dict) -> CueList:
    cues = []
    for p, disp, dur in read_m3u8(m3u):
        artist, title = _split_display(disp) if disp else _split_display(p.stem)
        meta = info.get((name, p.name)) or {}
        cues.append(Cue(path=_rel(folder, p), title=meta.get("title") or title or p.stem, artist=meta.get("artist") or artist,
                        duration=meta.get("duration") or dur,
                        gain_db=round(meta.get("gain_db") or 0.0, 1) if meta.get("gain_db") is not None else 0.0))
    return CueList(name=name, cues=cues, source=_rel(folder, m3u))


def build_show(folder: Path) -> Show:
    """Cue lists from a folder: a gig folder (one list per slot, in order), a playlist folder (one
    list), a plain folder of audio (one list, files in name order), or an .m3u8 file."""
    folder = Path(folder)
    if folder.is_file() and folder.suffix.lower() in (".m3u8", ".m3u"):
        show = Show(folder=str(folder.parent))
        show.lists.append(_list_from_m3u8(folder.parent, folder, folder.stem, _manifest_info(folder.parent)))
        return show
    show = Show(folder=str(folder))
    info = _manifest_info(folder)
    subs = sorted(d for d in folder.iterdir() if d.is_dir() and any(f.suffix.lower() in (".m3u8", ".m3u") for f in d.iterdir()))
    if subs:  # a gig folder
        for d in subs:
            m3u = next(f for f in sorted(d.iterdir()) if f.suffix.lower() in (".m3u8", ".m3u"))
            show.lists.append(_list_from_m3u8(folder, m3u, d.name, info))
        return show
    own = sorted(f for f in folder.iterdir() if f.is_file() and f.suffix.lower() in (".m3u8", ".m3u"))
    if own:
        show.lists.append(_list_from_m3u8(folder, own[0], folder.name, info))
        return show
    cues = [Cue(path=_rel(folder, f), title=f.stem) for f in sorted(folder.iterdir()) if f.is_file() and f.suffix.lower() in AUDIO_EXTS]
    show.lists.append(CueList(name=folder.name, cues=cues, source="."))
    return show


# ---------------------------------------------------------------- cues.json


def save_show(show: Show) -> Path:
    data = {
        "version": 2,
        "settings": show.settings,
        "lists": [{"name": cl.name, "source": cl.source, "cues": [asdict(c) for c in cl.cues]} for cl in show.lists],
        "pads": [asdict(p) for p in show.pads],
    }
    show.cues_path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    return show.cues_path


def _cue_from_dict(cd: dict) -> Cue:
    return Cue(**{k: v for k, v in cd.items() if k in Cue.__dataclass_fields__})


def load_show(folder: Path) -> Show:
    """The saved cues.json when there is one, otherwise the folder as `build_show` sees it.
    Saved lists keep their order, names and edits; a list that came from a playlist file is matched
    to it by source (new files in the folder are appended, vanished files dropped); lists made in the
    player (no source) are kept as saved; playlist files that appeared since are added at the end."""
    folder = Path(folder)
    base = folder.parent if folder.is_file() else folder
    fresh = build_show(folder)
    cp = base / CUES_FILE
    if not cp.is_file():
        return fresh
    try:
        data = json.loads(cp.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return fresh
    show = Show(folder=str(base))
    show.settings = dict(DEFAULT_SETTINGS) | (data.get("settings") or {})
    show.pads = [Pad(**{k: v for k, v in p.items() if k in Pad.__dataclass_fields__}) for p in data.get("pads", [])] or show.pads
    by_source = {cl.source: cl for cl in fresh.lists if cl.source}
    by_name = {cl.name: cl for cl in fresh.lists}
    used: set[int] = set()
    for s in data.get("lists", []):
        src = s.get("source", "")
        fl = by_source.get(src) if src else by_name.get(s.get("name", ""))
        if fl is not None and id(fl) in used:
            fl = None
        if fl is None:  # a list made in the player: keep what still exists
            cues = [c for c in (_cue_from_dict(cd) for cd in s.get("cues", [])) if show.abs_path(c.path).is_file()]
            show.lists.append(CueList(name=s.get("name") or "List", cues=cues, source=""))
            continue
        used.add(id(fl))
        by_path = {c.path.lower(): c for c in fl.cues}
        cues = []
        for cd in s.get("cues", []):
            c = _cue_from_dict(cd)
            if c.path.lower() in by_path or show.abs_path(c.path).is_file():
                cues.append(c)
                by_path.pop(c.path.lower(), None)
        cues.extend(by_path.values())  # files that arrived since the cues were saved
        show.lists.append(CueList(name=s.get("name") or fl.name, cues=cues, source=fl.source))
    for fl in fresh.lists:
        if id(fl) not in used:
            show.lists.append(fl)
    return show
