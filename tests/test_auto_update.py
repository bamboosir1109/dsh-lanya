"""Run the real updater against disposable local Git remotes, never GitHub."""
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest


REPO = Path(__file__).resolve().parents[1]
POWERSHELL = shutil.which("powershell.exe")
FIXTURE_EMAIL = "fixture" + chr(64) + "example.invalid"


@unittest.skipUnless(POWERSHELL and shutil.which("git"), "Windows PowerShell and Git required")
class AutoUpdateTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="lanya-update-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.env = dict(os.environ, GIT_CONFIG_GLOBAL=os.devnull,
                        GIT_CONFIG_NOSYSTEM="1", GIT_TERMINAL_PROMPT="0")
        self.remote = self.root / "remote.git"
        self.seed = self.root / "seed"
        self.work = self.root / "workspace"
        self.repo = self.work / "dsh-lanya"
        self.runtime = self.work / "napcat"
        self.runtime.mkdir(parents=True)
        shutil.copyfile(REPO / "scripts/auto-update.ps1", self.runtime / "auto-update.ps1")
        self.git("init", "--bare", "--initial-branch=main", str(self.remote))
        self.git("clone", str(self.remote), str(self.seed))
        self.git("config", "user.name", "Fixture", cwd=self.seed)
        self.git("config", "user.email", FIXTURE_EMAIL, cwd=self.seed)
        self.commit("base.txt", "base", "base")
        self.git("push", "origin", "main", cwd=self.seed)
        self.git("checkout", "-b", "dev", cwd=self.seed)
        self.commit("feature.txt", "feature", "feature")
        self.git("push", "-u", "origin", "dev", cwd=self.seed)
        self.git("clone", "--branch", "dev", str(self.remote), str(self.repo))
        self.git("config", "user.name", "Fixture", cwd=self.repo)
        self.git("config", "user.email", FIXTURE_EMAIL, cwd=self.repo)

    def git(self, *arguments, cwd=None):
        command = ["git", "-c", "init.templateDir="]
        if cwd is not None:
            command += ["-C", str(cwd)]
        result = subprocess.run(command + list(arguments), env=self.env,
                                capture_output=True, text=True, timeout=20,
                                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout.strip()

    def commit(self, name, text, message, cwd=None):
        cwd = cwd or self.seed
        (cwd / name).write_text(text, encoding="utf-8")
        self.git("add", "--", name, cwd=cwd)
        self.git("commit", "-m", message, cwd=cwd)

    def update(self, dry_run=False, change_branch_on_fetch=False):
        command = [POWERSHELL, "-NoProfile", "-ExecutionPolicy", "Bypass"]
        script = self.runtime / "auto-update.ps1"
        if change_branch_on_fetch:
            quote = lambda value: "'" + str(value).replace("'", "''") + "'"
            executable = quote(shutil.which("git"))
            setup = ("function git { if ($args[2] -eq 'fetch') { & " + executable +
                     " -C " + quote(self.repo) + " checkout --quiet alias }; & " +
                     executable + " @args }; & " + quote(script))
            command += ["-Command", setup]
        else:
            command += ["-File", str(script)]
        if dry_run:
            command.append("-DryRun")
        result = subprocess.run(command, cwd=self.root, env=self.env, capture_output=True,
                                text=True, encoding="utf-8", errors="replace", timeout=30,
                                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse((self.runtime / "make_snapshots.py").exists())
        return result.stdout

    def head(self):
        return self.git("rev-parse", "HEAD", cwd=self.repo)

    def advance_remote(self):
        self.commit("next.txt", "next", "next")
        self.git("push", "origin", "dev", cwd=self.seed)
        return self.git("rev-parse", "HEAD", cwd=self.seed)

    def test_current_upstream_is_used_instead_of_main(self):
        before = self.head()
        self.assertNotEqual(before, self.git("rev-parse", "origin/main", cwd=self.repo))
        self.assertIn("up to date", self.update(dry_run=True))
        self.assertEqual(self.head(), before)
        expected = self.advance_remote()
        self.assertIn("has updates", self.update(dry_run=True))
        self.assertEqual(self.head(), before)
        self.assertIn("source updated", self.update())
        self.assertEqual(self.head(), expected)
        self.assertEqual((self.repo / "next.txt").read_text(), "next")

    def test_unrelated_local_edits_are_preserved_and_block_update(self):
        before = self.head()
        self.advance_remote()
        (self.repo / "feature.txt").write_text("local edits", encoding="utf-8")
        self.assertIn("local changes", self.update())
        self.assertEqual(self.head(), before)
        self.assertEqual((self.repo / "feature.txt").read_text(), "local edits")
        self.assertFalse((self.repo / "next.txt").exists())

    def test_failed_fetch_cannot_apply_a_cached_remote_commit(self):
        before = self.head()
        self.advance_remote()
        self.git("fetch", "origin", cwd=self.repo)
        self.git("remote", "set-url", "origin", str(self.root / "missing.git"), cwd=self.repo)
        self.assertIn("fetch failed", self.update())
        self.assertEqual(self.head(), before)

    def test_local_ahead_and_detached_checkout_never_report_an_update(self):
        self.commit("local.txt", "local", "local", cwd=self.repo)
        before = self.head()
        output = self.update(dry_run=True)
        self.assertIn("ahead of upstream", output)
        self.assertNotIn("has updates", output)
        self.assertEqual(self.head(), before)
        self.git("checkout", "--detach", cwd=self.repo)
        self.assertIn("cannot verify", self.update())
        self.assertEqual(self.head(), before)

    def test_ignored_local_file_is_not_overwritten_by_a_new_tracked_file(self):
        before = self.head()
        self.git("config", "core.excludesFile", str(self.root / "ignored.txt"), cwd=self.repo)
        (self.root / "ignored.txt").write_text("private.local\n", encoding="utf-8")
        (self.repo / "private.local").write_text("local sentinel", encoding="utf-8")
        self.commit("private.local", "upstream file", "new tracked file")
        self.git("push", "origin", "dev", cwd=self.seed)
        self.assertIn("fast-forward refused", self.update())
        self.assertEqual(self.head(), before)
        self.assertEqual((self.repo / "private.local").read_text(), "local sentinel")

    def test_branch_change_during_fetch_stops_even_with_same_head_and_upstream(self):
        before = self.head()
        self.advance_remote()
        self.git("branch", "alias", cwd=self.repo)
        self.git("branch", "--set-upstream-to=origin/dev", "alias", cwd=self.repo)
        # Override Git in this child PowerShell only, keeping native argument
        # forwarding (cmd wrappers would change the meaning of ^{commit}).
        self.assertIn("changed during fetch", self.update(change_branch_on_fetch=True))
        self.assertEqual(self.git("branch", "--show-current", cwd=self.repo), "alias")
        self.assertEqual(self.head(), before)
        self.assertFalse((self.repo / "next.txt").exists())


if __name__ == "__main__":
    unittest.main()
