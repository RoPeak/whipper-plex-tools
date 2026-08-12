import json
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import patch

from lib.music_import_planner import (
    AlbumGroup,
    album_info_from_dir,
    cmd_covers,
    assign_destinations,
    discover_album_dirs,
    download_cover_jpg,
    download_deezer_cover,
    find_deezer_cover_url,
    edit_album,
    find_musicbrainz_release,
    group_tracks,
    sanitize_component,
    stage_import,
    merge_audio_tags,
    track_from_probe,
)


def probe(tags):
    return {
        "format": {"tags": tags},
        "streams": [{"codec_type": "audio"}],
    }


class MusicImportPlannerTests(unittest.TestCase):
    def test_tagged_album_uses_embedded_metadata(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / "Either_Or" / "01 Speed Trials.m4a"
            path.parent.mkdir()
            path.write_bytes(b"audio")

            track = merge_audio_tags(
                path,
                root,
                probe(
                    {
                        "title": "Speed Trials",
                        "artist": "Elliott Smith",
                        "album": "Either/Or",
                        "date": "1997-02-25",
                        "track": "1/12",
                    }
                ),
            )

            self.assertEqual(track.title, "Speed Trials")
            self.assertEqual(track.artist, "Elliott Smith")
            self.assertEqual(track.album, "Either/Or")
            self.assertEqual(track.year, "1997")
            self.assertEqual(track.track, 1)

    def test_attached_picture_stream_does_not_overwrite_tags(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / "01 - Helena (So Long & Goodnight).flac"
            path.write_bytes(b"audio")
            metadata = {
                "format": {
                    "tags": {
                        "title": "Helena (So Long & Goodnight)",
                        "artist": "My Chemical Romance",
                        "album": "Thre Cheers for Sweet Revenge", 
                        "date": "2004",
                        "track": "1",
                    }
                },
                "streams": [
                    {"codec_type": "audio", "tags": {}},
                    {
                        "codec_type": "video",
                        "disposition": {"attached_pic": 1},
                        "tags": {"title": "cover.jpg", "comment": "Cover (front)"},
                    },
                ],
            }

            track = track_from_probe(path, root, metadata)

            self.assertEqual(track.title, "Helena (So Long & Goodnight)")
            self.assertTrue(track.has_embedded_art)

    def test_attached_picture_title_does_not_replace_filename_fallback(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / "01 - Black Summer.mp3"
            path.write_bytes(b"audio")
            metadata = {
                "format": {
                    "tags": {
                        "artist": "Red Hot Chili Peppers",
                        "album": "Unlimited Love",
                        "date": "2022",
                        "track": "1",
                    }
                },
                "streams": [
                    {"codec_type": "audio", "tags": {}},
                    {
                        "codec_type": "video",
                        "disposition": {"attached_pic": 1},
                        "tags": {"title": "PMEDIA"},
                    },
                ],
            }

            track = track_from_probe(path, root, metadata)

            self.assertEqual(track.title, "Black Summer")
            self.assertTrue(track.has_embedded_art)

    def test_numbered_disc_album_tags_are_normalised_inside_cd_folders(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = [root / "CD 1" / "01. 15 Step.flac", root / "CD 2" / "01. Mk 1.flac"]
            for path in paths:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(b"audio")
            tracks = [
                track_from_probe(
                    paths[0],
                    root,
                    probe({"artist": "Radiohead", "album": "In Rainbows (1)", "date": "2023", "track": "1", "disc": "1"}),
                ),
                track_from_probe(
                    paths[1],
                    root,
                    probe({"artist": "Radiohead", "album": "In Rainbows (2)", "date": "2023", "track": "1", "disc": "2"}),
                ),
            ]

            groups = group_tracks(tracks)
            assign_destinations(groups, multidisc=False, include_track_artist=False)

            self.assertEqual(len(groups), 1)
            self.assertEqual(groups[0].album, "In Rainbows")
            self.assertEqual(tracks[0].proposed_rel, "Radiohead/In Rainbows (2023)/CD 1/01 - 15 Step.flac")
            self.assertEqual(tracks[1].proposed_rel, "Radiohead/In Rainbows (2023)/CD 2/01 - Mk 1.flac")

    def test_filename_fallback_handles_multidisc_album(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "Elliott Smith"
            path = root / "New Moon" / "2-08 Either_Or.mp3"
            path.parent.mkdir(parents=True)
            path.write_bytes(b"audio")

            track = merge_audio_tags(path, root, probe({}))
            groups = group_tracks([track])
            groups[0].year = "2007"
            assign_destinations(groups, multidisc=True, include_track_artist=False)

            self.assertEqual(track.artist, "Elliott Smith")
            self.assertEqual(track.disc, 2)
            self.assertEqual(track.track, 8)
            self.assertEqual(track.title, "Either_Or")
            self.assertEqual(
                track.proposed_rel,
                "Elliott Smith/New Moon (2007)/CD2/08 - Either_Or.mp3",
            )

    def test_unsafe_characters_are_sanitized(self):
        self.assertEqual(sanitize_component('Bad: Name? * " <x>|'), "Bad - Name (x) -")
        self.assertEqual(sanitize_component("Either/Or"), "Either - Or")

    def test_multidisc_without_cd_folders_uses_disc_track_prefix(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "Elliott Smith"
            paths = [
                root / "New Moon" / "1-01 Angel In The Snow.mp3",
                root / "New Moon" / "2-01 Georgia, Georgia.mp3",
            ]
            for path in paths:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(b"audio")
            tracks = [merge_audio_tags(path, root, probe({})) for path in paths]
            groups = group_tracks(tracks)
            groups[0].year = "2006"
            assign_destinations(groups, multidisc=False, include_track_artist=False)

            self.assertEqual(
                tracks[0].proposed_rel,
                "Elliott Smith/New Moon (2006)/1-01 - Angel In The Snow.mp3",
            )
            self.assertEqual(
                tracks[1].proposed_rel,
                "Elliott Smith/New Moon (2006)/2-01 - Georgia, Georgia.mp3",
            )

    def test_duplicate_destinations_are_disambiguated(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = [
                root / "B-Sides & Other Songs" / "Basement Demos - Stickman.mp3",
                root / "B-Sides & Other Songs" / "Basement Demos - Stickman (Alternate Version).mp3",
            ]
            for path in paths:
                path.parent.mkdir(exist_ok=True)
                path.write_bytes(b"audio")
            tracks = [
                merge_audio_tags(paths[0], root, probe({"artist": "Elliott Smith", "album": "B-Sides & Other Songs", "track": "1", "title": "Stickman"})),
                merge_audio_tags(paths[1], root, probe({"artist": "Elliott Smith", "album": "B-Sides & Other Songs", "track": "1", "title": "Stickman"})),
            ]

            groups = group_tracks(tracks)
            groups[0].year = "2000"
            assign_destinations(groups, multidisc=False, include_track_artist=False)

            destinations = {track.proposed_rel for track in tracks}
            self.assertEqual(len(destinations), 2)
            self.assertIn("Elliott Smith/B-Sides & Other Songs (2000)/01 - Stickman.mp3", destinations)
            self.assertIn("Elliott Smith/B-Sides & Other Songs (2000)/01 - Stickman (2).mp3", destinations)

    def test_stage_import_copies_without_mutating_source_and_writes_manifest(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "source"
            stage = Path(tmp) / "stage"
            source = root / "XO" / "03 Waltz #2.m4a"
            source.parent.mkdir(parents=True)
            source.write_bytes(b"audio")
            track = merge_audio_tags(
                source,
                root,
                probe({"artist": "Elliott Smith", "album": "XO", "date": "1998", "track": "3", "title": "Waltz #2"}),
            )
            groups = group_tracks([track])

            manifest = stage_import(groups, stage, root, multidisc=False, include_track_artist=False)

            self.assertTrue(source.exists())
            self.assertTrue((stage / "Elliott Smith/XO (1998)/03 - Waltz #2.m4a").exists())
            manifest_path = stage / "Elliott Smith/XO (1998)/.library-import/IMPORT_MANIFEST.json"
            self.assertTrue(manifest_path.exists())
            self.assertEqual(manifest["copied_tracks"], 1)
            self.assertEqual(json.loads(manifest_path.read_text())["album"], "XO")

    def test_discover_album_dirs_handles_direct_and_cd_audio(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            direct = root / "Elliott Smith" / "XO (1998)"
            multidisc = root / "Elliott Smith" / "New Moon (2006)"
            direct.mkdir(parents=True)
            (multidisc / "CD1").mkdir(parents=True)
            (multidisc / "CD2").mkdir(parents=True)
            (direct / "01 - Sweet Adeline.m4a").write_bytes(b"audio")
            (multidisc / "CD1" / "01 - Angel In The Snow.mp3").write_bytes(b"audio")
            (multidisc / "CD2" / "01 - Georgia, Georgia.mp3").write_bytes(b"audio")

            albums = discover_album_dirs(root)

            self.assertEqual(albums, [multidisc, direct])

    def test_album_info_prefers_import_manifest_then_folder_name(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            album_dir = root / "Elliott Smith" / "XO (1998)"
            manifest_dir = album_dir / ".library-import"
            manifest_dir.mkdir(parents=True)
            (manifest_dir / "IMPORT_MANIFEST.json").write_text(
                json.dumps(
                    {
                        "album_artist": "Elliott Smith",
                        "album": "XO",
                        "year": "1998",
                        "musicbrainz_releaseid": "release-id",
                        "musicbrainz_releasegroupid": "group-id",
                    }
                ),
                encoding="utf-8",
            )

            self.assertEqual(
                album_info_from_dir(album_dir, root),
                {
                    "album_artist": "Elliott Smith",
                    "album": "XO",
                    "year": "1998",
                    "musicbrainz_releaseid": "release-id",
                    "musicbrainz_releasegroupid": "group-id",
                },
            )

            fallback_dir = root / "Elliott Smith" / "Figure 8 (2000)"
            fallback_dir.mkdir(parents=True)
            self.assertEqual(
                album_info_from_dir(fallback_dir, root),
                {
                    "album_artist": "Elliott Smith",
                    "album": "Figure 8",
                    "year": "2000",
                    "musicbrainz_releaseid": "",
                    "musicbrainz_releasegroupid": "",
                },
            )

    def test_edit_album_clears_stale_release_id_when_identity_changes(self):
        group = AlbumGroup(
            key="",
            album_artist="Elliott Smith",
            artist="Elliott Smith",
            album="Elliott Smith",
            year="2020",
            musicbrainz_releaseid="old-release",
            match_status="MusicBrainz candidate: old-release",
        )

        with patch("builtins.input", side_effect=["", "", "", "1995"]):
            edit_album(group)

        self.assertEqual(group.year, "1995")
        self.assertEqual(group.musicbrainz_releaseid, "")
        self.assertEqual(group.match_status, "metadata edited; release lookup needed")

    def test_find_musicbrainz_release_retries_without_bad_year(self):
        responses = [
            {"releases": []},
            {
                "releases": [
                    {
                        "id": "release-id",
                        "title": "New Moon",
                        "date": "2007",
                        "artist-credit": [{"name": "Elliott Smith"}],
                        "release-group": {"id": "group-id"},
                    }
                ]
            },
        ]

        with patch("lib.music_import_planner.musicbrainz_json", side_effect=responses), patch("lib.music_import_planner.wait_for_musicbrainz", return_value=1.0):
            release_id, group_id, status, _last_request = find_musicbrainz_release("Elliott Smith", "New Moon", "2006", 0.0)

        self.assertEqual(release_id, "release-id")
        self.assertEqual(group_id, "group-id")
        self.assertEqual(status, "MusicBrainz candidate after retry without folder year")

    def test_download_cover_falls_back_to_release_group(self):
        target = Path(tempfile.mkdtemp()) / "cover.jpg"

        def fake_coverart(entity, entity_id):
            if entity == "release":
                raise urllib.error.HTTPError("", 404, "NOT FOUND", {}, None)
            self.assertEqual((entity, entity_id), ("release-group", "group-id"))
            return b"jpg"

        with patch("lib.music_import_planner.coverart_bytes", side_effect=fake_coverart):
            result, _last_request = download_cover_jpg("release-id", "group-id", target, 0.0)

        self.assertEqual(result, "downloaded from release group")
        self.assertEqual(target.read_bytes(), b"jpg")

    def test_find_deezer_cover_url_uses_strong_album_artist_match(self):
        response = {
            "data": [
                {
                    "title": "Greatest Hits",
                    "artist": {"name": "Elliott Smith"},
                    "cover_xl": "https://example.test/wrong.jpg",
                },
                {
                    "title": "Roman Candle",
                    "artist": {"name": "Elliott Smith"},
                    "cover_xl": "https://example.test/right.jpg",
                },
            ]
        }

        with patch("lib.music_import_planner.deezer_json", return_value=response):
            url, status = find_deezer_cover_url("Elliott Smith", "Roman Candle")

        self.assertEqual(url, "https://example.test/right.jpg")
        self.assertEqual(status, "Deezer: Elliott Smith - Roman Candle")

    def test_find_deezer_cover_url_rejects_weak_match(self):
        response = {
            "data": [
                {
                    "title": "Greatest Hits",
                    "artist": {"name": "Someone Else"},
                    "cover_xl": "https://example.test/wrong.jpg",
                }
            ]
        }

        with patch("lib.music_import_planner.deezer_json", return_value=response):
            url, status = find_deezer_cover_url("Elliott Smith", "Roman Candle")

        self.assertEqual(url, "")
        self.assertEqual(status, "weak Deezer match ignored")

    def test_download_deezer_cover_writes_cover_file(self):
        target = Path(tempfile.mkdtemp()) / "cover.jpg"

        with patch("lib.music_import_planner.find_deezer_cover_url", return_value=("https://example.test/cover.jpg", "Deezer: Artist - Album")), patch(
            "lib.music_import_planner.download_url_bytes", return_value=b"jpg"
        ):
            result = download_deezer_cover("Artist", "Album", target)

        self.assertEqual(result, "downloaded from Deezer: Artist - Album")
        self.assertEqual(target.read_bytes(), b"jpg")

    def test_cover_job_tries_deezer_when_musicbrainz_has_no_match(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            album_dir = root / "Elliott Smith" / "Roman Candle (1994)"
            album_dir.mkdir(parents=True)
            (album_dir / "01 - Roman Candle.mp3").write_bytes(b"audio")

            args = type(
                "Args",
                (),
                {
                    "library_root": str(root),
                    "overwrite": "no",
                    "delay": 0.0,
                },
            )()

            with patch("lib.music_import_planner.find_musicbrainz_release", return_value=("", "", "no MusicBrainz match", 0.0)), patch(
                "lib.music_import_planner.download_deezer_cover", return_value="downloaded from Deezer: Elliott Smith - Roman Candle"
            ) as deezer:
                status = cmd_covers(args)

        self.assertEqual(status, 0)
        deezer.assert_called_once()


if __name__ == "__main__":
    unittest.main()
