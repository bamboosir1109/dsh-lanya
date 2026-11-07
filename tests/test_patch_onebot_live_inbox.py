"""Incremental generation only; temporary fixtures, no installed files."""
import hashlib
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / 'scripts'))
import patch_onebot_policy as policy
import patch_onebot_replay as replay
import patch_onebot_live_inbox as live


class LiveInboxPatchTests(unittest.TestCase):
    def bridge(self):
        raw = (REPO / 'tests/fixtures/onebot_egress/lib/bridge.js').read_bytes()
        return replay.transform('lib/bridge.js', policy.transform('bridge.js', raw))

    def test_reviewed_increment_preserves_other_layers(self):
        before = self.bridge()
        after = live.transform(before)
        self.assertIn(b'LANYA_NATIVE_CLAIM_INBOX_V1', after)
        self.assertIn(b'LANYA_REPLAY_ISOLATION_V1', after)
        self.assertIn(b'nativeClaims: true', after)
        self.assertIn(b'.dispatchNative(chat, nativeInput, role,', after)
        self.assertIn(b'if (this.lanyaPolicy.nativeClaims)', after)
        self.assertIn(b'Preserve the explicit offline diagnostic assembly', after)
        self.assertEqual(after.count(b'guardedConnectionCall(action, params, chatId)'), before.count(b'guardedConnectionCall(action, params, chatId)'))
        with self.assertRaises(policy.PatchError):
            live.transform(after)

    def test_unknown_or_ambiguous_anchor_fails_closed(self):
        raw = self.bridge()
        with self.assertRaises(policy.PatchError):
            live.transform(raw.replace(b'chat.pendingTurnRoles.push(role);', b'changed();'))
        with self.assertRaises(policy.PatchError):
            live.transform(raw + b'\n' + b"{ configFile: new URL('./lanya-policy.config.json', import.meta.url) }")

    def test_preview_readonly_then_exclusive_artifact_cas(self):
        with tempfile.TemporaryDirectory() as root:
            source, output = Path(root) / 'bridge.js', Path(root) / 'generated.js'
            source.write_bytes(self.bridge())
            before = source.read_bytes()
            args = [sys.executable, '-B', str(REPO / 'scripts/patch_onebot_live_inbox.py'), '--bridge', str(source)]
            preview = subprocess.run(args, capture_output=True, text=True)
            self.assertEqual(preview.returncode, 0, preview.stderr)
            value = json.loads(preview.stdout)
            self.assertEqual(value['installation_writes'], 0)
            self.assertFalse(output.exists())
            failed = subprocess.run(args + ['--output', str(output), '--expected-sha256', 'wrong'], capture_output=True)
            self.assertNotEqual(failed.returncode, 0)
            self.assertFalse(output.exists())
            generated = subprocess.run(args + ['--output', str(output), '--expected-sha256', value['input_sha256']], capture_output=True)
            self.assertEqual(generated.returncode, 0, generated.stderr)
            self.assertEqual(hashlib.sha256(output.read_bytes()).hexdigest(), value['output_sha256'])
            duplicate = subprocess.run(args + ['--output', str(output), '--expected-sha256', value['input_sha256']], capture_output=True)
            self.assertNotEqual(duplicate.returncode, 0)
            self.assertEqual(source.read_bytes(), before)

    def test_full_build_preserves_egress_alias_and_copies_exact_helper(self):
        with tempfile.TemporaryDirectory() as root:
            source, output = Path(root) / 'bridge.js', Path(root) / 'build'
            source.write_bytes(self.bridge())
            args = [sys.executable, '-B', str(REPO / 'scripts/patch_onebot_live_inbox.py'), '--bridge', str(source), '--build-dir', str(output)]
            preview = subprocess.run(args, capture_output=True, text=True)
            self.assertEqual(preview.returncode, 0, preview.stderr)
            self.assertFalse(output.exists())
            value = json.loads(preview.stdout)
            run = subprocess.run(args + ['--expected-fingerprint', value['fingerprint']], capture_output=True, text=True)
            self.assertEqual(run.returncode, 0, run.stderr)
            self.assertEqual(set(p.name for p in output.iterdir()), {'bridge.js', 'qq-inbox-recovery.js', 'lanya-policy-adapter.js'})
            self.assertEqual((output / 'qq-inbox-recovery.js').read_bytes(), (REPO / 'plugin/lib/qq-inbox-recovery.js').read_bytes())
            adapter = (output / 'lanya-policy-adapter.js').read_bytes()
            self.assertTrue(b'LANYA_EGRESS_PATCH_V1' in adapter)
            self.assertIn(b"from './lanya-qq-egress.js'", adapter)
            self.assertIn(b"from './lanya-decision-policy.js'", adapter)
            repeat = subprocess.run(args + ['--expected-fingerprint', value['fingerprint']], capture_output=True)
            self.assertNotEqual(repeat.returncode, 0)


if __name__ == '__main__':
    unittest.main()
