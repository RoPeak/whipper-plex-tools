# whipper-music-tools

Small Linux tools for ripping CDs with Whipper and keeping a Plex, Jellyfin, or other music-server library tidy.

The main tool is `whipper-music-wizard`, an interactive Bash wizard for:

- ripping CDs to FLAC with MusicBrainz metadata, cover art, and AccurateRip verification
- importing existing digital music files without transcoding them
- publishing music into a media-server-friendly directory layout

The old `bin/whipper-plex-wizard` command is still available as a compatibility wrapper.

## Current Tools

### `bin/whipper-music-wizard`

Interactive CD ripping and digital music import wizard for Ubuntu.

It helps with:

- dependency checks for Whipper, FLAC tools, and digital-import metadata tools
- CD drive selection
- drive cache analysis
- safe AccurateRip offset detection for Whipper 0.10.0
- numbered MusicBrainz release selection for CDs
- keep-going ripping so one failed track does not discard the rest of the album
- damaged disc mode for quick salvage attempts with fewer retries per track
- merged recovery when normal and damaged attempts salvage different tracks
- additive repair of existing partial albums without overwriting existing tracks
- copy-first imports from existing MP3, M4A/ALAC/AAC, OGG, OPUS, and FLAC directories
- local tag reading, filename/folder inference, MusicBrainz search, and user confirmation for digital imports
- media-server/exFAT-safe path sanitising before publishing
- embedded and saved cover art where supported
- media-server-style output:

```text
Music/Artist/Album (Year)/01 - Track Title.flac
Music/Artist/Album (Year)/01 - Track Title.mp3
Music/Artist/Album (Year)/01 - Track Title.m4a
```

Optional multi-disc layout:

```text
Music/Artist/Album (Year)/CD1/01 - Track Title.flac
```

CD verification artifacts are kept under each album's hidden `.whipper/` folder. Digital import artifacts are kept under `.library-import/`.

## Requirements

Tested on Ubuntu with:

- `whipper 0.10.0`
- `flac`
- `metaflac`
- `ffmpeg` / `ffprobe`
- `python3-pil`

Install dependencies:

```bash
sudo apt update
sudo apt install -y whipper flac ffmpeg python3-pil
```

## Usage

Run the wizard:

```bash
./bin/whipper-music-wizard
```

Existing scripts can continue to use:

```bash
./bin/whipper-plex-wizard
```

First-time setup for a CD drive:

1. Pick the CD drive.
2. Run drive cache analysis.
3. Run safe offset detection.
4. Rip CDs.

You do not need to repeat drive setup every time. Whipper saves the drive offset and cache behavior in its own config.

For existing digital music, choose `Import existing digital music directory`, point the wizard at the source folder, review the inferred albums/tracks, make any corrections, and accept the import. The wizard copies into a temporary staging directory first and leaves the original source files untouched.

MP3 and M4A files are preserved as MP3 and M4A. Converting lossy files to FLAC is technically possible, but it does not restore quality and usually only makes the files larger.

## Safety Notes

The wizard stages each rip or digital import in `/tmp` first. Before publishing, it renames staged paths to avoid characters that commonly break exFAT/Windows-compatible drives, such as `:`, `?`, `*`, `"`, `<`, `>`, `\`, and `|`. After that, it copies the staged files into your music library only if doing so will not overwrite existing files.

If one or more CD tracks fail but other tracks were ripped, the wizard publishes a clearly labelled partial album, prints a completed/failed summary, and writes `.whipper/PARTIAL_RIP.txt`. This preserves useful work while making it obvious that the album still needs attention. Every rip or repair ends with a terminal session summary covering elapsed time, mode, completed tracks, failed or suspect tracks, AccurateRip counts, published files, conflicts, and any kept staging path.

Digital imports use `ffprobe` readability, embedded tags, filename/folder inference, MusicBrainz search, and your confirmation as their verification flow. They do not have AccurateRip verification because they are not being read from the original CD.

Damaged disc mode is available from the main menu. It keeps the same Whipper metadata, release selection, staging, cover-art, and publishing flow, but uses fewer retries per track so an obviously bad disc does not stall for ages. If a normal rip gives up on a track and readable FLACs were staged, the wizard can offer one automatic damaged-mode retry before publishing the partial album. Normal and damaged attempts are kept until publishing, then merged so the best available track from either attempt is used.

If publishing finds an existing album marked with `.whipper/PARTIAL_RIP.txt`, the wizard treats it as a repair candidate. It adds missing FLACs and preserves new `.whipper` artefacts with attempt-specific names, but it never overwrites existing tracks automatically. If the existing album is not marked partial, publishing stops and the staged output is kept for manual review.

It does not delete user music files. Temporary Whipper work directories are removed after successful or failed runs. Digital import source directories are not modified.

Avoid running the wizard with `sudo`; that can create root-owned config and music files. If your user cannot read the CD device, add yourself to the `cdrom` group and log out/in:

```bash
sudo usermod -aG cdrom "$USER"
```

## Troubleshooting

### Whipper offers the wrong MusicBrainz release

If MusicBrainz lists several releases, do not blindly accept the suggested release. Prefer the release that matches your actual CD country, barcode, catalog number, and edition. Bootlegs and large box sets can appear in the match list.

The wizard shows numbered choices and then passes the selected MusicBrainz release ID to Whipper. You should not need to type the long UUID manually. If MusicBrainz returns exactly one release, the wizard selects it automatically and shows the chosen release before ripping.

### Digital import metadata looks wrong

Use the import review to edit album or track metadata before accepting. The wizard reads embedded tags first, falls back to folder and filename patterns, and then searches MusicBrainz for likely release information. Messy downloads, bootlegs, singles folders, and unofficial compilations may still need human correction.

### Cover art fetch crashes before ripping

Some MusicBrainz/Cover Art Archive entries can trigger Whipper 0.10.0 network errors, including redirect loops. If that happens, the wizard offers to retry without Whipper cover-art fetching. The audio rip can still be AccurateRip-verified; artwork can be repaired later.

### `eject -t` warning

Some external drives do not support Whipper's tray-close command. This warning is usually harmless if ripping continues.

### `cdparanoia couldn't read any frames`

If Whipper retries a track several times and then gives up, it may print a `ZeroDivisionError` traceback. It can also crash while writing its final log with a `NoneType` division error after unreadable tracks. The wizard does not hide this Whipper output, but it treats these as known damaged-disc failure modes and keeps going where possible. If any FLACs were successfully created, it can retry once in damaged disc mode, merge the best tracks from each attempt, or publish/repair a clearly labelled partial album.

### Very high Q sub-channel CRC errors

Some damaged discs report very high Q sub-channel CRC error counts before audio ripping begins. If a track reports at least 1000 such errors, or the running total reaches 5000, the wizard warns and asks whether to stop the normal attempt and restart in damaged disc mode.

## Roadmap

Good next tools for this repo:

- `whipper-drive-report`: summarize Whipper version, drive offset, cache behavior, and permissions
- `music-cover-repair`: copy hidden rip/import cover art to album-level `cover.jpg` and embed missing FLAC pictures
- `music-flac-verify`: run `flac -t` across a library
- `whipper-rip-log-summary`: summarize AccurateRip results from `.whipper/*.log`
- `music-library-audit`: report missing artwork, missing MusicBrainz tags, duplicate track numbers, and suspicious album folders
