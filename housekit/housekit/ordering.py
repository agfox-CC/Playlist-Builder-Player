"""Within-slot sequencing: gentle energy rise, no back-to-back genres where avoidable."""
from __future__ import annotations

from .models import SlotSpec, SlotTrack


def order_slot(tracks: list[SlotTrack], spec: SlotSpec) -> list[SlotTrack]:
    if not tracks:
        return []
    mid = (spec.energy_min + spec.energy_max) / 2.0
    seq = sorted(tracks, key=lambda st: (st.track.energy if st.track.energy is not None else mid, st.track.id))

    # De-clump genres: if two neighbours share a genre, pull a different genre forward from the next few.
    for i in range(1, len(seq)):
        if seq[i].track.genre_norm != seq[i - 1].track.genre_norm:
            continue
        for j in range(i + 1, min(i + 5, len(seq))):
            cand = seq[j]
            if cand.track.genre_norm == seq[i - 1].track.genre_norm:
                continue
            nxt = seq[i + 1] if i + 1 < len(seq) and i + 1 != j else None
            if nxt is not None and nxt.track.genre_norm == cand.track.genre_norm:
                continue
            seq[i], seq[j] = seq[j], seq[i]
            break

    for pos, st in enumerate(seq, 1):
        st.position = pos
    return seq
