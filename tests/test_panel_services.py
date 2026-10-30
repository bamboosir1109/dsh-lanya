"""Works status/control and Windows state reducers; all service I/O is inert."""
import base64
import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
from types import SimpleNamespace
import unittest
from unittest import mock
import test_panel_autonomy as fixture


class PanelServiceTests(unittest.TestCase):
    setUp = fixture.PanelAutonomyTests.setUp
    write = fixture.PanelAutonomyTests.write
    route = fixture.PanelAutonomyTests.route

    def services(self):
        return {role: {"role": role, "port": port, "count": 1, "mb": mb, "healthy": True, "listening": True, "status": "healthy"}
                for role, port, mb in (("works", 19388, 3), ("api", 19389, 7))}

    def test_newest_valid_canonical_or_legacy_snapshot_is_used_with_fresh_service_counts(self):
        canonical = self.data / "made" / "lanya-state.json"
        legacy = self.data / "made" / "dafeiyu-state.json"
        self.write(legacy, {"at": "fresh-legacy", "memory": {"dafeiyuMb": 10},
                            "components": [{"tag": "works", "count": 0, "mb": 0}]})
        os.utime(canonical, (100, 100)); os.utime(legacy, (200, 200))
        with mock.patch.object(self.module, "panel_services", return_value=self.services()):
            state = self.module.panel_state_snapshot()
            self.assertEqual(state["at"], "fresh-legacy")
            self.assertEqual(state["memory"]["lanyaMb"], 20)
            self.assertEqual(state["memory"]["dafeiyuMb"], 20)
            works = next(row for row in state["components"] if row["tag"] == "works")
            self.assertEqual((works["count"], works["mb"], works["control"]), (1, 3, "ensure_works"))
            self.write(canonical, {"at": "fresh-canonical", "memory": {}, "components": []})
            self.assertEqual(self.module.panel_state_snapshot()["at"], "fresh-canonical")
            canonical.write_text("{broken", encoding="utf-8")
            self.assertEqual(self.module.panel_state_snapshot()["at"], "fresh-legacy")

    def test_health_endpoint_distinguishes_ports_and_attests_loaded_program(self):
        for port, role in ((19388, "works"), (19389, "api")):
            handler = object.__new__(self.module.Handler)
            handler.path = "/api/service-health"
            handler.server = SimpleNamespace(server_port=port)
            handler._json = lambda code, value: (code, value)
            code, value = handler.do_GET()
            self.assertEqual((code, value["port"], value["role"]), (200, port, role))
            self.assertEqual(value["pid"], os.getpid())
            self.assertEqual(value["script_sha256"], hashlib.sha256((fixture.REPO / "server" / "panel-server.py").read_bytes()).hexdigest())

    def test_works_control_uses_host_supervisor_without_scheduled_task_or_detached_script(self):
        calls = []
        def response(request, **kwargs):
            calls.append(request)
            return io.BytesIO(json.dumps({"ok": True, "service_check": True, "status": "starting", "healthy": False}).encode())
        with mock.patch("urllib.request.urlopen", side_effect=response):
            result = self.module.control_action({"target": "works", "op": "restart"})
            self.assertTrue(result["service_check"])
            self.assertFalse(result["healthy"])
            self.assertEqual(json.loads(calls[0].data), {"target": "works", "op": "ensure"})
            self.assertEqual(calls[0].full_url, "http://127.0.0.1:19387/dsh-lanya/api/action")
        for method in (self.module.run_task, self.module.run_script):
            with self.assertRaises(ValueError): method("works")
        self.assertFalse(self.module.control_action({"target": "works", "op": "stop"})["ok"])

    def test_bad_host_service_metadata_cannot_make_a_fake_green_component(self):
        for change in ({"count": 2}, {"port": 19389}, {"count": True}, {"listening": False}):
            values = self.services(); values["works"].update(change)
            with mock.patch("urllib.request.urlopen", return_value=io.BytesIO(json.dumps({"ok": True, "services": values}).encode())):
                status = self.module.panel_services()
                self.assertFalse(status["works"]["healthy"])
                self.assertIsNone(status["works"]["count"])

    def test_activity_ready_uses_http_health_rather_than_process_count(self):
        self.write(self.data / "activities.json", {"activities": [{"id": "fixture", "bound": "works"}]})
        values = self.services(); values["works"].update(status="unhealthy", healthy=False)
        with mock.patch.object(self.module, "panel_services", return_value=values):
            activity = self.route("/api/activities")[1]["data"]["activities"][0]
            self.assertFalse(activity["ready"])


