#!/usr/bin/env python3
"""Plan and stage digital music imports for Whipper Music Wizard."""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any



SUPPORTED_EXTENSIONS = {".flac", ".mp3", ".m4a", ".alac", ".aac", ".ogg", ".opus"}
COVER_NAMES = {
    "cover.jpg",
    "cover.jpeg",
    "cover.png",
    "folder.jpg",
    "folder.jpeg",
    "folder.png",
}
ARTIFACT_DIR_NAME = ".library-import"
IMPORT_MANIFEST_NAME = "IMPORT_MANIFEST.json"
USER_AGENT = "whipper-music-wizard/1.0 (https://musicbrainz.org/doc/XML_Web_Service/Rate_Limiting)"
STATUS_DELAY = 0.0


def status(message: str) -> None:
    print(message, flush=True)
    if STATUS_DELAY > 0:
        time.sleep(STATUS_DELAY)


@dataclass
class Track:
    source: Path
    rel_source: str
    extension: str
    title: str = ""
    artist: str = ""
    album_artist: str = ""
    album: str = ""
    year: str = ""
    track: int = 0
    disc: int = 1
    total_discs: int = 0
    genre: str = ""
    musicbrainz_trackid: str = ""
    musicbrainz_releaseid: str = ""
    has_embedded_art: bool = False
    readable: bool = False
    proposed_rel: str = ""
    folder_artist_hint: str = ""
    folder_album_hint: str = ""
    embedded_artist: str = ""
    embedded_album: str = ""
    codec: str = ""
    bitrate: int = 0
    sample_rate: int = 0
    compilation: bool = False


@dataclass
class AlbumGroup:
    key: str
    tracks: list[Track] = field(default_factory=list)
    artist: str = ""
    album_artist: str = ""
    album: str = ""
    year: str = ""
    musicbrainz_releaseid: str = ""
    musicbrainz_releasegroupid: str = ""
    match_status: str = "not searched"
    match_confidence: int = 0
    edition_clues: list[str] = field(default_factory=list)
    provider_candidates: list[dict[str, Any]] = field(default_factory=list)
    skip: bool = False
    artwork: dict[str, str] = field(default_factory=dict)


def sanitize_component(name: str) -> str:
    safe = name
    for old, new in {
        ":": " - ",
        "?": "",
        "*": "",
        '"': "",
        "<": "(",
        ">": ")",
        "\\": " - ",
        "/": " - ",
        "|": " -",
        "\r": " ",
        "\n": " ",
        "\t": " ",
    }.items():
        safe = safe.replace(old, new)
    safe = re.sub(r" {2,}", " ", safe).strip().rstrip(".")
    if not safe:
        safe = "_"
    stem = safe.split(".", 1)[0].upper()
    if stem in {"CON", "PRN", "AUX", "NUL"} or re.fullmatch(r"(COM|LPT)[1-9]", stem):
        safe = "_" + safe
    return safe


def first_tag(tags: dict[str, Any], *names: str) -> str:
    lowered = {str(k).lower(): v for k, v in tags.items()}
    for name in names:
        value = lowered.get(name.lower())
        if isinstance(value, list):
            value = value[0] if value else ""
        if value is not None and str(value).strip():
            return str(value).strip()
    return ""


def parse_number(value: str) -> int:
    if not value:
        return 0
    match = re.search(r"\d+", value)
    return int(match.group(0)) if match else 0


def parse_year(value: str) -> str:
    match = re.search(r"(?:19|20)\d{2}", value or "")
    return match.group(0) if match else ""


def split_filename(path: Path) -> tuple[int, int, str]:
    stem = path.stem.strip()
    patterns = [
        r"^(?P<disc>\d+)-(?P<track>\d+)\s*[-. ]\s*(?P<title>.+)$",
        r"^(?P<track>\d{1,3})\s*[-. ]\s*(?P<title>.+)$",
        r"^.+?\s+-\s+(?P<track>\d{1,3})\s+-\s+(?P<title>.+)$",
    ]
    for pattern in patterns:
        match = re.match(pattern, stem)
        if match:
            disc = int(match.groupdict().get("disc") or 1)
            return disc, int(match.group("track")), match.group("title").strip()
    return 1, 0, stem


