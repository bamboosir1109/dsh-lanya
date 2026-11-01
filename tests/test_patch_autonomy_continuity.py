"""Real historical layer transitions on synthetic temporary packages only."""
import importlib.util
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import patch_autonomy_continuity as continuity
from patch_onebot_policy import PatchError

spec = importlib.util.spec_from_file_location("continuity_parent_fixture", Path(__file__).with_name("test_patch_onebot_autonomy.py"))
fixture = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fixture)


class ContinuityPatchTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        fixture.AutonomyPatchTests.setUpClass()
        cls.addClassCleanup(fixture.AutonomyPatchTests.doClassCleanups)

    def setUp(self):
        self.parent = fixture.AutonomyPatchTests()
        self.parent.setUp()
        self.addCleanup(self.parent.doCleanups)
        self.parent.apply()
        self.plugin = self.parent.plugin
        self.backups = self.parent.root / "continuity"
        self.backups.mkdir()
        self.originals = {name: (self.plugin / name).read_bytes() for name in continuity.TARGETS}

    def preview(self):
        return continuity.patch(self.plugin, ROOT)

    def apply(self):
        return continuity.patch(self.plugin, ROOT, apply=True, host_stopped=True,
                                expected_fingerprint=self.preview()["fingerprint"], backup_dir=self.backups)

    def test_preview_apply_noop_restore_preserves_policy_and_parent_markers(self):
        preview = self.preview()
        self.assertEqual(preview["status"], "patch_needed")
        self.assertEqual({n: (self.plugin / n).read_bytes() for n in continuity.TARGETS}, self.originals)
        result = self.apply()
        self.assertEqual(result["status"], "patched")
        self.assertEqual(self.preview()["status"], "already_patched")
        self.assertEqual(self.apply()["status"], "already_patched")
        view = continuity.restore(self.plugin, result["manifest"])
        restored = continuity.restore(self.plugin, result["manifest"], apply=True, host_stopped=True,
                                       expected_fingerprint=view["fingerprint"])
        self.assertEqual(restored["status"], "restored")
        self.assertEqual({n: (self.plugin / n).read_bytes() for n in continuity.TARGETS}, self.originals)
        self.assertEqual(self.parent.sentinel.read_bytes(), b'{"synthetic_policy":"never-open-or-change"}\n')

    def test_application_requires_stopped_host_and_unchanged_preview(self):
        with self.assertRaises(PatchError):
            continuity.patch(self.plugin, ROOT, apply=True, expected_fingerprint=self.preview()["fingerprint"], backup_dir=self.backups)
        with self.assertRaises(PatchError):
            continuity.patch(self.plugin, ROOT, apply=True, host_stopped=True, expected_fingerprint="changed", backup_dir=self.backups)
        self.assertEqual({n: (self.plugin / n).read_bytes() for n in continuity.TARGETS}, self.originals)

    def test_restore_refuses_unknown_later_change_before_any_replacement(self):
        result = self.apply()
        (self.plugin / continuity.TARGETS[1]).write_bytes(b"// unknown later modification\n")
        adapter = (self.plugin / continuity.TARGETS[0]).read_bytes()
        with self.assertRaises(PatchError):
            continuity.restore(self.plugin, result["manifest"])
        self.assertEqual((self.plugin / continuity.TARGETS[0]).read_bytes(), adapter)

    def test_parent_evidence_drift_is_refused(self):
        result = self.apply()
        marker = self.plugin / "lib/lanya-policy.patch.json"
        marker.write_bytes(marker.read_bytes() + b" ")
        with self.assertRaises(PatchError):
            continuity.inspect(self.plugin, manifest=result["manifest"])


if __name__ == "__main__":
    unittest.main()
