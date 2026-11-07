"""No business scripts: temporary PowerShell children and fake task registration."""
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
FILE = ROOT / "scripts/hidden_task.py"
spec = importlib.util.spec_from_file_location("hidden_task", FILE)
hidden = importlib.util.module_from_spec(spec)
spec.loader.exec_module(hidden)


class HiddenTaskPaths(unittest.TestCase):
    def test_invalid_files_never_start_a_process(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            wrong = base / "script.py"
            wrong.write_text("raise RuntimeError('must not execute')", encoding="utf-8")
            with mock.patch.object(hidden.subprocess, "run") as child:
                for value in ("relative.ps1", "\\\\server\\share\\script.ps1", "//server/share/script.ps1",
                              str(base / "missing.ps1"), str(wrong), str(base / "folder.ps1")):
                    with self.subTest(path=value), self.assertRaises((ValueError, OSError)):
                        hidden.run_script(value)
                child.assert_not_called()

    def test_reparse_attribute_refuses_the_script(self):
        with tempfile.TemporaryDirectory() as directory:
            script = Path(directory) / "redirected.ps1"
            script.write_text("exit 0", encoding="ascii")
            original = Path.lstat

            def info(path):
                result = original(path)
                if path == script:
                    return type("Reparse", (), {"st_mode": result.st_mode, "st_file_attributes": 0x400})()
                return result

            with mock.patch.object(Path, "lstat", info), mock.patch.object(hidden.subprocess, "run") as child:
                with self.assertRaises(ValueError):
                    hidden.run_script(str(script))
                child.assert_not_called()

    def test_errors_do_not_expose_script_paths_or_exception_text(self):
        with mock.patch.object(hidden, "run_script", side_effect=OSError("private path and content")):
            with mock.patch.object(hidden.sys, "stderr") as stream:
                self.assertEqual(hidden.main(["--script", "unavailable.ps1"]), 1)
                stream.write.assert_called_once_with("hidden_task: OSError\n")
            with mock.patch.object(hidden.sys, "stderr", None):
                self.assertEqual(hidden.main(["--script", "unavailable.ps1"]), 1)
        with mock.patch.object(hidden.sys, "stderr") as stream:
            self.assertEqual(hidden.main(["--unexpected", "private value"]), 1)
            stream.write.assert_called_once_with("hidden_task: ValueError\n")


@unittest.skipUnless(os.name == "nt", "Windows console integration")
class HiddenTaskWindows(unittest.TestCase):
    def test_real_powershell_python_grandchild_has_no_console_and_preserves_exit_code(self):
        pythonw = Path(sys.executable).with_name("pythonw.exe")
        if not pythonw.is_file():
            self.skipTest("matching pythonw.exe unavailable")
        with tempfile.TemporaryDirectory(prefix="hidden grandchild ") as directory:
            base = Path(directory)
            output = base / "grandchild result.json"
            child = base / "console python child.py"
            script = base / "powershell parent.ps1"
            child.write_text(
                "import ctypes, json, os, sys\n"
                "probe = ctypes.WinDLL('kernel32').GetConsoleWindow\n"
                "probe.argtypes = []\n"
                "probe.restype = ctypes.c_void_p\n"
                "with open(sys.argv[1], 'w', encoding='utf-8') as stream:\n"
                "    json.dump({'console': int(probe() or 0), 'cwd': os.getcwd()}, stream)\n"
                "raise SystemExit(23)\n", encoding="ascii")
            quote = lambda value: "'" + str(value).replace("'", "''") + "'"
            script.write_text(
                "& " + quote(sys.executable) + " -B " + quote(child) + " " + quote(output) + "\n"
                "exit $LASTEXITCODE\n", encoding="ascii")
            result = subprocess.run([str(pythonw), "-B", str(FILE), "--script", str(script)],
                                    stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                    stderr=subprocess.DEVNULL, timeout=30,
                                    creationflags=subprocess.CREATE_NO_WINDOW)
            self.assertEqual(result.returncode, 23)
            data = json.loads(output.read_text(encoding="utf-8"))
            self.assertEqual(data["console"], 0)
            self.assertTrue(Path(data["cwd"]).samefile(base))

    def test_real_pythonw_child_has_no_console_and_preserves_exit_code_and_spaces(self):
        pythonw = Path(sys.executable).with_name("pythonw.exe")
        if not pythonw.is_file():
            self.skipTest("matching pythonw.exe unavailable")
        with tempfile.TemporaryDirectory(prefix="hidden task ") as directory:
            base = Path(directory)
            output = base / "child result.json"
            script = base / "child with spaces.ps1"
            destination = str(output).replace("'", "''")
            script.write_text(
                "Add-Type -TypeDefinition 'using System; using System.Runtime.InteropServices; "
                "public static class ConsoleProbe { [DllImport(\"kernel32.dll\")] "
                "public static extern IntPtr GetConsoleWindow(); }'\n"
                "$result = @{ console = [ConsoleProbe]::GetConsoleWindow().ToInt64(); cwd = $pwd.Path }\n"
                "[IO.File]::WriteAllText('" + destination + "', ($result | ConvertTo-Json -Compress))\n"
                "exit 37\n", encoding="ascii")
            result = subprocess.run([str(pythonw), "-B", str(FILE), "--script", str(script)],
                                    stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                    stderr=subprocess.DEVNULL, timeout=30,
                                    creationflags=subprocess.CREATE_NO_WINDOW)
            self.assertEqual(result.returncode, 37)
            data = json.loads(output.read_text(encoding="utf-8-sig"))
            self.assertEqual(data["console"], 0)
            self.assertTrue(Path(data["cwd"]).samefile(base))

    def test_child_receives_argument_vector_and_no_window_flag(self):
        with tempfile.TemporaryDirectory(prefix="hidden task ") as directory:
            script = Path(directory) / "test.ps1"
            script.write_text("exit 0", encoding="ascii")
            executable = hidden.powershell_executable()
            with mock.patch.object(hidden.subprocess, "run", return_value=type("Result", (), {"returncode": 19})()) as child:
                self.assertEqual(hidden.run_script(str(script)), 19)
            args, options = child.call_args
            self.assertEqual(args[0], [str(executable), "-NoLogo", "-NoProfile", "-NonInteractive",
                                      "-ExecutionPolicy", "Bypass", "-WindowStyle", "Hidden", "-File", str(script)])
            self.assertEqual(options["creationflags"], subprocess.CREATE_NO_WINDOW)
            self.assertEqual(options["stdin"], subprocess.DEVNULL)
            self.assertEqual(options["cwd"], str(script.parent))
            self.assertNotIn("shell", options)

    def registration_probe(self, base, *, existing=False, missing_setting=False, relative_setting=False):
        # Extract only this function from the parsed registration script. All
        # task APIs are stubs; no scheduled task is created, edited or started.
        quote = lambda value: "'" + str(value).replace("'", "''") + "'"
        source = quote(ROOT / "scripts/ensure-tasks.ps1")
        probe = base / "registration probe.ps1"
        probe.write_text(
            "$ErrorActionPreference = 'Stop'\n"
            "$napcat = " + quote(base) + "\n"
            "$configured = " + quote(base / "python.exe") + "\n"
            "$existing = $" + str(existing).lower() + "\n"
            "$missingSetting = $" + str(missing_setting).lower() + "\n"
            "$relativeSetting = $" + str(relative_setting).lower() + "\n"
            "$script:registered = $false\n"
            "$script:resolved = $false\n"
            "function Note([string]$message) {}\n"
            "function Get-ProjectConf { if ($missingSetting) { return @{} }; "
            "if ($relativeSetting) { return @{pythonExecutable='relative\\python.exe'} }; return @{pythonExecutable=$configured} }\n"
            "function Get-Command { param($Name,$CommandType,$ErrorAction); "
            "if ($Name -ne 'python.exe' -or $CommandType -ne 'Application') { throw 'Unexpected command resolution' }; "
            "$script:resolved=$true; return @{Path=$configured} }\n"
            "function Get-ScheduledTask { if ($existing) { return @{ State = 'Ready' } } }\n"
            "function New-ScheduledTaskAction { param($Execute, $Argument, $WorkingDirectory); "
            "$script:action = @{ Execute=$Execute; Argument=$Argument; WorkingDirectory=$WorkingDirectory }; return $script:action }\n"
            "function New-ScheduledTaskTrigger { param([switch]$Once, $At, $RepetitionInterval); return @{} }\n"
            "function New-ScheduledTaskSettingsSet { param([switch]$AllowStartIfOnBatteries, "
            "[switch]$DontStopIfGoingOnBatteries, [switch]$StartWhenAvailable, $MultipleInstances, $ExecutionTimeLimit); return @{} }\n"
            "function Register-ScheduledTask { param($TaskName,$Action,$Trigger,$Settings,[switch]$Force); $script:registered=$true }\n"
            "$errors = @(); $tokens = @()\n"
            "$ast = [Management.Automation.Language.Parser]::ParseFile(" + source + ",[ref]$tokens,[ref]$errors)\n"
            "if ($errors.Count) { throw 'Parse failed' }\n"
            "$function = $ast.Find({ param($node) $node -is [Management.Automation.Language.FunctionDefinitionAst] "
            "-and $node.Name -eq 'RepeatTask' }, $true)\n"
            ". ([scriptblock]::Create($function.Extent.Text))\n"
            "try { RepeatTask 'DSH-Test-Hidden-Only' 'budget.ps1' 5; $ok=$true } catch { $ok=$false }\n"
            "@{ ok=$ok; registered=$script:registered; resolved=$script:resolved; action=$script:action } | ConvertTo-Json -Compress\n",
            encoding="ascii")
        result = subprocess.run([str(hidden.powershell_executable()), "-NoProfile", "-NonInteractive", "-File", str(probe)],
                                capture_output=True, text=True, timeout=20,
                                creationflags=subprocess.CREATE_NO_WINDOW)
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def test_new_registration_uses_pythonw_and_preserves_script_argument(self):
        with tempfile.TemporaryDirectory(prefix="hidden registration ") as directory:
            base = Path(directory)
            for name in ("python.exe", "pythonw.exe", "hidden_task.py", "budget.ps1"):
                (base / name).write_text("temporary stub, never executed", encoding="ascii")
            result = self.registration_probe(base)
            self.assertTrue(result["ok"])
            self.assertTrue(result["registered"])
            self.assertEqual(result["action"]["Execute"], str(base / "pythonw.exe"))
            self.assertEqual(result["action"]["Argument"], '-B "' + str(base / "hidden_task.py") + '" --script "' + str(base / "budget.ps1") + '"')
            self.assertEqual(result["action"]["WorkingDirectory"], str(base))
            self.assertFalse(result["resolved"])

    def test_missing_configuration_resolves_default_python_without_executing_it(self):
        with tempfile.TemporaryDirectory(prefix="hidden registration ") as directory:
            base = Path(directory)
            for name in ("python.exe", "pythonw.exe", "hidden_task.py", "budget.ps1"):
                (base / name).write_text("temporary stub, never executed", encoding="ascii")
            result = self.registration_probe(base, missing_setting=True)
            self.assertTrue(result["ok"])
            self.assertTrue(result["registered"])
            self.assertTrue(result["resolved"])
            self.assertEqual(result["action"]["Execute"], str(base / "pythonw.exe"))

    def test_explicit_relative_python_does_not_fall_back_to_path(self):
        with tempfile.TemporaryDirectory(prefix="hidden registration ") as directory:
            result = self.registration_probe(Path(directory), relative_setting=True)
            self.assertFalse(result["ok"])
            self.assertFalse(result["registered"])
            self.assertFalse(result["resolved"])

    def test_missing_pythonw_does_not_register_visible_fallback(self):
        with tempfile.TemporaryDirectory(prefix="hidden registration ") as directory:
            base = Path(directory)
            for name in ("python.exe", "hidden_task.py", "budget.ps1"):
                (base / name).write_text("temporary stub, never executed", encoding="ascii")
            result = self.registration_probe(base)
            self.assertFalse(result["ok"])
            self.assertFalse(result["registered"])
            self.assertIsNone(result["action"])

    def test_existing_task_is_preserved_without_loading_a_new_action(self):
        with tempfile.TemporaryDirectory(prefix="hidden registration ") as directory:
            result = self.registration_probe(Path(directory), existing=True)
            self.assertTrue(result["ok"])
            self.assertFalse(result["registered"])
            self.assertIsNone(result["action"])


if __name__ == "__main__":
    unittest.main()