def ffprobe_json(path: Path) -> dict[str, Any]:
    result = subprocess.run(
        [
            "ffprobe",
            "-v",
            "quiet",
            "-print_format",
            "json",
            "-show_format",
            "-show_streams",
            str(path),
        ],
        check=False,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    if result.returncode != 0:
        return {}
    try:
        return json.loads(result.stdout or "{}")
    except json.JSONDecodeError:
        return {}


def merged_audio_tags(probe: dict[str, Any]) -> dict[str, Any]:
    """Return music metadata without letting attached picture streams overwrite it"""
    tags = dict(probe.get("format", {}).get("tags", {}) or {})
    known = {str(key).casefold() for key in tags}
    for stream in probe.get("streams", []) or []:
        if stream.get("codec_type") != "audio":
            continue
        for key, value in (stream.get("tags", {}) or {}).items():
            normalised = str(key).casefold()
            if normalised not in known:
                tags[key] = value
                known.add(normalised)
    return tags

def disc_folder_number(path: Path, root: Path) -> int:
    if path.parent == root:
        return 0
    match = re.search(
        r"(?:^|[\s\[(\-_])(?:CD|Disc)\s*[-_ ]?0*(\d+)(?=$|[\s\])_\-])",
        path.parent.name,
        flags=re.IGNORECASE,
    )
    return int(match.group(1)) if match else 0


def album_container_name(source: Path, root: Path, artist: str, year: str) -> str:
    """Infer the shared album name above an explicitly numbered disc folder."""
    container = source.parent.parent
    if container != root:
        try:
            container.relative_to(root)
        except ValueError:
            return ""

    name = container.name.strip()
    if artist:
        name = re.sub(
            rf"^{re.escape(artist)}\s*[-–—]\s*",
            "",
            name,
            count=1,
            flags=re.IGNORECASE,
        ).strip()

    year_match = re.search(r"\s*[\[(]\s*(?:19|20)\d{2}\s*[\])]", name)
    if year_match:
        name = name[: year_match.start()].strip()
    elif year:
        year_match = re.search(rf"\s*[\[(]\s*{re.escape(year)}\s*[\])]", name)
        if year_match:
            name = name[: year_match.start()].strip()
    return name


def normalise_disc_album_name(album: str, disc: int, parent_disc: int, container_album: str = ""):
    if not album or not parent_disc or disc != parent_disc:
        return album
    patterns = [
        r"^(?P<album>.+?)\s+\((?P<disc>\d+)\)$",
        r"^(?P<album>.+?)\s+\((?:CD|Disc)\s*(?P<disc>\d+)\)$",
        r"^(?P<album>.+?)\s*[-–—]\s*(?:CD|Disc)\s*(?P<disc>\d+)$",
    ]
    for pattern in patterns:
        match = re.match(pattern, album, flags=re.IGNORECASE)
        if match and int(match.group("disc")) == parent_disc:
            return match.group("album").strip()

    explicit_disc = re.search(
        r"(?:^|[\s\[(\-_])(?:CD|Disc)\s*[-_ ]?0*(\d+)(?=$|[\s\])_\-])",
        album,
        flags=re.IGNORECASE,
    )
    if explicit_disc and int(explicit_disc.group(1)) == parent_disc and container_album:
        return container_album
    return album


def track_from_probe(source: Path, root: Path, probe: dict[str, Any]) -> Track:
    tags = merged_audio_tags(probe)
    disc_from_name, track_from_name, title_from_name = split_filename(source)
    parent_disc = disc_folder_number(source, root)
    rel_parts = source.relative_to(root).parts
    parent_album = rel_parts[-3] if parent_disc and len(rel_parts) >= 3 else (rel_parts[-2] if len(rel_parts) >= 2 else "")
    parent_artist = (rel_parts[-4] if len(rel_parts) >= 4 else root.name) if parent_disc else (rel_parts[-3] if len(rel_parts) >= 3 else (root.name if len(rel_parts) >= 2 else ""))
    artist = first_tag(tags, "artist", "album_artist", "albumartist") or parent_artist
    year = parse_year(first_tag(tags, "date", "year"))
    disc_from_tag = parse_number(first_tag(tags, "disc", "discnumber"))
    disc = parent_disc or disc_from_tag or disc_from_name or 1
    album = first_tag(tags, "album") or parent_album
    container_album = album_container_name(source, root, artist, year) if parent_disc else ""
    album = normalise_disc_album_name(album, disc, parent_disc, container_album)

    audio_stream = next((stream for stream in (probe.get("streams", []) or []) if stream.get("codec_type") == "audio"), {})
    try:
        bitrate = int(probe.get("format", {}).get("bit_rate") or audio_stream.get("bit_rate") or 0)
    except (TypeError, ValueError):
        bitrate = 0
    try:
        sample_rate = int(audio_stream.get("sample_rate") or 0)
    except (TypeError, ValueError):
        sample_rate = 0
    compilation_value = first_tag(tags, "compilation", "cpil").casefold()
    track = Track(
        source=source,
        rel_source=str(source.relative_to(root)),
        extension=source.suffix.lower(),
        title=first_tag(tags, "title") or title_from_name,
        artist=artist,
        album_artist=first_tag(tags, "album_artist", "albumartist", "album artist"),
        album=album,
        year=year,
        track=parse_number(first_tag(tags, "track", "tracknumber")) or track_from_name,
        disc=disc,
        total_discs=parse_number(first_tag(tags, "disctotal", "totaldiscs")),
        genre=first_tag(tags, "genre"),
        musicbrainz_trackid=first_tag(tags, "musicbrainz_trackid", "musicbrainz/releasetrackid"),
        musicbrainz_releaseid=first_tag(tags, "musicbrainz_albumid", "musicbrainz_releaseid"),
        has_embedded_art=any((s.get("codec_type") == "video" and s.get("disposition", {}).get("attached_pic")) for s in probe.get("streams", []) or []),
        readable=bool(probe),
        folder_artist_hint=(rel_parts[-4] if len(rel_parts) >= 4 else "") if parent_disc else (rel_parts[-3] if len(rel_parts) >= 3 else ""),
        folder_album_hint=container_album or parent_album,
        embedded_artist=first_tag(tags, "album_artist", "albumartist", "album artist", "artist"),
        embedded_album=first_tag(tags, "album"),
        codec=str(audio_stream.get("codec_name") or source.suffix.lower().lstrip(".")),
        bitrate=bitrate,
        sample_rate=sample_rate,
        compilation=compilation_value in {"1", "true", "yes", "compilation"},
    )
    if not track.album_artist:
        track.album_artist = track.artist
    return track


def scan_source(root: Path) -> list[Track]:
    tracks: list[Track] = []
    paths = [path for path in sorted(root.rglob("*")) if path.is_file() and path.suffix.lower() in SUPPORTED_EXTENSIONS]
    status(f"Scanning {len(paths)} supported audio file(s) with ffprobe...")
    for index, path in enumerate(paths, 1):
        if index == 1 or index % 10 == 0 or index == len(paths):
            status(f"  ffprobe metadata: {index}/{len(paths)}")
        tracks.append(track_from_probe(path, root, ffprobe_json(path)))
    return tracks


def group_tracks(tracks: list[Track]) -> list[AlbumGroup]:
    buckets: dict[str, list[Track]] = defaultdict(list)
    for track in tracks:
        key = "\0".join(
            [
                (track.album_artist or track.artist or "Unknown Artist").casefold(),
                (track.album or "Unknown Album").casefold(),
            ]
        )
        buckets[key].append(track)

    groups: list[AlbumGroup] = []
    for key, items in buckets.items():
        group = AlbumGroup(key=key, tracks=sorted(items, key=lambda t: (t.disc or 1, t.track or 9999, t.rel_source)))
        group.artist = most_common([t.artist for t in items]) or "Unknown Artist"
        group.album_artist = most_common([t.album_artist or t.artist for t in items]) or group.artist
        group.album = most_common([t.album for t in items]) or "Unknown Album"
        group.year = most_common([t.year for t in items])
        group.musicbrainz_releaseid = most_common([t.musicbrainz_releaseid for t in items])
        group.edition_clues = edition_clues_for_group(group)
        groups.append(group)
    return sorted(groups, key=lambda g: (g.album_artist.casefold(), g.year, g.album.casefold()))


EDITION_WORDS = ("tour edition", "expanded edition", "deluxe edition", "remaster", "anniversary", "live", "instrumental", "bonus", "demo")


def edition_clues_for_group(group: AlbumGroup) -> list[str]:
    """Return explicit local release clues without changing the embedded album title."""
    haystack = " ".join(
        [group.album]
        + [track.folder_album_hint for track in group.tracks]
        + [track.embedded_album for track in group.tracks]
        + [str(track.source.parent) for track in group.tracks]
    ).casefold()
    return [word for word in EDITION_WORDS if word in haystack]


def most_common(values: list[str]) -> str:
    clean = [v for v in values if v]
    return Counter(clean).most_common(1)[0][0] if clean else ""


def musicbrainz_json(url: str) -> dict[str, Any]:
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
    retry_delays = (2.0, 4.0, 8.0)
    for attempt in range(len(retry_delays) + 1):
        try:
            with urllib.request.urlopen(request, timeout=12) as response:
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            transient = exc.code in {429, 500, 502, 503, 504}
            if not transient or attempt >= len(retry_delays):
                raise
            delay = retry_delays[attempt]
            retry_after = exc.headers.get("Retry-After") if exc.headers else None
            if retry_after:
                try:
                    delay = max(delay, float(retry_after))
                except ValueError:
                    pass
            status(f"    MusicBrainz HTTP {exc.code}; retrying in {delay:g}s...")
            time.sleep(delay)
    raise RuntimeError("unreachable")


def wait_for_musicbrainz(last_request: float) -> float:
    wait = 1.05 - (time.monotonic() - last_request)
    if wait > 0:
        time.sleep(wait)
    return time.monotonic()


def enrich_with_musicbrainz(groups: list[AlbumGroup]) -> None:
    last_request = 0.0
    status(f"Searching MusicBrainz for {len(groups)} album group(s)...")
    for index, group in enumerate(groups, 1):
        if group.musicbrainz_releaseid or group.album in {"", "Unknown Album"}:
            group.match_status = "embedded release id" if group.musicbrainz_releaseid else "not enough metadata"
            status(f"  MusicBrainz {index}/{len(groups)}: {group.album_artist} - {group.album}: {group.match_status}")
            continue
        status(f"  MusicBrainz {index}/{len(groups)}: {group.album_artist} - {group.album}")
        release_id, release_group_id_value, match_status, last_request = find_musicbrainz_release(
            group.album_artist, group.album, group.year, last_request, group=group
        )
        group.match_status = match_status
        if release_id:
            group.musicbrainz_releaseid = release_id
            group.musicbrainz_releasegroupid = release_group_id_value
            group.match_status = f"{match_status}: {group.musicbrainz_releaseid}"
        status(f"    {group.match_status}")


def release_group_id(release: dict[str, Any]) -> str:
    release_group = release.get("release-group") or release.get("release_group") or {}
    if isinstance(release_group, dict):
        return str(release_group.get("id") or "")
    return ""


def release_counts(release: dict[str, Any]) -> tuple[int, int]:
    media = release.get("media") or []
    discs = int(release.get("media-count") or release.get("media_count") or len(media) or 0)
    tracks = int(release.get("track-count") or release.get("track_count") or 0)
    if not tracks:
        tracks = sum(int(item.get("track-count") or item.get("track_count") or 0) for item in media if isinstance(item, dict))
    return discs, tracks


def release_type(release: dict[str, Any]) -> str:
    release_group = release.get("release-group") or release.get("release_group") or {}
    return str(release_group.get("primary-type") or release_group.get("primary_type") or "") if isinstance(release_group, dict) else ""


def release_artist(release: dict[str, Any]) -> str:
    credits = release.get("artist-credit") or release.get("artist_credit") or []
    values = []
    for item in credits:
        if isinstance(item, dict):
            artist = item.get("artist") or {}
            values.append(str(item.get("name") or artist.get("name") or ""))
    return " ".join(values)


def release_compatibility(group: AlbumGroup, release: dict[str, Any]) -> tuple[int, list[str]]:
    """Score local release evidence, deliberately penalising structural mismatches."""
    score, notes = 0, []
    title = normalize_text(str(release.get("title") or ""))
    wanted_title = normalize_text(group.album)
    artist = normalize_text(release_artist(release))
    wanted_artist = normalize_text(group.album_artist)
    if title == wanted_title:
        score += 30
    elif title and wanted_title and (title in wanted_title or wanted_title in title):
        score += 16
        notes.append("title differs")
    else:
        notes.append("title mismatch")
    if artist == wanted_artist:
        score += 20
    elif artist and wanted_artist and (artist in wanted_artist or wanted_artist in artist):
        score += 10
        notes.append("artist differs")
    else:
        notes.append("artist mismatch")
    if group.year:
        if str(release.get("date") or "").startswith(group.year):
            score += 10
        elif release.get("date"):
            score -= 6
            notes.append(f"year {release.get('date')}")
    local_discs = max((track.disc or 1) for track in group.tracks) if group.tracks else 0
    candidate_discs, candidate_tracks = release_counts(release)
    local_tracks = len(group.tracks)
    if candidate_tracks and local_tracks:
        if candidate_tracks == local_tracks:
            score += 22
        else:
            difference = abs(candidate_tracks - local_tracks) / max(candidate_tracks, local_tracks)
            penalty = 12 if difference <= .15 else 24 if difference <= .4 else 38
            score -= penalty
            notes.append(f"{candidate_tracks} tracks vs local {local_tracks}")
    if candidate_discs and local_discs:
        if candidate_discs == local_discs:
            score += 10
        else:
            score -= 18
            notes.append(f"{candidate_discs} discs vs local {local_discs}")
    kind = release_type(release)
    if kind.casefold() == "album":
        score += 8
    elif kind.casefold() == "single" and local_tracks > 2:
        score -= 30
        notes.append("single conflicts with album-sized source")
    elif kind:
        score -= 8
        notes.append(f"type {kind}")
    edition_text = " ".join(str(release.get(name) or "") for name in ("title", "disambiguation", "packaging")).casefold()
    for clue in group.edition_clues:
        if clue in edition_text:
            score += 8
        elif clue in {"tour edition", "expanded edition", "deluxe edition", "live", "instrumental"}:
            score -= 12
            notes.append(f"local clue '{clue}' absent")
    return max(0, min(100, score)), notes


def proposed_album_dir(group: AlbumGroup) -> str:
    album = group.album
    if group.year:
        album = f"{album} ({group.year})"
    return os.path.join(sanitize_component(group.album_artist), sanitize_component(album))


def assign_destinations(groups: list[AlbumGroup], multidisc: bool, include_track_artist: bool) -> None:
    for group in groups:
        used: set[str] = set()
        use_disc_prefix = group_has_multiple_discs(group) and not multidisc
        for index, track in enumerate(group.tracks, 1):
            track.artist = track.artist or group.artist
            track.album_artist = group.album_artist
            track.album = group.album
            track.year = group.year
            if not track.track:
                track.track = index
            parts = [proposed_album_dir(group)]
            if multidisc and (track.disc > 1 or group_has_multiple_discs(group)):
                parts.append(f"CD{track.disc or 1}")
            title = sanitize_component(track.title or f"Track {track.track}")
            track_prefix = f"{track.track:02d}"
            if use_disc_prefix:
                track_prefix = f"{track.disc or 1}-{track.track:02d}"
            if include_track_artist and track.artist:
                filename = f"{track_prefix} - {sanitize_component(track.artist)} - {title}{track.extension}"
            else:
                filename = f"{track_prefix} - {title}{track.extension}"
            rel = os.path.join(*parts, filename)
            rel = uniquify_rel(rel, used)
            used.add(rel)
            track.proposed_rel = rel


def group_has_multiple_discs(group: AlbumGroup) -> bool:
    discs = {t.disc for t in group.tracks if t.disc}
    return len(discs) > 1 or any(t.total_discs > 1 for t in group.tracks)


def uniquify_rel(rel: str, used: set[str]) -> str:
    if rel not in used:
        return rel
    path = Path(rel)
    stem = path.stem
    suffix = path.suffix
    i = 2
    while True:
        candidate = str(path.with_name(f"{stem} ({i}){suffix}"))
        if candidate not in used:
            return candidate
        i += 1


def print_review(groups: list[AlbumGroup], library_root: Path, multidisc: bool, include_track_artist: bool) -> None:
    assign_destinations(groups, multidisc, include_track_artist)
    print()
    print("Digital import review")
    for idx, group in enumerate(groups, 1):
        formats = ", ".join(f"{ext.lstrip('.').upper()} {count}" for ext, count in sorted(Counter(t.extension for t in group.tracks).items()))
        missing = sorted(
            {
                field
                for track in group.tracks
                for field, value in {
                    "title": track.title,
                    "artist": track.artist,
                    "album": track.album,
                    "track": track.track,
                    "readable media": track.readable,
                }.items()
                if not value
            }
        )
        prefix = "SKIP " if group.skip else ""
        print(f"{idx}) {prefix}{group.album_artist}")
        print(f"   Album  : {group.album} ({group.year or 'year unknown'})")
        if group.edition_clues:
            print(f"   Edition: {', '.join(group.edition_clues)}")
        codec_names = ", ".join(sorted({track.codec.upper() for track in group.tracks if track.codec})) or "unknown"
        bitrates = sorted({track.bitrate for track in group.tracks if track.bitrate})
        sample_rates = sorted({track.sample_rate for track in group.tracks if track.sample_rate})
        audio_detail = f"   Format : {codec_names}"
        if bitrates:
            bitrate_range = f"~{bitrates[0] // 1000} kb/s" if len(bitrates) == 1 else f"{bitrates[0] // 1000}-{bitrates[-1] // 1000} kb/s"
            audio_detail += f", {bitrate_range}"
        if sample_rates:
            rates = f"{sample_rates[0] / 1000:g} kHz" if len(sample_rates) == 1 else f"{sample_rates[0] / 1000:g}-{sample_rates[-1] / 1000:g} kHz"
            audio_detail += f", {rates}"
        print(f"   Tracks : {len(group.tracks)}   Discs: {max((t.disc or 1) for t in group.tracks)}   Files: {formats}")
        print(audio_detail)
        if any(track.compilation for track in group.tracks):
            print("   Type   : compilation flag present")
        print(f"   Metadata: {group.match_status}")
        print(f"   Missing: {', '.join(missing) if missing else 'none'}")
        print(f"   Dest   : {library_root / proposed_album_dir(group)}")
        artwork = describe_artwork(group, library_root)
        print(f"   Artwork: {artwork['label']}")
        print(f"   Art dest: {artwork['destination']}")
        discrepancies = grouped_discrepancies(group)
        for label, folder_value, tag_value, affected in discrepancies:
            print(f"   Folder/tag discrepancy ({affected}/{len(group.tracks)}):")
            print(f"     Folder-derived {label}: {folder_value}")
            print(f"     Embedded {label.title()}: {tag_value}")


def grouped_discrepancies(group: AlbumGroup) -> list[tuple[str, str, str, int]]:
    counts: Counter[tuple[str, str, str]] = Counter()
    for track in group.tracks:
        if track.embedded_artist and track.folder_artist_hint and track.embedded_artist.casefold() != track.folder_artist_hint.casefold():
            counts[("artist", track.folder_artist_hint, track.embedded_artist)] += 1
        if track.embedded_album and track.folder_album_hint and track.embedded_album.casefold() != track.folder_album_hint.casefold():
            counts[("release", track.folder_album_hint, track.embedded_album)] += 1
    return [(label, folder, tagged, affected) for (label, folder, tagged), affected in counts.most_common()]


def print_candidates(group: AlbumGroup) -> None:
    print(f"\nMusicBrainz candidates for {group.album_artist} - {group.album}")
    if not group.provider_candidates:
        print("  No provider candidates were retained. Current local metadata remains proposed.")
        return
    for index, release in enumerate(group.provider_candidates[:5], 1):
        print("  " + candidate_summary(release, group, index).replace("; ", "\n     "))


def print_track_mappings(group: AlbumGroup, library_root: Path) -> None:
    print(f"\nTrack mappings for {group.album_artist} - {group.album}")
    for track in group.tracks:
        print(f"  {track.rel_source} -> {library_root / track.proposed_rel}")


def edit_album(group: AlbumGroup) -> None:
    old_identity = (group.album_artist, group.artist, group.album, group.year)
    for attr, label in [("album_artist", "Album artist"), ("artist", "Default track artist"), ("album", "Album"), ("year", "Year")]:
        current = getattr(group, attr)
        value = input(f"{label} [{current}]: ").strip()
        if value:
            setattr(group, attr, value)
    if (group.album_artist, group.artist, group.album, group.year) != old_identity and group.musicbrainz_releaseid:
        group.musicbrainz_releaseid = ""
        group.match_status = "metadata edited; release lookup needed"
    for track in group.tracks:
        if not track.artist or track.artist == group.artist:
            track.artist = group.artist


def edit_track(group: AlbumGroup) -> None:
    for idx, track in enumerate(group.tracks, 1):
        print(f"{idx}) {track.disc}-{track.track:02d} {track.artist} - {track.title}")
    raw = input("Track number to edit: ").strip()
    if not raw.isdigit() or not (1 <= int(raw) <= len(group.tracks)):
        print("No matching track.")
        return
    track = group.tracks[int(raw) - 1]
    for attr, label in [("disc", "Disc"), ("track", "Track"), ("artist", "Artist"), ("title", "Title")]:
        current = getattr(track, attr)
        value = input(f"{label} [{current}]: ").strip()
        if not value:
            continue
        setattr(track, attr, int(value) if attr in {"disc", "track"} and value.isdigit() else value)


def interactive_review(groups: list[AlbumGroup], library_root: Path, multidisc: bool, include_track_artist: bool) -> bool:
    while True:
        print_review(groups, library_root, multidisc, include_track_artist)
        print()
        choice = input("Import options: [A]pprove current local metadata for all, [E]dit album, edit [T]rack, [C]andidates, [V]iew mappings, [S]kip, [Q]uit: ").strip().lower()
        if choice in {"", "a", "accept", "accept all"}:
            return True
        if choice in {"q", "quit", "abort"}:
            return False
        if choice in {"s", "skip"}:
            group = choose_group(groups)
            if group:
                group.skip = not group.skip
        elif choice in {"e", "edit"}:
            group = choose_group(groups)
            if group:
                edit_album(group)
        elif choice in {"t", "track"}:
            group = choose_group(groups)
            if group:
                edit_track(group)
        elif choice in {"c", "candidates"}:
            group = choose_group(groups)
            if group:
                print_candidates(group)
        elif choice in {"v", "view", "mappings"}:
            group = choose_group(groups)
            if group:
                assign_destinations(groups, multidisc, include_track_artist)
                print_track_mappings(group, library_root)
        else:
            print("Please choose A, E, T, C, V, S, or Q.")


def choose_group(groups: list[AlbumGroup]) -> AlbumGroup | None:
    raw = input("Album number: ").strip()
    if raw.isdigit() and 1 <= int(raw) <= len(groups):
        return groups[int(raw) - 1]
    print("No matching album.")
    return None


def stage_import(
    groups: list[AlbumGroup], stage: Path, source_root: Path, multidisc: bool, include_track_artist: bool, *,
    copy_media: bool = True, library_root: Path | None = None,
) -> dict[str, Any]:
    assign_destinations(groups, multidisc, include_track_artist)
    copied = 0
    skipped = 0
    albums: list[dict[str, Any]] = []
    for group in groups:
        if group.skip:
            skipped += len(group.tracks)
            status(f"Skipping album: {group.album_artist} - {group.album}")
            continue
        if copy_media:
            status(f"Staging album: {group.album_artist} - {group.album} ({len(group.tracks)} track(s))")
        album_dir = stage / proposed_album_dir(group)
        artifact_dir = album_dir / ARTIFACT_DIR_NAME
        if copy_media:
            artifact_dir.mkdir(parents=True, exist_ok=True)
        album_tracks = []
        for track in group.tracks:
            source_size = track.source.stat().st_size
            staged_size = 0
            if copy_media:
                target = stage / track.proposed_rel
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(track.source, target)
                copied += 1
                staged_size = target.stat().st_size
            album_tracks.append({
                "source": track.rel_source,
                "source_path": str(track.source),
                "destination": track.proposed_rel,
                "destination_path": str((library_root / track.proposed_rel) if library_root else track.proposed_rel),
                "readable": track.readable,
                "source_size_bytes": source_size,
                "staged_size_bytes": staged_size,
                "verified_size_match": source_size == staged_size if copy_media else None,
                "embedded_artist": track.embedded_artist,
                "folder_artist_hint": track.folder_artist_hint,
                "embedded_album": track.embedded_album,
                "folder_album_hint": track.folder_album_hint,
                "codec": track.codec,
                "bitrate": track.bitrate,
                "sample_rate": track.sample_rate,
                "compilation": track.compilation,
            })
        artwork = describe_artwork(group, library_root or stage)
        if copy_media:
            artwork = copy_album_cover(source_root, group, album_dir, artifact_dir, artwork)
        albums.append(
            {
                "album_artist": group.album_artist,
                "album": group.album,
                "year": group.year,
                "musicbrainz_releaseid": group.musicbrainz_releaseid,
                "musicbrainz_releasegroupid": group.musicbrainz_releasegroupid,
                "match_status": group.match_status,
                "match_confidence": group.match_confidence,
                "edition_clues": group.edition_clues,
                "artwork": artwork,
                "tracks": album_tracks,
            }
        )
    manifest = {
        "source_root": str(source_root),
        "library_root": str(library_root) if library_root else "",
        "staged_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "staging_performed": copy_media,
        "planned_tracks": sum(len(album["tracks"]) for album in albums),
        "staged_bytes": sum(track["staged_size_bytes"] for album in albums for track in album["tracks"]),
        "copied_tracks": copied,
        "skipped_tracks": skipped,
        "albums": albums,
    }
    for album in albums if copy_media else []:
        artifact_dir = stage / sanitize_component(album["album_artist"]) / sanitize_component(
            f'{album["album"]} ({album["year"]})' if album["year"] else album["album"]
        ) / ARTIFACT_DIR_NAME
        artifact_dir.mkdir(parents=True, exist_ok=True)
        (artifact_dir / IMPORT_MANIFEST_NAME).write_text(json.dumps(album, indent=2) + "\n", encoding="utf-8")
    return manifest


def describe_artwork(group: AlbumGroup, library_root: Path) -> dict[str, str]:
    destination = str(library_root / proposed_album_dir(group) / "cover.jpg")
    parents = [track.source.parent for track in group.tracks]
    for parent in parents:
        for candidate in parent.iterdir() if parent.exists() else []:
            if candidate.is_file() and candidate.name.lower() in COVER_NAMES:
                return {"status": "planned", "mechanism": "local sidecar", "source": str(candidate), "destination": destination, "label": f"local {candidate.name}"}
    if any(track.has_embedded_art for track in group.tracks):
        return {"status": "planned", "mechanism": "embedded", "source": "embedded front cover", "destination": destination, "label": "embedded front cover"}
    if group.musicbrainz_releaseid or group.musicbrainz_releasegroupid:
        return {"status": "planned", "mechanism": "cover-art-archive", "source": "Cover Art Archive candidate", "destination": destination, "label": "Cover Art Archive candidate"}
    return {"status": "missing", "mechanism": "none", "source": "", "destination": destination, "label": "none"}


def copy_album_cover(source_root: Path, group: AlbumGroup, album_dir: Path, artifact_dir: Path, artwork: dict[str, str]) -> dict[str, str]:
    parents = [track.source.parent for track in group.tracks]
    for parent in parents + [source_root]:
        for candidate in parent.iterdir() if parent.exists() else []:
            if candidate.is_file() and candidate.name.lower() in COVER_NAMES:
                target = album_dir / ("cover.png" if candidate.suffix.lower() == ".png" else "cover.jpg")
                artifact_target = artifact_dir / candidate.name
                if not target.exists():
                    shutil.copy2(candidate, target)
                if not artifact_target.exists():
                    shutil.copy2(candidate, artifact_target)
                artwork.update({"status": "published", "mechanism": "local sidecar", "source": str(candidate), "destination": str(target), "label": f"local {candidate.name}"})
                return artwork
    # Remote lookup is best-effort and intentionally cannot fail an audio import.
    target = album_dir / "cover.jpg"
    if artwork["mechanism"] == "cover-art-archive":
        try:
            result, _ = download_cover_jpg(group.musicbrainz_releaseid, group.musicbrainz_releasegroupid, target, 0.0)
            if result.startswith("downloaded"):
                artwork.update({"status": "published", "source": result, "destination": str(target), "label": "Cover Art Archive"})
                return artwork
            fallback = download_deezer_cover(group.album_artist, group.album, target)
            if fallback.startswith("downloaded"):
                artwork.update({"status": "published", "mechanism": "deezer", "source": fallback, "destination": str(target), "label": "Deezer cover"})
                return artwork
            artwork.update({"status": "missing", "source": f"{result}; {fallback}", "label": "none"})
        except Exception as exc:
            artwork.update({"status": "missing", "source": str(exc), "label": "none"})
    return artwork


def has_audio_files(path: Path) -> bool:
    return any(child.is_file() and child.suffix.lower() in SUPPORTED_EXTENSIONS for child in path.iterdir())


def has_album_cover(path: Path) -> bool:
    return any((path / name).exists() for name in COVER_NAMES)


def has_disc_audio(path: Path) -> bool:
    for child in path.iterdir():
        if child.is_dir() and re.fullmatch(r"CD ?\d+|Disc ?\d+", child.name, flags=re.IGNORECASE):
            if has_audio_files(child):
                return True
    return False


def discover_album_dirs(root: Path) -> list[Path]:
    albums: list[Path] = []
    for path, dirnames, _filenames in os.walk(root):
        dirnames[:] = [name for name in dirnames if not name.startswith(".")]
        current = Path(path)
        if has_audio_files(current) or has_disc_audio(current):
            albums.append(current)
            dirnames[:] = []
    return sorted(albums)


def parse_album_folder_name(name: str) -> tuple[str, str]:
    match = re.match(r"^(?P<album>.+?)\s+\((?P<year>(?:19|20)\d{2})\)$", name)
    if match:
        return match.group("album"), match.group("year")
    return name, ""


def album_info_from_dir(album_dir: Path, library_root: Path) -> dict[str, str]:
    manifest = album_dir / ARTIFACT_DIR_NAME / IMPORT_MANIFEST_NAME
    if manifest.exists():
        try:
            data = json.loads(manifest.read_text(encoding="utf-8"))
            return {
                "album_artist": str(data.get("album_artist") or album_dir.parent.name),
                "album": str(data.get("album") or parse_album_folder_name(album_dir.name)[0]),
                "year": str(data.get("year") or parse_album_folder_name(album_dir.name)[1]),
                "musicbrainz_releaseid": str(data.get("musicbrainz_releaseid") or ""),
                "musicbrainz_releasegroupid": str(data.get("musicbrainz_releasegroupid") or ""),
            }
        except (OSError, json.JSONDecodeError):
            pass
    album, year = parse_album_folder_name(album_dir.name)
    artist = album_dir.parent.name if album_dir.parent != library_root else "Unknown Artist"
    return {"album_artist": artist, "album": album, "year": year, "musicbrainz_releaseid": "", "musicbrainz_releasegroupid": ""}


def musicbrainz_release_details(release: dict[str, Any], compatibility: int, warnings: list[str] | None = None) -> str:
    artist_credit = release.get("artist-credit") or release.get("artist_credit") or []
    artists = []
    for item in artist_credit:
        if isinstance(item, dict):
            artist = item.get("artist") or {}
            artists.append(str(item.get("name") or artist.get("name") or ""))
        elif isinstance(item, str):
            artists.append(item)
    details = ["artist " + "".join(artists).strip() if artists else "artist unknown"]
    details.append("title " + str(release.get("title") or "unknown"))
    if release.get("date"):
        details.append("date " + str(release["date"]))
    if release.get("country"):
        details.append("country " + str(release["country"]))
    release_group = release.get("release-group") or release.get("release_group") or {}
    release_types = []
    if isinstance(release_group, dict):
        if release_group.get("primary-type") or release_group.get("primary_type"):
            release_types.append(str(release_group.get("primary-type") or release_group.get("primary_type")))
        secondary = release_group.get("secondary-types") or release_group.get("secondary_types") or []
        release_types.extend(str(value) for value in secondary)
    if release_types:
        details.append("type " + "/".join(release_types))
    edition = [str(value) for value in (release.get("disambiguation"), release.get("packaging")) if value and str(value).casefold() != "none"]
    if edition:
        details.append("edition " + ", ".join(edition))
    media = release.get("media") or []
    media_count = release.get("media-count") or release.get("media_count") or (len(media) if media else 0)
    track_count = release.get("track-count") or release.get("track_count") or sum(
        int(m.get("track-count") or m.get("track_count") or 0) for m in media if isinstance(m, dict)
    )
    if media_count:
        details.append(f"{media_count} disc(s)")
    if track_count:
        details.append(f"{track_count} track(s)")
    labels = []
    catalogue_numbers = []
    for info in release.get("label-info") or release.get("label_info") or []:
        if not isinstance(info, dict):
            continue
        label = info.get("label") or {}
        if isinstance(label, dict) and label.get("name"):
            labels.append(str(label["name"]))
        if info.get("catalog-number") or info.get("catalog_number"):
            catalogue_numbers.append(str(info.get("catalog-number") or info.get("catalog_number")))
    if release.get("label") and not labels:
        labels.append(str(release["label"]))
    if release.get("catno") and not catalogue_numbers:
        catalogue_numbers.append(str(release["catno"]))
    if labels:
        details.append("label " + ", ".join(dict.fromkeys(labels)))
    if catalogue_numbers:
        details.append("catalogue " + ", ".join(dict.fromkeys(catalogue_numbers)))
    if release.get("barcode"):
        details.append("barcode " + str(release["barcode"]))
    details.append(f"local compatibility {compatibility}%")
    if warnings:
        details.append("warning " + ", ".join(warnings))
    return "; ".join(details)


def candidate_summary(release: dict[str, Any], group: AlbumGroup, rank: int) -> str:
    compatibility, warnings = release_compatibility(group, release)
    details = musicbrainz_release_details(release, compatibility, warnings)
    return f"{rank}. {details}"


def find_musicbrainz_release(
    album_artist: str, album: str, year: str, last_request: float, *, group: AlbumGroup | None = None
) -> tuple[str, str, str, float]:
    if not album or album == "Unknown Album":
        return "", "", "not enough metadata", last_request

    attempts = [year]
    if year:
        attempts.append("")
    last_status = "no MusicBrainz match"
    for attempt_year in attempts:
        query_bits = [f'release:"{album}"']
        if album_artist and album_artist != "Unknown Artist":
            query_bits.append(f'artist:"{album_artist}"')
        if attempt_year:
            query_bits.append(f"date:{attempt_year}")
        query = " AND ".join(query_bits)
        url = "https://musicbrainz.org/ws/2/release/?" + urllib.parse.urlencode({"query": query, "fmt": "json", "limit": "10"})
        try:
            last_request = wait_for_musicbrainz(last_request)
            data = musicbrainz_json(url)
        except Exception as exc:
            return "", "", f"lookup failed: {exc}", last_request
        local_group = group or AlbumGroup(key="", album_artist=album_artist, album=album, year=attempt_year)
        releases = data.get("releases", []) or []
        if not releases:
            last_status = "no MusicBrainz match"
            continue
        ranked = sorted(releases, key=lambda r: release_compatibility(local_group, r)[0], reverse=True)
        best_score, best_notes = release_compatibility(local_group, ranked[0])
        if group is not None:
            group.provider_candidates = ranked[:5]
            group.match_confidence = best_score
        if best_score < 55:
            last_status = "weak MusicBrainz compatibility; local metadata retained"
            continue
        tied = [release for release in ranked if release_compatibility(local_group, release)[0] == best_score]
        # A candidate needs a meaningful lead over the next edition before it can identify a release.
        second_score = release_compatibility(local_group, ranked[1])[0] if len(ranked) > 1 else -1
        if len(tied) > 1 or (second_score >= best_score - 8):
            return "", "", f"ambiguous MusicBrainz editions; best local compatibility {best_score}%", last_request
        best = tied[0]
        status_text = "MusicBrainz candidate; " + musicbrainz_release_details(best, best_score, best_notes)
        if year and not attempt_year:
            status_text = "MusicBrainz candidate after retry without folder year; " + musicbrainz_release_details(best, best_score)
        return str(best.get("id") or ""), release_group_id(best), status_text, last_request
    return "", "", last_status, last_request


def release_group_id_for_release(release_id: str, last_request: float) -> tuple[str, float]:
    url = f"https://musicbrainz.org/ws/2/release/{urllib.parse.quote(release_id)}?" + urllib.parse.urlencode({"inc": "release-groups", "fmt": "json"})
    last_request = wait_for_musicbrainz(last_request)
    data = musicbrainz_json(url)
    return release_group_id(data), last_request


def coverart_bytes(entity: str, entity_id: str) -> bytes:
    url = f"https://coverartarchive.org/{entity}/{entity_id}/front-500"
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=20) as response:
        return response.read()


