"""Backend quote increment: reviewed bytes, no live QQ or installation."""
import hashlib
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

import test_patch_onebot_inbound_context as source
import patch_onebot_backend_context as patch
import patch_onebot_group_context as group
import patch_onebot_burst_intake as burst


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

    def test_current_group_and_burst_parents_keep_address_and_every_other_byte(self):
        grouped = group.transform('bridge.js', self.raw('bridge.js'))
        legacy = grouped.replace(patch.FORWARD_OBSERVER.encode(), patch.ADDRESSED_OBSERVER.encode())
        for raw in (legacy, grouped, burst.transform('bridge.js', grouped)):
            for newline in ('\n', '\r\n'):
                body = raw.decode().replace('\r\n', '\n').replace('\n', newline).encode()
                anchor = patch.FORWARD_OBSERVER if patch.FORWARD_OBSERVER.encode() in raw else patch.ADDRESSED_OBSERVER
                expected = body.decode().replace(anchor,
                    anchor + newline + '            historyAnchor: id => this.lanyaPolicy.backendGuard.historyAnchor(id),')
                result = patch.transform('bridge.js', body)
                self.assertEqual(result, (patch.MARKER.replace('\n', newline) + expected).encode())
                self.assertEqual(result.count(anchor.encode()), 1)
                for marker in (b'contextualGroupEnabled:', b'nativeBurstEnabled:', b'guardedConnectionCall(', b'dispatchFollowup('):
                    self.assertEqual(result.count(marker), body.count(marker))
                with tempfile.TemporaryDirectory() as directory:
                    file = Path(directory) / 'candidate.mjs'; file.write_bytes(result)
                    checked = subprocess.run(['node', '--check', str(file)], capture_output=True)
                    self.assertEqual(checked.returncode, 0, checked.stderr)

    def test_mixed_partial_unknown_and_existing_history_shapes_are_refused(self):
        raw = group.transform('bridge.js', self.raw('bridge.js')).decode()
        bad = (
            raw.replace(patch.GROUP_MARKER, ''),
            raw.replace(patch.FORWARD_OBSERVER, patch.SIMPLE_OBSERVER),
            raw.replace(patch.GROUP_HOOK, ''),
            raw.replace(patch.FORWARD_OBSERVER, patch.FORWARD_OBSERVER + '\n' + patch.SIMPLE_OBSERVER),
            raw + '\n// historyAnchor: unknown customized implementation\n',
            raw.replace('(inbound, address, forwardText)', '(inbound,address,forwardText)'),
        )
        for body in bad:
            with self.assertRaises(source.policy.PatchError): patch.transform('bridge.js', body.encode())

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
