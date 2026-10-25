# -*- coding: utf-8 -*-
"""直接用 JSON-RPC 驱动 B 站 MCP 服务器。

为什么这条路可行：
  面板不能起 DSH agent（profile 里没 bili 工具、还要 overlay），
  但 mcp_server.py 本身是个标准的 MCP stdio 服务器 —— 谁都能连。
  拿 venv 的 python 起它，按 JSON-RPC 说话就行了，不需要 LLM。

协议：换行分隔的 JSON-RPC 2.0
  → {"jsonrpc":"2.0","id":1,"method":"initialize",...}
  ← ...
  → {"jsonrpc":"2.0","method":"notifications/initialized"}
  → {"jsonrpc":"2.0","id":2,"method":"tools/call","params":{"name":..,"arguments":{..}}}
  ← {"jsonrpc":"2.0","id":2,"result":{"content":[...]}}
"""
import json
import os
import subprocess
import sys
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
VENV_PY = os.path.join(os.path.expanduser("~"), ".dsh", "bili-duty",
                       ".venv", "Scripts", "python.exe")
MCP = os.path.join(os.path.expanduser("~"), ".dsh", "bili-duty", "mcp_server.py")


class BiliMCP:
    def __init__(self, timeout=120):
        self.timeout = timeout
        self.proc = None
        self._id = 0
        self._buf = []
        self._lock = threading.Lock()
        self._reader = None

    def __enter__(self):
        env = dict(os.environ)
        env["PYTHONUTF8"] = "1"
        env["PYTHONIOENCODING"] = "utf-8"
        self.proc = subprocess.Popen(
            [VENV_PY, MCP],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            cwd=os.path.dirname(MCP), env=env,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))

        def _read():
            for raw in self.proc.stdout:
                line = raw.decode("utf-8", "replace").strip()
                if line:
                    with self._lock:
                        self._buf.append(line)

        self._reader = threading.Thread(target=_read, daemon=True)
        self._reader.start()

        self._rpc("initialize", {
            "protocolVersion": "2024-11-05",
            "capabilities": {},
            "clientInfo": {"name": "lanya-panel", "version": "1"}})
        self._notify("notifications/initialized", {})
        return self

    def __exit__(self, *a):
        try:
            self.proc.stdin.close()
        except Exception:
            pass
        try:
            self.proc.terminate()
        except Exception:
            pass

    def _notify(self, method, params):
        msg = {"jsonrpc": "2.0", "method": method}
        if params:
            msg["params"] = params
        self.proc.stdin.write((json.dumps(msg) + "\n").encode("utf-8"))
        self.proc.stdin.flush()

    def _rpc(self, method, params, want_id=True):
        self._id += 1
        mid = self._id
        msg = {"jsonrpc": "2.0", "method": method}
        if params:
            msg["params"] = params
        if want_id:
            msg["id"] = mid
        self.proc.stdin.write((json.dumps(msg) + "\n").encode("utf-8"))
        self.proc.stdin.flush()
        if not want_id:
            return None

        deadline = time.time() + self.timeout
        while time.time() < deadline:
            with self._lock:
                for i, line in enumerate(self._buf):
                    try:
                        obj = json.loads(line)
                    except Exception:
                        continue
                    if obj.get("id") == mid:
                        self._buf.pop(i)
                        if "error" in obj:
                            raise RuntimeError(str(obj["error"])[:300])
                        return obj.get("result")
            time.sleep(0.05)
        raise TimeoutError("MCP 超时：" + method)

    def call(self, name, args=None):
        res = self._rpc("tools/call", {"name": name, "arguments": args or {}})
        content = (res or {}).get("content") or []
        out = []
        for c in content:
            if c.get("type") == "text":
                out.append(c.get("text") or "")
        return "\n".join(out)

    def call_json(self, name, args=None):
        txt = self.call(name, args)
        try:
            return json.loads(txt)
        except Exception:
            return {"_raw": txt}


if __name__ == "__main__":
    # 自测：列工具 + 查一次推荐
    with BiliMCP() as m:
        tools = m._rpc("tools/list", {})
        names = [t.get("name") for t in (tools or {}).get("tools", [])]
        print("  工具数:", len(names))
        print("  前 8 个:", ", ".join(names[:8]))
        if "--rec" in sys.argv:
            r = m.call_json("bili_recommend", {"count": 3})
            print("  推荐:", json.dumps(r, ensure_ascii=False)[:300])
