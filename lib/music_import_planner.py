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


@dataclass
class AlbumGroup:
    key: str
    tracks: list[Track] = field(default_factory=list)
    artist: str = ""
    album_artist: str = ""
    album: str = ""
    year: str = ""
    musicbrainz_releaseid: str = ""
    match_status: str = "not searched"
    skip: bool = False


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


def track_from_probe(source: Path, root: Path, probe: dict[str, Any]) -> Track:
    tags = dict(probe.get("format", {}).get("tags", {}) or {})
    for stream in probe.get("streams", []) or []:
        tags.update(stream.get("tags", {}) or {})
    disc_from_name, track_from_name, title_from_name = split_filename(source)
    rel_parts = source.relative_to(root).parts
    parent_album = rel_parts[-2] if len(rel_parts) >= 2 else ""
    parent_artist = rel_parts[-3] if len(rel_parts) >= 3 else (root.name if len(rel_parts) >= 2 else "")

    track = Track(
        source=source,
        rel_source=str(source.relative_to(root)),
        extension=source.suffix.lower(),
        title=first_tag(tags, "title") or title_from_name,
        artist=first_tag(tags, "artist", "album_artist", "albumartist") or parent_artist,
        album_artist=first_tag(tags, "album_artist", "albumartist", "album artist"),
        album=first_tag(tags, "album") or parent_album,
        year=parse_year(first_tag(tags, "date", "year")),
        track=parse_number(first_tag(tags, "track", "tracknumber")) or track_from_name,
        disc=parse_number(first_tag(tags, "disc", "discnumber")) or disc_from_name or 1,
        total_discs=parse_number(first_tag(tags, "disctotal", "totaldiscs")),
        genre=first_tag(tags, "genre"),
        musicbrainz_trackid=first_tag(tags, "musicbrainz_trackid", "musicbrainz/releasetrackid"),
        musicbrainz_releaseid=first_tag(tags, "musicbrainz_albumid", "musicbrainz_releaseid"),
        has_embedded_art=any((s.get("codec_type") == "video" and s.get("disposition", {}).get("attached_pic")) for s in probe.get("streams", []) or []),
        readable=bool(probe),
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
        groups.append(group)
    return sorted(groups, key=lambda g: (g.album_artist.casefold(), g.year, g.album.casefold()))


def most_common(values: list[str]) -> str:
    clean = [v for v in values if v]
    return Counter(clean).most_common(1)[0][0] if clean else ""


def musicbrainz_json(url: str) -> dict[str, Any]:
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
    with urllib.request.urlopen(request, timeout=12) as response:
        return json.loads(response.read().decode("utf-8"))


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
        query_bits = [f'release:"{group.album}"']
        if group.album_artist and group.album_artist != "Unknown Artist":
            query_bits.append(f'artist:"{group.album_artist}"')
        if group.year:
            query_bits.append(f"date:{group.year}")
        query = " AND ".join(query_bits)
        url = "https://musicbrainz.org/ws/2/release/?" + urllib.parse.urlencode({"query": query, "fmt": "json", "limit": "5"})
        try:
            last_request = wait_for_musicbrainz(last_request)
            data = musicbrainz_json(url)
        except Exception as exc:
            group.match_status = f"lookup failed: {exc}"
            status(f"    {group.match_status}")
            continue
        releases = data.get("releases", []) or []
        if not releases:
            group.match_status = "no MusicBrainz match"
            status("    no MusicBrainz match")
            continue
        best = max(releases, key=lambda r: release_score(group, r))
        score = release_score(group, best)
        if score < 4:
            group.match_status = "weak MusicBrainz match ignored"
            status("    weak MusicBrainz match ignored")
            continue
        group.musicbrainz_releaseid = str(best.get("id") or "")
        group.year = group.year or parse_year(str(best.get("date") or ""))
        group.match_status = f"MusicBrainz candidate: {group.musicbrainz_releaseid}"
        status(f"    {group.match_status}")


def release_score(group: AlbumGroup, release: dict[str, Any]) -> int:
    score = 0
    title = str(release.get("title") or "")
    artist_credit = " ".join(str(ac.get("name") or "") for ac in release.get("artist-credit", []) if isinstance(ac, dict))
    if title.casefold() == group.album.casefold():
        score += 5
    elif group.album.casefold() in title.casefold() or title.casefold() in group.album.casefold():
        score += 2
    if group.album_artist != "Unknown Artist" and group.album_artist.casefold() in artist_credit.casefold():
        score += 3
    if group.year and str(release.get("date") or "").startswith(group.year):
        score += 2
    return score


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
        formats = ", ".join(f"{ext}:{count}" for ext, count in sorted(Counter(t.extension for t in group.tracks).items()))
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
        print(f"{idx}) {prefix}{group.album_artist} - {group.album} ({group.year or 'year unknown'})")
        print(f"   Tracks : {len(group.tracks)}   Formats: {formats}")
        print(f"   Match  : {group.match_status}")
        print(f"   Missing: {', '.join(missing) if missing else 'none'}")
        print(f"   Dest   : {library_root / proposed_album_dir(group)}")


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
        choice = input("Import options: [A]ccept all, [S]kip album, [E]dit album, edit [T]rack, [Q]uit: ").strip().lower()
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
        else:
            print("Please choose A, S, E, T, or Q.")


def choose_group(groups: list[AlbumGroup]) -> AlbumGroup | None:
    raw = input("Album number: ").strip()
    if raw.isdigit() and 1 <= int(raw) <= len(groups):
        return groups[int(raw) - 1]
    print("No matching album.")
    return None


def stage_import(groups: list[AlbumGroup], stage: Path, source_root: Path, multidisc: bool, include_track_artist: bool) -> dict[str, Any]:
    assign_destinations(groups, multidisc, include_track_artist)
    copied = 0
    skipped = 0
    albums: list[dict[str, Any]] = []
    for group in groups:
        if group.skip:
            skipped += len(group.tracks)
            status(f"Skipping album: {group.album_artist} - {group.album}")
            continue
        status(f"Staging album: {group.album_artist} - {group.album} ({len(group.tracks)} track(s))")
        album_dir = stage / proposed_album_dir(group)
        artifact_dir = album_dir / ARTIFACT_DIR_NAME
        artifact_dir.mkdir(parents=True, exist_ok=True)
        album_tracks = []
        for track in group.tracks:
            target = stage / track.proposed_rel
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(track.source, target)
            copied += 1
            album_tracks.append({"source": track.rel_source, "destination": track.proposed_rel, "readable": track.readable})
        copy_album_cover(source_root, group, album_dir, artifact_dir)
        albums.append(
            {
                "album_artist": group.album_artist,
                "album": group.album,
                "year": group.year,
                "musicbrainz_releaseid": group.musicbrainz_releaseid,
                "match_status": group.match_status,
                "tracks": album_tracks,
            }
        )
    manifest = {
        "source_root": str(source_root),
        "staged_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "copied_tracks": copied,
        "skipped_tracks": skipped,
        "albums": albums,
    }
    for album in albums:
        artifact_dir = stage / sanitize_component(album["album_artist"]) / sanitize_component(
            f'{album["album"]} ({album["year"]})' if album["year"] else album["album"]
        ) / ARTIFACT_DIR_NAME
        artifact_dir.mkdir(parents=True, exist_ok=True)
        (artifact_dir / IMPORT_MANIFEST_NAME).write_text(json.dumps(album, indent=2) + "\n", encoding="utf-8")
    return manifest


def copy_album_cover(source_root: Path, group: AlbumGroup, album_dir: Path, artifact_dir: Path) -> None:
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
                return


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
            }
        except (OSError, json.JSONDecodeError):
            pass
    album, year = parse_album_folder_name(album_dir.name)
    artist = album_dir.parent.name if album_dir.parent != library_root else "Unknown Artist"
    return {"album_artist": artist, "album": album, "year": year, "musicbrainz_releaseid": ""}


