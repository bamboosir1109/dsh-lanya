"""Legacy identity-settings merges use temporary files and inert service I/O."""
import json
from pathlib import Path
import unittest

import test_panel_bili_main_agent as fixture


class PanelConfigTests(unittest.TestCase):
    setUp = fixture.PanelBiliMainAgentTests.setUp
    route = fixture.PanelBiliMainAgentTests.route

    def test_unreadable_json_and_nonobject_config_are_not_replaced_by_a_partial_save(self):
        path = Path(self.panel.CONFIG)
        for raw in (b'{unfinished', b'[]', b'null', b'"old string"'):
            with self.subTest(raw=raw):
                path.write_bytes(raw)
                before = path.stat()
                names = set(path.parent.iterdir())
                code, result = self.route('/api/config', {'config': {'one_setting': 'new'}})
                self.assertEqual((code, result['code']), (409, 'existing_config_unavailable'))
                self.assertFalse(result['ok']); self.assertEqual(path.read_bytes(), raw)
                self.assertEqual((path.stat().st_ino, path.stat().st_mtime_ns), (before.st_ino, before.st_mtime_ns))
                self.assertEqual(set(path.parent.iterdir()), names)

    def test_missing_config_can_be_created_and_valid_partial_merge_preserves_other_fields(self):
        path = Path(self.panel.CONFIG); path.unlink()
        code, result = self.route('/api/config', {'config': {'preserve': {'nested': 'local fixture'}, 'remove': 1}})
        self.assertEqual(code, 200); self.assertTrue(result['ok'])
        code, result = self.route('/api/config', {'config': {'one_setting': 'new', 'remove': None}})
        self.assertEqual(code, 200); self.assertTrue(result['ok'])
        self.assertEqual(json.loads(path.read_text(encoding='utf-8')),
                         {'preserve': {'nested': 'local fixture'}, 'one_setting': 'new'})


if __name__ == '__main__': unittest.main()