def download_url_bytes(url: str) -> bytes:
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=20) as response:
        return response.read()


def download_cover_jpg(release_id: str, release_group_id_value: str, target: Path, last_request: float) -> tuple[str, float]:
    tmp = target.with_suffix(".jpg.tmp")
    errors: list[str] = []
    content = b""
    if release_id:
        try:
            content = coverart_bytes("release", release_id)
        except Exception as exc:
            errors.append(f"release cover: {exc}")
    if not content and not release_group_id_value and release_id:
        try:
            release_group_id_value, last_request = release_group_id_for_release(release_id, last_request)
        except Exception as exc:
            errors.append(f"release-group lookup: {exc}")
    if not content and release_group_id_value:
        try:
            content = coverart_bytes("release-group", release_group_id_value)
        except Exception as exc:
            errors.append(f"release-group cover: {exc}")
    if not content:
        return "; ".join(errors) if errors else "empty cover response", last_request
    tmp.write_bytes(content)
    tmp.replace(target)
    if release_group_id_value and errors:
        return "downloaded from release group", last_request
    return "downloaded", last_request


def normalize_text(value: str) -> str:
    value = re.sub(r"\([^)]*\)", "", value)
    value = re.sub(r"[^a-z0-9]+", " ", value.casefold())
    return re.sub(r" +", " ", value).strip()