def find_musicbrainz_release_id(album_artist: str, album: str, year: str, last_request: float) -> tuple[str, str, float]:
    if not album or album == "Unknown Album":
        return "", "not enough metadata", last_request
    query_bits = [f'release:"{album}"']
    if album_artist and album_artist != "Unknown Artist":
        query_bits.append(f'artist:"{album_artist}"')
    if year:
        query_bits.append(f"date:{year}")
    query = " AND ".join(query_bits)
    url = "https://musicbrainz.org/ws/2/release/?" + urllib.parse.urlencode({"query": query, "fmt": "json", "limit": "5"})
    try:
        last_request = wait_for_musicbrainz(last_request)
        data = musicbrainz_json(url)
    except Exception as exc:
        return "", f"lookup failed: {exc}", last_request
    group = AlbumGroup(key="", album_artist=album_artist, album=album, year=year)
    releases = data.get("releases", []) or []
    if not releases:
        return "", "no MusicBrainz match", last_request
    best = max(releases, key=lambda r: release_score(group, r))
    if release_score(group, best) < 4:
        return "", "weak MusicBrainz match ignored", last_request
    return str(best.get("id") or ""), "MusicBrainz candidate", last_request


def download_cover_jpg(release_id: str, target: Path) -> str:
    url = f"https://coverartarchive.org/release/{release_id}/front-500"
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    tmp = target.with_suffix(".jpg.tmp")
    with urllib.request.urlopen(request, timeout=20) as response:
        content = response.read()
    if not content:
        return "empty cover response"
    tmp.write_bytes(content)
    tmp.replace(target)
    return "downloaded"


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
        if not release_id:
            release_id, match_status, last_request = find_musicbrainz_release_id(info["album_artist"], info["album"], info["year"], last_request)
            if not release_id:
                status(f"    {match_status}; no cover downloaded")
                failed += 1
                continue
        try:
            result = download_cover_jpg(release_id, target)
        except Exception as exc:
            status(f"    cover download failed: {exc}")
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
    manifest = stage_import(groups, stage, source, args.multidisc == "yes", args.include_track_artist == "yes")
    Path(args.result_file).write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
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
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
