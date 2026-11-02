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
            "scripts/bili_social_backend.py", "scripts/bili_video_content.py", "scripts/patch_bili_content.py",
            "tests/fixtures/bili_video_content/observed.json",
            "tests/fixtures/bili_main/v1-main-agent.js", "tests/fixtures/bili_main/frames-registration.js",
            "tests/fixtures/bili_main/LICENSE", "tests/fixtures/bili_main/README.md",
            "plugin/lib/index.js", "plugin/lib/runtime.js", "cordis.patch.yml", "install.ps1",
            "server/panel-server.py", "server/meme_sources.py", "server/make_snapshots.py",
            "scripts/project_conf.py", "scripts/project_conf.ps1", "scripts/mind.py",
            "panel/panel.html", "panel/phone.html", "panel/theme-light.css",
            "games/2048/index.html", "games/2048/game.js", "games/2048/app.js",
            "docs/INSTALL.md", "docs/ARCHITECTURE.md", "docs/MAINTENANCE.md", "docs/LIMITATIONS.md",
            "docs/config.example.json", "docs/persona.example.md",
            "docs/ROADMAP.md", "docs/MEMORY.md", "docs/ANDROID.md", "docs/DECISIONS.md",
            "docs/DEVLOG.md", "scripts/patch_onebot_autonomy.py",
            "plugin/lib/decision-policy.js", "plugin/lib/onebot-policy-adapter.js",
            "plugin/lib/observation-queue.js", "plugin/lib/observation-producer.js",
            "plugin/lib/qq-inbox-recovery.js", "scripts/patch_onebot_live_inbox.py",
            "tests/test_onebot_live_inbox.mjs", "tests/test_patch_onebot_live_inbox.py",
            "tests/test_qq_inbox_recovery.mjs",
            "scripts/androidd.py", "scripts/android_panel.py", "scripts/memory_context.py",
            "scripts/panel_activity.py", "scripts/panel_record_files.py",
            "scripts/activity_journal.py", "tests/test_activity_journal.py",
            "scripts/panel_autonomy.py", "scripts/budget_runtime.py",
            "scripts/budget_reports.py", "scripts/budget-native-report.mjs", "tests/test_budget_reports.py",
            "scripts/main_agent_watch.py", "tests/test_main_agent_watch.py", "tests/test_scheduler_marker.mjs",
            "tests/test_panel_activity.py", "tests/test_panel_record_views.py",
            "tests/test_panel_record_files.py", "tests/test_panel_snapshots.py", "tests/test_panel_records.mjs",
            "tests/fixtures/onebot_policy/lib/bridge.js", "tests/fixtures/onebot_policy/package.json",
            "plugin/lib/memento-history.mjs", "scripts/patch_memento_history.py",
            "scripts/memory_native_history.py",
            "tests/fixtures/memento_native_0_5_23/lib/store.mjs",
            "tests/fixtures/memento_native_0_5_23/lib/protocol.mjs",
            "tests/fixtures/memento_native_0_5_23/native-tool.mjs",
            "tests/fixtures/memento_native_0_5_23/LICENSE.md",
            "tests/fixtures/memento_native_0_5_23/THIRD_PARTY_NOTICES.md",
            "scripts/patch_onebot_replay.py",
            "tests/fixtures/onebot_replay/lib/replay.js",
            "tests/fixtures/onebot_replay/run-case.mjs",
            "tests/fixtures/onebot_replay/LICENSE",
            "scripts/patch_memento_recall_scope.py", "plugin/lib/memento-recall-scope.mjs",
            "scripts/patch_onebot_egress.py", "plugin/lib/qq-egress.js",
            "scripts/patch_onebot_egress_activity.py", "tests/test_patch_onebot_egress_activity.py",
            "tests/test_qq_egress_activity.mjs", "tests/test_autonomy_sharing_guide.mjs",
            "tests/fixtures/onebot_egress/qq-egress-images-v1.js",
            "tests/fixtures/memento_native_0_5_23/index.mjs",
            "tests/fixtures/memento_native_0_5_23/lib/extract.mjs",
            "tests/fixtures/onebot_egress/lib/bridge.js",
            "tests/fixtures/onebot_egress/lib/inbound.js",
            "tests/fixtures/onebot_egress/lib/outbound.js",
            "tests/fixtures/onebot_egress/native-host.mjs",
            "tests/fixtures/onebot_egress/provenance.json",
            "tests/fixtures/onebot_egress/LICENSE",
            "plugin/lib/meme-qq-picker.mjs", "scripts/patch_meme_qq_picker.py",
            "tests/test_meme_qq_format.mjs",
            "tests/fixtures/meme_qq_picker/index.js", "tests/fixtures/meme_qq_picker/memes.js",
            "tests/fixtures/meme_qq_picker/bridge.js", "tests/fixtures/meme_qq_picker/provenance.json",
            "tests/fixtures/meme_qq_picker/LICENSE", "tests/fixtures/meme_qq_picker/onebot/LICENSE",
            "tests/fixtures/meme_qq_picker/sdk/cordis/index.js", "tests/fixtures/meme_qq_picker/sdk/cordis/LICENSE",
            "tests/fixtures/meme_qq_picker/sdk/cosmokit/index.js", "tests/fixtures/meme_qq_picker/sdk/cosmokit/LICENSE",
            "tests/fixtures/meme_qq_picker/sdk/provenance.json",
            "tests/fixtures/qq_expression_media/fence.js", "tests/fixtures/qq_expression_media/provenance.json",
            "tests/fixtures/qq_expression_media/LICENSE",
        }
        self.assertFalse(required - paths, "Missing packaged runtime files: " + repr(required - paths))
        self.assertNotIn("docs/cordis.patch.yml", paths)
        for path in paths:
            self.assertNotIn("__pycache__", path)
            self.assertFalse(path.endswith((".pyc", ".log", ".local.json", ".lock")), path)


if __name__ == "__main__":
    unittest.main()
