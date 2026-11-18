"""End-to-end tests for openwebui/sonny_pc_ops.py against live mock Open Terminal v0.17.20 servers."""

from __future__ import annotations

import asyncio
import importlib.util
import json
import threading
import unittest
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

KIT_ROOT = Path(__file__).resolve().parent.parent
TOOLS_PATH = KIT_ROOT / "openwebui" / "sonny_pc_ops.py"


def load_tools_module():
    spec = importlib.util.spec_from_file_location("sonny_pc_ops", TOOLS_PATH)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class _MockOpenTerminalServer:
    """Faithful mock of Open Terminal v0.17.20 including Bearer auth & (x-user-id, x-session-id) scoping."""

    def __init__(
        self,
        *,
        hostname: str,
        api_key: str,
        vram_used_mib: int = 1200,
        vram_total_mib: int = 32607,
    ) -> None:
        self.hostname = hostname
        self.api_key = api_key
        self.vram_used_mib = vram_used_mib
        self.vram_total_mib = vram_total_mib
        self.gaming_mode_locked = False
        self.jobs: dict[str, dict[str, Any]] = {}
        self.executed_commands: list[dict[str, Any]] = []
        self.killed_job_ids: list[str] = []
        self.killed_host_pids: list[int] = []
        self.stopped_containers: list[str] = []
        self.host_pid_table: dict[int, str] = {
            4200: "steam.exe",
            4300: "EasyAntiCheat.exe",
            4400: "explorer.exe",
            4500: "iCUE.exe",
            4600: "tailscaled.exe",
            4700: "ollama.exe",
            14320: "blender.exe",
        }
        self._seq = 0
        self._lock = threading.Lock()

        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
                return

            def _send_json(self, code: int, data: Any) -> None:
                raw = json.dumps(data).encode("utf-8")
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def _check_auth(self) -> bool:
                auth = self.headers.get("Authorization", "")
                if auth != f"Bearer {outer.api_key}":
                    # Intentionally echo secret in an error test vector to verify scrubbing
                    self._send_json(401, {"detail": f"Unauthorized (expected not {outer.api_key})"})
                    return False
                return True

            def do_GET(self) -> None:  # noqa: N802
                if outer.gaming_mode_locked:
                    # Simulate closed socket / instant refusal when Gaming Mode is ON
                    self.connection.close()
                    return

                parsed = urllib.parse.urlparse(self.path)
                path = parsed.path

                if path == "/health":
                    self._send_json(200, {"status": "ok"})
                    return

                if path == "/api/ps":
                    self._send_json(200, {"models": [{"name": "sonny7-uncensored-8b:latest"}]})
                    return

                if not self._check_auth():
                    return

                if path == "/system/info":
                    self._send_json(
                        200,
                        {
                            "sampled_at": "2026-11-18T19:00:00Z",
                            "hostname": outer.hostname,
                            "os": "Windows",
                            "arch": "AMD64",
                            "environment": "native",
                            "service_uptime_seconds": 3600,
                            "uptime_seconds": 7200,
                            "cpu": {"percent": 8.5, "cores": 16, "scope": "host"},
                            "memory": {"total": 64 * 1024**3, "used": 14 * 1024**3, "scope": "host"},
                            "disk": {"path": "C:\\", "total": 4000 * 1024**3, "used": 900 * 1024**3, "free": 3100 * 1024**3},
                            "processes": [
                                {"pid": 4200, "name": "steam.exe", "cpu_percent": 1.2, "memory_bytes": 420 * 1024**2},
                                {"pid": 14320, "name": "blender.exe", "cpu_percent": 65.0, "memory_bytes": 6200 * 1024**2},
                            ],
                            "process_scope": "host",
                        },
                    )
                    return

                if path == "/execute":
                    user_id = self.headers.get("X-User-Id", "")
                    sess_id = self.headers.get("X-Session-Id", "")
                    with outer._lock:
                        matched = [
                            {
                                "id": j["id"],
                                "command": j["command"],
                                "status": j["status"],
                                "exit_code": j["exit_code"],
                                "log_path": f"C:\\SonnyAgent\\logs\\{j['id']}.jsonl",
                            }
                            for j in outer.jobs.values()
                            if j["user_id"] == user_id and j["session_id"] == sess_id
                        ]
                    self._send_json(200, matched)
                    return

                if path.startswith("/execute/") and path.endswith("/status"):
                    jid = urllib.parse.unquote(path[len("/execute/") : -len("/status")])
                    user_id = self.headers.get("X-User-Id", "")
                    sess_id = self.headers.get("X-Session-Id", "")
                    job = outer.jobs.get(jid)
                    if not job or job["user_id"] != user_id or job["session_id"] != sess_id:
                        self._send_json(404, {"detail": "Process not found"})
                        return
                    self._send_json(
                        200,
                        {
                            "id": job["id"],
                            "command": job["command"],
                            "cwd": job["cwd"],
                            "status": job["status"],
                            "exit_code": job["exit_code"],
                            "output": [{"type": "stdout", "data": job["stdout"]}],
                            "next_offset": 1,
                            "truncated": False,
                        },
                    )
                    return

                self._send_json(404, {"detail": "Not found"})

            def do_POST(self) -> None:  # noqa: N802
                if outer.gaming_mode_locked:
                    self.connection.close()
                    return
                if not self._check_auth():
                    return

                parsed = urllib.parse.urlparse(self.path)
                if parsed.path != "/execute":
                    self._send_json(404, {"detail": "Not found"})
                    return

                length = int(self.headers.get("Content-Length", "0"))
                body = json.loads(self.rfile.read(length).decode("utf-8"))
                cmd = str(body.get("command", ""))
                cwd = str(body.get("cwd") or "C:\\SonnyWork")
                user_id = self.headers.get("X-User-Id", "")
                sess_id = self.headers.get("X-Session-Id", "")
                outer.executed_commands.append({"command": cmd, "cwd": cwd, "user_id": user_id, "session_id": sess_id})

                # Simulate nvidia-smi
                if cmd.lower().startswith("nvidia-smi"):
                    line = (
                        f"NVIDIA GeForce RTX 5090, 41, 12, {outer.vram_used_mib}, "
                        f"{outer.vram_total_mib}, 88.4\n"
                    )
                    self._send_json(
                        200,
                        {
                            "id": "gpu-q",
                            "command": cmd,
                            "cwd": cwd,
                            "status": "done",
                            "exit_code": 0,
                            "output": [{"type": "stdout", "data": line}],
                            "next_offset": 1,
                            "truncated": False,
                        },
                    )
                    return

                # Simulate tasklist PID verification
                if cmd.lower().startswith("tasklist /fi"):
                    pid = int(cmd.split("PID eq ")[1].split('"')[0])
                    exe = outer.host_pid_table.get(pid)
                    out = f'"{exe}","{pid}","Console","1","512,000 K"\r\n' if exe else "INFO: No tasks are running.\r\n"
                    self._send_json(
                        200,
                        {"id": "tl", "command": cmd, "status": "done", "exit_code": 0, "output": [{"type": "stdout", "data": out}]},
                    )
                    return

                # Simulate taskkill /PID <n> /T /F
                if cmd.lower().startswith("taskkill /pid "):
                    pid = int(cmd.split()[2])
                    outer.killed_host_pids.append(pid)
                    self._send_json(
                        200,
                        {
                            "id": "tk",
                            "command": cmd,
                            "status": "done",
                            "exit_code": 0,
                            "output": [{"type": "stdout", "data": f"SUCCESS: The process with PID {pid} has been terminated.\n"}],
                        },
                    )
                    return

                # Simulate docker stop -t 3 <container>
                if cmd.startswith("docker stop -t 3 "):
                    cname = cmd.split()[-1]
                    outer.stopped_containers.append(cname)
                    self._send_json(
                        200,
                        {"id": "ds", "command": cmd, "status": "done", "exit_code": 0, "output": [{"type": "stdout", "data": cname}]},
                    )
                    return

                # Simulate GAME_MODE_ON.cmd immediately shutting down open-terminal mid-request!
                if "GAME_MODE_ON.cmd" in cmd:
                    outer.gaming_mode_locked = True
                    with outer._lock:
                        for j in outer.jobs.values():
                            j["status"] = "killed"
                    self.connection.close()
                    return

                # Long-running vs instant job tracking
                is_bg = "long-job" in cmd
                with outer._lock:
                    outer._seq += 1
                    jid = f"job-{outer._seq}"
                    record = {
                        "id": jid,
                        "command": cmd,
                        "cwd": cwd,
                        "user_id": user_id,
                        "session_id": sess_id,
                        "status": "running" if is_bg else "done",
                        "exit_code": None if is_bg else 0,
                        "stdout": f"output for {cmd}\n",
                    }
                    outer.jobs[jid] = record

                self._send_json(
                    200,
                    {
                        "id": jid,
                        "command": cmd,
                        "cwd": cwd,
                        "status": record["status"],
                        "exit_code": record["exit_code"],
                        "output": [{"type": "stdout", "data": record["stdout"]}],
                        "next_offset": 1,
                        "truncated": False,
                    },
                )

            def do_DELETE(self) -> None:  # noqa: N802
                if outer.gaming_mode_locked:
                    self.connection.close()
                    return
                if not self._check_auth():
                    return
                parsed = urllib.parse.urlparse(self.path)
                if not parsed.path.startswith("/execute/"):
                    self._send_json(404, {"detail": "Not found"})
                    return
                jid = urllib.parse.unquote(parsed.path[len("/execute/") :])
                user_id = self.headers.get("X-User-Id", "")
                sess_id = self.headers.get("X-Session-Id", "")
                with outer._lock:
                    job = outer.jobs.get(jid)
                    if not job or job["user_id"] != user_id or job["session_id"] != sess_id:
                        self._send_json(404, {"detail": "Not found"})
                        return
                    job["status"] = "killed"
                    job["exit_code"] = -9
                    outer.killed_job_ids.append(jid)
                self._send_json(200, {"status": "killed"})

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)

    def start(self) -> None:
        self.thread.start()

    def stop(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(timeout=3)


class TestSonnyPCOps(unittest.TestCase):
    def setUp(self) -> None:
        self.mod = load_tools_module()
        self.ai_secret = "SECRET_AI_KEY_99887766"
        self.gaming_secret = "SECRET_GAMING_KEY_55443322"
        self.ai_srv = _MockOpenTerminalServer(hostname="JOSHAIPC", api_key=self.ai_secret)
        self.gaming_srv = _MockOpenTerminalServer(hostname="JOSHGAMINGPC", api_key=self.gaming_secret)
        self.ai_srv.start()
        self.gaming_srv.start()

        self.tools = self.mod.Tools()
        self.tools.valves.ai_pc_url = self.ai_srv.url
        self.tools.valves.ai_pc_api_key = self.ai_secret
        self.tools.valves.gaming_pc_url = self.gaming_srv.url
        self.tools.valves.gaming_pc_api_key = self.gaming_secret
        self.tools.valves.ollama_url = self.ai_srv.url
        self.tools.valves.gaming_media_containers = "comfyui, sonny-webui, bad;inject"
        self.tools.valves.health_timeout_seconds = 2

    def tearDown(self) -> None:
        self.ai_srv.stop()
        self.gaming_srv.stop()

    @staticmethod
    async def _approve(_: dict[str, Any]) -> bool:
        return True

    @staticmethod
    async def _deny(_: dict[str, Any]) -> bool:
        return False

    def test_check_homelab_status_both_online_and_with_gaming_mode_on(self) -> None:
        report = asyncio.run(self.tools.check_homelab_status())
        self.assertIn("JOSHAIPC", report)
        self.assertIn("JOSHGAMINGPC", report)
        self.assertIn("RTX 5090", report)
        self.assertIn("sonny7-uncensored-8b:latest", report)
        self.assertNotIn(self.ai_secret, report)
        self.assertNotIn(self.gaming_secret, report)

        # Now flip Gaming Mode ON -> AI PC still reports cleanly while Gaming PC shows GAMING MODE ON
        self.gaming_srv.gaming_mode_locked = True
        report_locked = asyncio.run(self.tools.check_homelab_status())
        self.assertIn("JOSHAIPC", report_locked)
        self.assertIn("GAMING MODE ON / OFFLINE", report_locked)

    def test_gaming_pc_coding_and_media_job_lifecycle_and_instant_kill(self) -> None:
        # Denied confirmation -> never runs
        denied = asyncio.run(
            self.tools.start_gaming_pc_job("coding", "pytest -q", __event_call__=self._deny)
        )
        self.assertIn("Cancelled", denied)
        self.assertEqual(len(self.gaming_srv.jobs), 0)

        # Missing __event_call__ -> fail-closed
        no_ui = asyncio.run(self.tools.start_gaming_pc_job("coding", "pytest -q"))
        self.assertIn("fail-closed", no_ui)
        self.assertEqual(len(self.gaming_srv.jobs), 0)

        # Start long-running coding job (defaults to C:\SonnyWork\code)
        c_out = asyncio.run(
            self.tools.start_gaming_pc_job("coding", "python build.py --long-job", __event_call__=self._approve)
        )
        self.assertIn("job-1", c_out)
        self.assertIn("C:\\SonnyWork\\code", c_out)

        # Start long-running media job with low VRAM warning check (defaults to C:\SonnyWork\media)
        self.gaming_srv.vram_used_mib = 31500
        prompts: list[dict[str, Any]] = []

        async def capture_and_approve(ev: dict[str, Any]) -> bool:
            prompts.append(ev)
            return True

        m_out = asyncio.run(
            self.tools.start_gaming_pc_job("media", "ffmpeg -i in.mp4 --long-job out.mp4", __event_call__=capture_and_approve)
        )
        self.assertIn("job-2", m_out)
        self.assertIn("C:\\SonnyWork\\media", m_out)
        self.assertIn("WARNING", prompts[0]["data"]["message"])

        # Every request must carry the fixed owner identity plus its session id. The mock is
        # self-consistent, so asserting only that jobs are visible would pass even with a wrong
        # constant owner -- assert the wire headers directly instead.
        self.assertTrue(self.gaming_srv.executed_commands)
        for record in self.gaming_srv.executed_commands:
            self.assertEqual(record["user_id"], "sonny-owner")
            self.assertTrue(
                record["session_id"].startswith("sonny-gaming-"),
                f"unexpected session id {record['session_id']!r}",
            )

        coding_sessions = {
            r["session_id"] for r in self.gaming_srv.executed_commands if r["cwd"].endswith("code")
        }
        media_sessions = {
            r["session_id"] for r in self.gaming_srv.executed_commands if r["cwd"].endswith("media")
        }
        self.assertEqual(coding_sessions, {"sonny-gaming-coding"})
        self.assertEqual(media_sessions, {"sonny-gaming-media"})

        # Session isolation: a job created under the coding session must NOT be readable
        # through the media session, even with the same owner id.
        mismatched = asyncio.run(self.tools.get_job_output("gaming", "job-1", job_type="media"))
        self.assertNotIn("output for python build.py", mismatched)

        # Inspect tracked job output & process listing (tests exact X-User-Id + X-Session-Id scoping!)
        tail = asyncio.run(self.tools.get_job_output("gaming", "job-1", job_type="coding"))
        self.assertIn("running", tail)
        self.assertIn("output for python build.py --long-job", tail)

        procs = asyncio.run(self.tools.list_pc_processes("gaming", filter_name="blender"))
        self.assertIn("blender.exe", procs)
        self.assertIn("14320", procs)

        # Owner wants to game -> instant stop (jobs_only) kills BOTH jobs + comfyui, skips sonny-webui & injection!
        stopped = asyncio.run(self.tools.stop_gaming_pc_work(mode="jobs_only"))
        self.assertIn("job-1", stopped)
        self.assertIn("job-2", stopped)
        self.assertEqual(sorted(self.gaming_srv.killed_job_ids), ["job-1", "job-2"])
        self.assertEqual(self.gaming_srv.stopped_containers, ["comfyui"])

    def test_single_job_stop_and_full_gaming_mode_lock(self) -> None:
        asyncio.run(self.tools.start_gaming_pc_job("media", "render_a --long-job", __event_call__=self._approve))
        asyncio.run(self.tools.start_gaming_pc_job("coding", "build_b --long-job", __event_call__=self._approve))

        single = asyncio.run(self.tools.stop_gaming_pc_work(mode="single", process_id="job-1"))
        self.assertIn("Force-killed", single)
        self.assertEqual(self.gaming_srv.jobs["job-1"]["status"], "killed")
        self.assertEqual(self.gaming_srv.jobs["job-2"]["status"], "running")

        # Full lock_gaming_mode requires confirmation, kills remaining job-2, and runs GAME_MODE_ON.cmd
        denied_lock = asyncio.run(self.tools.stop_gaming_pc_work(mode="lock_gaming_mode", __event_call__=self._deny))
        self.assertIn("Cancelled", denied_lock)
        self.assertFalse(self.gaming_srv.gaming_mode_locked)

        locked = asyncio.run(self.tools.stop_gaming_pc_work(mode="lock_gaming_mode", __event_call__=self._approve))
        self.assertIn("GAMING MODE IS NOW ON", locked)
        self.assertIn("job-2", locked)
        self.assertTrue(self.gaming_srv.gaming_mode_locked)

        # Subsequent job attempt while Gaming Mode is ON is cleanly refused
        refused = asyncio.run(
            self.tools.start_gaming_pc_job("coding", "npm test", __event_call__=self._approve)
        )
        self.assertIn("GAMING MODE / OFFLINE", refused)

    def test_read_only_fast_path_vs_chained_injection_and_hard_blocks(self) -> None:
        # Safe single read-only command needs NO confirmation. Assert the confirmation
        # callback is never invoked, not merely that the command ran.
        confirmations: list[dict[str, Any]] = []

        async def record_confirm(ev: dict[str, Any]) -> bool:
            confirmations.append(ev)
            return True

        ro = asyncio.run(self.tools.run_pc_command("ai", "git status", __event_call__=record_confirm))
        self.assertIn("done", ro)
        self.assertEqual(confirmations, [], "read-only commands must not prompt for confirmation")

        # A mutating but non-blocked command MUST prompt.
        mutating = asyncio.run(self.tools.run_pc_command("ai", "git commit -m wip", __event_call__=record_confirm))
        self.assertEqual(len(confirmations), 1, "mutating commands must prompt for confirmation")
        self.assertIn("done", mutating)

        # Shell chaining defeats read-only bypass -> fails closed without confirmation
        chained = asyncio.run(self.tools.run_pc_command("ai", "git status && del foo.txt", __event_call__=None))
        self.assertIn("fail-closed", chained)

        # Hard-blocked commands are refused even when owner clicks Confirm!
        forbidden_both = [
            "powershell -ExecutionPolicy Bypass -File x.ps1",
            "Set-ExecutionPolicy Unrestricted",
            r"del C:\SonnyAgent\GAMING_MODE.lock",
            r"C:\SonnyAgent\GAME_MODE_OFF.cmd",
            "tailscale funnel 3000",
            "taskkill /F /IM python.exe",
            "taskkill /F /IM steam.exe",
            "taskkill /F /IM EasyAntiCheat.exe",
            "Stop-Process -Name tailscaled -Force",
            "net stop Tailscale",
            "ollama pull llama3",
            "ollama rm sonny7-uncensored-8b:latest",
            "format D: /Q",
        ]
        for cmd in forbidden_both:
            res = asyncio.run(self.tools.run_pc_command("ai", cmd, __event_call__=self._approve))
            self.assertTrue(res.startswith("BLOCKED:"), f"Expected block for {cmd!r}, got {res!r}")

        # Role split: Gaming PC must never run Open WebUI or main Ollama AI!
        for g_cmd in (
            "docker run -d ghcr.io/open-webui/open-webui:main",
            "ollama serve",
            "ollama run sonny7-uncensored-8b:latest",
        ):
            res = asyncio.run(self.tools.start_gaming_pc_job("coding", g_cmd, __event_call__=self._approve))
            self.assertTrue(res.startswith("BLOCKED:"), f"Expected Gaming PC role block for {g_cmd!r}, got {res!r}")

        # Path traversal process_id rejected
        bad_id = asyncio.run(self.tools.get_job_output("gaming", "../../secret"))
        self.assertEqual(bad_id, "Invalid process_id.")

    def test_kill_host_process_protects_os_games_anticheat_and_requires_confirm(self) -> None:
        # Reserved PID blocked
        self.assertIn("BLOCKED", asyncio.run(self.tools.kill_host_process("gaming", 4, __event_call__=self._approve)))

        # Protected executables blocked before any kill
        for protected_pid in (4200, 4300, 4400, 4500, 4600, 4700):
            out = asyncio.run(self.tools.kill_host_process("gaming", protected_pid, __event_call__=self._approve))
            self.assertIn("BLOCKED", out)
        self.assertEqual(self.gaming_srv.killed_host_pids, [])

        # Even if require_confirmation=False, arbitrary host PID kill STILL requires live approval
        self.tools.valves.require_confirmation = False
        no_cb = asyncio.run(self.tools.kill_host_process("gaming", 14320, __event_call__=None))
        self.assertIn("fail-closed", no_cb)
        denied = asyncio.run(self.tools.kill_host_process("gaming", 14320, __event_call__=self._deny))
        self.assertIn("Cancelled", denied)
        self.assertEqual(self.gaming_srv.killed_host_pids, [])

        # Approved kill of stuck media renderer (blender.exe PID 14320) succeeds cleanly
        ok = asyncio.run(
            self.tools.kill_host_process("gaming", 14320, reason="stuck render before gaming", __event_call__=self._approve)
        )
        self.assertIn("Terminated `blender.exe`", ok)
        self.assertEqual(self.gaming_srv.killed_host_pids, [14320])

    def test_kill_switch_cannot_be_neutered_from_chat(self) -> None:
        """The owner's <1s escape hatch must be inviolable, even with Confirm pressed."""
        neutering = [
            "del C:\\SonnyAgent\\GAMING_MODE.lock",
            "del /f C:\\SonnyAgent\\GAME_MODE_ON.cmd",
            "ren C:\\SonnyAgent\\GAME_MODE_ON.cmd off.bak",
            "copy nul C:\\SonnyAgent\\GAME_MODE_ON.cmd",
            "copy nul GAME_MODE_ON.cmd",
            "icacls C:\\SonnyAgent\\GAME_MODE_ON.cmd /deny %USERNAME%:F",
            "takeown /f C:\\SonnyAgent\\GAME_MODE_ON.cmd",
            "move C:\\SonnyAgent\\GAME_MODE_OFF.cmd D:\\stash\\",
            "attrib +h +s C:\\SonnyAgent\\GAME_MODE_ON.cmd",
            "schtasks /Change /TN SonnyTerminal_GamingPC /DISABLE",
            "schtasks /Delete /TN SonnyTerminal_GamingPC /F",
            "schtasks /End /TN SonnyTerminal_GamingPC",
            "reg add HKLM\\System\\CurrentControlSet\\Services\\Tailscale /v Start /t REG_DWORD /d 4 /f",
            "sc config Tailscale start= disabled",
            "sc delete ollama",
            '"C:\\SonnyAgent\\GAME_MODE_ON.cmd"',
            "C:\\SonnyAgent\\GAME_MODE_ON.cmd --nopause",
        ]
        for cmd in neutering:
            res = asyncio.run(self.tools.run_pc_command("gaming", cmd, __event_call__=self._approve))
            self.assertTrue(res.startswith("BLOCKED:"), f"kill-switch tamper not blocked: {cmd!r} -> {res!r}")

        # Reading/inspecting the switch, and querying the task, stay allowed (no false positives).
        for allowed in (
            "dir C:\\SonnyAgent",
            "type C:\\SonnyAgent\\media_stop_list.txt",
            "schtasks /Query /TN SonnyTerminal_GamingPC",
            "sc query tailscale",
        ):
            res = asyncio.run(self.tools.run_pc_command("gaming", allowed, __event_call__=self._approve))
            self.assertFalse(res.startswith("BLOCKED:"), f"over-blocked legitimate command: {allowed!r}")

    def test_full_control_is_preserved(self) -> None:
        """Sonny must retain genuine full control of both PCs (the owner's explicit requirement)."""
        full_control = [
            ("ai", "taskkill /F /PID 1234"),
            ("ai", "sc query spooler"),
            ("ai", "net start spooler"),
            ("ai", "shutdown /r /t 60"),
            ("ai", "robocopy C:\\src D:\\dst /E"),
            ("ai", "reg query HKLM\\Software"),
            ("ai", "npm run build"),
            ("ai", "docker compose up -d"),
            ("ai", "ollama run sonny7-uncensored-8b:latest"),
            ("gaming", "git push"),
            ("gaming", "python train.py"),
            ("gaming", "ffmpeg -i in.mp4 out.mp4"),
            ("gaming", "docker run --rm hello-world"),
            ("gaming", "nvidia-smi"),
        ]
        for target, cmd in full_control:
            self.assertIsNone(
                self.mod._validate_command(target, cmd),
                f"{target} should permit {cmd!r}",
            )

    def test_api_keys_are_scrubbed_from_error_outputs(self) -> None:
        self.tools.valves.ai_pc_api_key = "WRONG_KEY_FOR_AUTH_TEST"
        status = asyncio.run(self.tools.check_homelab_status())
        self.assertIn("AUTH ERROR (401)", status)
        # Also verify the configured valve key itself is scrubbed if echoed back
        scrubbed = self.tools._scrub(f"leak={self.gaming_secret}")
        self.assertEqual(scrubbed, "leak=[REDACTED]")


if __name__ == "__main__":
    unittest.main()
