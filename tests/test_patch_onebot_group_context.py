import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / 'scripts'))
import patch_onebot_group_context as patch
from patch_onebot_policy import PatchError


class GroupContextPatchTests(unittest.TestCase):
    raw = (b'// LANYA_SCOPED_INBOUND_CONTEXT_V1\n// LANYA_INBOUND_ADMISSION_V1\n'
           b"        let forward = '';\n            forward = expansion.text;\n"
           b'await this.ctx.observeInbound(inbound);\n'
           b"        if (kind === 'group' && this.ctx.config.requireMention && !mentioned) {\n}\n")

    def test_narrow_gate_preserves_unknown_metadata_and_newlines(self):
        for newline in [b'\n', b'\r\n']:
            raw = self.raw.replace(b'\n', newline)
            result = patch.transform('inbound.js', raw)
            expected = patch.MARKER.encode().replace(b'\n', newline) + raw.replace(
                b'&& !mentioned)', b'&& !mentioned && this.ctx.contextualGroupEnabled?.(chatId) !== true)').replace(
                b'observeInbound(inbound);', b'observeInbound(inbound, address, forwardText);').replace(
                b"let forward = '';" + newline, b"let forward = '';" + newline + b"        let forwardText = '';" + newline).replace(
                b'forward = expansion.text;' + newline,
                b"forward = expansion.text;" + newline + b"            if (kind === 'group') forwardText = forward;" + newline)
            self.assertEqual(result, expected)
        for raw in [self.raw + self.raw, self.raw.replace(b'requireMention', b'unknown'),
                    self.raw.replace(b'LANYA_INBOUND_ADMISSION_V1', b'UNKNOWN'),
                    patch.transform('inbound.js', self.raw)]:
            with self.assertRaises(PatchError):
                patch.transform('inbound.js', raw)
        with self.assertRaises(PatchError):
            patch.transform('other.js', self.raw)

    def test_preview_and_exclusive_artifact(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / 'inbound.js'; source.write_bytes(self.raw)
            target = Path(directory) / 'candidate.js'
            args = [sys.executable, '-B', str(REPO / 'scripts/patch_onebot_group_context.py'), '--input', str(source)]
            run = subprocess.run(args, capture_output=True)
            self.assertEqual(run.returncode, 0, run.stderr)
            self.assertEqual(json.loads(run.stdout)['installation_writes'], 0)
            self.assertFalse(target.exists())
            invalid = subprocess.run(args + ['--output', str(target)], capture_output=True)
            self.assertNotEqual(invalid.returncode, 0); self.assertFalse(target.exists())
            build = args + ['--output', str(target), '--expected-sha256', hashlib.sha256(self.raw).hexdigest()]
            self.assertEqual(subprocess.run(build, capture_output=True).returncode, 0)
            self.assertNotEqual(subprocess.run(build, capture_output=True).returncode, 0)
            self.assertEqual(source.read_bytes(), self.raw)


if __name__ == '__main__':
    unittest.main()
