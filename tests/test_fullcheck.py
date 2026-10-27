"""Scanner regressions use temporary source trees, never the installed runtime."""
import contextlib
import importlib.util
import io
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import patch

REPO = Path(__file__).resolve().parents[1]


class FullcheckTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="lanya-fullcheck-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        spec = importlib.util.spec_from_file_location("fixture_fullcheck", REPO / "scripts" / "fullcheck.py")
        self.check = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.check)

    def run_check(self, *extra):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = self.check.main(["--root", str(self.root), "--quiet", *extra])
        return code, output.getvalue()

    def test_empty_and_only_skipped_trees_fail(self):
        self.assertEqual(self.run_check()[0], 2)
        (self.root / "fullcheck.py").write_text("pass\n", encoding="utf-8")
        self.assertEqual(self.run_check()[0], 2)

    def test_real_python_file_is_checked_and_syntax_failure_is_nonzero(self):
        path = self.root / "worker.py"
        path.write_text("if :\n", encoding="utf-8")
        code, output = self.run_check()
        self.assertEqual(code, 1)
        self.assertIn("worker.py", output)
        self.assertTrue(any(problem[0] == "语法" for problem in self.check.problems))
        path.write_text("pass\n", encoding="utf-8-sig")
        self.assertEqual(self.run_check()[0], 0)
        self.assertEqual(self.check.problems, [])

    def test_source_templates_are_not_reported_as_private_identifiers(self):
        (self.root / "worker.py").write_text("adminQQ = 'YOUR_ADMIN_QQ'\n", encoding="utf-8")
        self.assertEqual(self.run_check()[0], 0)
        code, _ = self.run_check("--live")
        self.assertEqual(code, 1)
        self.assertEqual(self.check.problems[0][0], "占位符")

    def test_hardcoded_identity_is_detected_without_echoing_value(self):
        identity = "98765" + "4321"
        (self.root / "worker.py").write_text("adminQQ = '" + identity + "'\n", encoding="utf-8")
        code, output = self.run_check()
        self.assertEqual(code, 1)
        self.assertNotIn(identity, output)
        self.assertEqual(self.check.problems[0][0], "仓库含硬编码业务号")

    def test_default_root_is_derived_from_script_location(self):
        scripts = self.root / "checkout" / "scripts"
        scripts.mkdir(parents=True)
        target = scripts / "fullcheck.py"
        shutil.copy2(REPO / "scripts" / "fullcheck.py", target)
        (scripts / "worker.py").write_text("pass\n", encoding="utf-8")
        spec = importlib.util.spec_from_file_location("relocated_fullcheck", target)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(module.main(["--quiet"]), 0)
        self.assertEqual(module.REPORT_ROOT, str(self.root / "checkout"))

    def test_installed_default_enforces_runtime_placeholder_rule(self):
        live = self.root / "napcat"
        live.mkdir()
        target = live / "fullcheck.py"
        shutil.copy2(REPO / "scripts" / "fullcheck.py", target)
        (live / "worker.py").write_text("adminQQ = 'YOUR_ADMIN_QQ'\n", encoding="utf-8")
        spec = importlib.util.spec_from_file_location("installed_fullcheck", target)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(module.main(["--quiet"]), 1)
        self.assertTrue(module.LIVE_MODE)
        self.assertEqual(module.problems[0][0], "占位符")

    def test_syntax_tool_failure_is_not_clean(self):
        (self.root / "worker.ps1").write_text("Write-Output 'fixture'\n", encoding="ascii")
        with patch.object(self.check.subprocess, "run", side_effect=FileNotFoundError("parser unavailable")):
            self.assertEqual(self.run_check()[0], 1)
        self.assertEqual(self.check.problems[0][0], "检查器")

    @unittest.skipUnless(shutil.which("powershell"), "Windows PowerShell unavailable")
    def test_deferred_function_calls_are_safe_but_early_top_level_calls_fail(self):
        path = self.root / "worker.ps1"
        definition = "function Invoke-After([string]$Message) { Write-Output $Message }\n"
        path.write_text("function Invoke-Entry { Invoke-After 'deferred' }\n"
                        "Write-Output \"Invoke-After 'not a command'\"\n"
                        + definition + "Invoke-Entry\n", encoding="ascii")
        self.assertEqual(self.run_check()[0], 0)
        path.write_text("Invoke-After 'early'\n" + definition, encoding="ascii")
        self.assertEqual(self.run_check()[0], 1)
        self.assertEqual(self.check.problems[0][0], "调用早于定义")

    def test_vendor_archives_are_excluded(self):
        (self.root / "worker.py").write_text("pass\n", encoding="utf-8")
        for folder in ("_backup", "_scratch", "framework", "node_modules"):
            target = self.root / folder
            target.mkdir()
            (target / "bad.py").write_text("if :\n", encoding="utf-8")
        self.assertEqual(self.run_check()[0], 0)


if __name__ == "__main__":
    unittest.main()
