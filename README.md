# whipper-plex-tools

Small Linux tools for ripping CDs with Whipper and keeping a Plex music library tidy.

The main tool is `whipper-plex-wizard`, an interactive Bash wrapper around Whipper for ripping CDs to FLAC with MusicBrainz metadata, cover art, AccurateRip verification, and a Plex-friendly directory layout.

## Current Tools

### `bin/whipper-plex-wizard`

Interactive CD ripping wizard for Ubuntu.

It helps with:

- dependency checks for Whipper and FLAC tools
- CD drive selection
- drive cache analysis
- safe AccurateRip offset detection for Whipper 0.10.0
- numbered MusicBrainz release selection
- keep-going ripping so one failed track does not discard the rest of the album
- damaged disc mode for quick salvage attempts with fewer retries per track
- merged recovery when normal and damaged attempts salvage different tracks
- additive repair of existing partial albums without overwriting existing tracks
- Plex/exFAT-safe path sanitising before publishing
- FLAC ripping with MusicBrainz metadata
- embedded and saved cover art
- Plex-style output:

```text
Music/Artist/Album (Year)/01 - Track Title.flac
```

Optional multi-disc layout:

```text
Music/Artist/Album (Year)/CD1/01 - Track Title.flac
```

Verification artifacts are kept under each album's hidden `.whipper/` folder.

## Requirements

Tested on Ubuntu with:

- `whipper 0.10.0`
- `flac`
- `metaflac`
- `python3-pil`

Install dependencies:

```bash
sudo apt update
sudo apt install -y whipper flac python3-pil
```

## Usage

Run the wizard:

```bash
./bin/whipper-plex-wizard
```

First-time setup for a drive:

1. Pick the CD drive.
2. Run drive cache analysis.
3. Run safe offset detection.
4. Rip CDs.

You do not need to repeat drive setup every time. Whipper saves the drive offset and cache behavior in its own config.

## Safety Notes

The wizard stages each rip in `/tmp` first. Before publishing, it renames staged paths to avoid characters that commonly break exFAT/Windows-compatible drives, such as `:`, `?`, `*`, `"`, `<`, `>`, `\`, and `|`. After that, it copies the staged files into your music library only if doing so will not overwrite existing files.

If one or more tracks fail but other tracks were ripped, the wizard publishes a clearly labelled partial album, prints a completed/failed summary, and writes `.whipper/PARTIAL_RIP.txt`. This preserves useful work while making it obvious that the album still needs attention. Every rip or repair ends with a terminal session summary covering elapsed time, mode, completed tracks, failed or suspect tracks, AccurateRip counts, published files, conflicts, and any kept staging path.

Damaged disc mode is available from the main menu. It keeps the same Whipper metadata, release selection, staging, cover-art, and publishing flow, but uses fewer retries per track so an obviously bad disc does not stall for ages. If a normal rip gives up on a track and readable FLACs were staged, the wizard can offer one automatic damaged-mode retry before publishing the partial album. Normal and damaged attempts are kept until publishing, then merged so the best available track from either attempt is used.

If publishing finds an existing album marked with `.whipper/PARTIAL_RIP.txt`, the wizard treats it as a repair candidate. It adds missing FLACs and preserves new `.whipper` artefacts with attempt-specific names, but it never overwrites existing tracks automatically. If the existing album is not marked partial, publishing stops and the staged rip is kept for manual review.

It does not delete user music files. Temporary Whipper work directories are removed after successful or failed runs.

Avoid running the wizard with `sudo`; that can create root-owned config and music files. If your user cannot read the CD device, add yourself to the `cdrom` group and log out/in:

```bash
sudo usermod -aG cdrom "$USER"
```

## Troubleshooting

### Whipper offers the wrong MusicBrainz release

If MusicBrainz lists several releases, do not blindly accept the suggested release. Prefer the release that matches your actual CD country, barcode, catalog number, and edition. Bootlegs and large box sets can appear in the match list.

The wizard shows numbered choices and then passes the selected MusicBrainz release ID to Whipper. You should not need to type the long UUID manually. If MusicBrainz returns exactly one release, the wizard selects it automatically and shows the chosen release before ripping.

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
- `plex-cover-repair`: copy hidden Whipper cover art to album-level `cover.jpg` and embed missing FLAC pictures
- `plex-flac-verify`: run `flac -t` across a library
- `whipper-rip-log-summary`: summarize AccurateRip results from `.whipper/*.log`
- `plex-music-audit`: report missing artwork, missing MusicBrainz tags, duplicate track numbers, and suspicious album folders
