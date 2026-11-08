"""Replay the one-line patch and production splitter with isolated public fixtures."""
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
TOOL = ROOT / "scripts" / "patch_onebot_burst.py"
FIXTURE = Path(__file__).parent / "fixtures" / "onebot_burst_0_8_1.js"
SPEC = importlib.util.spec_from_file_location("onebot_burst_under_test", TOOL)
PATCHER = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(PATCHER)
NODE = shutil.which("node")

NODE_REPLAY = r"""
const fs = require('node:fs');
const vm = require('node:vm');
const source = fs.readFileSync(process.argv[1], 'utf8');
const inputs = JSON.parse(fs.readFileSync(0, 'utf8'));
const box = {};
vm.runInNewContext(source + '\nglobalThis.api={splitForChat,mergeOverflow,MAX_PIECES,SegmentFixture};', box);
(async () => {
  const split = inputs.map(text => ({text, pieces:box.api.splitForChat(text)}));
  const segments=[{type:'image',data:{file:'file:///offline-meme.png'}},
                  {type:'text',data:{text:'image caption'}}];
  const sink=[];
  const pipeline=new box.api.SegmentFixture();
  pipeline.enqueue=(_chat,work)=>work();
  pipeline.writeGateRejectReason=()=>null;
  pipeline.auditWrite=()=>{};
  pipeline.sendMsg=async (_chat,actual)=>{sink.push(actual);return 'offline-id'};
  const id=await pipeline.sendSegments('group:offline',segments);
  console.log(JSON.stringify({split,max:box.api.MAX_PIECES,image:{id,sink,identical:sink[0]===segments}}));
})().catch(error=>{console.error(error);process.exitCode=1});
"""


class BurstPatchTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="onebot burst isolated ")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.plugin = self.root / "customized-plugin"
        (self.plugin / "lib").mkdir(parents=True)
        self.target = self.plugin / "lib" / "outbound.js"
        self.original = b"// User customization outside splitter must survive.\n" + FIXTURE.read_bytes()
        self.target.write_bytes(self.original)
        self.package = self.plugin / "package.json"
        self.package.write_text(json.dumps({"name": "dsh-onebot-qq", "version": "0.8.1", "type": "module"}))
        self.backups = self.root / "explicit-private-backups"

    def apply(self, sha=None):
        return PATCHER.patch(self.plugin, apply=True, expected_sha256=sha or PATCHER.sha256(self.original),
                             backup_dir=self.backups)

    def test_default_preview_writes_nothing(self):
        before = set(self.root.rglob("*"))
        result = PATCHER.patch(self.plugin)
        self.assertEqual(result["status"], "patch_needed")
        self.assertFalse(result["applied"])
        self.assertEqual(result["original_sha256"], hashlib.sha256(self.original).hexdigest())
        self.assertEqual(set(self.root.rglob("*")), before)
        self.assertEqual(self.target.read_bytes(), self.original)

    def test_cli_requires_explicit_plugin_and_apply_review_parameters(self):
        result = subprocess.run([sys.executable, "-B", str(TOOL)], capture_output=True, text=True)
        self.assertNotEqual(result.returncode, 0)
        for kwargs in ({}, {"expected_sha256": "0" * 64}, {"backup_dir": self.backups}):
            with self.subTest(kwargs=tuple(kwargs)):
                with self.assertRaises(PATCHER.PatchError):
                    PATCHER.patch(self.plugin, apply=True, **kwargs)
        self.assertFalse(self.backups.exists())
        self.assertEqual(self.target.read_bytes(), self.original)

    def test_stale_preview_sha_refuses_before_backup(self):
        self.target.write_bytes(b"// New local customization.\n" + self.original)
        with self.assertRaisesRegex(PATCHER.PatchError, "SHA mismatch"):
            self.apply()
        self.assertFalse(self.backups.exists())
        self.assertTrue(self.target.read_bytes().startswith(b"// New local customization."))

    @unittest.skipUnless(NODE, "Node required for syntax check")
    def test_package_manager_hardlinks_are_not_modified_in_place(self):
        package_peer = self.root / "package-store.json"
        source_peer = self.root / "outbound-store.js"
        os.link(self.package, package_peer)
        os.link(self.target, source_peer)
        package_original = package_peer.read_bytes()
        self.apply()
        self.assertEqual(source_peer.read_bytes(), self.original)
        self.assertEqual(package_peer.read_bytes(), package_original)
        self.assertEqual(self.target.read_bytes(), self.original.replace(PATCHER.OLD, PATCHER.NEW, 1))

    def test_unknown_shape_or_version_refuses(self):
        self.target.write_bytes(self.original.replace(b"const HARD = 26;", b"const HARD = 27;"))
        with self.assertRaisesRegex(PATCHER.PatchError, "Unknown splitter shape"):
            PATCHER.patch(self.plugin)
        self.target.write_bytes(self.original)
        self.package.write_text(json.dumps({"name": "dsh-onebot-qq", "version": "0.8.2", "type": "module"}))
        with self.assertRaisesRegex(PATCHER.PatchError, "0.8.1"):
            PATCHER.patch(self.plugin)

    @unittest.skipUnless(NODE, "Node required for syntax check")
    def test_apply_saves_verified_original_and_changes_only_reviewed_line(self):
        result = self.apply()
        backup = Path(result["backup"])
        self.assertEqual((backup / "outbound.js").read_bytes(), self.original)
        self.assertEqual((backup / "outbound.js.sha256").read_text().split()[0], PATCHER.sha256(self.original))
        metadata = json.loads((backup / "patch.json").read_text())
        self.assertEqual(metadata["original_sha256"], PATCHER.sha256(self.original))
        self.assertEqual(self.target.read_bytes(), self.original.replace(PATCHER.OLD, PATCHER.NEW, 1))
        self.assertTrue(result["applied"])
        before = set(self.backups.rglob("*"))
        again = self.apply(result["patched_sha256"])
        self.assertEqual(again["status"], "already_patched")
        self.assertFalse(again["applied"])
        self.assertEqual(set(self.backups.rglob("*")), before)

    @unittest.skipUnless(NODE, "Node required for syntax check")
    def test_crlf_and_utf8_bom_are_preserved(self):
        self.original = b"\xef\xbb\xbf" + self.original.replace(b"\n", b"\r\n")
        self.target.write_bytes(self.original)
        self.apply()
        self.assertEqual(self.target.read_bytes(), self.original.replace(PATCHER.OLD, PATCHER.NEW, 1))

    def test_failed_backup_does_not_replace_original(self):
        with mock.patch.object(PATCHER, "write_new", side_effect=OSError("offline disk failure")):
            with self.assertRaises(OSError):
                self.apply()
        self.assertEqual(self.target.read_bytes(), self.original)

    @unittest.skipUnless(NODE, "Node required for syntax check")
    def test_atomic_replace_failure_keeps_original_and_backup(self):
        with mock.patch.object(PATCHER.os, "replace", side_effect=OSError("offline replace failure")):
            with self.assertRaises(OSError):
                self.apply()
        self.assertEqual(self.target.read_bytes(), self.original)
        self.assertEqual(len(list(self.backups.glob("*/outbound.js"))), 1)
        self.assertEqual(list(self.target.parent.glob(".outbound-burst-*")), [])

    @unittest.skipUnless(NODE, "Node required for syntax check")
    def test_target_changed_during_backup_is_not_clobbered(self):
        original_backup = PATCHER.backup_original
        replacement = b"// concurrent local customization.\n" + self.original

        def changed(*args):
            backup = original_backup(*args)
            self.target.write_bytes(replacement)
            return backup

        with mock.patch.object(PATCHER, "backup_original", side_effect=changed):
            with self.assertRaisesRegex(PATCHER.PatchError, "Target changed"):
                self.apply()
        self.assertEqual(self.target.read_bytes(), replacement)

    def test_link_target_is_refused(self):
        linked = self.root / "linked-plugin"
        try:
            os.symlink(self.plugin, linked, target_is_directory=True)
        except (OSError, NotImplementedError):
            if os.name != "nt":
                self.skipTest("Link creation not available")
            # Directory junctions do not require the symbolic-link privilege.
            made = subprocess.run(["cmd.exe", "/c", "mklink", "/J", str(linked), str(self.plugin)],
                                  capture_output=True, timeout=10)
            if made.returncode:
                self.skipTest("Junction creation not available")
        try:
            with self.assertRaisesRegex(PATCHER.PatchError, "Link/reparse"):
                PATCHER.patch(linked)
        finally:
            # Both link and target were created under this isolated temp root.
            # Remove only the link; never recursively delete through it.
            if os.name == "nt" and not linked.is_symlink():
                linked.rmdir()
            else:
                linked.unlink()
        self.assertTrue(self.plugin.is_dir())

    @unittest.skipUnless(NODE, "Node required for real splitter replay")
    def test_production_splitter_caps_lines_and_preserves_content_and_segments(self):
        inputs = ["one\ntwo\nthree\nfour", "\n".join("line%02d" % n for n in range(80)),
                  "a" * 300, "甲乙丙丁，" * 70, "one\ntwo\nthree", "short",
                  "\r\nfirst\r\n\r\nsecond\r\nthird\r\nfourth\r\n"]

        def replay():
            run = subprocess.run([NODE, "-e", NODE_REPLAY, str(self.target)], input=json.dumps(inputs),
                                 capture_output=True, text=True, encoding="utf-8", timeout=10)
            self.assertEqual(run.returncode, 0, run.stderr)
            return json.loads(run.stdout)

        before = replay()
        self.assertEqual(len(before["split"][0]["pieces"]), 4)  # Demonstrate the real regression.
        self.apply()
        after = replay()
        self.assertEqual(after["max"], 3)
        for case in after["split"]:
            with self.subTest(text=case["text"][:30]):
                self.assertLessEqual(len(case["pieces"]), after["max"])
                expected = "".join(line.strip() for line in case["text"].splitlines())
                self.assertEqual("".join(case["pieces"]), expected)
        self.assertEqual(after["split"][0]["pieces"], ["one", "two", "threefour"])
        self.assertEqual(after["split"][4]["pieces"], ["one", "two", "three"])
        self.assertEqual(after["split"][5]["pieces"], ["short"])
        self.assertEqual(after["image"], before["image"])
        self.assertTrue(after["image"]["identical"])
        self.assertEqual(len(after["image"]["sink"]), 1)
        self.assertEqual(after["image"]["sink"][0][0]["type"], "image")


if __name__ == "__main__":
    unittest.main()
