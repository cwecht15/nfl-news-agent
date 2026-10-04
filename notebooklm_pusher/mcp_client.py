"""Minimal synchronous MCP stdio client for the notebooklm-mcp server.

Spawns `node tools/notebooklm-mcp/dist/index.js` — the *same* server Claude Code
talks to via `.mcp.json` — and speaks JSON-RPC 2.0 over its stdin/stdout. This is
the reliable transport; it does NOT use the HTTP wrapper on :3000.

The server logs to stderr, so stdout stays clean JSON-RPC. We keep one long-lived
child process (see get_client() in app.py, cached with st.cache_resource) and issue
tool calls synchronously. A background thread reads replies and routes them by id,
so long browser-driven calls (add_source, list_content) can have real timeouts.

Tool payloads come back as a JSON string inside content[0].text — we parse that and
return the tool's own {"success": ..., "data": {...}} object.
"""

from __future__ import annotations

import json
import os
import subprocess
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
NBLM_DIR = ROOT / "tools" / "notebooklm-mcp"
SERVER_JS = NBLM_DIR / "dist" / "index.js"
DATA_DIR = NBLM_DIR / "Data"
SERVER_LOG = ROOT / "notebooklm_pusher" / "mcp_server.log"

# Mirrors .mcp.json so we share auth/browser state with the Claude Code server.
SERVER_ENV = {
    "DATA_DIR": str(DATA_DIR),
    "CHROME_PROFILE_DIR": str(DATA_DIR / "chrome_profile"),
    "BROWSER_STATE_DIR": str(DATA_DIR / "browser_state"),
    "HEADLESS": "true",
    "STEALTH_ENABLED": "true",
    "NOTEBOOKLM_UI_LOCALE": "en",
}


class MCPError(Exception):
    """Raised on client/transport/tool failures."""


class NotebookLMClient:
    """Long-lived JSON-RPC client over a spawned notebooklm-mcp stdio server."""

    def __init__(self, node_exe: str = "node"):
        if not SERVER_JS.exists():
            raise MCPError(
                f"MCP server not built at {SERVER_JS}. Build it once:\n"
                "  cd tools/notebooklm-mcp && npm install && npm run build"
            )
        env = os.environ.copy()
        env.update(SERVER_ENV)
        try:
            self._stderr = open(SERVER_LOG, "a", encoding="utf-8")
        except OSError:
            self._stderr = subprocess.DEVNULL
        try:
            self.proc = subprocess.Popen(
                [node_exe, str(SERVER_JS)],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=self._stderr,
                env=env,
                text=True,
                encoding="utf-8",
                bufsize=1,
            )
        except FileNotFoundError as e:
            raise MCPError(
                "Node.js not found on PATH. Install Node 20+ (https://nodejs.org)."
            ) from e

        self._lock = threading.Lock()
        self._events: dict[str, threading.Event] = {}
        self._replies: dict[str, dict] = {}
        self._id = 0
        self._alive = True
        self._reader = threading.Thread(target=self._read_loop, daemon=True)
        self._reader.start()
        self._initialize()

    # -- transport ----------------------------------------------------------

    def _read_loop(self):
        try:
            for line in self.proc.stdout:
                line = line.strip()
                if not line:
                    continue
                try:
                    msg = json.loads(line)
                except json.JSONDecodeError:
                    continue  # ignore any non-JSON noise on stdout
                rid = msg.get("id")
                if rid is None:
                    continue
                key = str(rid)
                if key in self._events:
                    self._replies[key] = msg
                    self._events[key].set()
        finally:
            self._alive = False
            for ev in list(self._events.values()):
                ev.set()

    def _rpc(self, method: str, params: dict | None = None,
             timeout: float = 240, expect_reply: bool = True):
        if not self._alive or self.proc.poll() is not None:
            raise MCPError("MCP server process is not running (see mcp_server.log).")
        with self._lock:
            self._id += 1
            rid = str(self._id)
            msg = {"jsonrpc": "2.0", "method": method}
            if params is not None:
                msg["params"] = params
            ev = None
            if expect_reply:
                msg["id"] = self._id
                ev = threading.Event()
                self._events[rid] = ev
            try:
                self.proc.stdin.write(json.dumps(msg) + "\n")
                self.proc.stdin.flush()
            except (BrokenPipeError, OSError) as e:
                self._events.pop(rid, None)
                raise MCPError(f"Failed to send to MCP server: {e}") from e
        if not expect_reply:
            return None
        if not ev.wait(timeout):
            self._events.pop(rid, None)
            raise MCPError(f"Timed out after {int(timeout)}s waiting for '{method}'.")
        resp = self._replies.pop(rid, None)
        self._events.pop(rid, None)
        if resp is None:
            raise MCPError("MCP server exited before replying (see mcp_server.log).")
        if "error" in resp:
            raise MCPError(str(resp["error"]))
        return resp.get("result")

    def _initialize(self):
        self._rpc(
            "initialize",
            {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "nblm-pusher", "version": "1.0"},
            },
            timeout=45,
        )
        self._rpc("notifications/initialized", expect_reply=False)

    # -- tool call ----------------------------------------------------------

    def call_tool(self, name: str, arguments: dict | None = None,
                  timeout: float = 240) -> dict:
        """Call a tool; return its parsed {"success", "data", ...} payload."""
        result = self._rpc(
            "tools/call",
            {"name": name, "arguments": arguments or {}},
            timeout=timeout,
        )
        result = result or {}
        text = ""
        for c in result.get("content") or []:
            if c.get("type") == "text":
                text = c.get("text", "")
                break
        if not text:
            return {"success": not result.get("isError", False), "data": {}}
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            return {"success": not result.get("isError", False), "raw": text}

    # -- lifecycle ----------------------------------------------------------

    def alive(self) -> bool:
        return self._alive and self.proc.poll() is None

    def close(self):
        try:
            if self.proc and self.proc.poll() is None:
                self.proc.terminate()
        except Exception:  # noqa: BLE001
            pass
