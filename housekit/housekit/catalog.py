"""SQLite catalog: schema, upserts, queries, overrides, history. No printing, no config globals."""
from __future__ import annotations

import sqlite3
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable, Optional

from .models import Features, GigRecord, LibraryStatus, TagResult, Track

SCHEMA_VERSION = 1

FLAG_FIELDS = ("explicit", "junk", "holiday", "live", "instrumental")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);

CREATE TABLE IF NOT EXISTS tracks (
  id INTEGER PRIMARY KEY,
  path TEXT NOT NULL UNIQUE,
  root TEXT NOT NULL,
  size INTEGER, mtime REAL,
  song_key TEXT,
  title TEXT, artist TEXT, album_artist TEXT, album TEXT, version TEXT,
  genre_raw TEXT, genre_norm TEXT, year INTEGER, track_no INTEGER, comment TEXT,
  tag_confidence REAL,
  duration REAL, bitrate INTEGER, sample_rate INTEGER, channels INTEGER, codec TEXT,
  explicit_auto INTEGER, junk_auto INTEGER, holiday_auto INTEGER, live_auto INTEGER, instrumental_auto INTEGER,
  explicit_ovr INTEGER, junk_ovr INTEGER, holiday_ovr INTEGER, live_ovr INTEGER, instrumental_ovr INTEGER,
  genre_ovr TEXT,
  dupe_group TEXT,
  missing INTEGER NOT NULL DEFAULT 0,
  tag_error TEXT,
  first_seen REAL, last_seen REAL
);
CREATE INDEX IF NOT EXISTS idx_tracks_song_key ON tracks(song_key);
CREATE INDEX IF NOT EXISTS idx_tracks_artist ON tracks(artist);
CREATE INDEX IF NOT EXISTS idx_tracks_missing ON tracks(missing);

CREATE TABLE IF NOT EXISTS features (
  track_id INTEGER PRIMARY KEY REFERENCES tracks(id) ON DELETE CASCADE,
  analysis_version INTEGER, size INTEGER, mtime REAL,
  energy REAL, lufs REAL, peak_dbfs REAL, rms_db REAL, centroid_hz REAL, flux REAL,
  tempo REAL, onset_rate REAL, perc_ratio REAL, dyn_db REAL, flatness REAL,
  error TEXT, analyzed_at REAL
);

CREATE TABLE IF NOT EXISTS chart_songs (
  source TEXT NOT NULL, artist_norm TEXT NOT NULL, title_norm TEXT NOT NULL,
  artist TEXT, title TEXT,
  peak INTEGER, weeks INTEGER, first_year INTEGER, last_year INTEGER, score REAL, chart TEXT,
  PRIMARY KEY (source, artist_norm, title_norm)
);
CREATE INDEX IF NOT EXISTS idx_chart_title ON chart_songs(title_norm);
CREATE TABLE IF NOT EXISTS chart_albums (
  source TEXT NOT NULL, artist_norm TEXT NOT NULL, album_norm TEXT NOT NULL,
  artist TEXT, album TEXT,
  peak INTEGER, weeks INTEGER, first_year INTEGER, last_year INTEGER, score REAL,
  debut_years TEXT,
  PRIMARY KEY (source, artist_norm, album_norm)
);
CREATE INDEX IF NOT EXISTS idx_chart_album ON chart_albums(artist_norm);

