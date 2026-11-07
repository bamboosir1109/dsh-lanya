"""Real temporary meme files and routes; no platform, model or process I/O."""
import importlib.util
import csv
import io
import json
from pathlib import Path
import sys
import types
import unittest
from unittest import mock

spec = importlib.util.spec_from_file_location('meme_panel_isolated_base', Path(__file__).with_name('test_panel_bili_main_agent.py'))
fixture = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fixture)


class PanelMemeTargets(unittest.TestCase):
    def setUp(self):
        self.base = fixture.PanelBiliMainAgentTests()
        self.base.setUp()
        self.addCleanup(self.base.doCleanups)
        self.panel = self.base.panel
        self.collection = Path(self.panel.LANYA_MEMES)
        self.pack = self.base.root / 'pack'
        self.sources = [('pack', 'Pack', str(self.pack), True),
                        ('stolen', 'Collected', str(self.collection / 'stolen'), True)]
        patcher = mock.patch.object(self.panel, 'sources', return_value=self.sources)
        patcher.start()
        self.addCleanup(patcher.stop)

    def image(self, source, tag, name, body=b'synthetic image'):
        root = Path(next(row[2] for row in self.sources if row[0] == source))
        path = root / tag / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(body)
        return path

    def tags(self, value):
        Path(self.panel.MEME_TAGS).write_text(json.dumps(value), encoding='utf-8')

    def post(self, path, body):
        return self.base.route(path, body)

    def test_alias_keeps_exact_physical_location_and_stable_key(self):
        image = self.image('stolen', 'happy', 'fixture.png')
        self.tags({'happy/fixture.png': ['relaxed']})
        rows = self.panel.meme_groups_with_tags()
        alias = next(row for row in rows if row['tag'] == 'relaxed')
        item = alias['items'][0]
        self.assertEqual((item['source'], item['tag'], item['file']), ('stolen', 'happy', 'fixture.png'))
        self.assertEqual(self.panel.meme_path(item['source'], item['tag'], item['file']), str(image))
        happy = next(row for row in rows if row['tag'] == 'happy')
        self.assertEqual(item['key'], happy['items'][0]['key'])

    def test_duplicate_names_are_not_collapsed_or_guessed(self):
        self.image('stolen', 'happy', 'same.png')
        self.image('pack', 'happy', 'same.png')
        self.tags({'happy/same.png': ['ambiguous']})
        rows = self.panel.meme_groups_with_tags()
        self.assertNotIn('ambiguous', [row['tag'] for row in rows])
        group = next(row for row in rows if row['tag'] == 'happy')
        self.assertEqual(group['count'], 2)
        self.assertEqual(len({item['key'] for item in group['items']}), 2)
        self.assertNotIn('same.png', group['refs'])
        self.assertIsNone(self.panel.meme_path('merged', 'happy', 'same.png'))

    def test_exact_tags_apply_only_to_selected_source_and_directory(self):
        self.image('stolen', 'happy', 'same.png')
        self.image('pack', 'happy', 'same.png')
        self.image('stolen', 'sad', 'same.png')
        code, result = self.post('/api/memes/tag', {'source': 'stolen', 'tag': 'happy', 'file': 'same.png', 'tags': ['selected']})
        self.assertEqual((code, result['ok']), (200, True))
        chosen = next(row for row in self.panel.meme_groups_with_tags() if row['tag'] == 'selected')
        self.assertEqual([(item['source'], item['tag']) for item in chosen['items']], [('stolen', 'happy')])

    def test_empty_exact_tags_override_legacy_without_deleting_it(self):
        self.image('stolen', 'happy', 'fixture.png')
        self.tags({'happy/fixture.png': ['relaxed']})
        self.assertEqual(self.post('/api/memes/tag', {'source': 'stolen', 'tag': 'happy', 'file': 'fixture.png', 'tags': []})[0], 200)
        self.assertNotIn('relaxed', [row['tag'] for row in self.panel.meme_groups_with_tags()])
        saved = json.loads(Path(self.panel.MEME_TAGS).read_bytes())
        self.assertEqual(saved['happy/fixture.png'], ['relaxed'])

    def test_tagging_requires_real_exact_file_and_rejects_virtual_source(self):
        self.image('stolen', 'happy', 'fixture.png')
        for source, tag, name in [('merged', 'happy', 'fixture.png'), ('stolen', 'alias', 'fixture.png'),
                                  ('stolen', '..', 'fixture.png'), ('stolen', 'happy', '../fixture.png')]:
            self.assertEqual(self.post('/api/memes/tag', {'source': source, 'tag': tag, 'file': name, 'tags': ['x']})[0], 404)
        self.assertFalse(Path(self.panel.MEME_TAGS).exists())

    def test_existing_move_target_is_never_overwritten(self):
        original = self.image('stolen', 'happy', 'same.png', b'original')
        target = self.image('stolen', 'sad', 'same.png', b'existing')
        result = self.post('/api/memes/move', {'source': 'stolen', 'tag': 'happy', 'file': 'same.png', 'toTag': 'sad'})
        self.assertEqual(result[0], 409)
        self.assertEqual((original.read_bytes(), target.read_bytes()), (b'original', b'existing'))

    def test_move_delete_and_invalid_target_stay_on_exact_file(self):
        original = self.image('stolen', 'happy', 'same.png', b'selected')
        other = self.image('pack', 'happy', 'same.png', b'untouched')
        self.assertEqual(self.post('/api/memes/move', {'source': 'stolen', 'tag': 'happy', 'file': 'same.png', 'toTag': '_archive'})[0], 400)
        self.assertTrue(original.exists())
        self.assertEqual(self.post('/api/memes/move', {'source': 'stolen', 'tag': 'happy', 'file': 'same.png', 'toTag': 'relaxed'})[0], 200)
        self.assertFalse(original.exists())
        moved = self.collection / 'stolen/relaxed/same.png'
        self.assertEqual(moved.read_bytes(), b'selected')
        self.assertEqual(self.post('/api/memes/delete', {'source': 'stolen', 'tag': 'relaxed', 'file': 'same.png'})[0], 200)
        self.assertFalse(moved.exists())
        self.assertEqual(other.read_bytes(), b'untouched')

    def test_path_escape_and_duplicate_source_identity_are_refused(self):
        self.image('stolen', 'happy', 'fixture.png')
        for tag, name in [('.', 'fixture.png'), ('..', 'fixture.png'), ('happy', 'x:fixture.png'),
                          ('happy', '../fixture.png'), ('happy', '..\\fixture.png')]:
            self.assertIsNone(self.panel.meme_path('stolen', tag, name))
        self.sources.append(('stolen', 'Other', str(self.pack), True))
        self.assertIsNone(self.panel.meme_path('stolen', 'happy', 'fixture.png'))

    def test_unified_review_returns_real_result_without_starting_worker(self):
        result = {'mode': 'unified', 'queued': False, 'delivered': False, 'classified': 0,
                  'observed': 0, 'state': 'paused', 'collection': 'existing-files-only'}
        observer = mock.Mock(return_value=result)
        module = types.SimpleNamespace(observe_existing_memes=observer)
        with mock.patch.dict(sys.modules, {'meme_observations': module}):
            code, value = self.post('/api/memes/classify', {})
        self.assertEqual((code, value), (200, dict(result, ok=True, managed_by='main_agent')))
        observer.assert_called_once_with(limit=20)
        self.assertFalse(self.panel.meme_behavior()['independent_classification'])
        self.assertEqual(self.panel.meme_behavior()['batch_max'], 20)

    def test_config_rejects_ineffective_batch_values_and_keeps_unrelated_fields(self):
        path = Path(self.panel.MEMECFG)
        path.write_text('{"unrelated":"keep"}', encoding='utf-8')
        before = path.read_bytes()
        for value in (0, 21, True, '12'):
            self.assertEqual(self.post('/api/memes/config', {'config': {'maxPerRun': value}})[0], 400)
            self.assertEqual(path.read_bytes(), before)
        self.assertEqual(self.post('/api/memes/config', {'config': {'autoClassify': 'false'}})[0], 400)
        self.assertEqual(self.post('/api/memes/config', {'config': {'maxPerRun': 12, 'autoClassify': False}})[0], 200)
        self.assertEqual(json.loads(path.read_bytes()), {'unrelated': 'keep', 'maxPerRun': 12, 'autoClassify': False})

    def test_tool_runtime_without_detector_is_unknown_and_live_process_proves_installation(self):
        executable = self.base.root / 'runtime.exe'
        executable.write_bytes(b'synthetic executable')
        value = self.panel.tool_status({'key': 'python', 'name': 'Python', 'paths': [str(executable)]})
        self.assertTrue(value['installed'])
        self.assertIsNone(value['running'])
        self.assertFalse(value['update_supported'])
        response = types.SimpleNamespace(stdout=b'QQ.exe   123 Console', returncode=0)
        with mock.patch('subprocess.run', return_value=response):
            value = self.panel.tool_status({'key': 'qq', 'name': 'QQ', 'paths': [], 'proc': 'QQ.exe'})
        self.assertTrue(value['installed'])
        self.assertTrue(value['running'])
        self.assertEqual(value['path'], '')

    def test_only_verified_upstream_is_advertised(self):
        self.assertIn('napcat', self.panel.UPSTREAM)
        self.assertNotIn('dsh', self.panel.UPSTREAM)

    def test_only_explicit_disabled_retired_tasks_are_safe_to_hide(self):
        metadata = self.panel._scheduled_task_metadata
        row = metadata('DSH-Lanya-Life', '已禁用', '已禁用', 'unified')
        self.assertTrue(row['retired'])
        self.assertTrue(row['inactive_verified'])
        self.assertEqual(row['managed_by'], 'main_agent')
        for current, enabled in [('Running', 'Disabled'), ('Ready', 'Enabled'),
                                 ('unknown', 'Disabled'), ('Disabled', 'unknown')]:
            self.assertFalse(metadata('DSH-Lanya-Life', current, enabled, 'unified')['inactive_verified'])
        for mode in ('legacy', 'unavailable'):
            self.assertFalse(metadata('DSH-Lanya-Life', 'Disabled', 'Disabled', mode)['retired'])
        self.assertFalse(metadata('DSH-Unknown', 'Disabled', 'Disabled', 'unified')['retired'])
        self.assertFalse(metadata('DSH-Lanya-Budget', 'Ready', 'Enabled', 'unified')['retired'])

    def test_duplicate_task_rows_with_conflicting_state_are_not_hidden(self):
        output = io.StringIO()
        writer = csv.writer(output)
        writer.writerow(['header'] * 12)
        for state in ('Disabled', 'Running'):
            row = [''] * 12
            row[1], row[3], row[11] = '\\DSH-Lanya-Life', state, 'Disabled'
            writer.writerow(row)
        response = types.SimpleNamespace(stdout=b'\xef\xbb\xbf' + output.getvalue().encode(), returncode=0)
        with mock.patch('subprocess.run', return_value=response):
            rows = self.panel.scheduled_tasks()
        self.assertEqual(len(rows), 1)
        self.assertTrue(rows[0]['retired'])
        self.assertEqual(rows[0]['task_state'], 'unknown')
        self.assertFalse(rows[0]['inactive_verified'])


if __name__ == '__main__':
    unittest.main()
