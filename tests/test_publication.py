import os
import subprocess
import tempfile
import unittest
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[1]
PUBLISHER = PROJECT / "lib" / "publish.sh"


class PublicationApprovalTests(unittest.TestCase):
    def approve(self, stage, library, answer):
        source = stage / "Artist" / "Album" / "01 - Track.flac"
        return subprocess.run(
            ["bash", "-c", 'source "$1"; shift; confirm_publish_plan "$@"', "test", str(PUBLISHER), str(stage), str(library), "CD rip", str(source)],
            input=answer,
            check=False,
            capture_output=True,
            text=True,
        )

    def test_cd_plan_requires_typed_approval(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            stage, library = root / "stage", root / "library"
            result = self.approve(stage, library, "APPLY\n")
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("CD rip publication plan", result.stdout)
            self.assertIn("01 - Track.flac", result.stdout)
            self.assertIn(str(library / "Artist/Album/01 - Track.flac"), result.stdout)

    def test_cd_decline_is_clean_and_does_not_publish(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            stage, library = root / "stage", root / "library"
            result = self.approve(stage, library, "no\n")
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("Publication cancelled", result.stdout)
            self.assertFalse(library.exists())


class AtomicPublicationTests(unittest.TestCase):
    def publish(self, source, target, env=None):
        return subprocess.run(
            ["bash", "-c", 'source "$1"; publish_file_atomic "$2" "$3"', "test", str(PUBLISHER), str(source), str(target)],
            check=False,
            capture_output=True,
            text=True,
            env=env,
        )

    def test_complete_copy_appears_and_source_is_preserved(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source.mp3"
            target = root / "library" / "album" / "01.mp3"
            source.write_bytes(b"synthetic lossy audio bytes")
            result = self.publish(source, target)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(target.read_bytes(), source.read_bytes())
            self.assertTrue(source.exists())
            self.assertEqual(target.suffix, ".mp3")
            self.assertEqual(list(target.parent.glob(".music-ingest-publish-*.tmp")), [])

    def test_existing_destination_is_never_overwritten(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source"
            target = root / "target"
            source.write_bytes(b"new")
            target.write_bytes(b"original")
            result = self.publish(source, target)
            self.assertNotEqual(result.returncode, 0)
            self.assertEqual(target.read_bytes(), b"original")
            self.assertTrue(source.exists())

    def test_failed_copy_leaves_no_partial_final_file_or_temp(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source"
            target = root / "target"
            shim = root / "shim"
            shim.mkdir()
            source.write_bytes(b"audio")
            cp = shim / "cp"
            cp.write_text("#!/bin/sh\nexit 1\n")
            cp.chmod(0o755)
            env = os.environ.copy()
            env["PATH"] = str(shim) + os.pathsep + env["PATH"]
            result = self.publish(source, target, env)
            self.assertNotEqual(result.returncode, 0)
            self.assertFalse(target.exists())
            self.assertEqual(list(root.glob(".music-ingest-publish-*.tmp")), [])
            self.assertTrue(source.exists())


if __name__ == "__main__":
    unittest.main()
