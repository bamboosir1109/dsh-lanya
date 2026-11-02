"""Conditional alias bundle preserves old adapters and closes new helpers."""
from pathlib import Path
import sys
import unittest
from unittest import mock

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / 'scripts'))
import patch_onebot_policy as policy


class DiaryBundleTests(unittest.TestCase):
    def test_legacy_bytes_return_without_opening_a_new_helper(self):
        raw = b"import path from 'node:path'\nexport class Legacy {}\r\n"
        with mock.patch.object(policy, 'read_regular', side_effect=AssertionError('No new helper read')):
            self.assertEqual(policy.bundle_diary_helper(raw), raw)
            self.assertEqual(policy.bundle_intent_helper(raw), raw)

    def test_closed_bundle_uses_exact_explicit_helper_and_no_new_runtime_import(self):
        raw = b"import path from 'node:path'\nimport { createHash } from 'node:crypto'\nimport { NarrativeDiary } from './narrative-diary.js'\n"
        helper = (REPO / 'plugin/lib/narrative-diary.js').read_bytes()
        value = policy.bundle_diary_helper(raw, helper).decode('utf-8')
        self.assertIn('LANYA_DIARY_BUNDLE_V1 ' + policy.sha256(helper), value)
        self.assertIn('return { NarrativeDiary }', value)
        self.assertNotIn("from './narrative-diary.js'", value)
        self.assertEqual(value.count("import path from 'node:path'"), 1)

    def test_ambiguous_or_extra_dependency_refuses_instead_of_guessing(self):
        anchor = b"import { NarrativeDiary } from './narrative-diary.js'\n"
        helper = (REPO / 'plugin/lib/narrative-diary.js').read_bytes()
        with self.assertRaises(policy.PatchError): policy.bundle_diary_helper(anchor * 2, helper)
        with self.assertRaises(policy.PatchError): policy.bundle_diary_helper(anchor, helper + b"\nimport extra from 'unknown'\n")
        with self.assertRaises(policy.PatchError): policy.bundle_diary_helper(b"import { Other } from './narrative-diary.js'\n", helper)


if __name__ == '__main__': unittest.main()
