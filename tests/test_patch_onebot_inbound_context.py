"""Scoped quote generator: offline reviewed fixtures and exclusive artifacts."""
import hashlib
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
import patch_onebot_egress as egress
import patch_onebot_live_inbox as live
import patch_onebot_inbound_context as patch


class InboundContextPatchTests(unittest.TestCase):
    def bridge(self):
        raw = (REPO / 'tests/fixtures/onebot_egress/lib/bridge.js').read_bytes()
        return live.transform(egress.transform('lib/bridge.js', replay.transform('lib/bridge.js', policy.transform('bridge.js', raw))))

    def inbound(self):
        text = (REPO / 'tests/fixtures/onebot_egress/lib/inbound.js').read_text(encoding='utf-8')
        anchor = '        const nickname = sanitizeNickname(inbound.nickname);'
        self.assertEqual(text.count(anchor), 1)
        return ('// LANYA_POLICY_PATCH_V1\n' + text.replace(anchor,
            '        const observed = await this.ctx.observeInbound(inbound);\n        if (!observed.accepted) return;\n' + anchor)).encode()

    def test_bridge_preserves_reviewed_native_replay_egress_layers(self):
        before = self.bridge()
        after = patch.transform('bridge.js', before)
        for marker in [b'LANYA_NATIVE_CLAIM_INBOX_V1', b'LANYA_REPLAY_ISOLATION_V1', b'LANYA_EGRESS_PATCH_V1']:
            self.assertIn(marker, after)
        self.assertIn(b'expandQuote: (messageId, chatId)', after)
        self.assertIn(b'this.inbound.expandQuote(messageId, chatId)', after)
        self.assertEqual(after.count(b'guardedConnectionCall('), before.count(b'guardedConnectionCall('))
        with self.assertRaises(policy.PatchError):
            patch.transform('bridge.js', after)

    def test_inbound_requires_binding_and_unique_anchors_then_checks_real_syntax(self):
        before = self.inbound()
        after = patch.transform('inbound.js', before)
        self.assertIn(b'verified_images: verifiedImages', after)
        self.assertIn(b"'get_friend_msg_history'", after)
        self.assertIn(b"'get_group_msg_history'", after)
        for value in [before.replace(b'observeInbound(inbound)', b'unknown()'),
                      before + b'\n    async expandQuote(messageId) {', before.replace(b'LANYA_POLICY_PATCH_V1', b'UNKNOWN')]:
            with self.assertRaises(policy.PatchError):
                patch.transform('inbound.js', value)
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / 'inbound.mjs'
            target.write_bytes(after)
            run = subprocess.run(['node', '--check', str(target)], capture_output=True, text=True)
            self.assertEqual(run.returncode, 0, run.stderr)

    def test_incremental_prompt_matches_current_standard_and_keeps_other_layers(self):
        raw = (REPO / 'tests/fixtures/onebot_egress/lib/prompt.js').read_bytes()
        current = policy.transform('prompt.js', raw)
        legacy = current.decode().replace(patch.NEW_REVIEW, patch.OLD_REVIEW)
        guide_line = "    text += '- " + patch.CONTEXT_GUIDE + "\\n';\n"
        self.assertEqual(legacy.count(guide_line), 1)
        legacy = legacy.replace(guide_line, '')
        after = patch.transform('prompt.js', legacy.encode())
        self.assertEqual(after, patch.MARKER.encode() + current)
        self.assertNotIn(patch.OLD_REVIEW.encode(), after)
        with self.assertRaises(policy.PatchError):
            patch.transform('prompt.js', after)

    def test_readonly_preview_exact_hash_and_exclusive_artifact(self):
        with tempfile.TemporaryDirectory() as directory:
            source, target = Path(directory) / 'bridge.js', Path(directory) / 'candidate.mjs'
            before = self.bridge(); source.write_bytes(before)
            args = [sys.executable, '-B', str(REPO / 'scripts/patch_onebot_inbound_context.py'), '--input', str(source), '--name', 'bridge.js']
            preview = subprocess.run(args, capture_output=True, text=True)
            self.assertEqual(preview.returncode, 0, preview.stderr)
            value = json.loads(preview.stdout)
            self.assertEqual(value['installation_writes'], 0); self.assertFalse(target.exists())
            wrong = subprocess.run(args + ['--output', str(target), '--expected-sha256', 'wrong'], capture_output=True)
            self.assertNotEqual(wrong.returncode, 0); self.assertFalse(target.exists())
            output_args = args + ['--output', str(target), '--expected-sha256', value['input_sha256']]
            generated = subprocess.run(output_args, capture_output=True, text=True)
            self.assertEqual(generated.returncode, 0, generated.stderr)
            self.assertEqual(hashlib.sha256(target.read_bytes()).hexdigest(), value['output_sha256'])
            self.assertNotEqual(subprocess.run(output_args, capture_output=True).returncode, 0)
            self.assertEqual(source.read_bytes(), before)


if __name__ == '__main__':
    unittest.main()
