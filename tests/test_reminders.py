"""Run the real PowerShell reminder consumer against a local mock OneBot."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import threading
import unittest
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

SOURCE = Path(__file__).resolve().parents[1] / "scripts"
PLUGIN_RUNTIME = (SOURCE.parent / "plugin" / "lib" / "runtime.js").as_uri()


@unittest.skipUnless(shutil.which("powershell.exe"), "Windows PowerShell required")
class RemindersTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="reminder isolated ")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.runtime = self.root / "napcat"
        self.data = self.root / "Character"
        (self.runtime / "config").mkdir(parents=True)
        self.data.mkdir()
        for name in ("reminders.ps1", "project_conf.ps1", "qq_observation.ps1", "decision_events.py", "project_conf.py"):
            shutil.copyfile(SOURCE / name, self.runtime / name)
        # These fixtures exercise the explicit rollback transport against a
        # loopback mock. Unified mode is covered by test_qq_observation.py.
        (self.runtime / "config" / "project.json").write_text(json.dumps({
            "dataDirName": "Character", "decisions": {"mode": "legacy"}
        }))
        (self.data / "lanya-config.json").write_text(json.dumps({"adminQQ": ["123456789"]}))
        self.sent = []
        self.rejected = set()
        owner = self

        class MockOneBot(BaseHTTPRequestHandler):
            def do_POST(self):
                payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                owner.sent.append(payload)
                rejected = payload["message"] in owner.rejected
                body = json.dumps({"status": "failed" if rejected else "ok", "retcode": 100 if rejected else 0}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *_):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), MockOneBot)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        (self.runtime / "balance-alert.config.json").write_text(json.dumps({
            "napcatHttp": "http://127.0.0.1:" + str(self.server.server_port)
        }))

    def run_once(self):
        env = {**os.environ, "LANYA_WORKDIR": str(self.root)}
        env.pop("LANYA_PROJECT_CONFIG", None)
        result = subprocess.run(["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File",
                                 str(self.runtime / "reminders.ps1"), "-Once"], env=env,
                                capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads((self.data / "reminders.json").read_text(encoding="utf-8-sig"))

    def write(self, reminders):
        (self.data / "reminders.json").write_text(json.dumps(reminders), encoding="utf-8")

    def node_cycle(self, reminders, previous):
        code = (
            "import {processReminders} from " + json.dumps(PLUGIN_RUNTIME) + ";"
            "let sent=0; const r=await processReminders(JSON.parse(process.argv[1]),"
            "JSON.parse(process.argv[2]),new Date(),async()=>{sent++});"
            "console.log(JSON.stringify({...r,sent}));"
        )
        result = subprocess.run(["node", "--input-type=module", "-e", code,
                                 json.dumps(reminders), json.dumps(previous)],
                                capture_output=True, text=True, encoding="utf-8", check=True, timeout=10)
        return json.loads(result.stdout)

    @unittest.skipUnless(shutil.which("node"), "Node required for shared-state test")
    def test_plugin_to_external_keeps_delivery_state(self):
        daily = {"text": "shared ' 中文 &", "daily": datetime.now().strftime("%H:%M")}
        result = self.node_cycle([daily], {})
        self.assertEqual(result["sent"], 1)
        self.write(result["keep"])
        (self.data / "reminders.state.json").write_text(json.dumps(result["state"]), encoding="utf-8")
        self.assertEqual(self.run_once(), [daily])
        self.assertEqual(self.sent, [])

    @unittest.skipUnless(shutil.which("node"), "Node required for shared-state test")
    def test_external_to_plugin_keeps_delivery_state(self):
        daily = {"text": "shared ' 中文 &", "daily": datetime.now().strftime("%H:%M")}
        self.write([daily])
        self.run_once()
        state = json.loads((self.data / "reminders.state.json").read_text(encoding="utf-8-sig"))
        self.assertEqual(self.node_cycle([daily], state)["sent"], 0)

    def test_live_owner_lock_prevents_parallel_delivery(self):
        self.write([{"text": "blocked", "at": (datetime.now() - timedelta(hours=1)).isoformat()}])
        (self.data / "reminders-send.lock").write_text(str(os.getpid()))
        self.run_once()
        self.assertEqual(self.sent, [])

    def test_daily_within_grace_is_delivered_once(self):
        now = datetime.now()
        if now.hour == 0 and now.minute == 0:
            self.skipTest("grace does not cross calendar days")
        daily = {"text": "late", "daily": (now - timedelta(minutes=1)).strftime("%H:%M")}
        self.write([daily])
        self.assertEqual(self.run_once(), [daily])
        self.assertEqual(self.run_once(), [daily])
        self.assertEqual([item["message"] for item in self.sent], ["late"])

    def test_success_keeps_future_and_array_shape(self):
        future = {"text": "later", "at": (datetime.now() + timedelta(days=1)).isoformat()}
        self.write([{"text": "due", "at": (datetime.now() - timedelta(hours=1)).isoformat()}, future])
        self.assertEqual(self.run_once(), [future])
        self.assertEqual([item["message"] for item in self.sent], ["due"])

    def test_rejected_ack_keeps_due_and_success_is_not_retried(self):
        self.rejected.add("retry")
        retry = {"text": "retry", "at": (datetime.now() - timedelta(hours=1)).isoformat()}
        self.write([{"text": "sent", "at": retry["at"]}, retry])
        self.assertEqual(self.run_once(), [retry])
        self.assertEqual(self.run_once(), [retry])
        self.assertEqual([item["message"] for item in self.sent], ["sent", "retry", "retry"])

    def test_daily_not_repeated_when_a_previous_item_disappears(self):
        daily = {"text": "daily", "daily": datetime.now().strftime("%H:%M")}
        self.write([{"text": "once", "at": (datetime.now() - timedelta(hours=1)).isoformat()}, daily])
        self.assertEqual(self.run_once(), [daily])
        self.assertEqual(self.run_once(), [daily])
        self.assertEqual([item["message"] for item in self.sent], ["once", "daily"])


if __name__ == "__main__":
    unittest.main()