def album_match_score(album_artist: str, album: str, candidate: dict[str, Any]) -> int:
    candidate_album = normalize_text(str(candidate.get("title") or ""))
    wanted_album = normalize_text(album)
    artist_data = candidate.get("artist") if isinstance(candidate.get("artist"), dict) else {}
    candidate_artist = normalize_text(str(artist_data.get("name") or candidate.get("artistName") or ""))
    wanted_artist = normalize_text(album_artist)
    score = 0
    if candidate_album == wanted_album:
        score += 5
    elif candidate_album and wanted_album and (candidate_album in wanted_album or wanted_album in candidate_album):
        score += 2
    if candidate_artist == wanted_artist:
        score += 4
    elif candidate_artist and wanted_artist and (candidate_artist in wanted_artist or wanted_artist in candidate_artist):
        score += 2
    return score


def deezer_json(url: str) -> dict[str, Any]:
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=12) as response:
        return json.loads(response.read().decode("utf-8"))


def find_deezer_cover_url(album_artist: str, album: str) -> tuple[str, str]:
    query = f'artist:"{album_artist}" album:"{album}"'
    url = "https://api.deezer.com/search/album?" + urllib.parse.urlencode({"q": query, "limit": "10"})
    data = deezer_json(url)
    candidates = data.get("data", []) or []
    if not candidates:
        return "", "no Deezer match"
    best = max(candidates, key=lambda candidate: album_match_score(album_artist, album, candidate))
    if album_match_score(album_artist, album, best) < 7:
        return "", "weak Deezer match ignored"
    cover = str(best.get("cover_xl") or best.get("cover_big") or best.get("cover_medium") or "")
    if not cover:
        return "", "Deezer match had no cover URL"
    artist_data = best.get("artist") if isinstance(best.get("artist"), dict) else {}
    label = f"{artist_data.get('name') or album_artist} - {best.get('title') or album}"
    return cover, f"Deezer: {label}"


