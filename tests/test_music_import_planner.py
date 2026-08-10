import json
import tempfile
import unittest
from pathlib import Path

from lib.music_import_planner import (
    assign_destinations,
    group_tracks,
    sanitize_component,
    stage_import,
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

            track = track_from_probe(
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

    def test_filename_fallback_handles_multidisc_album(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "Elliott Smith"
            path = root / "New Moon" / "2-08 Either_Or.mp3"
            path.parent.mkdir(parents=True)
            path.write_bytes(b"audio")

            track = track_from_probe(path, root, probe({}))
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
            tracks = [track_from_probe(path, root, probe({})) for path in paths]
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
                track_from_probe(paths[0], root, probe({"artist": "Elliott Smith", "album": "B-Sides & Other Songs", "track": "1", "title": "Stickman"})),
                track_from_probe(paths[1], root, probe({"artist": "Elliott Smith", "album": "B-Sides & Other Songs", "track": "1", "title": "Stickman"})),
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
            track = track_from_probe(
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


if __name__ == "__main__":
    unittest.main()
