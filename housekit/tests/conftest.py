from __future__ import annotations

import itertools
import random
from pathlib import Path

import pytest

from housekit.config import DATA_DIR, Config, Penalties, Weights
from housekit.models import SlotSpec, Track
from housekit.textnorm import song_key

_ids = itertools.count(1)


def make_track(
    artist: str = "Artist",
    title: str = "Title",
    duration: float = 240.0,
    energy: float | None = 0.5,
    year: int | None = 1985,
    genre: str = "Classic Rock",
    album: str = "",
    explicit: bool | None = False,
    junk: bool = False,
    holiday: bool = False,
    live: bool = False,
    instrumental: bool = False,
    lufs: float | None = -12.0,
    path: str | None = None,
    version: str = "",
    track_id: int | None = None,
    tag_confidence: float = 1.0,
    codec: str = "FLAC",
) -> Track:
    tid = track_id if track_id is not None else next(_ids)
    album = album or f"{artist} Album {tid}"
    return Track(
        id=tid, path=path or f"C:/lib/{artist}/{tid} - {title}.flac", root="C:/lib", title=title, artist=artist,
        album_artist=artist, album=album, version=version, genre_raw=genre, genre_norm=genre, year=year, track_no=1,
        tag_confidence=tag_confidence, duration=duration, bitrate=320, codec=codec, explicit=explicit, junk=junk,
        holiday=holiday, live=live, instrumental=instrumental, song_key=song_key(artist, title), dupe_group=None,
        energy=energy, lufs=lufs, peak_dbfs=-1.0,
    )


def make_config(tmp_path: Path, **over) -> Config:
    lib = tmp_path / "library"
    lib.mkdir(exist_ok=True)
    cfg = Config(
        base_dir=tmp_path, config_path=None, library_roots=[lib], db_path=tmp_path / "library.db",
        output_dir=tmp_path / "gigs", playlists_dir=tmp_path / "playlists", setlist_path=tmp_path / "setlists",
        overrides_path=tmp_path / "setlist_overrides.toml", chart_file=tmp_path / "charts" / "hot.csv",
        slots=[
            SlotSpec("01_Doors", 45, 0.25, 0.50),
            SlotSpec("02_Break1", 20, 0.45, 0.65),
            SlotSpec("03_Break2", 20, 0.55, 0.80),
            SlotSpec("04_PostShow", 30, 0.25, 0.50),
        ],
        weights=Weights(), penalties=Penalties(), jitter=0.0,
        genres_file=DATA_DIR / "genres.toml", content_rules_file=DATA_DIR / "content_rules.toml",
    )
    for k, v in over.items():
        setattr(cfg, k, v)
    return cfg


def random_pool(n: int = 400, seed: int = 1) -> list[Track]:
    """A varied synthetic library: many artists, spread of durations/energies/decades/genres."""
    rng = random.Random(seed)
    genres = ["Classic Rock", "Pop", "Country", "Modern Country", "R&B/Soul", "Dance", "Alternative", "Yacht Rock"]
    out = []
    for i in range(n):
        artist = f"Artist {i % 120:03d}"
        out.append(make_track(
            artist=artist, title=f"Song {i:04d}", duration=rng.uniform(150, 330), energy=rng.uniform(0.05, 0.95),
            year=rng.choice([1968, 1975, 1982, 1988, 1994, 2003, 2011, 2019]), genre=rng.choice(genres),
            album=f"{artist} LP {i % 3}", explicit=rng.choice([False, False, False, None, True]),
            lufs=rng.uniform(-18, -7),
        ))
    return out


@pytest.fixture
def cfg(tmp_path: Path) -> Config:
    return make_config(tmp_path)
