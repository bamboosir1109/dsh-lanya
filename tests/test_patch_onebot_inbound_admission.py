"""Exact incremental admission delta; isolated artifact generation only."""
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
import patch_onebot_inbound_context as context
import patch_onebot_inbound_admission as patch


class InboundAdmissionPatchTests(unittest.TestCase):
    def inbound(self, name='lanyaObserved'):
        raw = (REPO / 'tests/fixtures/onebot_egress/lib/inbound.js').read_text(encoding='utf-8')
        anchor = '        const nickname = sanitizeNickname(inbound.nickname);'
        hook = ('        const ' + name + ' = await this.ctx.observeInbound(inbound);\n'
                '        if (!' + name + '.accepted) return;\n')
        self.assertEqual(raw.count(anchor), 1)
        return context.transform('inbound.js', ('// LANYA_POLICY_PATCH_V1\n' + raw.replace(anchor, hook + anchor)).encode('utf-8')), hook

    def test_exact_move_preserves_all_other_layers_and_newlines(self):
        for name in ['lanyaObserved', 'observed']:
            before, hook = self.inbound(name)
            for newline in ['\n', '\r\n']:
                raw = before.decode('utf-8').replace('\n', newline).encode('utf-8')
                result = patch.transform(raw)
                expected = patch.MARKER + before.decode('utf-8').replace(hook, '').replace(patch.FINAL, hook + patch.FINAL)
                self.assertEqual(result, expected.replace('\n', newline).encode('utf-8'))
                self.assertLess(result.index(b'if (this.rateLimited(chatId))'), result.index(b'this.ctx.observeInbound(inbound)'))
                self.assertLess(result.index(b'if (content.trim()'), result.index(b'this.ctx.observeInbound(inbound)'))
                with tempfile.TemporaryDirectory() as directory:
                    target = Path(directory) / 'inbound.mjs'; target.write_bytes(result)
                    run = subprocess.run(['node', '--check', str(target)], capture_output=True, text=True)
                    self.assertEqual(run.returncode, 0, run.stderr)

    def test_unknown_or_duplicate_boundaries_and_reapplication_fail_closed(self):
        before, _ = self.inbound()
        invalid = [before.replace(b'LANYA_POLICY_PATCH_V1', b'UNKNOWN'),
                   before.replace(b'LANYA_SCOPED_INBOUND_CONTEXT_V1', b'UNKNOWN'),
                   before.replace(b'lanyaObserved', b'arbitraryObserved'),
                   before.replace(b'if (content.trim()', b'if (unknown.trim()'),
                   before.replace(b'if (this.rateLimited(chatId))', b'if (unknown(chatId))'),
                   before + b'\nthis.ctx.observeInbound(inbound);',
                   patch.transform(before)]
        for raw in invalid:
            with self.assertRaises(policy.PatchError):
                patch.transform(raw)

    def test_preview_is_readonly_and_output_requires_hash_is_exclusive(self):
        before, _ = self.inbound()
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / 'inbound.js'; source.write_bytes(before)
            target = Path(directory) / 'candidate.js'
            args = [sys.executable, '-B', str(REPO / 'scripts/patch_onebot_inbound_admission.py'), '--input', str(source)]
            run = subprocess.run(args, capture_output=True, text=True)
            self.assertEqual(run.returncode, 0, run.stderr)
            value = json.loads(run.stdout)
            self.assertEqual(value['installation_writes'], 0)
            self.assertFalse(target.exists())
            bad = subprocess.run(args + ['--output', str(target), '--expected-sha256', 'wrong'], capture_output=True)
            self.assertNotEqual(bad.returncode, 0); self.assertFalse(target.exists())
            output = args + ['--output', str(target), '--expected-sha256', value['input_sha256']]
            generated = subprocess.run(output, capture_output=True, text=True)
            self.assertEqual(generated.returncode, 0, generated.stderr)
            self.assertEqual(hashlib.sha256(target.read_bytes()).hexdigest(), value['output_sha256'])
            self.assertEqual(target.read_bytes(), patch.transform(before))
            self.assertNotEqual(subprocess.run(output, capture_output=True).returncode, 0)
            self.assertEqual(source.read_bytes(), before)


if __name__ == '__main__':
    unittest.main()
