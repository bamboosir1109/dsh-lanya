"""Exercise the real persistent monitor against isolated local HTTP fixtures.

No DSH, QQ, model, task, installed profile or production data is modified.
"""
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import threading
import time
import unittest


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "monitor.ps1"
POWERSHELL = shutil.which("powershell.exe") if os.name == "nt" else None
PRIVATE = "PRIVATE-FIXTURE-CONTENT-DO-NOT-PRINT"


class FixtureServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_GET(self):
        self.server.requests.append((self.command, self.path))
        mode = self.server.mode
        if mode == "slow":
            time.sleep(2)
        if mode == "redirect":
            self.send_response(302)
            self.send_header("Location", self.server.location)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        if self.path == "/api/autonomy":
            value = {"ok": True, "autonomy": {
                "mode": "unified", "status": "processing", "reason": "decision_pending",
                "scheduler_verified": True, "consumer_verified": True,
                "can_request_check": True,
                "watch": {"configured": True, "status": "healthy"},
                "private_body": PRIVATE,
            }}
            if mode == "bad":
                value["autonomy"]["status"] = PRIVATE
        elif self.path == "/api/qq/backend/status":
            value = {"ok": True, "backend": {"provider": "snowluma", "name": PRIVATE,
                "verified": True, "status": "online", "private_body": PRIVATE}}
            if mode == "bad":
                value["backend"]["verified"] = "true"
                value["backend"]["provider"] = PRIVATE
        elif self.path == "/api/service-health":
            value = {"ok": True, "service": "lanya-panel", "version": 1,
                     "role": self.server.role, "port": self.server.server_port,
                     "pid": 123, "script_sha256": "a" * 64, "private_body": PRIVATE}
            if mode == "bad":
                value["ok"] = "true"
        else:
            value = {"private_body": PRIVATE}
        body = json.dumps(value).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            pass

    def do_POST(self):
        self.server.requests.append((self.command, self.path))
        self.send_error(405)


