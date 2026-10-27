"""Validate the shipped package without running package lifecycle scripts."""
import json
import os
import pathlib
import shutil
import subprocess
import unittest


REPO = pathlib.Path(__file__).resolve().parents[1]


class PackageTests(unittest.TestCase):
    def test_entry_and_bundle_target_this_plugin(self):
        package = json.loads((REPO / "package.json").read_text(encoding="utf-8"))
        self.assertEqual(package["main"], "plugin/lib/index.js")
        self.assertEqual(package["exports"]["."], "./plugin/lib/index.js")
        self.assertTrue((REPO / package["main"]).is_file())
        patch = (REPO / package["dsh"]["bundle"]["patch"]).read_text(encoding="utf-8")
        active = "\n".join(line for line in patch.splitlines() if not line.lstrip().startswith("#"))
        self.assertIn("- insert:", active)
        self.assertIn("name: dsh-lanya", active)
        for unrelated in ("dsh-onebot", "dsh-memento", "agent-default-model", "accessToken"):
            self.assertNotIn(unrelated, active)

    def test_pack_includes_runtime_and_excludes_local_artifacts(self):
        npm = shutil.which("npm.cmd" if os.name == "nt" else "npm")
        if not npm:
            self.skipTest("npm is not available")
        result = subprocess.run(
            [npm, "pack", "--dry-run", "--ignore-scripts", "--json"], cwd=REPO,
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        paths = {entry["path"] for entry in json.loads(result.stdout)[0]["files"]}
        required = {
            "plugin/lib/index.js", "plugin/lib/runtime.js", "cordis.patch.yml", "install.ps1",
            "server/panel-server.py", "server/meme_sources.py", "server/make_snapshots.py",
            "scripts/project_conf.py", "scripts/project_conf.ps1", "scripts/mind.py",
            "panel/panel.html", "panel/phone.html", "panel/theme-light.css",
            "docs/INSTALL.md", "docs/ARCHITECTURE.md", "docs/MAINTENANCE.md", "docs/LIMITATIONS.md",
            "docs/config.example.json", "docs/persona.example.md",
            "docs/ROADMAP.md", "docs/MEMORY.md", "docs/ANDROID.md", "docs/DECISIONS.md",
            "plugin/lib/decision-policy.js", "plugin/lib/onebot-policy-adapter.js",
            "plugin/lib/observation-queue.js", "plugin/lib/observation-producer.js",
            "scripts/androidd.py", "scripts/android_panel.py", "scripts/memory_context.py",
            "tests/fixtures/onebot_policy/lib/bridge.js", "tests/fixtures/onebot_policy/package.json",
        }
        self.assertFalse(required - paths, "Missing packaged runtime files: " + repr(required - paths))
        self.assertNotIn("docs/cordis.patch.yml", paths)
        for path in paths:
            self.assertNotIn("__pycache__", path)
            self.assertFalse(path.endswith((".pyc", ".log", ".local.json", ".lock")), path)


if __name__ == "__main__":
    unittest.main()
