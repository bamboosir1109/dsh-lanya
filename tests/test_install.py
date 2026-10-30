"""Install into disposable directories; never start the system or touch live data."""
import json
import pathlib
import subprocess
import tempfile
import unittest


REPO = pathlib.Path(__file__).resolve().parents[1]


class InstallTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="lanya-install-")
        self.addCleanup(self.cleanup_temp)
        self.target = pathlib.Path(self.temp.name) / "workspace with spaces"

    def cleanup_temp(self):
        # Check the exact disposable root before recursively removing fixtures.
        root = pathlib.Path(self.temp.name).resolve()
        self.assertEqual(root.parent, pathlib.Path(tempfile.gettempdir()).resolve())
        self.assertTrue(root.name.startswith("lanya-install-"))
        self.temp.cleanup()

    def install(self, *args, ok=True):
        result = subprocess.run(
            ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File",
             str(REPO / "install.ps1"), "-Target", str(self.target), *args],
            cwd=REPO, capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=60,
        )
        if ok:
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        else:
            self.assertNotEqual(result.returncode, 0)
        return result

    def test_fresh_install_whatif_and_force_protection(self):
        role = "role with spaces"
        self.install("-DataDir", role, "-WhatIf")
        self.assertFalse(self.target.exists())
        self.install("-DataDir", role)
        runtime = self.target / "napcat"
        data = self.target / role
        for relative in ("lanya-panel-server.py", "make_snapshots.py", "meme_sources.py",
                         "project_conf.py", "project_conf.ps1", "mind.py", "groupwatch.prompt.txt"):
            self.assertTrue((runtime / relative).is_file(), relative)
        for name in ("lanya-panel.html", "theme-light.css", "phone.html"):
            self.assertTrue((data / "made" / name).is_file(), name)
        for name in ("index.html", "game.js", "app.js"):
            self.assertEqual((data / "made" / "2048" / name).read_bytes(),
                             (REPO / "games" / "2048" / name).read_bytes())
        project = self.target / "config" / "project.json"
        settings = json.loads(project.read_text(encoding="utf-8-sig"))
        self.assertEqual(settings["dataDirName"], role)
        settings["panelPort"] = 23456
        project.write_text(json.dumps(settings), encoding="utf-8")
        protected = [project, runtime / "persona.md", data / "lanya-config.json",
                     data / "lanya-tunables.json", data / "activities.json"]
        for path in protected[1:]:
            path.write_text("local sentinel content\n", encoding="utf-8")
        before = {path: path.read_bytes() for path in protected}
        program = runtime / "mind.py"
        program.write_text("local program marker\n", encoding="utf-8")
        self.install()
        self.assertEqual(program.read_text(), "local program marker\n")
        self.install("-Force")
        self.assertEqual(program.read_bytes(), (REPO / "scripts" / "mind.py").read_bytes())
        for path, value in before.items():
            self.assertEqual(path.read_bytes(), value, str(path))

    def test_existing_legacy_configuration_and_persona_are_preserved(self):
        role = "legacy-role"
        runtime = self.target / "napcat"
        legacy_project = runtime / "config" / "project.json"
        legacy_project.parent.mkdir(parents=True)
        legacy_project.write_text(json.dumps({"dataDirName": role}), encoding="utf-8")
        data = self.target / role
        data.mkdir()
        persona = data / (role + "-\u4eba\u8bbe\u6b63\u6587.md")
        persona.write_text("existing persona", encoding="utf-8")
        identity = data / "dafeiyu-config.json"
        identity.write_text("existing identity", encoding="utf-8")
        self.install("-Force")
        self.assertEqual(persona.read_text(), "existing persona")
        self.assertEqual(identity.read_text(), "existing identity")
        self.assertFalse((runtime / "persona.md").exists())
        self.assertFalse((data / "lanya-config.json").exists())
        self.assertFalse((self.target / "config" / "project.json").exists())
        self.install("-DataDir", "different-role", ok=False)

    def test_unicode_data_name_is_persisted_and_installed(self):
        role = "role-\u6d4b\u8bd5"
        self.install("-DataDir", role)
        project = json.loads((self.target / "config" / "project.json").read_text(encoding="utf-8"))
        self.assertEqual(project["dataDirName"], role)
        self.assertTrue((self.target / role / "made" / "lanya-panel.html").is_file())

    def test_invalid_data_directory_fails_before_writes(self):
        for role in ("../outside", "napcat", "config", "CON", "NUL.txt"):
            with self.subTest(role=role):
                self.install("-DataDir", role, ok=False)
                self.assertFalse(self.target.exists())


if __name__ == "__main__":
    unittest.main()
