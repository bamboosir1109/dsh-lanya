"""Exercise the real source-update route using disposable local Git remotes."""
import os
from pathlib import Path
import shutil
import subprocess
import unittest
from unittest import mock

import test_panel_bili_main_agent as fixture

NATIVE_RUN = subprocess.run
NATIVE_POPEN = subprocess.Popen
FIXTURE_EMAIL = 'fixture' + chr(64) + 'example.invalid'


@unittest.skipUnless(shutil.which('git'), 'Git required')
class PanelSourceUpdateTests(unittest.TestCase):
    setUpPanel = fixture.PanelBiliMainAgentTests.setUp
    route = fixture.PanelBiliMainAgentTests.route

    def setUp(self):
        self.setUpPanel()
        self.module = self.panel
        self.origin, self.seed, self.checkout = [self.root / name for name in ('origin.git', 'seed', 'checkout')]
        self.git_env = dict(os.environ, GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_NOSYSTEM='1',
                            GIT_TERMINAL_PROMPT='0', GIT_AUTHOR_NAME='Fixture', GIT_COMMITTER_NAME='Fixture',
                            GIT_AUTHOR_EMAIL=FIXTURE_EMAIL, GIT_COMMITTER_EMAIL=FIXTURE_EMAIL)
        self.command('init', '--bare', str(self.origin))
        self.command('init', '-b', 'main', str(self.seed))
        self.commit('initial')
        self.command('-C', str(self.seed), 'remote', 'add', 'origin', str(self.origin))
        self.command('-C', str(self.seed), 'push', '-u', 'origin', 'main')
        self.command('-C', str(self.seed), 'checkout', '-b', 'dev')
        self.commit('dev-base')
        self.command('-C', str(self.seed), 'push', '-u', 'origin', 'dev')
        self.command('clone', '--single-branch', '-b', 'dev', str(self.origin), str(self.checkout))
        env = mock.patch.dict(os.environ, {'LANYA_REPO': str(self.checkout)})
        env.start(); self.addCleanup(env.stop)

    def command(self, *args):
        with mock.patch.object(subprocess, 'Popen', NATIVE_POPEN):
            result = NATIVE_RUN(['git', '-c', 'core.hooksPath=', '-c', 'commit.gpgsign=false', *args],
                                env=self.git_env, capture_output=True, text=True, encoding='utf-8',
                                timeout=20, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout.strip()

    def commit(self, name):
        (self.seed / (name + '.txt')).write_text(name, encoding='utf-8')
        self.command('-C', str(self.seed), 'add', '.')
        self.command('-C', str(self.seed), 'commit', '-m', name)

    def update(self, fail=None):
        calls = []
        def isolated(args, **kwargs):
            self.assertEqual(args[:3], ['git', '-C', str(self.checkout)])
            self.assertEqual(kwargs.get('creationflags'), getattr(subprocess, 'CREATE_NO_WINDOW', 0))
            self.assertLessEqual(kwargs['timeout'], 180)
            calls.append(args[3:])
            if fail and args[3] == fail:
                return subprocess.CompletedProcess(args, 1, '', 'not public output')
            with mock.patch.object(subprocess, 'Popen', NATIVE_POPEN):
                return NATIVE_RUN(args, env=self.git_env, **kwargs)
        with mock.patch.object(self.module.subprocess, 'run', side_effect=isolated):
            result = self.route('/api/update', {})
        return result, calls

    def test_only_current_upstream_is_pulled_and_success_does_not_claim_runtime_deployment(self):
        self.command('-C', str(self.seed), 'checkout', 'main')
        self.commit('main-only'); self.command('-C', str(self.seed), 'push')
        self.command('-C', str(self.seed), 'checkout', 'dev')
        self.commit('dev-new'); self.command('-C', str(self.seed), 'push')
        expected = self.command('-C', str(self.seed), 'rev-parse', 'HEAD')
        (code, value), calls = self.update()
        self.assertEqual(code, 200)
        self.assertTrue(value['ok']); self.assertEqual(value['after'], expected)
        self.assertTrue((self.checkout/'dev-new.txt').is_file())
        self.assertFalse((self.checkout/'main-only.txt').exists())
        self.assertEqual(calls.count(['fetch']), 1)
        self.assertEqual(sum(row[:4] == ['merge', '--ff-only', '--no-edit', '--no-overwrite-ignore'] for row in calls), 1)
        self.assertTrue(value['source_only']); self.assertFalse(value['deployment_performed'])
        self.assertEqual(value['runtime_loading'], 'not_verified')

    def test_dirty_tracked_and_untracked_files_prevent_pull_and_are_preserved(self):
        for name in ('initial.txt', 'untracked.txt'):
            with self.subTest(name=name):
                path = self.checkout/name; old = path.read_bytes() if path.exists() else None
                path.write_bytes(b'unsaved local work')
                (code, value), calls = self.update()
                self.assertEqual((code, value['code']), (409, 'source_worktree_dirty'))
                self.assertFalse(any(row[0] in ('fetch', 'merge', 'pull') for row in calls))
                self.assertEqual(path.read_bytes(), b'unsaved local work')
                if old is None: path.unlink()
                else: path.write_bytes(old)

    def test_missing_upstream_and_failed_status_never_pull(self):
        (code, value), calls = self.update(fail='status')
        self.assertEqual((code, value['code']), (503, 'source_status_unavailable'))
        self.assertFalse(any(row[0] in ('fetch', 'merge', 'pull') for row in calls))
        self.command('-C', str(self.checkout), 'branch', '--unset-upstream')
        (code, value), calls = self.update()
        self.assertEqual((code, value['code']), (409, 'source_upstream_unavailable'))
        self.assertFalse(any(row[0] in ('fetch', 'merge', 'pull') for row in calls))

    def test_failed_pull_and_concurrent_request_are_explicit_without_retries(self):
        before = self.command('-C', str(self.checkout), 'rev-parse', 'HEAD')
        self.command('-C', str(self.checkout), 'remote', 'set-url', 'origin', str(self.root/'missing-origin'))
        (code, value), calls = self.update()
        self.assertEqual((code, value['code']), (502, 'source_fetch_failed'))
        self.assertEqual(calls.count(['fetch']), 1)
        self.assertFalse(any(row[0] in ('merge', 'pull') for row in calls))
        self.assertEqual(self.command('-C', str(self.checkout), 'rev-parse', 'HEAD'), before)
        self.module._SOURCE_UPDATE_LOCK.acquire()
        try:
            (code, value), calls = self.update()
        finally:
            self.module._SOURCE_UPDATE_LOCK.release()
        self.assertEqual((code, value['code']), (409, 'source_update_busy'))
        self.assertEqual(calls, [])

    def test_ignored_local_file_collision_cannot_be_overwritten_by_remote_tracked_file(self):
        path = self.checkout/'settings.local'
        path.write_bytes(b'local private fixture')
        exclude = self.checkout/'.git/info/exclude'
        exclude.write_text('\nsettings.local\n', encoding='utf-8')
        (self.seed/'settings.local').write_bytes(b'remote tracked fixture')
        self.command('-C', str(self.seed), 'add', 'settings.local')
        self.command('-C', str(self.seed), 'commit', '-m', 'new tracked file')
        self.command('-C', str(self.seed), 'push')
        before = self.command('-C', str(self.checkout), 'rev-parse', 'HEAD')
        (code, value), calls = self.update()
        self.assertEqual((code, value['code']), (502, 'source_merge_failed'))
        self.assertEqual(path.read_bytes(), b'local private fixture')
        self.assertEqual(self.command('-C', str(self.checkout), 'rev-parse', 'HEAD'), before)
        self.assertEqual(sum(row[0] == 'merge' for row in calls), 1)


if __name__ == '__main__': unittest.main()
