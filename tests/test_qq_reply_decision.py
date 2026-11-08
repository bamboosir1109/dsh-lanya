"""Exercise real PowerShell decision code with model and delivery replaced locally.

Never run groupwatch's startup, polling, HTTP, state files or real model process.
The integration fixture extracts its actual decision-to-delivery statements by AST.
"""
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest


SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
POWERSHELL = shutil.which("powershell.exe")

FIXTURE = r"""
param([string]$Source, [string]$Helper, [string]$Cases)
$ErrorActionPreference = 'Stop'
. $Helper
$fixtures = Get-Content -LiteralPath $Cases -Raw -Encoding UTF8 | ConvertFrom-Json
$errors = $null
$tokens = $null
$ast = [System.Management.Automation.Language.Parser]::ParseFile($Source, [ref]$tokens, [ref]$errors)
if ($errors.Count) { throw ('groupwatch parse errors: ' + $errors.Count) }
$assignments = @($ast.FindAll({ param($node)
  $node -is [System.Management.Automation.Language.AssignmentStatementAst] -and
  $node.Left -is [System.Management.Automation.Language.VariableExpressionAst] -and
  $node.Left.VariablePath.UserPath -ceq 'decision'
}, $true))
if ($assignments.Count -ne 1) { throw 'Expected one production decision entry point' }
$entry = $assignments[0]
if ($entry.Parent -isnot [System.Management.Automation.Language.StatementBlockAst]) {
  throw 'Decision must be in the main try block'
}
# Use every original statement from the decision through both delivery calls.
# Only the enclosing poll loop/startup are omitted; no production guards are recreated.
$sourceText = [IO.File]::ReadAllText($Source)
$tail = $sourceText.Substring($entry.Extent.StartOffset,
  $entry.Parent.Extent.EndOffset - $entry.Extent.StartOffset - 1)
$cycle = [scriptblock]::Create("while (`$true) {`n" + $tail + "`nbreak`n}")

function Ask-Agent([string]$prompt) {
  $script:asks++
  if ($script:fixture.fail -eq 'timeout') { throw (New-Object System.TimeoutException('offline timeout')) }
  if ($script:fixture.fail) { throw 'offline model failure' }
  return $script:fixture.answer
}
function Invoke-NapCat([string]$action, $payload) {
  $script:sends.Add([pscustomobject]@{action=$action; payload=$payload})
  return '{"retcode":0}'
}
function Get-MemeIfDue {
  $script:images++
  return 'offline-meme.png'
}
function Get-Random { $script:randoms++; throw 'Unexpected random fallback' }
function Write-Log([string]$message) { $script:logs.Add($message) }
function Save-State { $script:saves++ }
function Start-Sleep { throw 'A once-only offline fixture must not sleep' }

$results = @(foreach ($item in $fixtures) {
  $script:fixture = $item
  $script:asks = 0; $script:images = 0; $script:randoms = 0; $script:saves = 0
  $script:sends = New-Object 'System.Collections.Generic.List[object]'
  $script:logs = New-Object 'System.Collections.Generic.List[string]'
  $task = 'Offline QQ context, candidate trigger only'
  $triggerKey = $item.trigger
  $Once = $true; $DryRun = $false; $PollSeconds = 0; $MemeEvery = 1; $GroupId = 'offline-group'
  # Make the image fallback due: a missing decision guard would now attempt an image.
  $state = [pscustomobject]@{
    speakSinceMeme=10; pendingMention=@{qq='offline'; at='2026-01-01T00:00:00'};
    lastSendAt=$null; sendTimes=@(); sendTimesQuiet=@()
  }
  if ($item.control) { $state.speakSinceMeme = 0 }
  & $cycle
  [pscustomobject]@{
    name=$item.name; asks=$script:asks; images=$script:images; randoms=$script:randoms;
    saves=$script:saves; sends=@($script:sends.ToArray()); logs=@($script:logs.ToArray())
  }
})
ConvertTo-Json -InputObject $results -Depth 8 -Compress
"""