class WindowsStateReducerTests(unittest.TestCase):
    @unittest.skipUnless(os.name == "nt", "Windows PowerShell 5 reducer fixture")
    def test_both_state_scripts_count_only_the_real_static_owner_and_verify_http(self):
        # Parse and evaluate one pure function; never execute the state scripts.
        code = r'''
$ErrorActionPreference='Stop'
$script='C:\fixture space\napcat\lanya-panel-server.py';$hash='a'*64;$checks=0
foreach($file in @('state.ps1','dafeiyu-state.ps1')){
 $tokens=$null;$errors=$null;$ast=[Management.Automation.Language.Parser]::ParseFile((Join-Path $env:LANYA_FIXTURE_SCRIPTS $file),[ref]$tokens,[ref]$errors)
 if(@($errors).Count){throw 'parse failed'}
 $nodes=@($ast.FindAll({param($n)$n-is[Management.Automation.Language.FunctionDefinitionAst]-and$n.Name-eq'Get-WorksServiceRow'},$false));if($nodes.Count-ne1){throw 'function count'}
 . ([scriptblock]::Create($nodes[0].Extent.Text))
 $p=[pscustomobject]@{Name='python.exe';ProcessId=101;WorkingSetSize=10MB;CommandLine=('python "'+$script+'" --port 19388')}
 $api=[pscustomobject]@{Name='python.exe';ProcessId=102;WorkingSetSize=20MB;CommandLine=('python "'+$script+'" --port 19389')}
 $listener=[pscustomobject]@{LocalPort=19388;OwningProcess=101}
 $health=[pscustomobject]@{ok=$true;version=1;service='lanya-panel';role='works';port=19388;pid=101;script_sha256=$hash}
 $r=Get-WorksServiceRow @($p,$api) @($listener) $health $script 19388 $hash;if($r.count-ne1-or$r.mb-ne10-or-not$r.service.healthy){throw 'real count or health'};$checks++
 $p.CommandLine=('python -B "'+$script+'" --port 19388');$r=Get-WorksServiceRow @($p,$api) @($listener) $health $script 19388 $hash;if($r.count-ne1-or-not$r.service.healthy){throw 'deployed -B rejected'};$checks++
 $p.CommandLine=('python -c "'+$script+'" --port 19388');$r=Get-WorksServiceRow @($p) @($listener) $health $script 19388 $hash;if($r.count-ne0-or$r.service.healthy){throw '-c treated as script'};$checks++
 $p.CommandLine=('python "'+$script+'" --port 19388')
 $r=Get-WorksServiceRow @($api) @() $null $script 19388 $hash;if($r.count-ne0-or$r.service.healthy){throw 'API counted as works'};$checks++
 $r=Get-WorksServiceRow @($p) @($listener) $null $script 19388 $hash;if($r.count-ne1-or$r.service.healthy){throw 'process alone green'};$checks++
 $other=[pscustomobject]@{LocalPort=19388;OwningProcess=999};$r=Get-WorksServiceRow @($p) @($other) $health $script 19388 $hash;if($r.service.healthy){throw 'wrong owner green'};$checks++
 $health.pid=999;$r=Get-WorksServiceRow @($p) @($listener) $health $script 19388 $hash;if($r.service.healthy){throw 'wrong health pid'};$checks++;$health.pid=101
 $health.version=$true;$r=Get-WorksServiceRow @($p) @($listener) $health $script 19388 $hash;if($r.service.healthy){throw 'boolean schema'};$checks++;$health.version=1
 $r=Get-WorksServiceRow @($p) @($listener) $health $script 19388 ('b'*64);if($r.service.healthy){throw 'old module green'};$checks++
 $p.CommandLine='python "C:\other\napcat\lanya-panel-server.py" --port 19388';$r=Get-WorksServiceRow @($p) @($listener) $health $script 19388 $hash;if($r.count-ne0-or$r.service.healthy){throw 'other workspace counted'};$checks++
}
if($checks-ne20){throw 'check count'}
Write-Output 'panel_state_fixture_20_pass'
'''
        env = dict(os.environ, LANYA_FIXTURE_SCRIPTS=str(fixture.REPO / "scripts"))
        result = subprocess.run([r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe", "-NoProfile", "-EncodedCommand",
            base64.b64encode(code.encode("utf-16le")).decode()], capture_output=True, timeout=20, env=env,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        self.assertEqual(result.returncode, 0, result.stderr.decode("utf-8", "backslashreplace"))
        self.assertIn(b"panel_state_fixture_20_pass", result.stdout)


if __name__ == "__main__": unittest.main()
