# housekit

Builds the **venue house music** for a live gig: the playback that runs over the PA before doors,
during set breaks and after the show. It indexes your local music library, reads the band's setlist,
picks tracks for each time slot, copies them into per-slot folders and writes the playlist files.

What it guarantees:

- **Nothing the band is about to play** gets picked. Every setlist title is excluded, in every version
  and by every artist, including lines the tool couldn't match (those are excluded by text and flagged
  for you to check).
- **Slots land on time.** Each slot is packed to within 60 seconds of its target using whole tracks only.
  If a slot can't be filled, the plan says so and says why.
- **Same lane as the band.** Tracks are scored on era, genre and energy against a profile derived from
  the setlist, so a 70s classic-rock set doesn't get EDM before doors.
- **One artist per slot, two per night, one album per slot.** Never the same song twice.
- **The library is read-only.** Files are copied, never moved, renamed or retagged.

## Portable app (no install)

`build_portable.ps1` produces a self-contained folder, `HouseKit-Portable`, with a copy of the
python.org interpreter, ffmpeg and the tool inside. Drop that folder next to your music (or inside
the music folder), double-click `HouseKit.lnk` (or `HouseKit.cmd`), and on first run choose the
music folders on the Setup tab (they are guessed from what's around the app). Then Reindex on the
Library tab, and the Gig and Playlist tabs do the rest. `housekit-cli.cmd` in the same folder is the
command line. Everything the app knows (catalog, config, chart data, setlist files and pins) lives in
that folder and travels with it; library paths are stored relative to it, so a different drive letter
is fine. The chart data refreshes itself in the background when the app opens and the data is more
than a week old (`[charts] auto_update_days`); builds never need the network. `housekit-cli.cmd
doctor` reports what the app can see.

Why an interpreter rather than a single `.exe`: Windows **Smart App Control** blocks unsigned
executables it has never seen, which is exactly what a freshly built PyInstaller exe is. The
python.org `python.exe`/`pythonw.exe` are signed and trusted, and the tool itself is plain source,
so this layout runs with Smart App Control on. (`-Layout exe` still builds the single-exe variant
for machines without it.)

```powershell
.\build_portable.ps1 -IncludeState     # bundle this install's catalog, config, charts, setlists too
```

**Moving things.** If the music folders move (the bundle copied to another drive, or `FLAC` and
`MP3` folded into a `Music` folder), the catalog follows them on the next start: it finds the
folders by name, including a folder moved inside a new root, and merges any duplicate rows a
reindex made in the meantime, keeping analysis, hand corrections and play history. Anything it
can't find is marked missing and never picked. If a build still reports skipped files, check the
roots on the Setup tab and press Reindex. To update a portable folder's program files without
touching its data, run `.\build_portable.ps1 -Out "<that folder>"` with the app closed.

**A different library altogether.** Point the Setup tab at the new folders, Save, then
"Clear catalog..." (same tab) and Reindex. Clearing forgets every indexed track, its analysis and
the play history; the chart data, setlist files and pins stay. Command line:
`housekit clear-catalog --yes` (`--charts` to drop the chart data too).

The Windows build must be made on Windows and a Mac build on a Mac (PyInstaller packages the
machine it runs on). The bundled ffmpeg is the Gyan.FFmpeg GPL build.

## Install (from source)

Requires Python 3.11+ and **ffmpeg** on the PATH (used to decode audio for analysis; every format
including WMA goes through it).

Windows (PowerShell):

```powershell
winget install --id Python.Python.3.12 -e
winget install --id Gyan.FFmpeg -e
cd housekit
python -m venv .venv
.\.venv\Scripts\pip install -e ".[dev]"
.\.venv\Scripts\housekit --help
```

macOS:

```bash
brew install python@3.12 ffmpeg
cd housekit
python3 -m venv .venv
./.venv/bin/pip install -e ".[dev]"
./.venv/bin/housekit --help
```

Edit `housekit.toml` (next to this file) so `library.roots` points at your music folders. Every
path in the file is relative to the file itself.

## Workflow

```bash
# 1. Catalog the library. Incremental: re-run any time; only new/changed files are touched.
housekit index
housekit index --limit 200        # quick look at a 200-file sample first
housekit status                   # counts, analysis coverage, flag totals
housekit export library.csv       # the whole catalog as a spreadsheet

# 2. Check the setlist matches. Unresolved lines are listed with candidates.
housekit setlist parse "setlists/2026-09-12 Zoobilee.pdf"
housekit setlist resolve "3. Wagon Wheel - Darius Rucker" --track 4821
housekit setlist resolve "Our Original Song - Us" --not-in-library

# 3. Preview, then build.
housekit build --setlist "setlists/2026-09-12 Zoobilee.pdf" --out "2026-09-12 Zoobilee" --venue "Sedgwick County Zoo" --clean --dry-run
housekit build --setlist "setlists/2026-09-12 Zoobilee.pdf" --out "2026-09-12 Zoobilee" --venue "Sedgwick County Zoo" --clean

# 4. Fix a mis-flagged track (survives re-indexing).
housekit search "cee lo"
housekit flag 1234 explicit=yes
housekit flag 1234 junk=no genre="R&B/Soul"

housekit history                  # gigs on record; the last 3 are excluded from new builds
```

`--json` on any command emits the result as JSON on stdout with no other output, so scripts and
agents can drive it.

**The standing setlist.** `housekit.toml` points `[setlist] path` at the `setlists/` folder. Whatever
set files are in there are the setlist for every build until you replace them, so a gig is just:

```bash
housekit build --out "2026-09-20 The Bar"
```

Setlists can be `.docx`, `.pdf` or `.txt`, and the setlist path can be a **folder** holding one
file per set (`SET 1.docx`, `SET 2.docx` ...); the files are read in order and named as sets. Stage-sheet
shorthand is understood: all caps, title-only lines, `*` vocalist markers, a key before the title
("D - THE MIDDLE"), text-speak ("HATE MYSELF 4 LOVIN U", "7 NATION ARMY") and abbreviations
("SWEET CHILD", "POUR SOME SUGAR"). A medley line ("HANDS 2 YERSF-SLOW RIDE") can be pinned to
several songs with repeated `--track` flags. A sample sheet is in `setlists/samples/`.

`housekit.cmd` in the folder above lets you type `housekit ...` from anywhere in the project.

**Clean mode on an untagged library is strict.** Most files carry no advisory tag, so they are
*unknown*, and `--clean` excludes unknown. The pre-1985 trust rule (`build.clean_trust_before_year`)
keeps classic rock in play; raise the year, add artists/titles to the known-explicit list, or use
`housekit flag <id> explicit=no` on tracks you know are clean to widen the pool. `--dry-run` prints
the exclusion counts so you can see exactly what the filter cost.

### The gig folder

`gigs/2026-09-12 Zoobilee/` (the `gigs` dir is configurable):

```
01_Doors/        001 - Artist - Title.flac ...  01_Doors.m3u8
02_Break1/       ...                             02_Break1.m3u8
03_Break2/       ...                             03_Break2.m3u8
04_PostShow/     ...                             04_PostShow.m3u8
housemusic.m3u8  the whole night, relative paths
plan.txt         the plan with energy, loudness, gain and scores per track
manifest.json    everything above as structured data
```

`plan.txt` lists each track's measured loudness and the gain (dB) that would bring it to the target
(default -16 LUFS), and flags anything more than 3 LU off the slot median, so you know which
tracks to ride before the gig.

### Build options

| Flag | Meaning |
|---|---|
| `--clean` / `--no-clean` | Family-safe. Drops explicit tracks **and** unknown-rating tracks, except pre-1985 non-hip-hop (see `clean_trust_before_year`). |
| `--doors 45 --break1 20 --break2 20 --post 30` | Slot lengths in minutes for this gig. |
| `--exclude-last N` | Skip songs used at the last N gigs (default 3, `0` to disable). |
| `--allow-holiday` | Allow holiday songs outside December. |
| `--seed N` | Different seed, different (equally valid) plan. |
| `--dry-run` | Print the plan, copy nothing, log nothing. |
| `--overwrite` | Replace the slot folders of an existing gig folder. |
| `--no-log` | Don't record the build in play history. |

## Popularity (chart data)

Local files carry no popularity, so it comes from public chart data that is downloaded once into
the `charts\` folder and used offline from then on:

| source | what it is | how to get it |
|---|---|---|
| `hot100` | Billboard Hot 100, every week since 1958 | `charts fetch` (also refreshed weekly by `charts update`) |
| `hot100-archive` | a second Hot 100 export, 1958-2021, fills gaps | `charts fetch` |
| `billboard200` | Billboard 200 album chart, weekly since 1963: dates albums and lifts their tracks | `charts fetch` |
| `radio` | FiveThirtyEight's classic-rock radio spins (25 stations, ~2,200 songs) | `charts fetch` |
| `yearend` | Billboard **year-end genre charts** scraped from billboard.com: country, rock, mainstream rock, alternative, R&B/hip-hop, dance, adult contemporary, pop airplay | `charts scrape` |

```bash
housekit charts fetch            # download the four files and import them (network, once)
housekit charts scrape           # pull the year-end genre charts, ~500 pages, polite, resumable
housekit charts load             # import whatever files are in charts\ (no network at all)
housekit charts status           # what is loaded and how much of the library it covers
housekit charts import my.csv --source mine   # your own file: title, artist, peak columns
```

Every source is scored on one 0-1 scale (a #1 that stayed half a year is 1.0; a year-end #1 is
0.9, a #100 0.37; radio spins scale with the log of the play count; album chart runs count at 85%),
and a track's **popularity** is the best score across the sources that know it. Matching is by
normalized title plus artist, with a fuzzy artist step for spellings like "ACDC" vs "AC/DC" and
credits like "Featuring Bruno Mars"; a song is never matched on title alone, so a cover or a
different song with the same name doesn't inherit a hit's run. Known songs then outscore unknown
ones in every build (`familiarity` weight in the config), and `--top 40` restricts a build to songs
that reached that Hot 100 position. `search` and `plan.txt` show a Hot 100 run as `#3/22w`, and
otherwise the source that knows the song (`radio`, `album`, `yearend`). Re-running `housekit index`
attaches the data to newly added tracks automatically.

The window loads the chart files that came with the folder on its first start and refreshes the
weekly ones online when they are more than a week old; if there is no network, nothing changes and
nothing complains. "Load chart files (offline)" on the Library tab re-imports the files by hand.

## Custom playlists

The same catalog and ranking build one-off playlists: an artist's top songs, a decade, a genre at
a given energy, or any combination. Songs that charted come first (best peak first), then the
rest by how many copies you own; one copy per song; live recordings and junk left out.

```bash
housekit playlist "Steely Dan Top 20" --artist "Steely Dan" --count 20
housekit playlist "80s Party" --decade 80s --top 10 --energy 0.55-0.8 --count 40
housekit playlist "Sunday Morning" --genre "Yacht Rock" --genre "R&B/Soul" --minutes 90 --order energy
housekit playlist "Country Dinner" --genre "Modern Country" --clean --minutes 60 --no-copy
housekit playlist "Oldies" --decade 50s --decade 60s --years 1995-1999 --count 30
housekit playlist "Phil Collins Night" --artist "Phil Collins" --count 20 --fill
```

A song that never charted is still included; it just ranks after the ones that did (only `--top N`
leaves uncharted songs out). When an artist or `--search` list comes up short because the library
holds only a few of that act's songs, `--fill` ("Fill with similar songs when short" in the window)
tops it up with songs by other artists that sit closest to what matched: the same decades and
genres, similar energy, best known first, no more than two per act, every other filter (era,
genre, energy, family-safe) still applied. Filled songs are marked `(similar)` in the list and
come after the real matches in rank order.

Each lands in `playlists\<name>\` with numbered copies, `<name>.m3u8`, `playlist.txt` and
`manifest.json`. A song appears once however its artist is tagged: "Doobie Brothers, The" and
"The Doobie Brothers", "Jimmy Buffet" and "Jimmy Buffett", "England Dan, John Ford Coley" and
"England Dan & John Ford Coley", "Aerosmith" and "Aerosmith & Run D.M.C.", or a compilation copy
tagged "Various Artists" all count as the same song, and the best copy (studio, named artist,
lossless) is the one used. A wrong artist tag doesn't make a second song either: "Chris Martin -
Viva La Vida" beside "Coldplay - Viva La Vida" is one song, because the two copies are plainly the
same recording (same length, tempo and energy), and the same goes for an album name in the artist
field or a producer credit. Different songs that share a title ("Angel", "Home") run differently
and stay separate, as do two hits the charts credit to different acts, like Taylor Swift's and
Mariah Carey's "Shake It Off". `--no-copy` writes an M3U8 that points at the library files
instead of copying. `--order` can be `rank` (default), `energy`, `year`, `alpha` or `random`. In
any order but `alpha`, no two songs by the same act play back to back when the mix allows it (an
act you asked for by name is exempt, so a Steely Dan list stays in rank order); `--search` filters on
words in the title or album; `--allow-live` keeps live recordings; `--dry-run` just prints the list.
`--energy` takes a word (`mellow`, `laid-back`, `upbeat`, `high energy`, `full-on`) or a 0-1 range;
`--genre` and `--decade` may be repeated, and a song passes the era filter if it falls in **any**
ticked decade **or** the `--years` range. In the window, decades and genres are checkboxes (genres
built from what the scan found, with counts), the custom year range sits next to the decades, and
energy is a dropdown of those same words.

**Look.** Both windows use the Sun Valley (Windows 11 style) theme and follow the Windows app
colour setting (light or dark) by default. The Setup tab's "Look" box and the player's View menu
switch to Dark or Light for good; the choice is saved in `ui.json` next to the app and shared by
both. Type comes from what Windows ships: Bahnschrift for headings and the player's time readouts,
Segoe UI Variable for text, Cascadia Mono for plans, Fluent icons on the transport.

## HouseKit Player

`HouseKit Player.lnk` (or `HouseKit Player.cmd`, or `housekit player <folder>`) is a standalone
cue-list player for the folders HouseKit builds. It needs no catalog and no config: open a gig
folder and each slot becomes a cue list, open a playlist folder for one list, or any folder of
audio. The per-track gain from the build's manifest is applied, so levels sit flat without riding.

- **Transport.** Space is GO (the standby cue starts, crossfading over anything playing), P pauses,
  Esc stops, Shift+Esc is the panic fade, Up/Down move the standby cue, double-click a cue to GO on it.
- **Gapless follow-ons.** The next cue is decoded ahead and scheduled to the sample, so nothing
  gaps. How it comes in depends on how the current track ends: under a fade-out it overlaps by the
  crossfade setting, after a ring-out by a second and a half, after a cold stop it butt-splices with
  a hair of fade against clicks. Leading and trailing silence are trimmed ("Trim silence").
- **Per-cue settings.** Gain, pre-wait, fade in, fade out, in and out points, a fixed crossfade,
  auto-follow on or off. Enter or Apply saves them to `cues.json` in the folder, so a folder copied
  to the show laptop carries its cues; files added to a slot later show up, vanished ones drop out.
- **Pads.** Eight hot keys (1-8) for a walk-on song, a sting or an announcement; right-click a pad
  (or drop a file on it) to assign a file. A pad ducks the music by 12 dB while it plays; pressing
  it again fades it out.
- **Quick List.** With nothing open, drop audio files (or use Add files) and the player starts a
  Quick List for them, kept under the playlists folder, so a handful of songs can be played
  without building anything first. A dropped folder or playlist file opens as a show instead.
- **Editing lists.** Drag cues to reorder them (or Ctrl+Up / Ctrl+Down), Delete removes one from
  the list (the file stays), Add files / Add folder append after the selected cue, and files,
  folders or playlists **dropped from Explorer** land where you drop them: on the cue list at that
  spot, on the list of lists as a new list, on a pad to assign it, or on an empty player to open
  that folder. New, Copy, Rename and Delete under the list of lists manage the lists themselves;
  a list made in the player lives in the folder's `cues.json` like everything else.
- **Loop a cue.** The loop switch on a cue repeats it between its in and out points until GO or
  Stop, for a walk-in bed. **Audition join** plays the last fifteen seconds of the selected cue
  into the next one and stops a few seconds later, so a crossfade can be checked before doors.
- **Output.** Choose the interface in the toolbar (the FiiO, the laptop, whatever is plugged in)
  and set the master fader. The **STEREO / MONO** switch beside the transport (or the M key) folds
  left and right onto both outputs for a single cable into one board channel; it lights amber
  when mono is on. All of it is remembered per folder.
- **Show display (F11).** A black window with the current cue, time remaining and the next cue in
  letters readable from across the stage.
- **The deck.** The bottom strip shows what is playing with a large elapsed / remaining readout, a
  waveform of the track with the trimmed ends dimmed, the crossfade region marked and a moving play
  head, a segmented level meter with peak hold, the master fader and the mono switch. Click or
  drag on the waveform to jump to that spot: the old position fades out while the new one fades
  in over a fraction of a second, so it never clicks or cuts. Left / Right arrows nudge five
  seconds, with Shift thirty. The cue list
  marks the playing (▶), queued (…) and standby (▷) cues and shows each track's ending type
  (fade, ring, cold) once it has been decoded.

The main window's Gig and Playlist tabs have a "Play in HouseKit Player" button that opens the
folder you just built in the player as its own process, so the builder stays free.

## Converting audio

The Convert tab (and `housekit convert`) turns files or whole folders into another format with the
bundled ffmpeg: MP3 at 320/256/192/128 kbps, AAC (.m4a), FLAC, WAV or OGG. It works **in place**:
point it at the gig or playlist folder you just built and that folder ends up holding the MP3s
instead of the FLACs, with every `.m3u8` in it rewritten to match. Files already in the target
format are left alone (tick "Redo" / `--overwrite` to re-encode them). Tags and artwork carry
over. "Match loudness" brings every file to a target LUFS: tracks the catalog has measured get a
plain gain (transparent, peak-safe), anything else goes through ffmpeg's loudnorm. Sample rates
above 48 kHz are brought down for lossy formats. The music library itself is never converted in
place (the tool refuses); give an output folder (`--out` / "Copy to folder") to make a separate
converted copy of anything, library folders included.

```bash
housekit convert "gigs\2026-09-20 The Bar" --to mp3-320 --match-loudness -16   # the gig folder becomes MP3
housekit convert "D:\rips" --to flac --out "E:\stick\flac"                       # a copy elsewhere
```

In the window, the artist, title/album and search fields suggest values from the catalog as you
type (press Down to move into the list, Enter or click to pick).

## How tracks are chosen

1. **Exclusions** (hard): setlist titles and their duplicates, junk (intros, skits, karaoke, anything
   under 90 s), holiday songs outside December, explicit/unknown in clean mode, songs from recent gigs,
   tracks over 7 minutes, missing files.
2. **Scoring** per slot, weights in `housekit.toml`:
   energy fit against the slot's band (0.35), era fit against the setlist's decade mix (0.25), genre fit
   through an editable similarity table (0.25), familiarity (0.15). Local files carry no popularity
   data, so familiarity comes from the library itself: a song you own several copies of (album,
   greatest hits, compilation) is probably a hit, and compilation album titles are a hint. The
   earliest year among a song's copies is its era, so a 2019 "Classic Rock Collection" copy of
   Hotel California still scores as 1976. Live, instrumental, acoustic/demo/remix versions and
   poorly tagged tracks are multiplied down.
3. **Packing**: greedy by score, then a swap pass to land inside the tolerance window.
4. **Ordering**: a gentle energy rise within the slot, no two consecutive tracks of the same genre where
   the pool allows.

### Slots (default, edit in `housekit.toml`)

| Slot | Minutes | Energy band |
|---|---|---|
| `01_Doors` | 45 | 0.25 - 0.50 |
| `02_Break1` | 20 | 0.45 - 0.65 |
| `03_Break2` | 20 | 0.55 - 0.80 |
| `04_PostShow` | 30 | 0.25 - 0.50 |

### What the analysis measures

A 30-second excerpt from the middle of each track is decoded through ffmpeg. From it:

- **Loudness** (integrated LUFS, `pyloudnorm`) and peak.
- **Energy**, 0 to 1, from loudness-invariant cues: onset rate, percussive share, spectral
  flatness (wall-of-sound vs sparse), brightness, spectral flux, tempo and mix density, plus a
  small loudness term. Roughly: 0.1-0.3 ballads and acoustic, 0.4-0.55 mid-tempo, 0.6-0.8
  rockers and dance, 0.8+ metal. Different masters of the same song land within about 0.1.
- **Tempo**, coarse.

Analysis runs in a process pool (CPU count minus one by default) and is cached per file. Re-running
`housekit index` only analyzes new or changed files. Set `analysis.version = 3` in the config to
force re-analysis of everything.

### Years and remasters

Tag years on remasters, reissues, compilations and live albums are the reissue year, which
wrecks era scoring for classic rock. After every scan and chart refresh the catalog works out each
track's original year (`year`, with the raw tag kept as `year_tag` and the reasoning as
`year_source`), in order of strength:

| source | how |
|---|---|
| `chart` | the song's first chart week (or its release year from the radio / year-end data), when it is two or more years before the tag year |
| `album chart` | the year the album itself first entered the Billboard 200, when that is two or more years before the tag year: an unmarked remaster of a non-single album track. Self-titled albums that charted several times are told apart by their chart debuts |
| `tag` | the tag year, when nothing marks the release as a reissue, compilation or live album (a year printed on the album name, like "(1980)", counts in its favour) |
| `copy` | the earliest credible year among your other copies of the same song |
| `album` | the median credible year of the other tracks on the same non-compilation album, so a remastered album inherits the year of the single that charted |
| `artist` | the artist's median credible year, only when their catalog spans 15 years or less |
| `tag?` | the tag year kept for lack of anything better |

`housekit flag <id> year=1977` (or "Set year" on the Library tab) pins a year by hand and wins over
all of the above; `year=auto` clears it.

### Content flags

`explicit` comes from the MP4 advisory atom, `ITUNESADVISORY` tags, `[Explicit]` / `[Clean]` markers
and the artist+title list in `housekit/data/content_rules.toml`. Anything else is **unknown**, and
clean mode treats unknown as unsafe. `junk`, `holiday`, `live` and `instrumental` are pattern
rules in the same file. Genre mapping lives in `housekit/data/genres.toml`. Both files are meant to
be edited; run `housekit index --rescan` afterwards. Hand corrections made with `housekit flag`
always win and survive re-indexing, even if the file moves.

## Tests

```bash
.\.venv\Scripts\python -m pytest
```

Covers setlist exclusion with apostrophe/feat./remaster/swapped-order variants, duration packing
across pool sizes, artist and album caps, clean mode, filename sanitization, history exclusion,
override persistence across re-index, synthetic-audio feature extraction, chart readers and
scoring for every source, the year-end scraper's stand-in detection and resume, offline loading,
era inference, decade filters, clearing the catalog, and CLI round-trips.
