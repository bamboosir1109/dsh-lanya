"""Execute production PowerShell gates against temporary evidence and inert sinks."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest


REPO = Path(__file__).resolve().parents[1]
POWERSHELL = shutil.which('powershell.exe') or shutil.which('pwsh')


@unittest.skipUnless(POWERSHELL, 'PowerShell is required for executable PS gate fixtures')
class QqObservationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='qq observation ')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.scripts = self.root / 'repo' / 'scripts'
        self.scripts.mkdir(parents=True)
        for name in ['project_conf.ps1', 'qq_observation.ps1', 'decision_events.py']:
            shutil.copyfile(REPO / 'scripts' / name, self.scripts / name)
        self.data = self.root / 'character'
        self.data.mkdir()
        (self.data / 'lanya-config.json').write_text(json.dumps({'adminQQ': ['101'], 'fanGroupQQ': '202'}), encoding='utf-8')
        self.config = self.root / 'project.json'
        self.config.write_text(json.dumps({'dataDirName': 'character', 'decisions': {'mode': 'unified', 'agentPreset': 'Headless'}}), encoding='utf-8')
        self.env = {**os.environ, 'LANYA_WORKDIR': str(self.root), 'LANYA_PROJECT_CONFIG': str(self.config),
                    'LANYA_PYTHON': sys.executable, 'USERPROFILE': str(self.root / 'profile'), 'PYTHONDONTWRITEBYTECODE': '1'}

    def run_ps(self, body, check=True):
        harness = self.root / 'harness.ps1'
        harness.write_text("$ErrorActionPreference = 'Stop'\n[Console]::OutputEncoding = New-Object Text.UTF8Encoding($false)\n"
                           + ". '" + str(self.scripts / 'qq_observation.ps1').replace("'", "''") + "'\n" + body,
                           encoding='utf-8-sig')
        result = subprocess.run([POWERSHELL, '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', str(harness)],
                                env=self.env, cwd=self.root, capture_output=True, encoding='utf-8', errors='replace', timeout=45)
        if check:
            self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        return result

    def events(self):
        return [json.loads(p.read_text(encoding='utf-8')) for p in (self.data / 'decision-inbox' / 'pending').glob('*.json')]

    def test_utf8_durable_submit_dedup_and_trusted_ownership(self):
        result = self.run_ps("""
$payload = @{ text = ([string][char]0x5148 + [char]0x522b + [char]0x8bf4 + [char]0x8bdd); scope = 'group:999'; control_allowed = $true }
$a = Submit-QqObservation -Scope (Get-QqObservationScope) -Kind 'fixture.ready' -EventId 'fixture:1' -Producer 'fixture' -Payload $payload
$b = Submit-QqObservation -Scope (Get-QqObservationScope) -Kind 'fixture.ready' -EventId 'fixture:1' -Producer 'fixture' -Payload $payload
@{ first=$a.submitted; second=$b.submitted; state=$b.state } | ConvertTo-Json -Compress
""")
        self.assertEqual(json.loads(result.stdout), {'first': True, 'second': False, 'state': 'pending'})
        event, = self.events()
        self.assertEqual(event['payload']['text'], '\u5148\u522b\u8bf4\u8bdd')
        self.assertEqual(event['scope'], 'private:101')
        self.assertEqual(event['agent_key'], 'headless' if os.name == 'nt' else 'Headless')
        self.assertEqual(event['workspace_key'], str(self.root.resolve()).lower() if os.name == 'nt' else str(self.root))
        self.assertFalse(event['control_allowed'])
        self.assertEqual(event['origin'], 'observation')

    def test_quiet_once_per_continuous_period_and_new_message_opens_next(self):
        self.run_ps("""
