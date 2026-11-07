"""Reviewed transport candidate; no host connection, installation or send."""
import hashlib
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import patch_onebot_backend_transport as patch

FIXTURE = Path(__file__).parent / 'fixtures/onebot_egress/lib/connection.js'


class BackendTransportTests(unittest.TestCase):
    def test_exact_delta_preserves_all_unrelated_bytes_and_generated_syntax(self):
        raw = FIXTURE.read_bytes()
        self.assertEqual(hashlib.sha256(raw).hexdigest(), patch.SOURCE_SHA)
        expected = raw.decode().replace(patch.FRAME, patch.OWNED_FRAME).replace(patch.ATTACH, patch.OWNED_ATTACH)
        expected = expected.replace('export class OneBotConnection {',
            'export class OneBotConnection {\n    get lanyaBackendTransportVersion() { return 1; }')
        result = patch.transform(raw)
        self.assertEqual(result, (patch.MARKER + expected).encode())
        self.assertEqual(result.count(b'if (this.socket !== socket || this.stopping)'), 2)
        with tempfile.TemporaryDirectory() as directory:
            file = Path(directory) / 'candidate.mjs'; file.write_bytes(result)
            check = subprocess.run(['node', '--check', str(file)], capture_output=True)
            self.assertEqual(check.returncode, 0, check.stderr)

    def test_unknown_edited_and_already_patched_inputs_are_refused(self):
        raw = FIXTURE.read_bytes()
        for changed in (b'', raw + b'\n', raw.replace(b'const previous', b'let previous'), patch.transform(raw)):
            with self.assertRaises(patch.PatchError): patch.transform(changed)

    def test_preview_is_read_only_and_candidate_requires_exact_hash_exclusive_path(self):
        with tempfile.TemporaryDirectory() as directory:
            file = Path(directory) / 'input.js'; raw = FIXTURE.read_bytes(); file.write_bytes(raw)
            output = Path(directory) / 'candidate.mjs'
            args = [sys.executable, '-B', patch.__file__, '--input', str(file)]
            self.assertEqual(subprocess.run(args, capture_output=True).returncode, 0)
            self.assertFalse(output.exists())
            self.assertNotEqual(subprocess.run(args + ['--output', str(output)], capture_output=True).returncode, 0)
            exact = args + ['--expected-sha256', patch.SOURCE_SHA]
            self.assertNotEqual(subprocess.run(exact + ['--output', str(file)], capture_output=True).returncode, 0)
            self.assertEqual(subprocess.run(exact + ['--output', str(output)], capture_output=True).returncode, 0)
            self.assertEqual(output.read_bytes(), patch.transform(raw))
            self.assertNotEqual(subprocess.run(exact + ['--output', str(output)], capture_output=True).returncode, 0)
            self.assertEqual(file.read_bytes(), raw)


if __name__ == '__main__': unittest.main()
