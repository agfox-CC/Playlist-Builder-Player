"""Read-only check of the song-identity rules against a real catalog: which same-title tracks by
*different* acts count as one song (wrong artist tags, covers, album names in the artist field),
with the evidence (length / tempo / energy) so false merges stand out.

    python tools\\merge_check.py [path\\to\\library.db]

Defaults to the user's live catalog. Prints every merged title; eyeball it after touching
identity.py (a rule that folded any non-charting act into the charting one merged 339 titles,
mostly wrong; the same-recording rule merges ~36, all real, on 2026-09-20)."""
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from housekit.catalog import Catalog  # noqa: E402
from housekit.identity import song_groups  # noqa: E402
from housekit.textnorm import norm_artist  # noqa: E402

db = sys.argv[1] if len(sys.argv) > 1 else r"C:\Users\Dell\Desktop\Test 2\HouseKit-Portable\library.db"
cat = Catalog(db)
tracks = [t for t in cat.all_tracks() if not t.missing]
rows = [(t.id, t.artist or "", t.album_artist or "", t.title or "", t.duration, t.tempo, t.energy) for t in tracks]
known = cat.chart_artist_keys()
acts = cat.chart_title_acts()
old = song_groups([r[:4] for r in rows], known)  # names only
new = song_groups(rows, known, acts)  # names + same-recording evidence + chart guard
info = {t.id: t for t in tracks}

merged = defaultdict(set)  # new key -> old keys folded into it
for id_, k in new.items():
    merged[k].add(old[id_])
changed = {k: v for k, v in merged.items() if len(v) > 1}
print(f"{len(rows)} tracks; {len(set(old.values()))} songs by name, {len(set(new.values()))} with the recording rule; "
      f"{len(changed)} titles where different acts became one song\n")


def _desc(t) -> str:
    d = int(t.duration or 0)
    s = f"{d // 60}:{d % 60:02d}"
    if t.tempo:
        s += f"/{int(t.tempo)}bpm"
    if t.energy is not None:
        s += f"/e{t.energy:.2f}"
    return s


for k in sorted(changed):
    tn = k.split("|", 1)[1]
    members = [info[i] for i, kk in new.items() if kk == k]
    by_act = defaultdict(list)
    for m in members:
        by_act[norm_artist(m.artist) or norm_artist(m.album_artist) or "?"].append(m)
    parts = [f"{ms[0].artist or ms[0].album_artist or '?'} ({', '.join(_desc(m) for m in sorted(ms, key=lambda m: m.duration or 0))})"
             for ms in by_act.values()]
    print(f"{tn!r}: " + "  |  ".join(parts))
cat.close()