@unittest.skipUnless(POWERSHELL, "Windows PowerShell required")
class MonitorTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="monitor workspace ")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.env = os.environ.copy()
        self.env.pop("LANYA_PROJECT_CONFIG", None)
        self.env.pop("LANYA_WORKDIR", None)
        self.env["USERPROFILE"] = str(self.root / "unused-profile")
        self.config = self.root / "napcat" / "config" / "project.json"
        self.config.parent.mkdir(parents=True)
        self.data = self.root / "data folder"
        self.data.mkdir()

    def server(self, role="api", mode="normal"):
        server = FixtureServer(("127.0.0.1", 0), Handler)
        server.mode, server.role = mode, role
        server.requests = []
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        return server

    def configure(self, api=0, works=0, qq=0, data="data folder"):
        self.config.write_text(json.dumps({"dataDirName": data, "panelPort": api,
            "panelStaticPort": works, "onebotPort": qq, "private_body": PRIVATE}), encoding="utf-8")

    def arguments(self, once=True, root=None):
        args = [POWERSHELL, "-NoLogo", "-NoProfile", "-ExecutionPolicy", "Bypass",
                "-File", str(SCRIPT), "-Root", str(root or self.root)]
        if once:
            args.append("-Once")
        return args

    def run_monitor(self, **kwargs):
        return subprocess.run(self.arguments(**kwargs), env=self.env, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True, encoding="utf-8", timeout=15,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))

    def snapshot(self):
        return {str(file.relative_to(self.root)): hashlib.sha256(file.read_bytes()).hexdigest()
                for file in self.root.rglob("*") if file.is_file()}

    def test_live_http_fixture_read_only_and_private_fields_never_printed(self):
        api, works = self.server(), self.server("works")
        self.configure(api.server_port, works.server_port, api.server_port)
        source = self.root / "dsh-lanya" / "package.json"
        source.parent.mkdir()
        source.write_text(json.dumps({"name": "dsh-lanya", "version": "9.8.7-dev.1"}))
        installed = Path(self.env["USERPROFILE"]) / ".dsh" / "profiles" / "desktop" / "node_modules" / "dsh-lanya" / "package.json"
        installed.parent.mkdir(parents=True)
        installed.write_text(json.dumps({"name": "dsh-lanya", "version": "9.8.6-dev.9"}))
        before = self.snapshot()
        result = self.run_monitor()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("scheduler + consumer verified by panel API", result.stdout)
        self.assertIn("Autonomy: processing", result.stdout)
        self.assertIn("Watch   : healthy (reported by panel API)", result.stdout)
        self.assertIn("SnowLuma account + provider verified; client delivery unverified", result.stdout)
        self.assertEqual(result.stdout.count("HTTP ready"), 2)
        self.assertIn("Source  : 9.8.7-dev.1", result.stdout)
        self.assertIn("Installed: 9.8.6-dev.9", result.stdout)
        self.assertIn("Loaded  : unverified (no process loading proof)", result.stdout)
        self.assertNotIn(PRIVATE, result.stdout + result.stderr)
        self.assertNotIn(str(self.root), result.stdout + result.stderr)
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(api.requests, [("GET", "/api/autonomy"), ("GET", "/api/qq/backend/status"), ("GET", "/api/service-health")])
        self.assertEqual(works.requests, [("GET", "/api/service-health")])

    def test_invalid_explicit_config_does_not_fall_back_or_probe(self):
        api = self.server()
        self.configure(api.server_port, api.server_port, api.server_port)
        self.env["LANYA_PROJECT_CONFIG"] = str(self.root / "missing-config.json")
        result = self.run_monitor()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("configuration unavailable", result.stdout)
        self.assertIn("Source  : unknown", result.stdout)
        self.assertIn("Installed: unknown", result.stdout)
        self.assertIn("Loaded  : unverified (no process loading proof)", result.stdout)
        self.assertEqual(api.requests, [])

    def test_data_folder_escape_is_rejected_without_network_probe(self):
        api = self.server()
        self.configure(api.server_port, api.server_port, api.server_port, data="../escape")
        result = self.run_monitor()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("configuration unavailable", result.stdout)
        self.assertEqual(api.requests, [])

    def test_stale_watch_is_not_healthy_and_no_private_status_text_escapes(self):
        self.configure()
        (self.data / "main-agent-watch.status.json").write_text(json.dumps({
            "schema": "dsh-main-agent-watch-status/v1", "state": "healthy",
            "at": int((time.time() - 300) * 1000), "reason": PRIVATE}), encoding="utf-8")
        result = self.run_monitor()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Watch   : stale", result.stdout)
        self.assertNotIn(PRIVATE, result.stdout + result.stderr)

    def test_bad_http_types_or_private_statuses_stay_unknown(self):
        api, works = self.server(mode="bad"), self.server("works", mode="bad")
        self.configure(api.server_port, works.server_port)
        result = self.run_monitor()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Autonomy: unknown", result.stdout)
        self.assertNotIn("HTTP ready", result.stdout)
        self.assertNotIn(PRIVATE, result.stdout + result.stderr)

    def test_http_redirect_is_not_followed(self):
        redirected = self.server()
        api = self.server(mode="redirect")
        api.location = "http://127.0.0.1:%d/should-not-be-requested" % redirected.server_port
        self.configure(api.server_port, api.server_port)
        result = self.run_monitor()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(redirected.requests, [])
        self.assertEqual(len(api.requests), 4)
        self.assertNotIn("HTTP ready", result.stdout)

    def test_slow_endpoints_are_bounded_and_not_retried(self):
        api = self.server(mode="slow")
        self.configure(api.server_port, api.server_port)
        started = time.monotonic()
        result = self.run_monitor()
        elapsed = time.monotonic() - started
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertLess(elapsed, 7)
        self.assertEqual(len(api.requests), 4)
        self.assertNotIn("HTTP ready", result.stdout)

    def test_native_mutex_blocks_second_window_and_releases_after_owner_exit(self):
        self.configure()
        owner = subprocess.Popen(self.arguments(once=False), env=self.env,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8",
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        self.addCleanup(lambda: owner.kill() if owner.poll() is None else None)
        try:
            # A real first snapshot proves the first process has acquired its
            # native named mutex, before attempting the competing process.
            self.assertIn("DSH Monitor", owner.stdout.readline())
            duplicate = self.run_monitor(root=self.root / ".")
            self.assertEqual(duplicate.returncode, 2, duplicate.stderr)
            self.assertIn("already open", duplicate.stdout)
        finally:
            owner.terminate()
            owner.communicate(timeout=5)
        # Windows also releases a named mutex when its process is terminated;
        # closing a monitor does not require a stale-lock file or any takeover.
        next_owner = self.run_monitor()
        self.assertEqual(next_owner.returncode, 0, next_owner.stderr)
        self.assertIn("DSH Monitor", next_owner.stdout)


if __name__ == "__main__":
    unittest.main()
