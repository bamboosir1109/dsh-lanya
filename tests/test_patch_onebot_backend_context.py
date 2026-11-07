"""Backend quote increment: reviewed bytes, no live QQ or installation."""
import hashlib
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

import test_patch_onebot_inbound_context as source
import patch_onebot_backend_context as patch


class BackendContextTests(unittest.TestCase):
    def raw(self, name):
        fixture = source.InboundContextPatchTests()
        raw = fixture.bridge() if name == 'bridge.js' else fixture.inbound()
        return source.patch.transform(name, raw)

    def test_exact_increment_preserves_all_earlier_layers_and_boundaries(self):
        for name in ('bridge.js', 'inbound.js'):
            raw = self.raw(name); out = patch.transform(name, raw)
            text = raw.decode().replace('\r\n', '\n')
            if name == 'bridge.js':
                self.assertIn(b'historyAnchor: id => this.lanyaPolicy.backendGuard.historyAnchor(id)', out)
                marker = '            observeInbound: inbound => this.lanyaPolicy.observeInbound(inbound),'
                expected = text.replace(marker, marker + '\n            historyAnchor: id => this.lanyaPolicy.backendGuard.historyAnchor(id),')
            else:
                expected = text.replace(patch.HISTORY, patch.REPLACEMENT).replace('export class InboundPipeline {',
                    'export class InboundPipeline {\n    get lanyaBackendQuoteVersion() { return 1; }')
                for anchor in ('scope_mismatch', 'scope_unverified', 'verified_images', '3500'):
                    self.assertEqual(out.count(anchor.encode()), raw.count(anchor.encode()))
            self.assertEqual(out.decode().replace('\r\n', '\n'), patch.MARKER + expected)
            with self.assertRaises(source.policy.PatchError): patch.transform(name, out)

    def test_missing_or_changed_anchor_is_refused_and_generated_syntax_is_valid(self):
        for name in ('bridge.js', 'inbound.js'):
            raw = self.raw(name)
            with self.assertRaises(source.policy.PatchError): patch.transform(name, raw.replace(b'LANYA_SCOPED_INBOUND_CONTEXT_V1', b'UNKNOWN'))
            if name == 'inbound.js':
                with self.assertRaises(source.policy.PatchError): patch.transform(name, raw.replace(b'message_seq: id, count: 3', b'unknown: id, count: 3'))
            with tempfile.TemporaryDirectory() as directory:
                file = Path(directory) / name.replace('.js', '.mjs'); file.write_bytes(patch.transform(name, raw))
                result = subprocess.run(['node', '--check', str(file)], capture_output=True)
                self.assertEqual(result.returncode, 0, result.stderr)

    def test_cli_preview_is_read_only_and_output_requires_exact_input_and_exclusive_path(self):
        with tempfile.TemporaryDirectory() as directory:
            file = Path(directory) / 'inbound.js'; raw = self.raw('inbound.js'); file.write_bytes(raw)
            output = Path(directory) / 'candidate.mjs'
            args = [sys.executable, '-B', str(Path(patch.__file__)), '--input', str(file), '--name', 'inbound.js']
            self.assertEqual(subprocess.run(args, capture_output=True).returncode, 0); self.assertFalse(output.exists())
            self.assertNotEqual(subprocess.run(args + ['--output', str(output)], capture_output=True).returncode, 0)
            exact = args + ['--output', str(output), '--expected-sha256', hashlib.sha256(raw).hexdigest()]
            self.assertEqual(subprocess.run(exact, capture_output=True).returncode, 0)
            self.assertEqual(output.read_bytes(), patch.transform('inbound.js', raw))
            self.assertNotEqual(subprocess.run(exact, capture_output=True).returncode, 0)
            self.assertEqual(file.read_bytes(), raw)