def download_deezer_cover(album_artist: str, album: str, target: Path) -> str:
    cover_url, match_status = find_deezer_cover_url(album_artist, album)
    if not cover_url:
        return match_status
    content = download_url_bytes(cover_url)
    if not content:
        return "empty Deezer cover response"
    tmp = target.with_suffix(".jpg.tmp")
    tmp.write_bytes(content)
    tmp.replace(target)
    return f"downloaded from {match_status}"


def cmd_covers(args: argparse.Namespace) -> int:
    global STATUS_DELAY
    STATUS_DELAY = args.delay
    root = Path(args.library_root).expanduser().resolve()
    overwrite = args.overwrite == "yes"
    if not root.is_dir():
        print(f"Not a directory: {root}", file=sys.stderr)
        return 2

    albums = discover_album_dirs(root)
    status(f"Scanning {root} for album folders...")
    status(f"Found {len(albums)} album folder(s).")
    if not albums:
        print()
        print("No album folders were found. Check that the selected directory contains album folders with audio files.")
        print("For an artist import, this is usually the artist directory, for example:")
        print("  /path/to/Music/Example Artist")
    downloaded = 0
    skipped = 0
    failed = 0
    last_request = 0.0

    for index, album_dir in enumerate(albums, 1):
        info = album_info_from_dir(album_dir, root)
        label = f"{info['album_artist']} - {info['album']}"
        status(f"  Covers {index}/{len(albums)}: {label}")
        target = album_dir / "cover.jpg"
        if has_album_cover(album_dir) and not overwrite:
            status("    local cover already exists; skipping")
            skipped += 1
            continue
        release_id = info["musicbrainz_releaseid"]
        release_group_id_value = info["musicbrainz_releasegroupid"]
        if not release_id:
            release_id, release_group_id_value, match_status, last_request = find_musicbrainz_release(info["album_artist"], info["album"], info["year"], last_request)
            if not release_id:
                status(f"    primary lookup failed: {match_status}")
                try:
                    result = download_deezer_cover(info["album_artist"], info["album"], target)
                except Exception as exc:
                    result = f"Deezer lookup failed: {exc}"
                if not result.startswith("downloaded"):
                    status(f"    secondary cover lookup failed: {result}")
                    failed += 1
                    continue
                status(f"    saved {target.name} ({result})")
                downloaded += 1
                continue
        try:
            result, last_request = download_cover_jpg(release_id, release_group_id_value, target, last_request)
        except Exception as exc:
            result = f"cover download failed: {exc}"
        if result != "downloaded" and not result.startswith("downloaded"):
            status(f"    Cover Art Archive failed: {result}")
            try:
                result = download_deezer_cover(info["album_artist"], info["album"], target)
            except Exception as exc:
                result = f"Deezer lookup failed: {exc}"
            if not result.startswith("downloaded"):
                status(f"    secondary cover lookup failed: {result}")
                failed += 1
                continue
        status(f"    saved {target.name} ({result})")
        downloaded += 1

    print()
    print("Cover download summary:")
    print(f"  Albums scanned:       {len(albums)}")
    print(f"  Covers downloaded:    {downloaded}")
    print(f"  Existing/skipped:     {skipped}")
    print(f"  Missing/failed:       {failed}")
    return 0 if failed == 0 or downloaded > 0 or skipped > 0 else 1