@unittest.skipUnless(POWERSHELL, "Windows PowerShell required")
class QqReplyDecisionTests(unittest.TestCase):
    def run_powershell(self, body, arguments=()):
        with tempfile.TemporaryDirectory(prefix="qq decision offline ") as folder:
            root = Path(folder)
            script = root / "fixture.ps1"
            script.write_text(body, encoding="utf-8-sig")
            env = dict(os.environ)
            for key in list(env):
                if key.startswith(("LANYA_", "DSH_")):
                    env.pop(key)
            result = subprocess.run(
                [POWERSHELL, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(script), *arguments],
                capture_output=True, text=True, encoding="utf-8-sig", errors="replace", timeout=30, env=env,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            return json.loads(result.stdout)

    def cycles(self, cases):
        with tempfile.TemporaryDirectory(prefix="qq decision cases ") as folder:
            cases_file = Path(folder) / "cases.json"
            cases_file.write_text(json.dumps(cases), encoding="utf-8")
            return self.run_powershell(FIXTURE, (str(SCRIPTS / "groupwatch.ps1"),
                                               str(SCRIPTS / "qq_reply_decision.ps1"), str(cases_file)))

    def test_rejected_decisions_never_retry_or_reach_text_or_image_sinks(self):
        outputs = {
            "silent": "SILENT", "empty": "", "whitespace": " \r\n\t",
            "empty_speak": "SPEAK\n", "blank_reply_with_image": "SPEAK\n\nIMG happy",
            "inline_speak": "SPEAK hello", "prefix_speak": "SPEAKER\nhello",
            "lowercase": "speak\nhello", "silent_with_explanation": "SILENT\nkeeping quiet",
            "invalid_image": "SPEAK\nhello\nimage happy",
            "extra_line": "SPEAK\nhello\nIMG happy\nextra",
            "stage": "SPEAK\n(waves)", "wide_stage": "SPEAK\n（保持沉默）",
            "reserved_reply": "SPEAK\nSILENT", "image_instead_of_text": "SPEAK\nIMG happy",
        }
        cases = [{"name": name + "-" + trigger, "trigger": trigger, "answer": answer}
                 for name, answer in outputs.items() for trigger in ("mention", "active", "quiet")]
        for fail in ("timeout", "failed"):
            cases.append({"name": fail, "trigger": "mention", "fail": fail})
        results = self.cycles(cases)
        self.assertEqual(len(results), len(cases))
        for result in results:
            with self.subTest(case=result["name"]):
                self.assertEqual(result["asks"], 1)
                self.assertEqual(result["sends"], [])
                self.assertEqual(result["images"], 0)
                self.assertEqual(result["randoms"], 0)
                self.assertEqual(result["saves"], 1)
                self.assertTrue(any("decision=SILENT" in log for log in result["logs"]))
                if result["name"] == "timeout":
                    self.assertTrue(any("TimeoutException" in log for log in result["logs"]))

    def test_valid_speak_still_reaches_original_delivery_branch_once(self):
        cases = [{"name": trigger, "trigger": trigger, "answer": "SPEAK\r\nhello",
                  "control": True} for trigger in ("mention", "active")]
        results = self.cycles(cases)
        self.assertEqual(len(results), len(cases))
        for result in results:
            with self.subTest(trigger=result["name"]):
                self.assertEqual(result["asks"], 1)
                self.assertEqual(result["images"], 0)
                self.assertEqual(result["randoms"], 0)
                self.assertEqual(len(result["sends"]), 1)
                self.assertEqual(result["sends"][0]["action"], "send_group_msg")
                self.assertEqual(result["sends"][0]["payload"]["message"], "hello")

    def test_parser_preserves_valid_text_and_image_tag(self):
        code = r"""
param([string]$Helper)
$ErrorActionPreference = 'Stop'
. $Helper
@(ConvertFrom-QqReplyDecision "SPEAK`r`n hello `r`nIMG happy";
  ConvertFrom-QqReplyDecision "SPEAK`nhello`n IMG speechless_2-okay ";
  ConvertFrom-QqReplyDecision (([char]0xFEFF) + "SPEAK`nhello");
  ConvertFrom-QqReplyDecision "`r`nSILENT`r`n") | ConvertTo-Json -Compress
"""
        results = self.run_powershell(code, (str(SCRIPTS / "qq_reply_decision.ps1"),))
        self.assertEqual([(r["Action"], r["Text"], r["ImageTag"]) for r in results], [
            ("SPEAK", "hello", "happy"), ("SPEAK", "hello", "speechless_2-okay"),
            ("SPEAK", "hello", ""), ("SILENT", "", ""),
        ])

    def test_real_agent_reader_discards_valid_looking_partial_output_on_failure(self):
        # Extract only Ask-Agent, then replace its executable with an inert local
        # batch file. No profile, model, network or real groupwatch is executed.
        code = r"""
param([string]$Source, [string]$Helper, [string]$Stub, [string]$Work)
$ErrorActionPreference = 'Stop'
. $Helper
$errors=$null; $tokens=$null
$ast=[System.Management.Automation.Language.Parser]::ParseFile($Source,[ref]$tokens,[ref]$errors)
if($errors.Count) { throw 'Source parse failed' }
$function=@($ast.FindAll({param($node)
  $node -is [System.Management.Automation.Language.FunctionDefinitionAst] -and $node.Name -eq 'Ask-Agent'
},$true))
if($function.Count -ne 1) { throw 'Expected one Ask-Agent function' }
Invoke-Expression $function[0].Extent.Text
$dsh=$Stub; $root=$Work
Get-QqReplyDecision -Task 'offline' -Ask {param($prompt) Ask-Agent $prompt -TimeoutMilliseconds 200} |
  ConvertTo-Json -Compress
"""
        with tempfile.TemporaryDirectory(prefix="qq inert cli ") as folder:
            root = Path(folder)
            stub = root / "offline.cmd"
            for name, ending, expected_action in (
                ("nonzero", "exit /b 7", "SILENT"),
                ("timeout", ":wait\r\ngoto wait", "SILENT"),
                ("success", "exit /b 0", "SPEAK"),
            ):
                with self.subTest(case=name):
                    stub.write_bytes(("@echo off\r\necho SPEAK\r\necho local fixture\r\n" +
                                      ending + "\r\n").encode("ascii"))
                    result = self.run_powershell(code, (str(SCRIPTS / "groupwatch.ps1"),
                                                       str(SCRIPTS / "qq_reply_decision.ps1"),
                                                       str(stub), str(root)))
                    self.assertEqual(result["Action"], expected_action)
                    self.assertEqual(result["Text"], "local fixture" if name == "success" else "")
                    if name != "success":
                        self.assertEqual(result["Reason"], "ask_error")
                    if name == "timeout":
                        self.assertEqual(result["ErrorType"], "TimeoutException")


if __name__ == "__main__":
    unittest.main()
