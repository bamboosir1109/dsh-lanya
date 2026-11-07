"""Exact mention generator, syntax and exclusive offline candidate artifacts."""
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / 'scripts'))
import patch_onebot_mentions as patch
from patch_onebot_policy import PatchError


class MentionPatchTests(unittest.TestCase):
    def original(self):
        return (REPO / 'tests/fixtures/onebot_egress/lib/cq.js').read_bytes()

    def test_only_exact_function_changes_and_newlines_are_preserved(self):
        for newline in (b'\n', b'\r\n'):
            raw = self.original().replace(b'\r\n', b'\n').replace(b'\n', newline)
            after = patch.transform(raw)
            reverted = after[len(patch.MARKER.replace('\n', newline.decode()).encode()):].replace(
                patch.NEW_FUNCTION.replace('\n', newline.decode()).encode(),
                patch.OLD_FUNCTION.replace('\n', newline.decode()).encode(), 1)
            self.assertEqual(reverted, raw)
            with tempfile.TemporaryDirectory() as directory:
                candidate = Path(directory) / 'cq.mjs'; candidate.write_bytes(after)
                done = subprocess.run(['node', '--check', str(candidate)], capture_output=True, text=True)
                self.assertEqual(done.returncode, 0, done.stderr)

    def test_unknown_duplicate_or_reapplied_function_is_refused(self):
        raw = self.original()
        for value in (raw.replace(b'qq === bot', b'unknown === bot'),
                      raw + b'\n' + patch.OLD_FUNCTION.encode(), patch.transform(raw)):
            with self.assertRaises(PatchError):
                patch.transform(value)

    def test_preview_is_readonly_and_output_requires_exact_sha_and_exclusive_slot(self):
        with tempfile.TemporaryDirectory() as directory:
            source, target = Path(directory) / 'cq.js', Path(directory) / 'candidate.mjs'
            before = self.original(); source.write_bytes(before)
            args = [sys.executable, '-B', str(REPO / 'scripts/patch_onebot_mentions.py'), '--input', str(source)]
            preview = subprocess.run(args, capture_output=True, text=True)
            self.assertEqual(preview.returncode, 0, preview.stderr)
            value = json.loads(preview.stdout)
            self.assertEqual(value['installation_writes'], 0); self.assertFalse(target.exists())
            wrong = subprocess.run(args + ['--output', str(target), '--expected-sha256', 'wrong'], capture_output=True)
            self.assertNotEqual(wrong.returncode, 0); self.assertFalse(target.exists())
            output_args = args + ['--output', str(target), '--expected-sha256', value['input_sha256']]
            done = subprocess.run(output_args, capture_output=True, text=True)
            self.assertEqual(done.returncode, 0, done.stderr)
            self.assertEqual(hashlib.sha256(target.read_bytes()).hexdigest(), value['output_sha256'])
            self.assertNotEqual(subprocess.run(output_args, capture_output=True).returncode, 0)
            self.assertEqual(source.read_bytes(), before)


if __name__ == '__main__':
    unittest.main()
