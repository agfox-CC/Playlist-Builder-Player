# Playlist Builder & Player

**housekit** builds a cover band's venue house music (doors, set breaks, load-out) from a music
library and the band's setlist, ranks songs by chart popularity fully offline, makes custom
playlists, converts audio, and plays it all back with a QLab-style cue player.

Everything lives in [`housekit/`](housekit/): source, tests, the portable-build script and the
full manual in [`housekit/README.md`](housekit/README.md). [`CLAUDE.md`](CLAUDE.md) is the
operator's runbook.

Not in the repo: the music library, the packaged `HouseKit-Portable` folder (built with
`housekit/build_portable.ps1`), the catalog (`library.db`), the chart CSVs (`housekit charts fetch`
downloads them) and the gig / playlist output folders.
