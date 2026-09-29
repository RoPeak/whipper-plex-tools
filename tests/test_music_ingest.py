import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from lib import music_ingest


class MusicIngestConfigTests(unittest.TestCase):
    def test_path_overrides_work_before_or_after_subcommand(self):
        parser = music_ingest.build_parser()
        before = parser.parse_args(["--library", "/tmp/before", "digital"])
        after = parser.parse_args(["digital", "--library", "/tmp/after"])
        self.assertEqual(str(before.library), "/tmp/before")
        self.assertEqual(str(after.library), "/tmp/after")

    def test_defaults_are_safe(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = music_ingest.load_config(Path(tmp) / "missing.toml")
        self.assertEqual(config["default_mode"], "dry-run")
        self.assertEqual(config["publication"], "copy")
        self.assertIs(config["preserve_source"], True)

    def test_config_overrides_defaults_and_expands_paths(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.toml"
            path.write_text('incoming = "~/incoming"\nlibrary = "/library"\nstaging = "/stage"\n')
            config = music_ingest.load_config(path)
        self.assertEqual(config["incoming"], str(Path.home() / "incoming"))
        self.assertEqual(config["library"], "/library")

    def test_unknown_keys_and_unsafe_move_policy_are_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "bad.toml"
            path.write_text('unexpected = true\n')
            with self.assertRaisesRegex(ValueError, "unknown config"):
                music_ingest.load_config(path)
            path.write_text('publication = "move"\n')
            with self.assertRaisesRegex(ValueError, "only publication"):
                music_ingest.load_config(path)

    def test_digital_preflight_refuses_missing_library_without_creating_it(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            incoming = root / "incoming"
            library = root / "missing-library"
            incoming.mkdir()
            cfg = {**music_ingest.DEFAULTS, "incoming": str(incoming), "library": str(library), "staging": str(root / "stage")}
            self.assertEqual(music_ingest.run_action("digital", cfg), 2)
            self.assertFalse(library.exists())

    def test_digital_action_passes_effective_policy_to_wizard(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            incoming, library, staging = root / "incoming", root / "library", root / "stage"
            incoming.mkdir()
            library.mkdir()
            cfg = {**music_ingest.DEFAULTS, "incoming": str(incoming), "library": str(library), "staging": str(staging), "musicbrainz_lookup": False}
            with patch("lib.music_ingest.subprocess.run") as run:
                run.return_value.returncode = 0
                self.assertEqual(music_ingest.run_action("digital", cfg, "apply"), 0)
            env = run.call_args.kwargs["env"]
            self.assertEqual(env["MUSIC_INGEST_MODE"], "apply")
            self.assertEqual(env["MUSIC_INGEST_ACTION"], "digital")
            self.assertEqual(env["MUSIC_INGEST_LOOKUP"], "no")
            self.assertTrue(staging.is_dir())

    def test_cd_action_explains_missing_whipper(self):
        cfg = dict(music_ingest.DEFAULTS)
        with patch.object(music_ingest.shutil, "which", return_value=None):
            self.assertEqual(music_ingest.run_action("cd", cfg), 2)

    def test_cd_action_explains_missing_optical_drive(self):
        cfg = dict(music_ingest.DEFAULTS)
        with patch.object(music_ingest.shutil, "which", return_value="/usr/bin/whipper"), patch.object(
            music_ingest.Path, "exists", return_value=False
        ), patch.object(music_ingest.Path, "glob", return_value=[]):
            self.assertEqual(music_ingest.run_action("cd", cfg), 2)

    def test_bare_wizard_cancellation_is_clean(self):
        with patch("builtins.input", side_effect=EOFError):
            self.assertEqual(music_ingest.wizard(dict(music_ingest.DEFAULTS)), 0)


if __name__ == "__main__":
    unittest.main()