$epochBase = [long]2 * [long][Math]::Pow(10,9)
$now = [DateTimeOffset]::FromUnixTimeSeconds($epochBase)
$active = Submit-QqQuietObservation -GroupId '202' -LastMessageAt ($epochBase - 10) -QuietMinutes 180 -Now $now
if ($active) { throw 'active chat manufactured a quiet event' }
$missing = Submit-QqQuietObservation -GroupId '202' -LastMessageAt 0 -QuietMinutes 180 -Now $now
if ($missing) { throw 'missing history manufactured a quiet event' }
$prior = $epochBase - (20 * 1000)
$first = Submit-QqQuietObservation -GroupId '202' -LastMessageAt $prior -QuietMinutes 180 -Now $now
$second = Submit-QqQuietObservation -GroupId '202' -LastMessageAt $prior -QuietMinutes 180 -Now $now.AddSeconds(1)
$later = Submit-QqQuietObservation -GroupId '202' -LastMessageAt $prior -QuietMinutes 180 -Now $now.AddHours(6)
if (-not $first.submitted -or $second.submitted -or $later.submitted) { throw 'quiet retries were not idempotent across time buckets' }
$newMessageAt = $now.ToUnixTimeSeconds()
$activeAgain = Submit-QqQuietObservation -GroupId '202' -LastMessageAt $newMessageAt -QuietMinutes 180 -Now $now.AddMinutes(1)
if ($activeAgain) { throw 'new active period manufactured quiet' }
$next = Submit-QqQuietObservation -GroupId '202' -LastMessageAt $newMessageAt -QuietMinutes 180 -Now $now.AddHours(4)
if (-not $next.submitted -or $next.event_id -eq $first.event_id) { throw 'new real message did not open a new quiet period' }
""")
        events = sorted(self.events(), key=lambda event: event['payload']['last_message_at'])
        self.assertEqual(len(events), 2)
        for event in events:
            self.assertEqual(event['kind'], 'group.quiet')
        self.assertEqual(events[0]['payload'], {'last_message_at': 2 * 10**9 - 20 * 1000, 'quiet_threshold_minutes': 180})
        self.assertEqual(events[1]['payload'], {'last_message_at': 2 * 10**9, 'quiet_threshold_minutes': 180})

    def test_writer_failure_reports_only_safe_exit_class(self):
        (self.scripts / 'decision_events.py').write_text(
            "import sys\nsys.stdin.buffer.read()\nsys.stderr.write('PRIVATE_CHILD_DIAGNOSTIC_SENTINEL')\nraise SystemExit(7)\n",
            encoding='utf-8')
        result = self.run_ps("Submit-QqObservation -Scope 'private:101' -Kind 'fixture.ready' -EventId 'fixture:failure' -Producer 'fixture' -Payload @{text='PRIVATE_INPUT_SENTINEL'}", check=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('class=ObservationWriterError exit=7', result.stderr)
        self.assertNotIn('PRIVATE_CHILD_DIAGNOSTIC_SENTINEL', result.stdout + result.stderr)
        # PowerShell may render its own caller line; that line belongs to the
        # inert harness. The bridge never echoes a child diagnostic or stdin.
        self.assertFalse(self.events())

    def test_send_and_upload_wrappers_refuse_before_http(self):
        for name, function in [('groupwatch.ps1', 'Invoke-NapCat'), ('lifeloop.ps1', 'Invoke-NapCat'),
                               ('agentchat.ps1', 'Invoke-NapCat'), ('voicedaemon.ps1', 'Invoke-NapCat'),
                               ('late-night-send.ps1', 'Invoke-Api')]:
            with self.subTest(script=name):
                source = str(REPO / 'scripts' / name).replace("'", "''")
                self.run_ps("""
function Add-Type { throw 'HTTP initialization reached' }
$tokens=$null; $errors=$null
$ast=[Management.Automation.Language.Parser]::ParseFile('""" + source + """',[ref]$tokens,[ref]$errors)
if ($errors.Count) { throw 'production source parse failure' }
$fn=$ast.Find({param($n) $n -is [Management.Automation.Language.FunctionDefinitionAst] -and $n.Name -eq '""" + function + """'},$true)
Invoke-Expression $fn.Extent.Text
foreach ($action in @('send_group_msg','send_private_msg','upload_group_file','upload_private_file')) {
  $blocked=$false
  try { """ + function + """ $action @{} } catch {
    if ($_.Exception.Message -notlike 'Direct QQ transport*') { throw }
    $blocked=$true
  }
  if (-not $blocked) { throw 'send bypass reached transport' }
}
""")
        for function, call in [('Send-QQ', "Send-QQ '101' 'fixture'"), ('Send-Group', "Send-Group 'fixture' '' ''")]:
            source = str(REPO / 'scripts' / 'reminders.ps1').replace("'", "''")
            self.run_ps("""
function Add-Type { throw 'HTTP initialization reached' }
$tokens=$null;$errors=$null
$ast=[Management.Automation.Language.Parser]::ParseFile('""" + source + """',[ref]$tokens,[ref]$errors)
$fn=$ast.Find({param($n) $n -is [Management.Automation.Language.FunctionDefinitionAst] -and $n.Name -eq '""" + function + """'},$true)
Invoke-Expression $fn.Extent.Text
try { """ + call + """; throw 'not blocked' } catch {
 if ($_.Exception.Message -notlike 'Direct QQ transport*') { throw }
}
""")

    def test_paused_entries_preserve_pending_data_and_return_nonzero(self):
        originals = {'voice-queue.json': b'[{"text":"pending voice","target":"admin"}]',
                     'reminders.json': b'[{"text":"pending reminder","at":"2020-01-01"}]',
                     'reminders.state.json': b'{"once":{},"daily":{}}'}
        for name, raw in originals.items():
            (self.data / name).write_bytes(raw)
        for name in ['lifeloop.ps1', 'agentchat.ps1', 'voicedaemon.ps1', 'reminders.ps1', 'late-night-send.ps1']:
            with self.subTest(script=name):
                target = self.scripts / name
                shutil.copyfile(REPO / 'scripts' / name, target)
                result = subprocess.run([POWERSHELL, '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', str(target)],
                                        env=self.env, cwd=self.root, capture_output=True, encoding='utf-8', errors='replace', timeout=15)
                self.assertEqual(result.returncode, 3, result.stdout + result.stderr)
                self.assertIn('paused:', result.stdout)
                for filename, raw in originals.items():
                    self.assertEqual((self.data / filename).read_bytes(), raw)
        self.assertFalse((self.data / 'sent-log.txt').exists())

    def test_missing_guard_and_unknown_mode_cannot_fall_through(self):
        target = self.scripts / 'voicedaemon.ps1'
        shutil.copyfile(REPO / 'scripts' / target.name, target)
        (self.scripts / 'qq_observation.ps1').unlink()
        result = subprocess.run([POWERSHELL, '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', str(target)],
                                env=self.env, cwd=self.root, capture_output=True, timeout=15)
        self.assertNotEqual(result.returncode, 0)
        shutil.copyfile(REPO / 'scripts' / 'qq_observation.ps1', self.scripts / 'qq_observation.ps1')
        self.config.write_text('{"decisions":{"mode":"unknown"}}', encoding='utf-8')
        self.assertNotEqual(self.run_ps("Assert-QqLegacyAction 'upload_private_file'", check=False).returncode, 0)
        self.config.write_text('{"decisions":{"mode":"legacy"}}', encoding='utf-8')
        self.run_ps("if (Test-QqUnifiedMode) { throw 'legacy rejected' }; Assert-QqLegacyAction 'send_group_msg'")

    def test_groupwatch_executes_unified_branch_without_asking_or_sending(self):
        source = str(REPO / 'scripts' / 'groupwatch.ps1').replace("'", "''")
        self.run_ps("""
