"""Real PowerShell private-pipe resolver, synthetic files only, no network."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

SOURCE = Path(__file__).resolve().parents[1] / 'scripts'


@unittest.skipUnless(shutil.which('powershell.exe'), 'Windows PowerShell required')
class BackendPowerShellTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='backend isolated space ')
        self.addCleanup(self.temp.cleanup); self.root = Path(self.temp.name)
        self.runtime = self.root / 'napcat'; self.runtime.mkdir()
        for name in ['qq_backend.ps1', 'qq_backend.py', 'project_conf.py']:
            shutil.copyfile(SOURCE / name, self.runtime / name)
        self.config = self.root / 'project.json'

    def write(self, relative, value):
        path = self.root / relative; path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value), encoding='utf-8'); return path

    def run_resolver(self, provider, should_fail=False):
        self.write('project.json', {'onebotPort': 8721, 'qqBackend': {'provider': provider}})
        self.write('napcat/framework/config/onebot11.json', {'network': {'httpServers': [
            {'port': 8721, 'token': 'fixture-napcat'}]}})
        helper = str(self.runtime / 'qq_backend.ps1').replace("'", "''")
        python = sys.executable.replace("'", "''")
        script = ". '" + helper + "'; try { $c=Get-QqBackendConnection -Python '" + python + "'; "
        script += "@{provider=$c.provider; endpoint=$c.napcatHttp; tokenMatches=($c.napcatToken -eq 'fixture-" + provider + "')} | ConvertTo-Json -Compress"
        script += " } catch { Write-Output 'resolver-refused'; exit 2 }"
        env = {**os.environ, 'LANYA_WORKDIR': str(self.root), 'LANYA_PROJECT_CONFIG': str(self.config)}
        result = subprocess.run(['powershell.exe', '-NoProfile', '-NonInteractive', '-Command', script],
            capture_output=True, env=env, timeout=25, text=True, encoding='utf-8', errors='replace')
        if should_fail:
            self.assertEqual(result.returncode, 2)
            self.assertNotIn('fixture-napcat', result.stdout + result.stderr)
            self.assertNotIn('fixture-snowluma', result.stdout + result.stderr)
            return None
        self.assertEqual(result.returncode, 0, result.stderr)
        value = json.loads(result.stdout); self.assertTrue(value['tokenMatches']); return value

    def test_both_providers_resolve_inside_a_path_with_spaces_without_exposing_token(self):
        self.write('snowluma/config/onebot.json', {'networks': {'httpServers': [
            {'name': 'fixture', 'port': 8730, 'accessToken': 'fixture-snowluma'}]}})
        self.assertEqual(self.run_resolver('napcat')['endpoint'], 'http://127.0.0.1:8721')
        self.assertEqual(self.run_resolver('snowluma')['endpoint'], 'http://127.0.0.1:8730')

    def test_missing_and_corrupt_snowluma_do_not_use_the_valid_napcat(self):
        self.run_resolver('snowluma', should_fail=True)
        path = self.write('snowluma/config/onebot.json', {})
        path.write_text('{', encoding='utf-8')
        self.run_resolver('snowluma', should_fail=True)


if __name__ == '__main__':
    unittest.main()