CREATE TABLE IF NOT EXISTS gigs (
  id INTEGER PRIMARY KEY,
  name TEXT, date TEXT, venue TEXT, out_dir TEXT, logged_at REAL
);
CREATE TABLE IF NOT EXISTS gig_tracks (
  gig_id INTEGER REFERENCES gigs(id) ON DELETE CASCADE,
  track_id INTEGER, slot TEXT, position INTEGER, song_key TEXT, display TEXT
);
CREATE INDEX IF NOT EXISTS idx_gig_tracks_gig ON gig_tracks(gig_id);
"""

_TRACK_COLS = (
    "id, path, root, title, artist, album_artist, album, version, genre_raw, "
    "COALESCE(genre_ovr, genre_norm) AS genre_norm, COALESCE(year_ovr, year_orig, year) AS year, year AS year_tag, "
    "CASE WHEN year_ovr IS NOT NULL THEN 'hand' ELSE COALESCE(year_source, CASE WHEN year IS NULL THEN '' ELSE 'tag?' END) END AS year_source, "
    "track_no, tag_confidence, duration, bitrate, codec, "
    "COALESCE(explicit_ovr, explicit_auto) AS explicit, "
    "COALESCE(junk_ovr, junk_auto, 0) AS junk, "
    "COALESCE(holiday_ovr, holiday_auto, 0) AS holiday, "
    "COALESCE(live_ovr, live_auto, 0) AS live, "
    "COALESCE(instrumental_ovr, instrumental_auto, 0) AS instrumental, "
    "song_key, dupe_group, missing, tag_error, chart_peak, chart_weeks, chart_year, popularity, pop_source, album_year"
)

_TRACK_SELECT = f"""
SELECT {_TRACK_COLS}, f.energy, f.lufs, f.peak_dbfs, f.tempo, f.error AS analysis_error
FROM tracks t LEFT JOIN features f ON f.track_id = t.id
"""


def _b(v: Optional[bool]) -> Optional[int]:
    return None if v is None else (1 if v else 0)


def _ob(v: Any) -> Optional[bool]:
    return None if v is None else bool(v)


class Catalog:
    def __init__(self, db_path: Path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(self.db_path), timeout=30, isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=NORMAL")
        self.conn.execute("PRAGMA foreign_keys=ON")
        self._migrate()

    # ------------------------------------------------------------ lifecycle

    def _migrate(self) -> None:
        self.conn.executescript(_SCHEMA)
        # Columns added after a table was first created (older catalogs pick them up here).
        wanted = {
            "features": [("tempo", "REAL"), ("onset_rate", "REAL"), ("perc_ratio", "REAL"), ("dyn_db", "REAL"), ("flatness", "REAL")],
            "tracks": [("chart_peak", "INTEGER"), ("chart_weeks", "INTEGER"), ("chart_year", "INTEGER"),
                       ("year_orig", "INTEGER"), ("year_source", "TEXT"), ("year_ovr", "INTEGER"),
                       ("popularity", "REAL"), ("pop_source", "TEXT"), ("album_year", "INTEGER")],
            "chart_albums": [("debut_years", "TEXT")],
        }
        for table, cols in wanted.items():
            have = {r["name"] for r in self.conn.execute(f"PRAGMA table_info({table})")}
            for name, typ in cols:
                if name not in have:
                    self.conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {typ}")
        # chart_songs grew a per-source key; an older single-source table is dropped and re-imported from its file
        have = {r["name"] for r in self.conn.execute("PRAGMA table_info(chart_songs)")}
        if have and "score" not in have:
            self.conn.executescript("DROP TABLE chart_songs; " + _SCHEMA)
        cur = self.conn.execute("SELECT value FROM meta WHERE key='schema_version'")
        row = cur.fetchone()
        if row is None:
            self.conn.execute("INSERT INTO meta(key, value) VALUES ('schema_version', ?)", (str(SCHEMA_VERSION),))

    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> "Catalog":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    def set_meta(self, key: str, value: str) -> None:
        self.conn.execute("INSERT INTO meta(key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))

    def get_meta(self, key: str) -> Optional[str]:
        row = self.conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return row["value"] if row else None

    # ------------------------------------------------------------ scan support

    def known_files(self) -> dict[str, tuple[int, float, int]]:
        """path -> (size, mtime, id) for every catalogued file."""
        return {r["path"]: (r["size"], r["mtime"], r["id"]) for r in self.conn.execute("SELECT path, size, mtime, id FROM tracks")}

    def upsert_tags(self, results: Iterable[TagResult]) -> None:
        """Insert or refresh tag rows. Only *_auto and tag columns are rewritten; overrides are untouched."""
        now = time.time()
        rows = []
        for r in results:
            rows.append((
                r.path, r.root, r.size, r.mtime, r.song_key,
                r.title, r.artist, r.album_artist, r.album, r.version,
                r.genre_raw, r.genre_norm, r.year, r.track_no, r.comment,
                r.tag_confidence, r.duration, r.bitrate, r.sample_rate, r.channels, r.codec,
                _b(r.explicit_auto), _b(r.junk_auto), _b(r.holiday_auto), _b(r.live_auto), _b(r.instrumental_auto),
                r.error or None, now, now,
            ))
        with self.conn:
            self.conn.executemany(
                """
                INSERT INTO tracks (path, root, size, mtime, song_key, title, artist, album_artist, album, version,
                    genre_raw, genre_norm, year, track_no, comment, tag_confidence, duration, bitrate, sample_rate,
                    channels, codec, explicit_auto, junk_auto, holiday_auto, live_auto, instrumental_auto,
                    tag_error, first_seen, last_seen)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(path) DO UPDATE SET
                    root=excluded.root, size=excluded.size, mtime=excluded.mtime, song_key=excluded.song_key,
                    title=excluded.title, artist=excluded.artist, album_artist=excluded.album_artist,
                    album=excluded.album, version=excluded.version, genre_raw=excluded.genre_raw,
                    genre_norm=excluded.genre_norm, year=excluded.year, track_no=excluded.track_no,
                    comment=excluded.comment, tag_confidence=excluded.tag_confidence, duration=excluded.duration,
                    bitrate=excluded.bitrate, sample_rate=excluded.sample_rate, channels=excluded.channels,
                    codec=excluded.codec, explicit_auto=excluded.explicit_auto, junk_auto=excluded.junk_auto,
                    holiday_auto=excluded.holiday_auto, live_auto=excluded.live_auto,
                    instrumental_auto=excluded.instrumental_auto, tag_error=excluded.tag_error,
                    last_seen=excluded.last_seen, missing=0
                """,
                rows,
            )

    def touch_seen(self, paths: Iterable[str]) -> None:
        now = time.time()
        with self.conn:
            self.conn.executemany("UPDATE tracks SET last_seen=?, missing=0 WHERE path=?", [(now, p) for p in paths])

    def mark_missing(self, seen_paths: set[str], roots: list[str]) -> int:
        """Flag rows under the scanned roots whose file was not seen this scan."""
        n = 0
        with self.conn:
            for row in self.conn.execute("SELECT id, path, root FROM tracks WHERE missing=0"):
                if row["root"] in roots and row["path"] not in seen_paths:
                    self.conn.execute("UPDATE tracks SET missing=1 WHERE id=?", (row["id"],))
                    n += 1
        return n

    def relocate_roots(self, current_roots: list) -> dict[str, Any]:
        """Follow the music when the bundle moved. For every stored root folder that no longer exists,
        look for it as (1) a configured root with the same folder name, or (2) that folder name
        directly inside a configured root ("FLAC" moved into "Music"), and check that the stored
        files really are there. Paths are rewritten in place; rows that then collide with rows a
        later scan already created are merged into them (keeping hand corrections and play history).
        Roots that cannot be found are marked missing so nothing dead gets picked for a gig."""
        import os

        current = [Path(r) for r in current_roots if Path(r).is_dir()]
        stored_roots = [r[0] for r in self.conn.execute("SELECT DISTINCT root FROM tracks")]
        report: dict[str, Any] = {"relocated": [], "merged": 0, "marked_missing": 0, "restored": 0}
        for old in stored_roots:
            oldp = Path(old)
            if oldp.is_dir():
                continue
            name = oldp.name.lower()
            cands: list[tuple[Path, Path]] = []  # (folder now holding the old root's files, configured root)
            for r in current:
                if r.name.lower() == name and str(r).lower() != old.lower():
                    cands.append((r, r))
                sub = r / oldp.name
                if sub.is_dir():
                    cands.append((sub, r))
            sample = [row[0] for row in self.conn.execute("SELECT path FROM tracks WHERE root=? ORDER BY id LIMIT 25", (old,))]
            chosen = None
            for prefix, root in cands:
                hits = sum(1 for p in sample if (prefix / os.path.relpath(p, old)).exists())
                if sample and hits >= max(1, int(0.6 * len(sample))):
                    chosen = (prefix, root)
                    break
            if chosen is None:
                with self.conn:
                    cur = self.conn.execute("UPDATE tracks SET missing=1 WHERE root=? AND missing=0", (old,))
                report["marked_missing"] += cur.rowcount
                continue
            prefix, root = chosen
            rows = self.conn.execute("SELECT id, path FROM tracks WHERE root=?", (old,)).fetchall()
            with self.conn:
                for r in rows:
                    new_path = str(prefix / os.path.relpath(r["path"], old))
                    existing = self.conn.execute("SELECT id FROM tracks WHERE path=? AND id!=?", (new_path, r["id"])).fetchone()
                    if existing is not None:
                        self._merge_track(r["id"], existing["id"])
                        report["merged"] += 1
                    else:
                        self.conn.execute("UPDATE tracks SET path=?, root=?, missing=? WHERE id=?",
                                          (new_path, str(root), 0 if os.path.exists(new_path) else 1, r["id"]))
            report["relocated"].append((old, str(prefix)))
        return report

    def _merge_track(self, old_id: int, new_id: int) -> None:
        """Fold a stale duplicate row into the row that now represents the same file."""
        cols = [f"{f}_ovr" for f in FLAG_FIELDS] + ["genre_ovr", "year_ovr"]
        old = self.conn.execute(f"SELECT {', '.join(cols)} FROM tracks WHERE id=?", (old_id,)).fetchone()
        if old is not None:
            for c in cols:
                if old[c] is not None:
                    self.conn.execute(f"UPDATE tracks SET {c}=COALESCE({c}, ?) WHERE id=?", (old[c], new_id))
        self.conn.execute("UPDATE gig_tracks SET track_id=? WHERE track_id=?", (new_id, old_id))
        self.conn.execute("DELETE FROM features WHERE track_id=?", (old_id,))
        self.conn.execute("DELETE FROM tracks WHERE id=?", (old_id,))

    def inherit_overrides_from_missing(self) -> int:
        """A moved/renamed file shows up as a new row; carry hand-corrections over from the missing twin."""
        ovr_cols = [f"{f}_ovr" for f in FLAG_FIELDS] + ["genre_ovr", "year_ovr"]
        where_any = " OR ".join(f"{c} IS NOT NULL" for c in ovr_cols)
        n = 0
        with self.conn:
            donors = self.conn.execute(f"SELECT * FROM tracks WHERE missing=1 AND ({where_any})").fetchall()
            for d in donors:
                targets = self.conn.execute(
                    f"SELECT id FROM tracks WHERE missing=0 AND song_key=? AND ABS(COALESCE(duration,0)-?)<2 AND NOT ({where_any})",
                    (d["song_key"], d["duration"] or 0),
                ).fetchall()
                for t in targets:
                    sets = ", ".join(f"{c}=?" for c in ovr_cols)
                    self.conn.execute(f"UPDATE tracks SET {sets} WHERE id=?", tuple(d[c] for c in ovr_cols) + (t["id"],))
                    n += 1
        return n

    def fill_genres_from_artist(self) -> int:
        """Tracks with no genre tag inherit the most common normalized genre among the same artist's
        tagged tracks. Only touches the auto column; a genre override still wins."""
        rows = self.conn.execute(
            """
            SELECT artist, genre_norm, COUNT(*) AS n FROM tracks
            WHERE missing=0 AND artist != '' AND genre_raw != '' AND genre_norm != 'Other'
            GROUP BY artist, genre_norm
            """
        ).fetchall()
        best: dict[str, tuple[str, int]] = {}
        for r in rows:
            cur = best.get(r["artist"])
            if cur is None or r["n"] > cur[1]:
                best[r["artist"]] = (r["genre_norm"], r["n"])
        n = 0
        with self.conn:
            for artist, (genre, _) in best.items():
                cur = self.conn.execute(
                    "UPDATE tracks SET genre_norm=? WHERE missing=0 AND artist=? AND genre_raw='' AND genre_norm != ?",
                    (genre, artist, genre),
                )
                n += cur.rowcount
        return n

    def recompute_song_keys(self) -> int:
        """Recompute every track's song key with the current normalization rules (and carry the new
        keys into play history). Returns how many keys changed."""
        from .textnorm import song_key

        rows = self.conn.execute("SELECT id, artist, title, song_key FROM tracks").fetchall()
        changed = [(song_key(r["artist"] or "", r["title"] or ""), r["id"]) for r in rows]
        changed = [(k, i) for (k, i), r in zip(changed, rows) if k != (r["song_key"] or "")]
        with self.conn:
            self.conn.executemany("UPDATE tracks SET song_key=? WHERE id=?", changed)
            self.conn.execute(
                "UPDATE gig_tracks SET song_key=(SELECT song_key FROM tracks t WHERE t.id=gig_tracks.track_id) "
                "WHERE track_id IN (SELECT id FROM tracks)"
            )
        return len(changed)

    def rebuild_dupe_groups(self) -> int:
        """Group every copy of a song, across artist spellings (see identity.py), and store the group
        key in dupe_group for tracks that have at least one sibling. Returns the number of groups."""
        from collections import Counter

        from .identity import song_groups

        rows = self.conn.execute(
            "SELECT t.id, t.artist, t.album_artist, t.title, t.duration, f.tempo, f.energy "
            "FROM tracks t LEFT JOIN features f ON f.track_id = t.id WHERE t.missing=0"
        ).fetchall()
        groups = song_groups(
            ((r["id"], r["artist"] or "", r["album_artist"] or "", r["title"] or "", r["duration"], r["tempo"], r["energy"]) for r in rows),
            self.chart_artist_keys(), self.chart_title_acts(),
        )
        counts = Counter(groups.values())
        with self.conn:
            self.conn.execute("UPDATE tracks SET dupe_group=NULL")
            self.conn.executemany(
                "UPDATE tracks SET dupe_group=? WHERE id=?",
                [(g, i) for i, g in groups.items() if counts[g] > 1 and not g.startswith("|") and not g.endswith("|")],
            )
        return self.conn.execute("SELECT COUNT(DISTINCT dupe_group) FROM tracks WHERE dupe_group IS NOT NULL").fetchone()[0]

    def chart_artist_keys(self) -> set[str]:
        """Every artist spelling the chart data knows (normalized): a spelling oracle for song groups."""
        return {r[0] for r in self.conn.execute(
            "SELECT DISTINCT artist_norm FROM chart_songs UNION SELECT DISTINCT artist_norm FROM chart_albums"
        ) if r[0]}

    def chart_title_acts(self) -> dict[str, set[str]]:
        """Normalized title -> the acts (as the charts write them) that charted with it: the evidence
        for whether two same-title tracks by different artists are two songs or one mis-tagged one."""
        out: dict[str, set[str]] = {}
        for tn, a, an in self.conn.execute("SELECT DISTINCT title_norm, artist, artist_norm FROM chart_songs"):
            if tn and (a or an):
                out.setdefault(tn, set()).add(a or an)
        return out

    def rebuild_dupe_groups_by_key(self) -> int:
        """Older, exact-key grouping (kept for reference / tests)."""
        with self.conn:
            self.conn.execute("UPDATE tracks SET dupe_group=NULL")
            self.conn.execute(
                """
                UPDATE tracks SET dupe_group=song_key WHERE missing=0 AND song_key IN (
                    SELECT song_key FROM tracks WHERE missing=0 AND song_key != '' AND song_key NOT LIKE '|%'
                    GROUP BY song_key HAVING COUNT(*) > 1
                )
                """
            )
        return self.conn.execute("SELECT COUNT(DISTINCT dupe_group) FROM tracks WHERE dupe_group IS NOT NULL").fetchone()[0]

    # ------------------------------------------------------------ analysis support

    def analysis_jobs(self, version: int, retry_errors: bool = False, limit: Optional[int] = None) -> list[tuple[int, str, Optional[float], int, float]]:
        """(id, path, duration, size, mtime) for every present, tagged track lacking current features."""
        cond = "f.track_id IS NULL OR f.analysis_version != ? OR f.size != t.size OR f.mtime != t.mtime"
        if retry_errors:
            cond += " OR (f.error IS NOT NULL AND f.error != '')"
        sql = f"""
            SELECT t.id, t.path, t.duration, t.size, t.mtime FROM tracks t
            LEFT JOIN features f ON f.track_id = t.id
            WHERE t.missing=0 AND t.tag_error IS NULL AND t.duration IS NOT NULL AND ({cond})
            ORDER BY t.id
        """
        if limit:
            sql += f" LIMIT {int(limit)}"
        return [(r[0], r[1], r[2], r[3], r[4]) for r in self.conn.execute(sql, (version,))]

    def store_features(self, feats: Iterable[Features]) -> None:
        now = time.time()
        rows = [
            (f.track_id, f.analysis_version, f.size, f.mtime, f.energy, f.lufs, f.peak_dbfs, f.rms_db, f.centroid_hz, f.flux,
             f.tempo, f.onset_rate, f.perc_ratio, f.dyn_db, f.flatness, f.error or "", now)
            for f in feats
        ]
        with self.conn:
            self.conn.executemany(
                """
                INSERT INTO features (track_id, analysis_version, size, mtime, energy, lufs, peak_dbfs, rms_db, centroid_hz, flux,
                                      tempo, onset_rate, perc_ratio, dyn_db, flatness, error, analyzed_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(track_id) DO UPDATE SET
                    analysis_version=excluded.analysis_version, size=excluded.size, mtime=excluded.mtime,
                    energy=excluded.energy, lufs=excluded.lufs, peak_dbfs=excluded.peak_dbfs, rms_db=excluded.rms_db,
                    centroid_hz=excluded.centroid_hz, flux=excluded.flux, tempo=excluded.tempo, onset_rate=excluded.onset_rate,
                    perc_ratio=excluded.perc_ratio, dyn_db=excluded.dyn_db, flatness=excluded.flatness, error=excluded.error,
                    analyzed_at=excluded.analyzed_at
                """,
                rows,
            )

    # ------------------------------------------------------------ queries

    @staticmethod
    def _row_to_track(r: sqlite3.Row) -> Track:
        return Track(
            id=r["id"], path=r["path"], root=r["root"], title=r["title"] or "", artist=r["artist"] or "",
            album_artist=r["album_artist"] or "", album=r["album"] or "", version=r["version"] or "",
            genre_raw=r["genre_raw"] or "", genre_norm=r["genre_norm"] or "Other", year=r["year"], track_no=r["track_no"],
            tag_confidence=float(r["tag_confidence"] or 0.0), duration=r["duration"], bitrate=r["bitrate"], codec=r["codec"] or "",
            explicit=_ob(r["explicit"]), junk=bool(r["junk"]), holiday=bool(r["holiday"]), live=bool(r["live"]),
            instrumental=bool(r["instrumental"]), song_key=r["song_key"] or "", dupe_group=r["dupe_group"],
            energy=r["energy"], lufs=r["lufs"], peak_dbfs=r["peak_dbfs"], missing=bool(r["missing"]),
            analysis_error=r["analysis_error"] or "", tempo=r["tempo"],
            chart_peak=r["chart_peak"], chart_weeks=r["chart_weeks"], chart_year=r["chart_year"],
            year_tag=r["year_tag"], year_source=r["year_source"] or "",
            popularity=r["popularity"], pop_source=r["pop_source"] or "", album_year=r["album_year"],
        )

    def all_tracks(self, include_missing: bool = False) -> list[Track]:
        sql = _TRACK_SELECT + ("" if include_missing else " WHERE t.missing=0") + " ORDER BY t.artist, t.album, t.track_no, t.title"
        return [self._row_to_track(r) for r in self.conn.execute(sql)]

    def get_track(self, track_id: int) -> Optional[Track]:
        r = self.conn.execute(_TRACK_SELECT + " WHERE t.id=?", (track_id,)).fetchone()
        return self._row_to_track(r) if r else None

    def get_tracks(self, ids: Iterable[int]) -> list[Track]:
        ids = list(ids)
        if not ids:
            return []
        out = []
        for i in range(0, len(ids), 500):
            chunk = ids[i : i + 500]
            q = ",".join("?" * len(chunk))
            out.extend(self._row_to_track(r) for r in self.conn.execute(_TRACK_SELECT + f" WHERE t.id IN ({q})", chunk))
        return out

    def search(self, query: str, limit: int = 50, include_missing: bool = False) -> list[Track]:
        """Case-insensitive substring search over artist, title, album. Multiple words must all hit."""
        words = [w for w in query.split() if w]
        clauses = []
        params: list[Any] = []
        for w in words:
            clauses.append("(t.artist LIKE ? OR t.title LIKE ? OR t.album LIKE ? OR t.album_artist LIKE ?)")
            params += [f"%{w}%"] * 4
        if not include_missing:
            clauses.append("t.missing=0")
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        sql = _TRACK_SELECT + where + " ORDER BY t.artist, t.title LIMIT ?"
        params.append(limit)
        return [self._row_to_track(r) for r in self.conn.execute(sql, params)]

    def distinct_values(self) -> dict[str, list[str]]:
        """Artists, titles and albums present in the catalog (for type-ahead), most common first."""
        out: dict[str, list[str]] = {}
        for key, col in (("artists", "artist"), ("titles", "title"), ("albums", "album")):
            rows = self.conn.execute(
                f"SELECT {col} AS v, COUNT(*) AS n FROM tracks WHERE missing=0 AND {col} != '' GROUP BY {col} COLLATE NOCASE ORDER BY n DESC, v"
            ).fetchall()
            out[key] = [r["v"] for r in rows]
        return out

    def loudness_map(self) -> dict[str, tuple[Optional[float], Optional[float]]]:
        """lowercased path -> (lufs, peak_dbfs) for every analyzed track (used to match loudness on conversion)."""
        rows = self.conn.execute(
            "SELECT t.path, f.lufs, f.peak_dbfs FROM tracks t JOIN features f ON f.track_id=t.id WHERE t.missing=0 AND f.lufs IS NOT NULL"
        ).fetchall()
        return {r["path"].lower(): (r["lufs"], r["peak_dbfs"]) for r in rows}

    def rate_map(self) -> dict[str, int]:
        rows = self.conn.execute("SELECT path, sample_rate FROM tracks WHERE missing=0 AND sample_rate IS NOT NULL").fetchall()
        return {r["path"].lower(): int(r["sample_rate"]) for r in rows}

    def genre_counts(self) -> list[tuple[str, int]]:
        """Normalized genres actually present in the catalog (overrides applied), most common first."""
        rows = self.conn.execute(
            "SELECT COALESCE(genre_ovr, genre_norm, 'Other') AS g, COUNT(*) AS n FROM tracks WHERE missing=0 GROUP BY g ORDER BY n DESC, g"
        ).fetchall()
        return [(r["g"], r["n"]) for r in rows]

    def status(self, roots: list[str]) -> LibraryStatus:
        c = self.conn
        total = c.execute("SELECT COUNT(*) FROM tracks WHERE missing=0").fetchone()[0]
        missing = c.execute("SELECT COUNT(*) FROM tracks WHERE missing=1").fetchone()[0]
        analyzed = c.execute("SELECT COUNT(*) FROM tracks t JOIN features f ON f.track_id=t.id WHERE t.missing=0 AND f.energy IS NOT NULL").fetchone()[0]
        aerr = c.execute("SELECT COUNT(*) FROM tracks t JOIN features f ON f.track_id=t.id WHERE t.missing=0 AND f.error != ''").fetchone()[0]
        terr = c.execute("SELECT COUNT(*) FROM tracks WHERE missing=0 AND tag_error IS NOT NULL").fetchone()[0]
        flags = {}
        for f in FLAG_FIELDS:
            flags[f] = c.execute(f"SELECT COUNT(*) FROM tracks WHERE missing=0 AND COALESCE({f}_ovr, {f}_auto)=1").fetchone()[0]
        flags["explicit_unknown"] = c.execute("SELECT COUNT(*) FROM tracks WHERE missing=0 AND COALESCE(explicit_ovr, explicit_auto) IS NULL").fetchone()[0]
        flags["dupe_groups"] = c.execute("SELECT COUNT(DISTINCT dupe_group) FROM tracks WHERE dupe_group IS NOT NULL").fetchone()[0]
        flags["charted"] = c.execute("SELECT COUNT(*) FROM tracks WHERE missing=0 AND chart_peak IS NOT NULL").fetchone()[0]
        flags["popular"] = c.execute("SELECT COUNT(*) FROM tracks WHERE missing=0 AND popularity IS NOT NULL").fetchone()[0]
        flags["hand_corrections"] = c.execute(
            "SELECT COUNT(*) FROM tracks WHERE explicit_ovr IS NOT NULL OR junk_ovr IS NOT NULL OR holiday_ovr IS NOT NULL OR live_ovr IS NOT NULL OR instrumental_ovr IS NOT NULL OR genre_ovr IS NOT NULL OR year_ovr IS NOT NULL"
        ).fetchone()[0]
        return LibraryStatus(
            roots=roots, db_path=str(self.db_path), tracks=total, missing=missing, analyzed=analyzed,
            analysis_errors=aerr, tag_errors=terr, last_scan=self.get_meta("last_scan"), flags=flags,
        )

    def export_rows(self) -> list[dict[str, Any]]:
        sql = _TRACK_SELECT + " WHERE t.missing=0 ORDER BY t.artist, t.album, t.track_no"
        cols = [
            "id", "path", "artist", "title", "version", "album", "genre_norm", "genre_raw", "year", "year_tag", "year_source", "duration",
            "energy", "tempo", "lufs", "peak_dbfs", "chart_peak", "chart_weeks", "chart_year", "popularity", "pop_source", "album_year",
            "explicit", "junk", "holiday", "live", "instrumental", "dupe_group",
            "bitrate", "codec", "tag_confidence", "tag_error", "analysis_error",
        ]
        out = []
        for r in self.conn.execute(sql):
            d = {c: r[c] for c in cols}
            d["explicit"] = {None: "unknown", 0: "no", 1: "yes"}.get(r["explicit"], "unknown")
            for f in ("junk", "holiday", "live", "instrumental"):
                d[f] = "yes" if r[f] else ""
            out.append(d)
        return out

    # ------------------------------------------------------------ charts

    def import_chart_rows(self, rows: Iterable, source: str = "hot100", kind: Optional[str] = None) -> int:
        """Aggregate a source's rows (one per entry per week, or one per song) into one scored record
        per song (chart_songs) or per album (chart_albums). Re-importing a source replaces its records."""
        from .charts import SOURCES, aggregate, score_for

        from .textnorm import NORM_VERSION

        kind = kind or (SOURCES[source]["kind"] if source in SOURCES else "hot100")
        agg = aggregate(rows)
        self.set_meta("charts_norm_version", str(NORM_VERSION))  # artist keys below follow today's rules
        spins_max = max((a["spins"] for a in agg.values()), default=1) or 1
        table = "chart_albums" if kind == "billboard200" else "chart_songs"
        with self.conn:
            self.conn.execute(f"DELETE FROM {table} WHERE source=?", (source,))
            if table == "chart_albums":
                self.conn.executemany(
                    "INSERT OR REPLACE INTO chart_albums (source, artist_norm, album_norm, artist, album, peak, weeks, first_year, last_year, score, debut_years) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                    [(source, k[0], k[1], a["artist"], a["title"], a["peak"], a["weeks"], a["first_year"], a["last_year"], score_for(kind, a, spins_max),
                      ",".join(str(y) for y in sorted(a.get("debuts") or [])) or None)
                     for k, a in agg.items()],
                )
            else:
                self.conn.executemany(
                    "INSERT OR REPLACE INTO chart_songs (source, artist_norm, title_norm, artist, title, peak, weeks, first_year, last_year, score, chart) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                    [(source, k[0], k[1], a["artist"], a["title"], a["peak"], a["weeks"], a["first_year"], a["last_year"],
                      score_for(kind, a, spins_max), a.get("chart") or "") for k, a in agg.items()],
                )
        return len(agg)

    @staticmethod
    def _best_artist_match(cands: list, artists: list[str], cutoff: int):
        """Exact artist first, then a fuzzy artist among candidates with the same title ("AC/DC" vs "ACDC").
        Never title-only: a different song with the same name must not inherit a hit's run."""
        from rapidfuzz import fuzz

        for a in artists:
            if not a:
                continue
            exact = [c for c in cands if c["artist_norm"] == a]
            if exact:
                return exact
            best, best_s = [], 0.0
            for c in cands:
                s = max(fuzz.token_set_ratio(a, c["artist_norm"]), fuzz.ratio(a, c["artist_norm"]))
                if s > best_s:
                    best, best_s = [c], s
                elif s == best_s and best:
                    best.append(c)
            if best and best_s >= cutoff:
                return best
        return []

    @staticmethod
    def _album_year_for(row, tag_year: Optional[int]) -> Optional[int]:
        """The year the album a track is tagged with first charted. Same-named albums by one artist
        (self-titled records) are separate chart debuts, so the latest debut on or before the tag
        year is the one meant; a remaster tagged 2003 of an album that only ever debuted in 1980 is 1980."""
        debuts = [int(x) for x in (row["debut_years"] or "").split(",") if x.strip().isdigit()]
        if not debuts:
            return row["first_year"]
        if tag_year:
            fitting = [d for d in debuts if d <= tag_year + 1]
            if fitting:
                return max(fitting)
        return min(debuts)

    def apply_chart_data(self, artist_cutoff: int = 70) -> int:
        """Match every library track against every popularity source and store:
        chart_peak/weeks/year from the Hot 100 (for display and --top), popularity = best score
        across sources, pop_source, and album_year from the album chart (for era inference)."""
        from .textnorm import norm_artist, norm_basic, norm_title

        by_title: dict[str, list[sqlite3.Row]] = {}
        for r in self.conn.execute("SELECT source, artist_norm, title_norm, peak, weeks, first_year, score FROM chart_songs"):
            by_title.setdefault(r["title_norm"], []).append(r)
        by_album: dict[tuple[str, str], sqlite3.Row] = {}
        for r in self.conn.execute("SELECT source, artist_norm, album_norm, first_year, score, debut_years FROM chart_albums"):
            by_album[(r["artist_norm"], r["album_norm"])] = r
        if not by_title and not by_album:
            return 0
        updates = []
        for t in self.conn.execute("SELECT id, artist, album_artist, album, title, year FROM tracks WHERE missing=0"):
            artists = [norm_artist(t["artist"] or ""), norm_artist(t["album_artist"] or "")]
            hits = self._best_artist_match(by_title.get(norm_title(t["title"] or ""), []), artists, artist_cutoff)
            peak = weeks = year = None
            pop, pop_src = None, ""
            for h in hits:
                if h["peak"] is not None:  # a song chart with positions (Hot 100 or a file in that shape)
                    if peak is None or h["peak"] < peak or (h["peak"] == peak and (h["weeks"] or 0) > (weeks or 0)):
                        peak, weeks = h["peak"], h["weeks"]
                if h["first_year"] is not None:
                    year = h["first_year"] if year is None else min(year, h["first_year"])
                if h["score"] is not None and (pop is None or h["score"] > pop):
                    pop, pop_src = h["score"], h["source"]
            album_year = None
            alb = norm_basic(t["album"] or "")
            if alb:
                for a in artists:
                    ar = by_album.get((a, alb)) if a else None
                    if ar is not None:
                        album_year = self._album_year_for(ar, t["year"])
                        if ar["score"] is not None and (pop is None or ar["score"] > pop):
                            pop, pop_src = ar["score"], "billboard200"
                        break
            updates.append((peak, weeks, year, pop, pop_src, album_year, t["id"]))
        with self.conn:
            self.conn.executemany(
                "UPDATE tracks SET chart_peak=?, chart_weeks=?, chart_year=?, popularity=?, pop_source=?, album_year=? WHERE id=?", updates
            )
        return sum(1 for u in updates if u[3] is not None)

    def chart_stats(self) -> dict[str, Any]:
        c = self.conn
        per_source = {r[0]: r[1] for r in c.execute("SELECT source, COUNT(*) FROM chart_songs GROUP BY source")}
        per_source.update({r[0]: r[1] for r in c.execute("SELECT source, COUNT(*) FROM chart_albums GROUP BY source")})
        return {
            "chart_songs": c.execute("SELECT COUNT(*) FROM chart_songs").fetchone()[0],
            "chart_albums": c.execute("SELECT COUNT(*) FROM chart_albums").fetchone()[0],
            "per_source": per_source,
            "charted_tracks": c.execute("SELECT COUNT(*) FROM tracks WHERE missing=0 AND chart_peak IS NOT NULL").fetchone()[0],
            "top40_tracks": c.execute("SELECT COUNT(*) FROM tracks WHERE missing=0 AND chart_peak <= 40").fetchone()[0],
            "popular_tracks": c.execute("SELECT COUNT(*) FROM tracks WHERE missing=0 AND popularity IS NOT NULL").fetchone()[0],
        }

    def clear_catalog(self, keep_charts: bool = True) -> None:
        """Forget every track, its analysis and the play history (for pointing the app at a new library).
        Downloaded chart data is kept unless asked otherwise; setlist pins live in a file and are untouched."""
        with self.conn:
            self.conn.execute("DELETE FROM gig_tracks")
            self.conn.execute("DELETE FROM gigs")
            self.conn.execute("DELETE FROM features")
            self.conn.execute("DELETE FROM tracks")
            self.conn.execute("DELETE FROM meta WHERE key IN ('last_scan', 'library_roots', 'last_relocation')")
            if not keep_charts:
                self.conn.execute("DELETE FROM chart_songs")
                self.conn.execute("DELETE FROM chart_albums")
                self.conn.execute("DELETE FROM meta WHERE key IN ('charts_updated_at', 'charts_source_url')")
        self.conn.execute("VACUUM")

    # ------------------------------------------------------------ overrides

    def set_flag(self, track_id: int, flag: str, value: Optional[bool]) -> None:
        """Hand-correct a content flag. value=None clears the override (falls back to auto)."""
        if flag not in FLAG_FIELDS:
            raise ValueError(f"unknown flag {flag!r}; expected one of {FLAG_FIELDS}")
        with self.conn:
            self.conn.execute(f"UPDATE tracks SET {flag}_ovr=? WHERE id=?", (_b(value), track_id))

    def set_genre(self, track_id: int, genre: Optional[str]) -> None:
        with self.conn:
            self.conn.execute("UPDATE tracks SET genre_ovr=? WHERE id=?", (genre, track_id))

    # ------------------------------------------------------------ years

    def set_year(self, track_id: int, year: Optional[int]) -> None:
        """Hand-set a track's original year (None clears it and the inferred year applies again)."""
        with self.conn:
            self.conn.execute("UPDATE tracks SET year_ovr=? WHERE id=?", (year, track_id))

    def compute_era_years(self) -> dict[str, int]:
        """Work out every track's original year from tag, chart, copies, album and artist evidence
        (see era.py) and store it as year_orig / year_source. Returns counts per source."""
        from .era import infer_years

        rows = [dict(r) for r in self.conn.execute(
            "SELECT id, artist, album, version, year, chart_year, album_year, COALESCE(dupe_group, song_key) AS song_key "
            "FROM tracks WHERE missing=0"
        )]
        inferred = infer_years(rows)
        counts: dict[str, int] = {}
        with self.conn:
            self.conn.executemany(
                "UPDATE tracks SET year_orig=?, year_source=? WHERE id=?",
                [(y, src, tid) for tid, (y, src) in inferred.items()],
            )
        for _, (_, src) in inferred.items():
            counts[src or "none"] = counts.get(src or "none", 0) + 1
        return counts

    # ------------------------------------------------------------ history

    def log_gig(self, name: str, date: str, venue: str, out_dir: str, tracks: list[tuple[int, str, int, str, str]]) -> int:
        """tracks: (track_id, slot, position, song_key, display)."""
        with self.conn:
            cur = self.conn.execute(
                "INSERT INTO gigs(name, date, venue, out_dir, logged_at) VALUES (?,?,?,?,?)",
                (name, date, venue, out_dir, time.time()),
            )
            gid = cur.lastrowid
            self.conn.executemany(
                "INSERT INTO gig_tracks(gig_id, track_id, slot, position, song_key, display) VALUES (?,?,?,?,?,?)",
                [(gid, t[0], t[1], t[2], t[3], t[4]) for t in tracks],
            )
        return int(gid)

    def list_gigs(self) -> list[GigRecord]:
        rows = self.conn.execute(
            "SELECT g.*, (SELECT COUNT(*) FROM gig_tracks gt WHERE gt.gig_id=g.id) AS n FROM gigs g ORDER BY g.logged_at DESC, g.id DESC"
        ).fetchall()
        return [
            GigRecord(
                id=r["id"], name=r["name"] or "", date=r["date"] or "", venue=r["venue"] or "", out_dir=r["out_dir"] or "",
                logged_at=datetime.fromtimestamp(r["logged_at"]).isoformat(timespec="minutes"), track_count=r["n"],
            )
            for r in rows
        ]

    def recent_song_keys(self, last_n_gigs: int) -> set[str]:
        if last_n_gigs <= 0:
            return set()
        rows = self.conn.execute(
            "SELECT DISTINCT gt.song_key FROM gig_tracks gt WHERE gt.gig_id IN (SELECT id FROM gigs ORDER BY logged_at DESC, id DESC LIMIT ?)",
            (last_n_gigs,),
        ).fetchall()
        return {r[0] for r in rows if r[0]}

    def delete_gig(self, gig_id: int) -> bool:
        with self.conn:
            cur = self.conn.execute("DELETE FROM gigs WHERE id=?", (gig_id,))
        return cur.rowcount > 0
