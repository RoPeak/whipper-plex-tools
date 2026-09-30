# Music Ingest (Whipper Music Tools)

`music-ingest` is a user-run workflow for reviewing digital music and ripping audio CDs. The historical repository and `whipper-music-wizard` / `whipper-plex-wizard` commands remain supported.

## Install and configure

Expose the `music-ingest` command on your `PATH`; it works from any directory.
The checkout remains the source of the scripts. A TOML file at
`~/.config/music-ingest/config.toml` holds paths and conservative policy:

```toml
incoming = "/path/to/incoming/music"
library = "/path/to/library/Music"
staging = "/path/to/staging/music-ingest"
default_mode = "dry-run"
publication = "copy"
preserve_source = true
musicbrainz_lookup = true
multidisc_folders = false
cover_mode = "file"
```

Inspect effective values with `music-ingest config`. The command refuses to create a missing production root and checks that source and library roots do not overlap. CLI options such as `--incoming`, `--library`, `--staging`, and digital `--mode` override configuration. Keep staging on the same filesystem as the library so large temporary media does not consume the OS filesystem.

Runtime requirements are Python 3.11+ (standard library only), Bash, `ffprobe` (provided by FFmpeg), `flac`/`metaflac`, and standard GNU/Linux utilities. CD ripping additionally requires Whipper, `cd-paranoia`, `cdrdao`, `eject`, and access to an optical drive. The Ubuntu Whipper package supplies its Python libraries and related dependencies. Run the synthetic suite with `python3 -m unittest discover -s tests`. Artwork lookup uses Python's standard library and network requests only when explicitly requested from the cover-art menu. MusicBrainz lookup is optional and can be disabled in config.

Use normal system package management to install the CD-ripping dependencies where administrator access is available. If a user-local runtime is unavoidable, document its provenance and update process locally: it will not be tracked by the system package manager or receive automatic OS package updates.

## Digital import

Run `music-ingest`, choose digital import, and enter or accept the incoming directory. The workflow checks roots, discovers supported files, reads embedded tags and media properties with `ffprobe`, uses folder/filename evidence as fallback, groups by album artist and album, optionally searches MusicBrainz, and presents discrepancies and a review. MusicBrainz candidates are ranked by local compatibility: artist/title, year, track count, disc count, release type, and explicit edition clues such as Tour, Expanded, Deluxe, Live, Bonus, or Instrumental. Provider search relevance is never presented as a release match score. Structurally poor candidates remain visible through `[C]andidates` but are penalized and are not silently selected. You can edit album/track data, view exact mappings, and skip groups.

The default mode is `dry-run`: it probes sources, validates the destination plan and filesystem assumptions, and writes a report, but copies **0 media bytes** and retains no bulk staging directory. After review, run `music-ingest digital --mode apply`; publication still requires typing `APPLY` at the final plan. Use `--incoming`, `--library`, and `--staging` for one-run path overrides. Originals are preserved. Files retain their source extension and encoded audio: lossy inputs are never converted to FLAC. Tags are not rewritten; approved metadata affects organization and the import manifest only. Existing identical files are skipped, differing conflicts block the import, and no destination is overwritten. Each copied file is staged beside its destination, size checked, then atomically linked into place without clobbering a concurrent destination.

Disc numbers are retained. Multi-disc releases use disc-prefixed track names by default (for example `2-01 - Track.mp3`) to prevent collisions while matching the existing library's flat album convention. The optional `multidisc_folders` setting uses `CD1/` folders instead. Edition, live, bonus, and instrumental identity in album/track tags and titles is retained; inspect poor tags before approval. Local cover art may be copied; remote downloads are a separate explicit action and existing covers are not replaced by default.

Reports include the exact source path, final destination, lifecycle, approval, verification, and completion fields needed by the separate `ingest-cleanup` tool. A dry-run is explicitly cleanup-ineligible. Inspect known tool-owned staging with `music-ingest staging status`; `music-ingest staging cleanup` lists only immediate `music-import-out-*` and `whipper-out-*` directories under the configured staging root and requires typing `CLEANUP`. No automatic source deletion occurs.

## Audio CD workflow

Choose CD ripping from the menu or run `music-ingest cd`. The legacy Whipper wizard handles drive detection/setup, MusicBrainz release selection, secure ripping/AccurateRip where supported, cover handling, damaged-disc recovery, and additive repair. Rips stage before publication; the wizard lists every staged-to-library path and requires typing `APPLY` before publication. Declining retains the staged rip for later review. Conflicts do not overwrite existing tracks. If the drive, Whipper, or media utilities are unavailable, the CD path reports that condition and digital import remains usable. No optical-drive availability is inferred from package installation alone.

## Compatibility and operation

`bin/whipper-music-wizard` and `bin/whipper-plex-wizard` remain available and activate the user-local runtime when it exists. Do not run as root. The project does not watch directories, alter Jellyfin, configure mounts, delete incoming files, or manage backups. Jellyfin sees the production media tree read-only; library discovery/playback is a separate check. Backups cover the production media tree according to the server's backup policy, not incoming files.

Keep deployment-specific paths, staging choices, dependency workarounds, and recovery notes in local operational documentation rather than this public repository.
