"""Import/path regressions with temporary data and no browser, model or network."""
import ast
from contextlib import contextmanager
import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile
import types
import unittest
from unittest import mock


SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
NAMES = ("browserd.py", "export-chats.py", "greetd.py", "stealmemes.py",
         "memory-maintenance.py", "settings-guard.py")


@contextmanager
def stub_modules(values):
    """Restore only our stubs; preserve modules imported while the fixture runs."""
    missing = object()
    previous = {name: sys.modules.get(name, missing) for name in values}
    try:
        sys.modules.update(values)
        yield
    finally:
        for name, module in previous.items():
            if module is missing:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = module


class DaemonPathsTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.data = self.root / "custom-character"
        self.data.mkdir()
        self.runtime = self.root / "napcat"
        self.runtime.mkdir()
        # This marker is deliberately fresh: a plugin-dispatched consumer must run.
        (self.runtime / "scheduler-in-plugin.flag").write_text("offline", encoding="utf-8")
        self.config = self.root / "project.json"
        self.config.write_text(json.dumps({"dataDirName": "custom-character"}), encoding="utf-8")
        env = mock.patch.dict(os.environ, {"LANYA_PROJECT_CONFIG": str(self.config),
                                           "LANYA_WORKDIR": str(self.root),
                                           "USERPROFILE": str(self.root),
                                           "HOME": str(self.root)})
        env.start()
        self.addCleanup(env.stop)
        paths = mock.patch.object(sys, "path", [str(SCRIPTS)] + sys.path)
        paths.start()
        self.addCleanup(paths.stop)
        import project_conf
        project_conf.load(reload=True)
        self.addCleanup(setattr, project_conf, "_CACHE", None)

        fake_playwright = types.ModuleType("playwright.async_api")
        self.launch = mock.Mock(side_effect=AssertionError("browser launch is forbidden in this test"))
        fake_playwright.async_playwright = self.launch
        fake_ctypes = types.SimpleNamespace(windll=types.SimpleNamespace(
            user32=types.SimpleNamespace(ShowWindow=mock.Mock()),
            kernel32=types.SimpleNamespace(GetConsoleWindow=mock.Mock())))
        modules = stub_modules({
            "playwright": types.ModuleType("playwright"),
            "playwright.async_api": fake_playwright,
            "zstandard": types.ModuleType("zstandard"),
            "ctypes": fake_ctypes,
        })
        modules.__enter__()
        self.addCleanup(modules.__exit__, None, None, None)
        self.run = self.guard("subprocess.run")
        self.popen = self.guard("subprocess.Popen")
        self.urlopen = self.guard("urllib.request.urlopen")

    def guard(self, target):
        patch = mock.patch(target, side_effect=AssertionError("external work is forbidden in this test"))
        result = patch.start()
        self.addCleanup(patch.stop)
        return result

    def load(self, name):
        spec = importlib.util.spec_from_file_location(name.replace("-", "_") + "_under_test", SCRIPTS / name)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def assert_no_external_work(self):
        self.run.assert_not_called()
        self.popen.assert_not_called()
        self.urlopen.assert_not_called()
        self.launch.assert_not_called()

    def test_fresh_marker_does_not_exit_plugin_consumers(self):
        for name in NAMES[:4]:
            with self.subTest(script=name):
                module = self.load(name)
                entry = "run_scan" if name == "stealmemes.py" else "main"
                self.assertTrue(callable(getattr(module, entry)))
        self.assert_no_external_work()

    def test_all_daemon_paths_follow_project_configuration(self):
        browser = self.load("browserd.py")
        export = self.load("export-chats.py")
        greet = self.load("greetd.py")
        steal = self.load("stealmemes.py")
        memory = self.load("memory-maintenance.py")
        settings = self.load("settings-guard.py")
        self.assertEqual(Path(browser.BASE), self.root)
        self.assertEqual(Path(browser.BROWSER_DIR), self.data / "browser")
        self.assertEqual(Path(export.OUT_DIR), self.data / "chats")
        self.assertEqual(Path(export.DSH_HOME), self.root / ".dsh")
        self.assertEqual(Path(greet.PEOPLE), self.data / "people")
        self.assertEqual(Path(greet.PROMPT), self.runtime / "greet.prompt.txt")
        self.assertEqual(Path(steal.MEMES), self.data / "memes")
        self.assertEqual(Path(steal.PROMPT), self.runtime / "classify-meme.prompt.txt")
        self.assertEqual(Path(memory.LOG), self.data / "cross-chat.md")
        self.assertEqual(Path(settings.NAPCAT), self.runtime)
        self.assertEqual(Path(settings.LOCK_ROOT), Path(str(self.data) + "-\u5f52\u6863"))
        self.assert_no_external_work()

    def test_identity_configuration_is_read_from_the_configured_directory(self):
        (self.data / "lanya-config.json").write_text(
            json.dumps({"adminQQ": [4242], "fanGroupQQ": "1234", "ownerLabel": "synthetic owner"}),
            encoding="utf-8")
        greet = self.load("greetd.py")
        steal = self.load("stealmemes.py")
        export = self.load("export-chats.py")
        self.assertEqual(greet.ADMIN, "4242")
        self.assertEqual(steal.GROUP, "1234")
        self.assertEqual(Path(export._config_path()), self.data / "lanya-config.json")
        self.assertEqual(export._speaker_name(), "synthetic owner")
        self.assert_no_external_work()

    def test_meme_business_switch_is_still_respected(self):
        (self.data / "lanya-memes-config.json").write_text('{"stealEnabled": false}', encoding="utf-8")
        with self.assertRaises(SystemExit) as stopped:
            self.load("stealmemes.py")
        self.assertEqual(stopped.exception.code, 0)
        self.assert_no_external_work()

    def test_existing_settings_baseline_path_is_preserved(self):
        archive = Path(str(self.data) + "-\u5f52\u6863")
        archive.mkdir()
        baseline = archive / "settings.lock.json"
        baseline.write_text('{"synthetic": 1}', encoding="utf-8")
        before = baseline.read_bytes()
        settings = self.load("settings-guard.py")
        self.assertEqual(Path(settings.LOCK), baseline)
        self.assertEqual(baseline.read_bytes(), before)
        self.assert_no_external_work()

    def test_no_executable_placeholder_or_consumer_marker_remains(self):
        for name in NAMES:
            tree = ast.parse((SCRIPTS / name).read_text(encoding="utf-8-sig"))
            strings = [n.value for n in ast.walk(tree) if isinstance(n, ast.Constant) and isinstance(n.value, str)]
            with self.subTest(script=name):
                self.assertFalse(any("<WORKDIR>" in value for value in strings))
                self.assertFalse(any("scheduler-in-plugin.flag" in value for value in strings))

    def test_stub_cleanup_keeps_modules_imported_during_the_fixture(self):
        name = "_daemon_paths_unrelated_fixture_module"
        module = types.ModuleType(name)
        try:
            with stub_modules({"_daemon_paths_stub_fixture_module": types.ModuleType("stub")}):
                sys.modules[name] = module
            self.assertIs(sys.modules[name], module)
            self.assertNotIn("_daemon_paths_stub_fixture_module", sys.modules)
        finally:
            sys.modules.pop(name, None)


if __name__ == "__main__":
    unittest.main()
