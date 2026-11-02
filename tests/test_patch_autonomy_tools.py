"""Current helper bundling and explicit tool-factory artifact generation only."""
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
import patch_onebot_autonomy as autonomy
import patch_onebot_egress as egress
import patch_autonomy_tools as tools


class AutonomyToolBuildTests(unittest.TestCase):
    def test_current_alias_embeds_explicit_helper_and_has_real_factory_registration(self):
        source = (REPO / 'plugin/lib/onebot-policy-adapter.js').read_bytes()
        helper = (REPO / 'plugin/lib/autonomy-intents.js').read_bytes()
        alias = autonomy.compile_adapter(source, helper)
        self.assertIn(b'LANYA_INTENT_BUNDLE_V1 ' + hashlib.sha256(helper).hexdigest().encode(), alias)
        self.assertNotIn(b"from './autonomy-intents.js'", alias)
        self.assertNotIn(b"from '@deepseek-ai/dsh-tools'", alias)
        self.assertIn(b'registerIntentTools(ctx, defineTool)', alias)
        self.assertIn(b'ctx.tools.register(defineTool(definition))', alias)
        self.assertIn(b'const { AutonomyPlanner } = (() => {', alias)
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / 'adapter.mjs'; target.write_bytes(alias)
            checked = subprocess.run(['node', '--check', str(target)], capture_output=True, text=True)
            self.assertEqual(checked.returncode, 0, checked.stderr)
        with self.assertRaises(policy.PatchError):
            autonomy.compile_adapter(source, helper + b"\nimport unknown from 'unreviewed'\n")

    def test_legacy_adapter_without_import_does_not_read_or_bundle_a_new_helper(self):
        result = subprocess.run(['git', 'show', 'HEAD:plugin/lib/onebot-policy-adapter.js'], cwd=REPO, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        old = result.stdout
        self.assertNotIn(b'autonomy-intents.js', old)
        base = old.replace(b"from './decision-policy.js'", b"from './lanya-decision-policy.js'").replace(b"from './observation-queue.js'", b"from './lanya-observation-queue.js'")
        expected = egress.transform(autonomy.TARGET, base)
        self.assertEqual(autonomy.compile_adapter(old, b'not a helper; must never be read'), expected)
        self.assertEqual(policy.bundle_intent_helper(old, b'invalid'), old)

    def before(self):
        raw = (REPO / 'tests/fixtures/onebot_egress/lib/tools.js').read_bytes()
        current = policy.transform('tools.js', raw)
        hook = b'    disposers.push(...bridge.lanyaPolicy.registerIntentTools(ctx, defineTool));\n'
        self.assertEqual(current.count(hook), 1)
        return current.replace(hook, b'')

    def test_incremental_hook_uses_existing_sdk_factory_and_refuses_ambiguous_layers(self):
        before = self.before(); after = tools.transform(before)
        self.assertEqual(after.count(b'registerIntentTools(ctx, defineTool)'), 1)
        self.assertEqual(before.count(b'qq_send_image'), after.count(b'qq_send_image'))
        self.assertEqual(before.count(b'qq_decide'), after.count(b'qq_decide'))
        for raw in [after, before + b'\n    const disposers = [];', before.replace(b'LANYA_POLICY_PATCH_V1', b'UNKNOWN')]:
            with self.assertRaises(policy.PatchError):
                tools.transform(raw)

    def test_preview_is_readonly_and_output_requires_exact_pin_and_exclusive_destination(self):
        with tempfile.TemporaryDirectory() as directory:
            source, output = Path(directory) / 'tools.js', Path(directory) / 'candidate.mjs'
            before = self.before(); source.write_bytes(before)
            args = [sys.executable, '-B', str(REPO / 'scripts/patch_autonomy_tools.py'), '--input', str(source)]
            preview = subprocess.run(args, capture_output=True, text=True)
            self.assertEqual(preview.returncode, 0, preview.stderr)
            result = json.loads(preview.stdout)
            self.assertEqual(result['installation_writes'], 0); self.assertFalse(output.exists())
            wrong = subprocess.run(args + ['--output', str(output), '--expected-sha256', 'wrong'], capture_output=True)
            self.assertNotEqual(wrong.returncode, 0); self.assertFalse(output.exists())
            generate = args + ['--output', str(output), '--expected-sha256', result['input_sha256']]
            run = subprocess.run(generate, capture_output=True, text=True)
            self.assertEqual(run.returncode, 0, run.stderr)
            self.assertEqual(hashlib.sha256(output.read_bytes()).hexdigest(), result['output_sha256'])
            self.assertNotEqual(subprocess.run(generate, capture_output=True).returncode, 0)
            self.assertEqual(source.read_bytes(), before)


if __name__ == '__main__':
    unittest.main()