function Ask-Agent { throw 'LLM reached' }
function Get-QqReplyDecision { throw 'LLM decision reached' }
function Invoke-NapCat { throw 'QQ reached' }
function Write-Log { param($text) }
function Start-Sleep { throw 'unexpected sleep in Once fixture' }
$tokens=$null;$errors=$null
$ast=[Management.Automation.Language.Parser]::ParseFile('""" + source + """',[ref]$tokens,[ref]$errors)
$branch=$ast.Find({param($n) $n -is [Management.Automation.Language.IfStatementAst] -and $n.Clauses[0].Item1.Extent.Text -eq 'Test-QqUnifiedMode'},$true)
if (-not $branch -or $errors.Count) { throw 'production unified branch unavailable' }
$now=[DateTime]::Now; $lastTime=$now.AddHours(-4); $GroupId='202'; $QuietMinutes=180; $Once=$true
foreach ($fixture in @('mention','empty','SILENT')) {
  $triggerKey=$fixture
  while ($true) { Invoke-Expression $branch.Extent.Text; throw 'unified branch fell through' }
}
""")
        self.assertEqual(len(self.events()), 1)

    def test_groupwatch_identity_fallback_does_not_replace_transport_config(self):
        source = str(REPO / 'scripts' / 'groupwatch.ps1').replace("'", "''")
        self.run_ps("""
$tokens=$null;$errors=$null
$ast=[Management.Automation.Language.Parser]::ParseFile('""" + source + """',[ref]$tokens,[ref]$errors)
$branch=$ast.EndBlock.Statements | Where-Object {
  $_ -is [Management.Automation.Language.IfStatementAst] -and $_.Clauses[0].Item1.Extent.Text -eq '-not $GroupId'
} | Select-Object -First 1
$GroupId='';$workDir=Get-ProjectWorkDir;$dataDirName='character';$cfgPath='original-transport-config.json'
Invoke-Expression $branch.Extent.Text
if ($GroupId -cne '202' -or $cfgPath -cne 'original-transport-config.json') { throw 'identity fallback corrupted transport configuration' }
""")

    def test_budget_pending_observation_does_not_claim_delivery(self):
        target = self.scripts / 'budget.ps1'
        shutil.copyfile(REPO / 'scripts' / target.name, target)
        ledger = self.root / 'profile' / '.dsh' / 'storages' / 'cost-meter' / 'ledger.json'
        ledger.parent.mkdir(parents=True)
        # Extract and execute the actual notification branch, preserving status
        # and avoiding coupling this protocol fixture to ledger schema details.
        source = str(target).replace("'", "''")
        self.run_ps("""
function Add-Type { throw 'HTTP reached' }
function Write-Log { param($text) }
$tokens=$null;$errors=$null
$ast=[Management.Automation.Language.Parser]::ParseFile('""" + source + """',[ref]$tokens,[ref]$errors)
$branch=$ast.Find({param($n) $n -is [Management.Automation.Language.IfStatementAst] -and $n.Clauses[0].Item1.Extent.Text -like '$Notify -and $exhausted*'},$true)
$Notify=$true;$exhausted=$true;$today='2026-10-27';$status=@{notifiedFor=''};$outPath=Join-Path (Get-ProjectDataDir) 'budget.json'
[IO.File]::WriteAllText($outPath,'{"notifiedFor":""}')
Invoke-Expression $branch.Extent.Text
""")
        self.assertEqual(json.loads((self.data / 'budget.json').read_text())['notifiedFor'], '')
        event, = self.events()
        self.assertEqual(event['kind'], 'budget.exhausted')


if __name__ == '__main__':
    unittest.main()