def cmd_import(args: argparse.Namespace) -> int:
    global STATUS_DELAY
    STATUS_DELAY = args.delay
    source = Path(args.source).expanduser().resolve()
    stage = Path(args.stage).expanduser().resolve()
    library_root = Path(args.library_root).expanduser()
    if not source.is_dir():
        print(f"Not a directory: {source}", file=sys.stderr)
        return 2
    tracks = scan_source(source)
    if not tracks:
        print("No supported audio files found.", file=sys.stderr)
        return 3
    unreadable = [track.rel_source for track in tracks if not track.readable]
    if unreadable:
        print("Warning: ffprobe could not read metadata for:")
        for rel in unreadable:
            print(f"  {rel}")
    groups = group_tracks(tracks)
    status(f"Grouped files into {len(groups)} album candidate(s).")
    if args.lookup == "yes":
        enrich_with_musicbrainz(groups)
    if not interactive_review(groups, library_root, args.multidisc == "yes", args.include_track_artist == "yes"):
        print("Import aborted before staging.")
        return 4
    manifest = stage_import(
        groups, stage, source, args.multidisc == "yes", args.include_track_artist == "yes",
        copy_media=args.mode == "apply", library_root=library_root,
    )
    print("\nSafe publication plan (source -> destination):")
    for album in manifest["albums"]:
        album_name = f'{album["album"]} ({album["year"]})' if album.get("year") else album["album"]
        print(f'  {album["album_artist"]} - {album_name}; {len(album["tracks"])} track(s)')
        print(f'    MusicBrainz: {album["match_status"]}')
        displayed_tracks = album["tracks"][:3]
        for track in displayed_tracks:
            print(f'    {source / track["source"]} -> {library_root / track["destination"]}')
        remaining = len(album["tracks"]) - len(displayed_tracks)
        if remaining:
            print(f"    ... {remaining} more mapping(s); use [V]iew mappings during review to inspect every path.")
    approved = False
    if args.mode == "apply" and manifest["copied_tracks"]:
        try:
            approved = input("Type APPLY to publish these staged copies (anything else cancels): ").strip() == "APPLY"
        except (EOFError, KeyboardInterrupt):
            approved = False
        if not approved:
            print(f"Publication cancelled. Staged files remain at: {stage}")
    manifest["mode"] = args.mode
    manifest["approved_to_publish"] = approved
    manifest["publication"] = "copy"
    manifest["source_lifecycle"] = "preserved"
    Path(args.result_file).write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    if args.mode == "dry-run":
        print("Plan recorded without staging media.")
    else:
        print(f"Staged {manifest['copied_tracks']} digital track(s) in: {stage}")
    if manifest["skipped_tracks"]:
        print(f"Skipped {manifest['skipped_tracks']} track(s).")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    importer = subparsers.add_parser("import", help="scan, review, and stage digital music")
    importer.add_argument("--source", required=True)
    importer.add_argument("--stage", required=True)
    importer.add_argument("--library-root", required=True)
    importer.add_argument("--multidisc", choices=["yes", "no"], default="no")
    importer.add_argument("--include-track-artist", choices=["yes", "no"], default="no")
    importer.add_argument("--lookup", choices=["yes", "no"], default="yes")
    importer.add_argument("--mode", choices=["dry-run", "apply"], default="dry-run")
    importer.add_argument("--delay", type=float, default=0.0)
    importer.add_argument("--result-file", required=True)
    importer.set_defaults(func=cmd_import)
    covers = subparsers.add_parser("covers", help="download missing album cover.jpg files")
    covers.add_argument("--library-root", required=True)
    covers.add_argument("--overwrite", choices=["yes", "no"], default="no")
    covers.add_argument("--delay", type=float, default=0.0)
    covers.set_defaults(func=cmd_covers)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except (EOFError, KeyboardInterrupt):
        print("Import cancelled; nothing was published.")
        return 4
    except Exception as exc:
        if os.environ.get("MUSIC_INGEST_DEBUG") == "1":
            raise
        print(f"music-ingest: import failed: {exc} (set MUSIC_INGEST_DEBUG=1 for traceback)", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
