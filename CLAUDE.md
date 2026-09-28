# PlayList Project

This folder holds a music library and **housekit**, a CLI that builds venue house music
(pre-show, set breaks, load-out) for a cover band's gig from that library and the band's setlist.

The user does not want to type commands. When they say something like *"build the house music for
Saturday at the zoo, family friendly, setlist attached"*, do the whole thing for them and report
back with the folder path and anything they need to check.

## Layout

- `FLAC\`, `MP3\` and `FLAC Vinyl\` — the music library, ~17k files. **Read-only. Never write, move, rename or retag anything here.**
  (`FLAC Vinyl` appeared 2026-09-13 and was added to the roots; if another folder of music appears
  next to these, add it to `library.roots` in `housekit.toml` and reindex.)
- `HouseKit-Portable\` — the packaged app in the **python layout**: `python\` (copy of the signed
  python.org 3.12 interpreter with the deps pip-installed), `housekit\` (the tool as source),
  `HouseKit.lnk` / `HouseKit.cmd` (window, via `pythonw -m housekit.app`), `housekit-cli.cmd`
  (`python -m housekit.cli_main`), bundled `ffmpeg\`, plus its own catalog/config/charts/setlists.
  Built from `housekit\` with `build_portable.ps1` (updates in place, never deletes data).
  **Why not an exe:** this PC has Windows Smart App Control ON (since 21:45 on 2026-09-13); it blocks
  unsigned, never-seen executables, so PyInstaller builds (`-Layout exe`) are refused here while the
  signed python.exe and plain scripts run fine. Don't ship exes to this user unless they are signed.
  **Never name a build output `..\HouseKit`** — Windows treats it as the same folder as `housekit`
  and the script would try to delete the project. The script refuses that now.
- `housekit\` — the tool's source. Python venv at `housekit\.venv`. Config in `housekit\housekit.toml`.
- `housekit\setlists\` — put setlist PDFs / text files here (copy attachments in).
- `gigs\` — output. One folder per gig, e.g. `gigs\2026-09-12 Zoobilee\`.
- `housekit\library.db` — the catalog. Already fully indexed and analyzed.

## Running it

Run from `housekit\`. In the PowerShell tool the PATH can be stale (ffmpeg lives on the *user* PATH), so
prefix commands with:

```powershell
$env:Path = [Environment]::GetEnvironmentVariable("Path","Machine") + ";" + [Environment]::GetEnvironmentVariable("Path","User"); $env:PYTHONIOENCODING="utf-8"
```

Then use `.\.venv\Scripts\housekit.exe ...` (or `..\housekit.cmd ...` from anywhere, which does the PATH fix itself).

## The standing setlist

The band plays the same set every gig. The sheets in `housekit\setlists\` (`SET 1.docx`,
`SET 2.docx`, `SET 3.docx`) **are** the setlist until the user replaces those files. `housekit build`
reads that folder by default, so nothing about the setlist needs to be passed per gig.

The sheets are all caps, title only, in shorthand ("7 NATION ARMY", "HATE MYSELF 4 LOVIN U", "*SHINE"
where `*` marks a vocalist, "D - THE MIDDLE" where D is the key, "HANDS 2 YERSF-SLOW RIDE" is a
medley). The parser handles all of that. Every line is currently pinned or matched; pins live in
`housekit\setlist_overrides.toml` and persist.

**Only when the user says the set changed** (new files in the folder): run `housekit setlist parse`
and make sure every line is `matched`, `override` or `not_in_library`. For an `unresolved` line,
`housekit search "<words>"` the library; if the song is there, `housekit setlist resolve "<line
exactly as shown>" --track <id>` (repeat `--track` for each song of a medley), otherwise
`--not-in-library`. Ask the user only if a line is genuinely ambiguous.

## The standard job: "build the house music for <gig>"

