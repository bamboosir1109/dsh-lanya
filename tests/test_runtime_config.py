"""Configuration and panel regressions; all files and credentials are fixtures."""
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

REPO = Path(__file__).resolve().parents[1]


def load_module(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class RuntimeConfigTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="lanya-config-")
        self.addCleanup(self.tmp.cleanup)
        self.work = Path(self.tmp.name).resolve()
        self.repo = self.work / "checkout"
        self.scripts = self.repo / "scripts"
        self.live = self.work / "napcat"
        self.scripts.mkdir(parents=True)
        self.live.mkdir()
        for folder in (self.scripts, self.live):
            for name in ("project_conf.py", "project_conf.ps1"):
                shutil.copy2(REPO / "scripts" / name, folder / name)
        self.env = patch.dict(os.environ, {
            "LANYA_PROJECT_CONFIG": "", "LANYA_WORKDIR": "",
            "HOME": str(self.work), "USERPROFILE": str(self.work),
        })
        self.env.start()
        self.addCleanup(self.env.stop)
        old_cwd = os.getcwd()
        os.chdir(self.work)
        self.addCleanup(os.chdir, old_cwd)

    def config(self, base, data, bom=False):
        path = base / "config" / "project.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data), encoding="utf-8-sig" if bom else "utf-8")
        return path

    def python_config(self, live=False):
        return load_module((self.live if live else self.scripts) / "project_conf.py", "fixture_conf")

    def powershell_config(self, live=False):
        helper = (self.live if live else self.scripts) / "project_conf.ps1"
        quoted = str(helper).replace("'", "''")
        command = ("[Console]::OutputEncoding = New-Object Text.UTF8Encoding($false); "
                   ". '%s'; $c = Get-ProjectConf; "
                   "@{conf=$c;path=(Get-ProjectConfPath);work=(Get-ProjectWorkDir);"
                   "data=(Get-ProjectDataDir)} | ConvertTo-Json -Depth 8" % quoted)
        process = subprocess.run(["powershell", "-NoProfile", "-Command", command],
                                 capture_output=True, text=True, encoding="utf-8", timeout=20, check=True)
        return json.loads(process.stdout)

    def powershell_probe(self, script, live=True):
        helper = (self.live if live else self.scripts) / "project_conf.ps1"
        command = ("[Console]::OutputEncoding = New-Object Text.UTF8Encoding($false); "
                   ". '%s'; " % str(helper).replace("'", "''")) + script
        process = subprocess.run(["powershell", "-NoProfile", "-Command", command],
                                 capture_output=True, text=True, encoding="utf-8", timeout=20, check=True)
        self.assertEqual(process.stderr, "")
        return json.loads(process.stdout)

    def test_source_does_not_load_adjacent_private_install(self):
        expected = self.config(self.repo, {"dataDirName": "public-role"})
        self.config(self.live, {"dataDirName": "private-role"})
        conf = self.python_config()
        self.assertEqual(conf.config_path(), str(expected))
        self.assertEqual(conf.work_dir(), str(self.work))
        self.assertEqual(conf.data_dir(), str(self.work / "public-role"))

    def test_installed_config_precedence_bom_and_nested_defaults(self):
        self.config(self.work, {"dataDirName": "legacy-role"})
        expected = self.config(self.live, {"dataDirName": "configured-role", "daemon": {"pollSeconds": 7}}, True)
        conf = self.python_config(live=True)
        self.assertEqual(conf.config_path(), str(expected))
        self.assertEqual(conf.get("daemon.pollSeconds"), 7)
        self.assertEqual(conf.get("daemon.maxConcurrentAgents"), 3)
        self.assertEqual(conf.data_dir(), str(self.work / "configured-role"))

    def test_invalid_or_missing_explicit_override_never_uses_legacy_fallback(self):
        invalid = self.work / "invalid.json"
        os.environ["LANYA_PROJECT_CONFIG"] = str(invalid)
        self.config(self.live, {"decisions": {"mode": "legacy"}})
        self.config(self.work, {"panelPort": 22334, "decisions": {"mode": "legacy"}})
        for content in (None, "[1, 2]", "null", '"private-fixture-do-not-echo"', '{"private-fixture-do-not-echo":'):
            with self.subTest(content_type=type(content).__name__):
                if content is not None:
                    invalid.write_text(content, encoding="utf-8")
                conf = self.python_config(live=True)
                with self.assertRaises(conf.ConfigError) as caught:
                    conf.get("decisions.mode", "legacy")
                self.assertNotIn("private-fixture-do-not-echo", str(caught.exception))
                self.assertIsNone(conf._CACHE)
                self.assertIsNone(conf._CONFIG_PATH)

    def test_first_existing_candidate_invalid_refuses_lower_priority_legacy(self):
        local = self.config(self.live, {})
        self.config(self.work, {"decisions": {"mode": "legacy"}})
        for content in ("{broken", "[]", "null", "123"):
            local.write_text(content, encoding="utf-8")
            conf = self.python_config(live=True)
            with self.assertRaises(conf.ConfigError):
                conf.load()
            self.assertIsNone(conf._CACHE)

    def test_unreadable_or_invalid_utf8_candidate_cannot_fall_back(self):
        local = self.config(self.live, {"decisions": {"mode": "legacy"}})
        self.config(self.work, {"decisions": {"mode": "legacy"}})
        conf = self.python_config(live=True)
        with patch("builtins.open", side_effect=PermissionError("private-fixture-do-not-echo")):
            with self.assertRaises(conf.ConfigError) as caught:
                conf.load()
        self.assertNotIn("private-fixture", str(caught.exception))
        local.write_bytes(b'{"decisions":{"mode":"legacy"},"invalid":"\xff"}')
        with self.assertRaises(conf.ConfigError):
            conf.get("decisions.mode", "legacy")
        self.assertIsNone(conf._CACHE)

    def test_only_no_configuration_defaults_to_unified(self):
        conf = self.python_config(live=True)
        self.assertEqual(conf.get("decisions.mode"), "unified")
        self.assertIsNone(conf.config_path())

    def test_failed_reload_revokes_cached_legacy_for_all_later_reads(self):
        override = self.work / "explicit.json"
        override.write_text('{"decisions":{"mode":"legacy"}}', encoding="utf-8")
        os.environ["LANYA_PROJECT_CONFIG"] = str(override)
        conf = self.python_config(live=True)
        self.assertEqual(conf.get("decisions.mode"), "legacy")
        for content in ("{private-fixture-do-not-echo", "[]", None):
            if content is None:
                override.unlink()
            else:
                override.write_text(content, encoding="utf-8")
            with self.assertRaises(conf.ConfigError):
                conf.load(reload=True)
            for read in (lambda: conf.get("decisions.mode", "legacy"), conf.config_path, conf.data_dir):
                with self.assertRaises(conf.ConfigError):
                    read()
                self.assertIsNone(conf._CACHE)
                self.assertIsNone(conf._CONFIG_PATH)
        override.write_text('{"decisions":{"mode":"unified"}}', encoding="utf-8")
        self.assertEqual(conf.get("decisions.mode"), "unified")

    def test_explicit_overrides_and_reload(self):
        override = self.work / "explicit.json"
        override.write_text('{"dataDirName":"explicit-role"}', encoding="utf-8-sig")
        os.environ["LANYA_PROJECT_CONFIG"] = str(override)
        os.environ["LANYA_WORKDIR"] = str(self.work / "other-workspace")
        conf = self.python_config()
        self.assertEqual(conf.data_dir(), str(self.work / "other-workspace" / "explicit-role"))
        override.write_text('{"dataDirName":"new-role"}', encoding="utf-8")
        self.assertEqual(conf.get("dataDirName"), "explicit-role")
        conf.load(reload=True)
        self.assertEqual(conf.get("dataDirName"), "new-role")

    @unittest.skipUnless(shutil.which("powershell"), "Windows PowerShell unavailable")
    def test_powershell_matches_python_in_both_layouts(self):
        self.config(self.repo, {"dataDirName": "source-role", "panelPort": 22111})
        self.config(self.live, {"dataDirName": "live-role", "panelPort": 22222}, True)
        for live in (False, True):
            with self.subTest(live=live):
                py = self.python_config(live)
                ps = self.powershell_config(live)
                self.assertEqual(ps["conf"], py.load())
                self.assertEqual(ps["path"], py.config_path())
                self.assertEqual(ps["work"], py.work_dir())
                self.assertEqual(ps["data"], py.data_dir())

    @unittest.skipUnless(shutil.which("powershell"), "Windows PowerShell unavailable")
    def test_powershell_invalid_or_missing_override_blocks_legacy(self):
        override = self.work / "override.json"
        os.environ["LANYA_PROJECT_CONFIG"] = str(override)
        self.config(self.live, {"decisions": {"mode": "legacy"}})
        self.config(self.work, {"decisions": {"mode": "legacy"}})
        for content in (None, '"private-fixture-do-not-echo"', "[]", "null", "{broken"):
            if content is not None:
                override.write_text(content, encoding="utf-8")
            result = self.powershell_probe(
                "$r=@{blocked=$false;error='';mode=$null}; "
                "try {$r.mode=Get-ProjectConfValue 'decisions.mode' 'legacy'} "
                "catch {$r.blocked=$true;$r.error=$_.FullyQualifiedErrorId}; "
                "$r | ConvertTo-Json -Compress")
            self.assertTrue(result["blocked"])
            self.assertIsNone(result["mode"])
            self.assertIn("ConfigError", result["error"])
            self.assertNotIn("private-fixture", result["error"])

    @unittest.skipUnless(shutil.which("powershell"), "Windows PowerShell unavailable")
    def test_powershell_existing_invalid_candidate_and_no_config_default(self):
        default = self.powershell_probe("@{mode=(Get-ProjectConfValue 'decisions.mode')} | ConvertTo-Json -Compress")
        self.assertEqual(default["mode"], "unified")
        local = self.config(self.live, {})
        local.write_text("{broken", encoding="utf-8")
        self.config(self.work, {"decisions": {"mode": "legacy"}})
        result = self.powershell_probe(
            "$r=@{blocked=$false};try {$null=Get-ProjectConf} catch {$r.blocked=$true};"
            "$r | ConvertTo-Json -Compress")
        self.assertTrue(result["blocked"])
        local.write_bytes(b'{"decisions":{"mode":"legacy"},"invalid":"\xff"}')
        result = self.powershell_probe(
            "$r=@{blocked=$false};try {$null=Get-ProjectConf} catch {$r.blocked=$true};"
            "$r | ConvertTo-Json -Compress")
        self.assertTrue(result["blocked"])

    @unittest.skipUnless(shutil.which("powershell"), "Windows PowerShell unavailable")
    def test_powershell_failed_reload_clears_cached_legacy(self):
        override = self.work / "override.json"
        override.write_text('{"decisions":{"mode":"legacy"}}', encoding="utf-8")
        os.environ["LANYA_PROJECT_CONFIG"] = str(override)
        quoted = str(override).replace("'", "''")
        result = self.powershell_probe(
            "$r=@{before=(Get-ProjectConfValue 'decisions.mode');reloadBlocked=$false;"
            "laterBlocked=$false;cacheCleared=$false;pathCleared=$false}; "
            "[IO.File]::WriteAllText('%s','{broken'); " % quoted +
            "try {$null=Get-ProjectConf -Reload} catch {$r.reloadBlocked=$true}; "
            "$r.cacheCleared=($null -eq $script:ProjectConfCache);"
            "$r.pathCleared=($null -eq $script:ProjectConfSelectedPath);"
            "try {$null=Get-ProjectConfValue 'decisions.mode' 'legacy'} catch {$r.laterBlocked=$true}; "
            "$r | ConvertTo-Json -Compress")
        self.assertEqual(result["before"], "legacy")
        self.assertTrue(all(result[key] for key in ("reloadBlocked", "laterBlocked", "cacheCleared", "pathCleared")))

    def test_direct_config_commands_never_dump_private_json(self):
        override = self.work / "explicit.json"
        marker = "private-fixture-do-not-echo"
        os.environ["LANYA_PROJECT_CONFIG"] = str(override)
        for content, expected_code in ((json.dumps({"private": marker}), 0), ("{" + marker, 1)):
            override.write_text(content, encoding="utf-8")
            commands = [[sys.executable, "-B", str(self.live / "project_conf.py")]]
            if shutil.which("powershell"):
                commands.append(["powershell", "-NoProfile", "-File", str(self.live / "project_conf.ps1")])
            for command in commands:
                result = subprocess.run(command, capture_output=True, text=True, encoding="utf-8", timeout=20)
                self.assertEqual(result.returncode, expected_code, result.stderr)
                self.assertNotIn(marker, result.stdout + result.stderr)
                if not expected_code:
                    self.assertEqual(result.stdout.strip(), "project_config_ok")

    def panel(self):
        server = self.repo / "server"
        server.mkdir(exist_ok=True)
        target = server / "panel-server.py"
        shutil.copy2(REPO / "server" / "panel-server.py", target)
        helper = self.python_config()
        with patch.dict(sys.modules, {"project_conf": helper}), patch.object(sys, "path", list(sys.path)):
            return load_module(target, "fixture_panel")

    def test_panel_respects_configured_data_even_if_old_role_has_more_files(self):
        self.config(self.repo, {"dataDirName": "new-role"})
        old = self.work / "\u5927\u80a5\u9c7c"
        old.mkdir()
        for name in ("budget.json", "lanya-config.json", "activities.json"):
            (old / name).write_text("{}", encoding="utf-8")
        panel = self.panel()
        self.assertEqual(panel.DATA, str(self.work / "new-role"))
        self.assertEqual(panel.NAPCAT, str(self.live))

    def test_panel_port_uses_shared_config_and_explicit_overrides(self):
        self.config(self.repo, {"dataDirName": "fixture-role", "panelPort": 22444})
        panel = self.panel()
        with patch.object(panel, "ThreadingHTTPServer") as server, patch.object(sys, "argv", ["panel-server.py"]):
            panel.main()
            self.assertEqual(server.call_args.args[0], ("127.0.0.1", 22444))
            os.environ["LANYA_PANEL_PORT"] = "22555"
            self.addCleanup(os.environ.pop, "LANYA_PANEL_PORT", None)
            panel.main()
            self.assertEqual(server.call_args.args[0], ("127.0.0.1", 22555))
            with patch.object(sys, "argv", ["panel-server.py", "--port", "22666"]):
                panel.main()
            self.assertEqual(server.call_args.args[0], ("127.0.0.1", 22666))

    def test_panel_persona_prefers_canonical_and_honors_explicit_override(self):
        self.config(self.repo, {"dataDirName": "fixture-role"})
        legacy = self.live / "fixture-role-\u4eba\u8bbe\u6b63\u6587.md"
        legacy.write_text("legacy persona", encoding="utf-8")
        self.assertEqual(self.panel().PERSONA, str(legacy))
        canonical = self.live / "persona.md"
        canonical.write_text("canonical persona", encoding="utf-8")
        self.assertEqual(self.panel().PERSONA, str(canonical))
        explicit = self.work / "explicit-persona.md"
        with patch.dict(os.environ, {"LANYA_PERSONA_FILE": str(explicit)}):
            self.assertEqual(self.panel().PERSONA, str(explicit))

    def test_panel_persona_falls_back_to_existing_data_file(self):
        self.config(self.repo, {"dataDirName": "fixture-role"})
        data = self.work / "fixture-role"
        data.mkdir()
        legacy = data / "fixture-role-\u4eba\u8bbe\u6b63\u6587.md"
        legacy.write_text("data persona", encoding="utf-8")
        self.assertEqual(self.panel().PERSONA, str(legacy))

    def test_napcat_missing_preflight_does_not_launch_any_process(self):
        self.config(self.repo, {"dataDirName": "fixture-role"})
        panel = self.panel()
        with patch.object(panel.subprocess, "run") as run:
            with self.assertRaises(FileNotFoundError):
                panel.start_napcat_elevated()
            run.assert_not_called()

    def test_napcat_delegates_to_local_launcher_and_reports_cancelled_elevation(self):
        self.config(self.repo, {"dataDirName": "fixture-role"})
        panel = self.panel()
        launcher = self.live / "\u542f\u52a8NapCat-\u6b63\u786e.cmd"
        launcher.write_text("fixture only", encoding="ascii")
        framework = self.live / "framework"
        framework.mkdir()
        for name in ("napimain.exe", "napiloader.dll", "nativeLoader.cjs"):
            (framework / name).write_text("fixture only", encoding="ascii")
        with patch.object(panel.subprocess, "run") as run:
            run.return_value = subprocess.CompletedProcess([], 0, stdout=b"4321\r\n")
            self.assertEqual(panel.start_napcat_elevated(), 4321)
            command = run.call_args.args[0][-1]
            self.assertIn(str(launcher), command)
            self.assertNotIn("taskkill", command)
            self.assertNotIn("os.path.join", command)
            run.return_value = subprocess.CompletedProcess([], 1, stdout=b"")
            with self.assertRaises(RuntimeError):
                panel.start_napcat_elevated()

    def test_tts_uses_existing_local_start_script_instead_of_author_environment(self):
        self.config(self.repo, {"dataDirName": "fixture-role"})
        launcher = self.work / "tts" / "start-tts-server.ps1"
        launcher.parent.mkdir()
        launcher.write_text("fixture only", encoding="ascii")
        panel = self.panel()
        with patch.object(panel.subprocess, "Popen") as popen:
            popen.return_value.pid = 4321
            self.assertEqual(panel.run_script("tts"), 4321)
            self.assertEqual(popen.call_args.args[0][-1], str(launcher))
            self.assertEqual(popen.call_args.kwargs["cwd"], str(launcher.parent))

    def test_configured_dsh_install_root_is_not_overwritten_by_meme_home(self):
        install = self.work / "configured-dsh-install"
        self.config(self.repo, {"dataDirName": "fixture-role", "dshRoot": str(install)})
        with patch.dict(os.environ, {"DSH_ROOT": ""}):
            panel = self.panel()
        self.assertEqual(panel.DSH_ROOT, str(install))
        self.assertEqual(panel.DSH_CLI, str(install / "resources" / "runtime" / "cli" / "bin" / "dsh.cmd"))
        self.assertEqual(panel.DSH_HOME, str(self.work / ".dsh"))

    def test_credential_and_activity_routes_use_bili_duty_then_legacy(self):
        self.config(self.repo, {"dataDirName": "fixture-role"})
        panel = self.panel()
        duty = self.work / ".dsh" / "bili-duty" / "bili_credential.json"
        duty.parent.mkdir(parents=True)
        fixture = {"sessdata": "fixture-session", "bili_jct": "fixture-csrf", "dedeuserid": "fixture-user"}
        duty.write_text(json.dumps(fixture), encoding="utf-8-sig")
        self.assertEqual(panel._bili_cred(), fixture)
        Path(panel.DATA).mkdir()
        Path(panel.ACTIVITIES).write_text('{"activities":[{"id":"bili","bound":"bili-duty"}]}', encoding="utf-8")
        handler = object.__new__(panel.Handler)
        handler._json = lambda code, data: (code, data)
        handler.path = "/api/bili/login/credential"
        status = handler.do_GET()[1]
        self.assertTrue(status["loggedIn"])
        self.assertNotIn("sessdata", status)
        self.assertNotIn("bili_jct", status)
        handler.path = "/api/activities"
        self.assertTrue(handler.do_GET()[1]["data"]["activities"][0]["ready"])
        duty.write_text("{broken", encoding="utf-8")
        legacy = duty.parent.parent / duty.name
        legacy.write_text(json.dumps(fixture), encoding="utf-8")
        self.assertEqual(panel._bili_cred(), fixture)
        legacy.write_text("[]", encoding="utf-8")
        self.assertEqual(panel._bili_cred(), {})


if __name__ == "__main__":
    unittest.main()
