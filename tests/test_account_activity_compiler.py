"""Current account helper bundling plus unchanged legacy imports; no host load."""
import hashlib
from pathlib import Path
import subprocess
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
import patch_onebot_policy as policy
import patch_onebot_autonomy as autonomy


class AccountCompilerTests(unittest.TestCase):
    def test_current_compiler_embeds_exact_account_dependency_and_parses(self):
        adapter = (ROOT / 'plugin/lib/onebot-policy-adapter.js').read_bytes()
        helper = (ROOT / 'plugin/lib/account-activity.js').read_bytes()
        compiled = autonomy.compile_adapter(adapter, account_raw=helper)
        marker = b'LANYA_ACCOUNT_ACTIVITY_BUNDLE_V1 ' + hashlib.sha256(helper).hexdigest().encode()
        self.assertEqual(compiled.count(marker), 1)
        self.assertNotIn(b"from './account-activity.js'", compiled)
        checked = subprocess.run(['node', '--input-type=module', '--check'], input=compiled,
                                 capture_output=True, timeout=20,
                                 creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        self.assertEqual(checked.returncode, 0, checked.stderr)

    def test_legacy_adapter_is_byte_exact_and_unknown_imports_are_rejected(self):
        old = b'// No account activity import in this historical parent.\r\n'
        self.assertEqual(policy.bundle_account_helper(old), old)
        helper = (ROOT / 'plugin/lib/account-activity.js').read_bytes()
        anchor = b"import { AccountActivity, accountPublicationAction } from './account-activity.js'"
        for source, dependency in ((anchor + b'\n' + anchor, helper),
                                   (b"import unknown from './account-activity.js'", helper),
                                   (anchor, helper + b"\nimport extra from 'node:child_process'\n")):
            with self.subTest(source=source[:30]), self.assertRaises(policy.PatchError):
                policy.bundle_account_helper(source, dependency)


if __name__ == '__main__': unittest.main()
