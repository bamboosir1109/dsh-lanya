"""Offline replay/backup tests against a synthetic plugin, never installation."""
import hashlib
import importlib.util
from contextlib import redirect_stdout
import io
import json
import os
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("patch_onebot_policy", ROOT / "scripts" / "patch_onebot_policy.py")
patcher = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(patcher)


class OneBotPolicyPatchTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="lanya-onebot-patch-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.plugin = self.root / "plugin"
        shutil.copytree(ROOT / "tests" / "fixtures" / "onebot_policy", self.plugin)
        self.state_file = self.root / "private" / "decision-policy.json"

    def tree(self):
        return {p.relative_to(self.plugin).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
                for p in self.plugin.rglob("*") if p.is_file()}

    def preview(self):
        return patcher.patch(self.plugin, state_file=self.state_file)

    def apply(self, preview=None, **extra):
        return patcher.patch(self.plugin, state_file=self.state_file, apply=True, host_stopped=True,
                             backup_dir=self.root / "backups", expected_fingerprint=(preview or self.preview())["fingerprint"], **extra)

    def test_preview_has_no_filesystem_side_effects(self):
        before = self.tree()
        result = self.preview()
        self.assertEqual(result["status"], "patch_needed")
        self.assertFalse(result["applied"])
        self.assertEqual(self.tree(), before)
        self.assertFalse(self.state_file.exists())
        self.assertFalse((self.root / "backups").exists())

    def test_state_path_is_private_explicit_json_and_outside_package(self):
        before = self.tree()
        for file in [Path("relative.json"), self.root / "policy.txt", self.plugin / "policy.json"]:
            with self.subTest(path=str(file)), self.assertRaises(patcher.PatchError):
                patcher.patch(self.plugin, state_file=file)
        directory = self.root / "directory.json"
        directory.mkdir()
        with self.assertRaises(patcher.PatchError):
            patcher.patch(self.plugin, state_file=directory)
        self.assertEqual(self.tree(), before)

    def test_apply_verifies_backup_and_is_replayable(self):
        before = {name: (self.plugin / "lib" / name).read_bytes() for name in patcher.TARGETS}
        result = self.apply()
        self.assertTrue(result["applied"])
        backup = Path(result["backup"])
        for name, original in before.items():
            self.assertEqual((backup / name).read_bytes(), original)
            self.assertEqual(hashlib.sha256(original).hexdigest(), result["before"][name])
        configuration = json.loads((self.plugin / "lib" / "lanya-policy.config.json").read_text())
        self.assertEqual(configuration, {"stateFile": str(self.state_file)})
        self.assertFalse(self.state_file.exists())
        inbound = (self.plugin / "lib" / "inbound.js").read_text()
        self.assertNotIn("SYNTHETIC_CROSS_CHAT", inbound)
        self.assertNotIn("personPrefix + crossPrefix", inbound)
        self.assertIn("observeInbound", inbound)
        self.assertIn("qq_decide", (self.plugin / "lib" / "tools.js").read_text())
        replay = self.preview()
        self.assertEqual(replay["status"], "already_patched")
        tree = self.tree()
        self.assertFalse(self.apply(preview=replay)["applied"])
        self.assertEqual(self.tree(), tree)

    def test_unchanged_preview_and_stopped_host_are_mandatory(self):
        before = self.tree()
        preview = self.preview()
        for kwargs in [{}, {"host_stopped": True}, {"host_stopped": True, "backup_dir": self.root / "backups", "expected_fingerprint": "bad"}]:
            with self.subTest(kwargs=list(kwargs)), self.assertRaises(patcher.PatchError):
                patcher.patch(self.plugin, state_file=self.state_file, apply=True, **kwargs)
        self.assertEqual(self.tree(), before)
        self.assertFalse((self.root / "backups").exists())
        self.assertTrue(preview["fingerprint"])

    def test_unknown_shape_and_existing_owned_file_refuse_before_writes(self):
        bridge = self.plugin / "lib" / "bridge.js"
        original = bridge.read_bytes()
        bridge.write_bytes(original + b"\n// unreviewed\n")
        with self.assertRaisesRegex(patcher.PatchError, "Unknown reviewed"):
            self.preview()
        bridge.write_bytes(original)
        owned = self.plugin / "lib" / "lanya-policy.config.json"
        owned.write_text("existing local config")
        with self.assertRaisesRegex(patcher.PatchError, "already exists"):
            self.preview()
        self.assertEqual(owned.read_text(), "existing local config")

    def test_changed_preview_or_deployed_module_refuses(self):
        preview = self.preview()
        self.state_file = self.root / "other-state.json"
        with self.assertRaisesRegex(patcher.PatchError, "unchanged preview"):
            self.apply(preview=preview)
        self.apply()
        module = self.plugin / "lib" / "lanya-decision-policy.js"
        module.write_bytes(module.read_bytes() + b"\n// modified\n")
        with self.assertRaisesRegex(patcher.PatchError, "explicit migration"):
            self.preview()

    def test_syntax_failure_leaves_plugin_untouched(self):
        before = self.tree()
        original = patcher.transform
        with mock.patch.object(patcher, "transform", side_effect=lambda name, raw: b"export {" if name == "bridge.js" else original(name, raw)):
            preview = self.preview()
            with self.assertRaisesRegex(patcher.PatchError, "syntax check failed"):
                self.apply(preview=preview)
        self.assertEqual(self.tree(), before)
        self.assertFalse(self.state_file.exists())
        self.assertEqual(len(list((self.root / "backups").glob("*/interrupted.json"))), 1)

    def restore(self, manifest, *, apply=False, fingerprint=None, host_stopped=True):
        return patcher.restore(self.plugin, manifest, apply=apply,
                               host_stopped=host_stopped, expected_fingerprint=fingerprint)

    def test_restore_previews_without_writes_and_restores_complete_patch(self):
        before = self.tree()
        patched = self.apply()
        backup = Path(patched["backup"])
        manifest = backup / "patch.json"
        originals = {p.name: p.read_bytes() for p in backup.iterdir() if p.is_file()}
        self.state_file.parent.mkdir()
        self.state_file.write_text('{"fixture_silent":true}', encoding="utf-8")
        state = self.state_file.read_bytes()
        installed = self.tree()
        preview = self.restore(manifest)
        self.assertFalse(preview["applied"])
        self.assertEqual(self.tree(), installed)
        self.assertEqual(set(preview["restore"]), set(patcher.TARGETS))
        self.assertEqual(set(preview["remove"]), set(patcher.GENERATED) | {"lanya-policy.patch.json"})
        restored = self.restore(manifest, apply=True, fingerprint=preview["fingerprint"])
        self.assertEqual(restored["status"], "restored")
        self.assertEqual(self.tree(), before)
        self.assertEqual(self.state_file.read_bytes(), state)
        self.assertEqual({p.name: p.read_bytes() for p in backup.iterdir() if p.is_file()}, originals)
        self.assertEqual(self.restore(manifest)["status"], "already_restored")

    def test_restore_partial_apply_failure_uses_private_originals(self):
        before = self.tree()
        real_replace = patcher.os.replace
        def fail_second_original(source, target):
            if Path(target) == self.plugin / "lib" / "inbound.js":
                raise OSError("fixture apply interruption")
            return real_replace(source, target)
        with mock.patch.object(patcher.os, "replace", side_effect=fail_second_original):
            with self.assertRaisesRegex(OSError, "interruption"):
                self.apply()
        manifest = next((self.root / "backups").glob("*/patch.json"))
        self.assertFalse((self.plugin / "lib" / "lanya-policy.patch.json").exists())
        preview = self.restore(manifest)
        self.assertEqual(preview["restore"], ["bridge.js"])
        self.restore(manifest, apply=True, fingerprint=preview["fingerprint"])
        self.assertEqual(self.tree(), before)

    def test_interrupted_restore_can_resume_only_exact_before_after_states(self):
        before = self.tree()
        manifest = Path(self.apply()["backup"]) / "patch.json"
        preview = self.restore(manifest)
        real_replace = patcher.os.replace
        def fail_second_original(source, target):
            if Path(target) == self.plugin / "lib" / "inbound.js":
                raise OSError("fixture restore interruption")
            return real_replace(source, target)
        with mock.patch.object(patcher.os, "replace", side_effect=fail_second_original):
            with self.assertRaisesRegex(OSError, "interruption"):
                self.restore(manifest, apply=True, fingerprint=preview["fingerprint"])
        again = self.restore(manifest)
        self.assertNotIn("bridge.js", again["restore"])
        self.assertFalse(list((self.plugin / "lib").glob(".lanya-restore-*")))
        self.restore(manifest, apply=True, fingerprint=again["fingerprint"])
        self.assertEqual(self.tree(), before)

    def test_any_later_original_generated_marker_or_package_change_refuses_entire_batch(self):
        manifest = Path(self.apply()["backup"]) / "patch.json"
        for target in (self.plugin / "lib" / "prompt.js",
                       self.plugin / "lib" / "lanya-policy-adapter.js",
                       self.plugin / "lib" / "lanya-policy.patch.json",
                       self.plugin / "package.json"):
            with self.subTest(name=target.name):
                saved = target.read_bytes()
                target.write_bytes(saved + b"\n ")
                before = self.tree()
                with self.assertRaises(patcher.PatchError):
                    self.restore(manifest)
                self.assertEqual(self.tree(), before)
                target.write_bytes(saved)

    def test_restore_refuses_damaged_originals_or_mismatched_manifest_paths(self):
        manifest = Path(self.apply()["backup"]) / "patch.json"
        saved = manifest.read_bytes()
        record = json.loads(saved)
        original = manifest.parent / "bridge.js"
        original_bytes = original.read_bytes()
        original.write_bytes(original_bytes + b"\n")
        with self.assertRaisesRegex(patcher.PatchError, "backup hash"):
            self.restore(manifest)
        original.write_bytes(original_bytes)
        for changes in ({"patch": "unknown"}, {"plugin": str(self.root / "another-package")},
                        {"backup": str(self.root)}, {"before": {}}, {"after": {}}):
            with self.subTest(keys=list(changes)):
                manifest.write_text(json.dumps(dict(record, **changes)), encoding="utf-8")
                before = self.tree()
                with self.assertRaises(patcher.PatchError):
                    self.restore(manifest)
                self.assertEqual(self.tree(), before)
        manifest.write_bytes(saved)

    def test_restore_apply_needs_stopped_host_and_fresh_whole_batch_fingerprint(self):
        manifest = Path(self.apply()["backup"]) / "patch.json"
        preview = self.restore(manifest)
        before = self.tree()
        for kwargs in (dict(), dict(fingerprint="stale"), dict(fingerprint=preview["fingerprint"], host_stopped=False)):
            with self.assertRaises(patcher.PatchError):
                self.restore(manifest, apply=True, **kwargs)
            self.assertEqual(self.tree(), before)
        # This is a known original, but it still changes the reviewed batch.
        (self.plugin / "lib" / "bridge.js").write_bytes((manifest.parent / "bridge.js").read_bytes())
        before = self.tree()
        with self.assertRaisesRegex(patcher.PatchError, "unchanged preview"):
            self.restore(manifest, apply=True, fingerprint=preview["fingerprint"])
        self.assertEqual(self.tree(), before)

    def test_restore_replaces_hardlinks_without_changing_their_shared_sources(self):
        before = self.tree()
        manifest = Path(self.apply()["backup"]) / "patch.json"
        target = self.plugin / "lib" / "bridge.js"
        generated = self.plugin / "lib" / "lanya-policy-adapter.js"
        shared, shared_generated = self.root / "shared-installed.js", self.root / "shared-adapter.js"
        try:
            os.link(target, shared)
            os.link(generated, shared_generated)
        except OSError as error:
            self.skipTest("Hardlinks unavailable: " + type(error).__name__)
        expected, expected_generated = shared.read_bytes(), shared_generated.read_bytes()
        preview = self.restore(manifest)
        self.restore(manifest, apply=True, fingerprint=preview["fingerprint"])
        self.assertEqual(self.tree(), before)
        self.assertEqual(shared.read_bytes(), expected)
        self.assertEqual(shared_generated.read_bytes(), expected_generated)
        self.assertFalse(os.path.samefile(target, shared))
        self.assertFalse(generated.exists())

    def test_restore_cli_uses_explicit_mode_and_preview_does_not_mutate(self):
        manifest = Path(self.apply()["backup"]) / "patch.json"
        before = self.tree()
        output = io.StringIO()
        with redirect_stdout(output):
            status = patcher.main(["--plugin-dir", str(self.plugin), "--restore-manifest", str(manifest)])
        self.assertEqual(status, 0)
        result = json.loads(output.getvalue())
        self.assertFalse(result["applied"])
        self.assertEqual(self.tree(), before)

    def test_backup_directory_inside_public_repository_is_refused_before_writes(self):
        preview = self.preview()
        before = self.tree()
        with self.assertRaisesRegex(patcher.PatchError, "source repository"):
            patcher.patch(self.plugin, state_file=self.state_file, apply=True, host_stopped=True,
                          expected_fingerprint=preview["fingerprint"], backup_dir=ROOT / "private-backup-forbidden")
        self.assertEqual(self.tree(), before)


if __name__ == "__main__":
    unittest.main()