1. `housekit build --out "<YYYY-MM-DD Venue>" --venue "<venue>" [--clean] --dry-run`
   - `--clean` for family / all-ages gigs (zoo, church, school, daytime). Omit for bars (the usual case).
   - Slot lengths default to 180/80/80/120 minutes in `housekit.toml`: four times the real windows
     (45/20/20/30) because the user wants plenty of spare songs in every folder (~108 tracks,
     ~3.7 GB per build). Override per gig with `--doors N --break1 N --break2 N --post N` only if asked.
   - `--seed N` to get a different plan if the user doesn't like one.
   - Every slot should say `[OK]`. If one says SHORT/LONG, the WARNINGS explain why.
   - If the user gave no date/venue, `--out` can be omitted (folder becomes `<today> Gig`).
2. Same command without `--dry-run` to copy the files and write playlists. Add `--overwrite` if that
   gig folder already exists. This logs the gig to history so the next three builds avoid repeats,
   which matters here precisely because the set never changes.
3. Report: the gig folder path, the four slot runtimes, and any `!LEVEL` tracks worth riding
   (plan.txt lists per-track gain to -16 LUFS).

Output folder contents: `01_Doors\`, `02_Break1\`, `03_Break2\`, `04_PostShow\` with numbered copies
and an `.m3u8` each, `housemusic.m3u8` for the whole night, `plan.txt`, `manifest.json`.

## Popularity / chart data

The user wants popular songs preferred, and the app must work **100% offline** once the data is in.
Five sources live as CSVs in `housekit\charts\` (git-ignored) and are imported into the catalog
(`chart_songs` / `chart_albums` tables, keyed by source): `hot100` (Billboard Hot 100 weekly since
1958, utdata/rwd-billboard-data), `hot100-archive` (a second 1958-2021 export), `billboard200`
(album chart, weekly since 1963: dates albums, lifts album tracks at 85%), `radio` (FiveThirtyEight
classic-rock radio spins, ~2,200 songs) and `yearend` (Billboard year-end genre charts scraped from
billboard.com: country, rock, mainstream rock, alternative, R&B/hip-hop, dance, AC, pop airplay;
`charts scrape`, resumable, newest-first because billboard.com serves the nearest later year for a
missing year and the scraper skips those stand-ins). All were fetched 2026-09-13.

Every source scores 0-1 on the Hot 100 scale; `tracks.popularity` is the best score across
sources, `pop_source` says which, `chart_peak/weeks/year` keep the Hot 100 run (for `--top N`
and the `#3/22w` label; other sources show as `radio` / `album` / `yearend`), `album_year` is
the album's own Billboard 200 debut (used by era inference). Commands: `charts fetch [all|name]`
(download + import, network once), `charts scrape`, `charts load` (import whatever files are in
`charts\`, no network; the window does this on first start of a copied folder), `charts import
<file> --source name`, `charts status`, `charts update` (weekly Hot 100 + Billboard 200 refresh
when older than `[charts] auto_update_days`, run in the background by the window; failure offline
is silent and harmless). Builds never touch the network.

## Custom playlists ("top 20 Steely Dan", "80s party mix", ...)

`housekit playlist "<Name>" [--artist X]... [--genre G]... [--decade 70s]... [--years 1975-1985]
[--energy mellow|laid-back|upbeat|"high energy"|full-on|0.3-0.6]
[--top N] [--search words] [--count N | --minutes M] [--order rank|energy|year|alpha|random]
[--clean] [--allow-live] [--fill] [--no-copy] [--dry-run] [--overwrite]`. Output: `playlists\<Name>\` with
numbered copies (or an M3U8 into the library with `--no-copy`), `<Name>.m3u8`, `playlist.txt`.
Ranking: songs with known popularity first (best score), then unknown by copies owned (uncharted
songs are kept, just ranked last; only `--top N` drops them); one copy
per song; live and junk excluded unless asked. `--order random` also spaces the songs so the same
act never plays twice in a row when the mix allows (user request 2026-09-13; `spread_artists`).
**`--fill`** (window: "Fill with similar songs when short", user request 2026-09-20 after a Phil
Collins list stopped at 6: the library holds only 6 distinct Phil Collins songs, mostly single
tracks and compilation copies): when an artist / `--search` list is short of `--count` or
`--minutes`, `fill_similar` tops it up with songs by other acts scored against the matched songs'
profile (`derive_profile`: era_fit 0.35, genre_fit 0.30, familiarity 0.20, energy closeness 0.15),
same base filters, no song or act already in the list, at most 2 per act; marked `(similar)` in
`playlist.txt` (`PlaylistTrack.filled`) and placed after the real matches in rank order. Off by
default so "top 20 Steely Dan" stays all Steely Dan unless asked. `--decade` repeats (50, 60s, 1970 all work) and is
OR-ed with `--years`; the window has decade checkboxes 50s-2020s plus a custom range box. Genre
names must be the normalized ones (Classic Rock, Southern Rock, Pop, Dance, Country, Modern
Country, R&B/Soul, Motown, Funk, Hip-Hop, Yacht Rock, Alternative, Blues, Other). Use `--dry-run`
first when the user is exploring; copy when they want the files.

## Years (remasters lie)

`Track.year` is the inferred original year, not the tag: `era.infer_years` runs after every scan
and chart refresh (`Catalog.compute_era_years`) and stores `year_orig`/`year_source`; the raw tag
stays in `year_tag`. Order of evidence: chart first-week year (or the radio / year-end release
year) > the album's own Billboard 200 debut when it is 2+ years before the tag (`album chart`:
unmarked remasters of album tracks; self-titled albums are told apart by their chart debuts) >
credible tag year (no reissue / compilation / live-album markers, or the year printed on the
album name) > earliest credible copy of the song > median of the same non-compilation album >
artist median (span <= 15 y) > tag kept as `tag?`. Hand override: `housekit flag <id> year=1977`
/ "Set year" on the Library tab (`year_ovr`, source shows `hand`). On 2026-09-13 this corrected
4,683 of ~17k tracks in the user's catalog before the album-chart tier existed.

## HouseKit Player (added 2026-09-14 at the user's request, standalone from the builder)

`housekit/player/` (decode, engine, cues, show) + `housekit/player_app.py` (tkinter window,
`python -m housekit.player_app [folder]`, launchers `HouseKit Player.lnk/.cmd`, CLI `housekit
player`). Reads a gig folder (one cue list per slot from the M3U8s, gains from manifest.json), a
playlist folder, a plain folder or an .m3u8; needs no catalog. Audio: ffmpeg decodes to int16 PCM
in a worker thread (two cues ahead), `engine.Engine` mixes voices in numpy with equal-power fades
and **sample-clock scheduled starts** (gapless follow-ons), pads duck the bed, master fader, mono
fold (STEREO/MONO toggle button by the transport, hotkey M, amber when on: L+R averaged onto both
outputs, user requests 2026-09-14 and 2026-09-20 "make a switch in the player"), output
device choice via `sounddevice` (PortAudio wheel; loads fine under Smart App Control). `decode.
analyze_edges` finds leading/trailing silence and the ending type (fade / ring / cold), which sets
the overlap into the next cue. Per-cue gain / pre-wait / fades / in-out points / crossfade /
auto-follow and the pads and settings persist in `cues.json` in the folder. Keys: Space GO, P
pause, Esc stop, Shift+Esc panic, 1-8 pads, F11 show display. `housekit player --selftest` opens
and closes the window. The mixer is testable offline (`Engine.render`), see tests/test_player.py.
The user tested real playback on their own setup on 2026-09-20 and was happy with it.
Mono files are decoded through a `pan` filter at unity (ffmpeg's `-ac 2` would drop them 3 dB).
List editing (user request 2026-09-20 "add the list features and also file drop in"): `Player.add_cues /
remove_cue / move_cue / new_list / duplicate_list / rename_list / delete_list` keep the standby cue
and drop any queued follow-on; `Cue.loop` (engine `Voice.loop` wraps seamlessly inside a block, no
follow-on while looping); `Player.audition(i)` = `go(preroll=15)` then stop 8 s after the join and
put the standby back. `cues.json` v2 stores each list's `source` (its M3U8) so renamed lists still
match their folder and player-made lists (source "") persist; `cues_from_paths` turns dropped
files / folders / playlists into cues with tags via `quick_tags` (mutagen, name fallback that
strips "001 - "). Drag-and-drop from Explorer uses `tkinterdnd2` (tkdnd 2.10 loads fine under
Smart App Control); targets: tree (insert at the drop row), lists box (new list), pads, window.
Tree rows drag to reorder (`_drag_*`), Delete / Ctrl+Up / Ctrl+Down keys. The builder also
takes drops: Convert tab (files/folders -> convert list), Setup tab (folders -> music roots).
**Audio glitches** (user, 2026-09-20: "static when the song starts, and a second after a jog"):
the decode worker's analysis was building huge temporaries (`astype(int32)` of whole tracks,
float copies for RMS: hundreds of MB per track) exactly when the next cue is prefetched, starving
the PortAudio callback (GIL + memory stalls). Fixed: `_block_peaks` uses int16 max/-min per block
in 256k-frame chunks, RMS is chunked and the body level sampled from 40 one-second windows
(`_body_level_db`); analysis is now ~15-40 ms and ~4 MB per track (`scratchpad/analysis_cost.py`).
Also `Engine.start` uses `latency=0.35` (a deep buffer; mixing runs in Python) and every voice gets
at least an 8 ms fade-in. If crackle ever returns, look for anything holding the GIL or allocating
big arrays on the main/worker threads while audio runs.
**Seek** (user request 2026-09-20 "pick a spot in the waveform, smooth transition"): `Player.seek(sample)`
starts a second Voice on the same PCM at the new position with a `ramp_from/ramp_len` fade-in
while the old voice gets a manual fade-out (`settings.seek_crossfade`, 0.4 s); the queued
follow-on is dropped and re-timed by the next tick. `Engine.play` keeps a pre-set `pos` beyond
`in_pt` (that is how a seek starts mid-track). Waveform click/drag calls `on_seek(frac)`;
Left/Right = 5 s, Shift = 30 s.
**Quick List** (the user's actual DnD complaint, 2026-09-20: dropping files on an *empty* player
did nothing because no show/list existed): `classify_drop` + `_ensure_show` open a dropped folder
or playlist as the show, and loose files (or Add files / Add folder / New list with nothing open)
create `<playlists dir>/Quick List/` (cues.json there, absolute paths to the files, never written
beside the music) and add to it.
Status-line messages in the player go through `flash()` (the 100 ms poll repaints the line, so
plain `status_var.set` messages vanished at once; that hid drop feedback until 2026-09-20).
**Verifying drag-and-drop for real** (2026-09-20, user reported "file drag and drop isn't
working"): the desktop-control tools grant File Explorer at "click" tier (no drags from it), so
the test used a tkdnd *drag source* window (`scratchpad/drag_source.py`, returns
`(COPY, DND_FILES, (path,))` -- a tuple, or the path is split into fragments) launched under the
venv's python.exe, dragged with mouse_down/move/up along a path that stays over Python windows;
a real OLE drop landed as a cue and in-list drag reorder worked. The extension loads and registers
in the Test 2 copy too (`dnd_ok True`). The user's failure was not reproduced.

## Theme and styling (user requests 2026-09-20: dark / follow system; "less sterile")

`housekit/theme.py`: preference system | dark | light in `ui.json` next to the app (shared by the
builder and the player; `load_pref`/`save_pref`). `ThemeMixin` on both windows applies it at start
and re-checks the Windows setting every 4 s while on "system". `apply_theme` uses the **Sun Valley**
ttk theme (`sv-ttk` package, MIT, pure Tcl + PNGs; falls back to a restyled "clam" when missing),
recolours classic widgets (Listbox/Text/Scrollbar/Canvas...) via option defaults plus a widget-tree
walk that runs **twice** (Sun Valley's `tk_setPalette` repaints classic widgets during the idle
pass, so the second walk has the last word), and sets a dark title bar via DwmSetWindowAttribute.
Widgets that keep their own colours set `keep_colors = True` (GO button, show display, meter and
waveform canvases). Fonts (`setup_fonts`/`font()`): Bahnschrift for display, Segoe UI Variable
for body, Cascadia Mono for plans, Segoe Fluent Icons for transport glyphs (`glyph()` with text
fallbacks). Extra styles: Title/Muted/Accent/Mono.TLabel, Glyph/Big.TButton; Sun Valley's
Accent.TButton and Switch.TCheckbutton are used for primary buttons and toggles. Player deck:
`Meter` (segmented, peak hold) and `Waveform` (overview from `decode.envelope`, trimmed ends
dimmed, crossfade region, play head) canvases; cue rows carry ▶ … ▷ markers and an ending column.
Builder: plan / playlist text panes are coloured by `_set_pane` (head/ok/warn tags), tables have
zebra rows, primary buttons use Accent. Screenshots are taken with the scratch `shoot.py` (Pillow,
dev venv only).

## Converting audio

`housekit convert <files/folders>... --to mp3-320|mp3-256|mp3-192|mp3-128|m4a-256|flac|wav|ogg
[--out DIR] [--match-loudness -16] [--flat] [--overwrite]` (Convert tab in the window). **In place
by default** (user, 2026-09-13: "dont make a converted folder in the program folder, just overwrite
the files in the playlist folder"): the gig/playlist folder's files are replaced by the converted
ones, originals removed after a successful encode, its `.m3u8` playlists rewritten, files already
in the target format skipped. `--out DIR` makes a separate copy instead (mirrors layout unless
`--flat`). Keeps tags/artwork, downsamples >48 kHz for lossy formats; loudness matching uses the
catalog's measured LUFS as a plain gain where known, loudnorm otherwise. In-place conversion of
anything inside a library root is refused. Typical ask: "make tonight's gig MP3" -> convert the
gig folder in place. (Test 2 has an old `converted\` folder from before this change; it is the
user's earlier output, leave it unless they say to delete it.)

The window's artist, title/album and search boxes suggest catalog values as you type
(`AutocompleteEntry` in gui.py; lists come from `Catalog.distinct_values()`).

## Other things the user may ask for

- **New music added** → `housekit index` (incremental, touches only new/changed files; a full
  re-analysis takes ~15 min and only happens if `analysis.version` in the config is bumped).
- **"That track is explicit / junk / wrong genre"** → `housekit search "words"` to get the id, then
  `housekit flag <id> explicit=yes` / `junk=yes` / `genre="Country"`. Corrections survive re-indexing.
- **"What did we play last time"** → `housekit history`. `--forget <id>` drops a gig from history.
- **Spreadsheet of the library** → `housekit export library.csv`.
- **Tests** → `.\.venv\Scripts\python -m pytest` (from `housekit\`).

## Moving the music or the app

The catalog stores absolute paths, but it follows the music: on every open (`jobs.open_catalog`)
stored roots that no longer exist are looked for as a configured root with the same folder name,
or as that folder moved inside a configured root (FLAC and MP3 moved into Music). Paths are
rewritten, rows that collide with ones a later reindex created are merged (corrections and history
kept), and anything unfindable is marked missing so it is never picked. If files are still
"SKIPPED" when copying, the window says so; the answer is Setup tab (check roots) then Reindex.
To update a portable folder's program files without touching its data:
`.\build_portable.ps1 -Out "<that folder>"` (the app there must be closed). The script also
copies any chart CSV the folder lacks into its `charts\` (never overwrites one it has).

**A different library** (the user moves the app to another machine / music collection): Setup tab
→ new folders → Save → "Clear catalog..." → Reindex. CLI: `housekit clear-catalog --yes`
(`--charts` also drops the chart data; by default it is kept, as are setlist files and pins).

## Current state (2026-09-13)

- The user's live copy is `C:\Users\Dell\Desktop\Test 2\HouseKit-Portable\` with the music merged
  into `Test 2\Music\` (FLAC + MP3 inside it) and root `../Music`; `FLAC Vinyl` is still under
  `PlayList Project` and its rows stay present there. The dev catalog under `housekit\` is stale
  (FLAC/MP3 no longer exist in `PlayList Project`); treat Test 2 as the source of truth.
  Late 2026-09-13 all five chart sources were loaded there: 73,239 chart songs + 38,236 albums;
  9,939 of 17,088 tracks have known popularity (4,634 with a Hot 100 run, 3,367 top-40; sources:
  billboard200 ~5.1k, hot100 ~3.0k, radio ~1.3k, yearend ~0.5k), and 4,960 tracks carry a year
  different from their tag (309 of them from the album chart). The same CSVs sit in both portable
  folders' `charts\`, so a copy of either works offline; `charts load` re-imports them.

- The real SET 1-3 sheets are in `housekit\setlists\`, all lines pinned. `gigs\2026-09-13 Gig\`
  is the current build (53 tracks, doubled slot lengths, chart data applied) and is logged in
  history. `gigs\Next Gig\` was an earlier unlogged preview and `gigs\2026-09-12 Zoobilee\` a demo
  from the sample sheet; both are safe to delete.

## Things to know

- Clean mode is strict: most files have no advisory tag and count as *unknown*, which `--clean`
  excludes, except pre-1985 non-hip-hop tracks (`build.clean_trust_before_year` in the config).
  A clean build will lean heavily on older material; that is expected. Widen the pool by flagging
  known-clean tracks or raising that year.
- Setlist songs are excluded by **title alone**, every version and artist. That is deliberate.
- **Song identity** (user complaint 2026-09-13, "still some duplicates"): `identity.song_groups`
  groups same-title tracks whose artists are the same act (", The" suffix, comma vs "&" credits,
  misspellings via rapidfuzz, lead-artist / any-credited-act overlap, one name inside the other,
  placeholder artists like "Various Interprets" attached to the biggest real group). **Wrong artist
  tags** (user, 2026-09-20, "Chris Martin - Viva La Vida" next to "Coldplay - Viva La Vida": "assume
  the same song name is the same song if the tag has the artist incorrect"): two acts on one title
  are one song when their copies are plainly the same recording (`same_recording`: length within
  4 s, tempo within 3 % allowing half/double, energy within 0.08; rows carry duration, tempo and
  energy as fields 5-7, the catalog joins `features` for them). The charts only guard:
  `Catalog.chart_title_acts()` (title -> charting acts, passed everywhere `known_artists` is)
  keeps two acts both credited with the title apart whatever their lengths, and joins two library
  spellings of one charting act. A first cut that folded any non-charting act into the one
  charting act was wrong (339 titles: "Talk" Coldplay / Jason Aldean, four different "Home"s...);
  the same-recording rule merges 36 titles in the live catalog, all real (Jack White / The White
  Stripes / "Elephant" album-as-artist tags, MC 5 / MC5, Michael McDonald's Doobies songs,
  workout-compilation covers). Different songs sharing a title ("Angel", "Home", the "Crazy"s)
  stay apart. Re-check with `housekit\tools\merge_check.py [library.db]` after touching the rule. Applied live
  at the start of every build and playlist (`apply_song_identity` rewrites `Track.song_key` in
  memory) and persisted by `rebuild_dupe_groups` (`dupe_group`), which era inference's `copy`
  tier also uses. **No act back to back** in playlists, whatever the order (same user message):
  rank / energy / year go through `space_in_order` (the nearest non-clashing song is pulled
  forward, else an earlier gap; the order is otherwise kept), random through `spread_artists`;
  `spacing_key` exempts acts the user asked for by name so an artist list stays in rank order.
  Gig slots already allow one song per artist per slot (`artist_cap_slot`). `norm_artist` (NORM_VERSION 3) now also drops connector words and handles
  "X, The". Chart artist keys are stored normalized, so after a normalization change the files
  are re-imported automatically (`charts_norm_version` meta; the indexer and the window's start-up
  check both do it).
- The library must never be a `--out` target; the tool refuses, don't work around it.
- Config is `housekit\housekit.toml` (slots, weights, caps, tolerance). Genre and content-flag rules
  are in `housekit\housekit\data\*.toml`; run `housekit index --rescan` after editing those.
- The user's original spec (MCP server, web console, live show-mode player) was cut to this CLI on
  purpose. Don't rebuild those unless asked.
