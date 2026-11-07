"""Import and file-output checks in isolated source/installed layouts."""
import importlib.util
import json
import os
from pathlib import Path
import shutil
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


class ServerHelperTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="lanya-server-helpers-")
        self.addCleanup(self.tmp.cleanup)
        self.work = Path(self.tmp.name)
        self.env = patch.dict(os.environ, {
            "LANYA_WORKDIR": "", "LANYA_PROJECT_CONFIG": "",
            "HOME": str(self.work), "USERPROFILE": str(self.work),
        })
        self.env.start()
        self.addCleanup(self.env.stop)
        self.paths = patch.object(sys, "path", list(sys.path))
        self.paths.start()
        self.addCleanup(self.paths.stop)

    def fixture(self, installed):
        project = self.work / ("napcat" if installed else "checkout")
        scripts = project if installed else project / "scripts"
        server = project if installed else project / "server"
        scripts.mkdir(parents=True, exist_ok=True)
        server.mkdir(exist_ok=True)
        shutil.copy2(REPO / "scripts" / "project_conf.py", scripts / "project_conf.py")
        for name in ("make_snapshots.py", "meme_sources.py"):
            shutil.copy2(REPO / "server" / name, server / name)
        shutil.copy2(REPO / "server" / "panel-server.py",
                     server / ("lanya-panel-server.py" if installed else "panel-server.py"))
        config = project / "config"
        config.mkdir()
        (config / "project.json").write_text('{"dataDirName":"fixture-role"}', encoding="utf-8")
        helper = load_module(scripts / "project_conf.py", "fixture_conf")
        return helper, server

    def test_helpers_import_and_snapshot_prefix_matches_panel_in_both_layouts(self):
        for installed in (False, True):
            with self.subTest(installed=installed):
                helper, server = self.fixture(installed)
                with patch.dict(sys.modules, {"project_conf": helper}):
                    snapshots = load_module(server / "make_snapshots.py", "fixture_snapshots")
                    memes = load_module(server / "meme_sources.py", "fixture_memes")
                data = self.work / "fixture-role"
                self.assertEqual(snapshots.OUT, str(data / "made"))
                self.assertEqual(snapshots.lps.DATA, str(data))
                self.assertEqual(memes.DAFEIYU_MEMES, str(data / "memes"))
                Path(snapshots.OUT).mkdir(parents=True, exist_ok=True)
                self.assertTrue(snapshots.dump("budget", {"ok": True, "budget": {"spent": 0}}))
                output = Path(snapshots.OUT) / "lanya-budget.json"
                self.assertEqual(json.loads(output.read_text(encoding="utf-8"))["budget"]["spent"], 0)
                self.assertFalse((Path(snapshots.OUT) / "dafeiyu-budget.json").exists())

    def test_meme_sources_use_configured_collection(self):
        helper, server = self.fixture(False)
        with patch.dict(sys.modules, {"project_conf": helper}):
            memes = load_module(server / "meme_sources.py", "fixture_memes")
        collection = self.work / "fixture-role" / "memes" / "stolen" / "happy"
        collection.mkdir(parents=True)
        (collection / "fixture.png").write_bytes(b"fixture-image")
        groups = memes.meme_groups()
        stolen = next(group for group in groups if group["source"] == "stolen")
        self.assertEqual(stolen["files"], ["fixture.png"])
        self.assertEqual(memes.meme_path("stolen", "happy", "fixture.png"), str(collection / "fixture.png"))


if __name__ == "__main__":
    unittest.main()
