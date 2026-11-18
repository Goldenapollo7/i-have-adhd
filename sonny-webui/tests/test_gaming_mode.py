"""Tests for Gaming PC instant kill-switch, media_stop_helper.py, and batch guardrails."""

from __future__ import annotations

import importlib.util
import tempfile
import unittest
from pathlib import Path

KIT_ROOT = Path(__file__).resolve().parent.parent
HELPER_PATH = KIT_ROOT / "gaming-pc" / "media_stop_helper.py"


def load_helper():
    spec = importlib.util.spec_from_file_location("media_stop_helper", HELPER_PATH)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class TestGamingModeKillSwitch(unittest.TestCase):
    def setUp(self) -> None:
        self.helper = load_helper()

    def test_default_media_stop_list_is_safe_opt_in_noop(self) -> None:
        calls: list[list[str]] = []

        def fake_runner(cmd: list[str], timeout: int) -> tuple[int, str]:
            calls.append(cmd)
            return 0, ""

        default_list = KIT_ROOT / "gaming-pc" / "media_stop_list.txt"
        res = self.helper.execute_stop_list(default_list, runner=fake_runner)
        self.assertEqual(calls, [])
        self.assertEqual(res["stopped"], [])
        self.assertEqual(res["warnings"], [])
        self.assertEqual(res["errors"], [])

    def test_stops_only_opt_in_media_workloads_and_deduplicates(self) -> None:
        sample = """
        # User opt-in standalone Gaming PC media generators
        container:comfyui
        container:comfyui
        container:invokeai
        process:ffmpeg.exe
        process:FFMPEG.EXE
        process:blender.exe
        """
        calls: list[list[str]] = []

        def fake_runner(cmd: list[str], timeout: int) -> tuple[int, str]:
            calls.append(cmd)
            if cmd[-1] == "invokeai":
                return 1, "Error response from daemon: No such container: invokeai"
            if cmd[-1] == "blender.exe":
                return 128, "ERROR: The process blender.exe not found."
            return 0, "ok"

        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "media_stop_list.txt"
            p.write_text(sample, encoding="utf-8")
            res = self.helper.execute_stop_list(p, runner=fake_runner)

        self.assertEqual(
            calls,
            [
                ["docker", "stop", "-t", "3", "comfyui"],
                ["docker", "stop", "-t", "3", "invokeai"],
                ["taskkill", "/F", "/T", "/IM", "ffmpeg.exe"],
                ["taskkill", "/F", "/T", "/IM", "blender.exe"],
            ],
        )
        self.assertEqual(res["stopped"], ["container:comfyui", "process:ffmpeg.exe"])
        self.assertEqual(res["not_running"], ["container:invokeai", "process:blender.exe"])
        self.assertEqual(res["errors"], [])

    def test_blocks_protected_processes_containers_and_shell_injection(self) -> None:
        hostile = "\n".join(
            [
                "process:python.exe",
                "process:explorer.exe",
                "process:steam.exe",
                "process:EasyAntiCheat.exe",
                "process:BEService.exe",
                "process:vgc.exe",
                "process:iCUE.exe",
                "process:discord.exe",
                "process:obs64.exe",
                "process:tailscaled.exe",
                "process:dockerd.exe",
                "process:code.exe",
                "process:cmd.exe",
                "process:powershell.exe",
                "process:not_exe",
                "process:bad;inject.exe",
                "container:sonny-webui",
                "container:open-webui",
                "container:tailscale",
                "container:comfy && calc",
                "unknown:foo",
                "malformed_no_colon",
            ]
        )
        entries, warnings = self.helper.parse_stop_list(hostile)
        self.assertEqual(entries, [])
        self.assertEqual(len(warnings), 22)

    def test_dry_run_never_executes_commands(self) -> None:
        called = False

        def boom(cmd: list[str], timeout: int) -> tuple[int, str]:
            nonlocal called
            called = True
            return 0, ""

        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "media_stop_list.txt"
            p.write_text("container:comfyui\nprocess:ffmpeg.exe\n", encoding="utf-8")
            res = self.helper.execute_stop_list(p, dry_run=True, runner=boom)

        self.assertFalse(called)
        self.assertEqual(len(res["stopped"]), 2)

    def test_gaming_mode_batch_ordering_and_firewall_contracts(self) -> None:
        gm_on = (KIT_ROOT / "gaming-pc" / "GAME_MODE_ON.cmd").read_text(encoding="ascii")
        # Lock file MUST be written BEFORE ending task and killing open-terminal to prevent restart races!
        lock_pos = gm_on.index('"%LOCK_FILE%"')
        schtasks_pos = gm_on.index('schtasks /End /TN "SonnyTerminal_GamingPC"')
        kill_pos = gm_on.index("taskkill /F /T /IM open-terminal.exe")
        self.assertLess(lock_pos, schtasks_pos)
        self.assertLess(schtasks_pos, kill_pos)

        # The kill must target ONLY open-terminal.exe. `python.exe` legitimately appears in
        # GAME_MODE_ON.cmd as the venv path used to run media_stop_helper.py, so assert on
        # actual taskkill invocations rather than the raw substring.
        taskkill_lines = [
            line.strip().lower() for line in gm_on.splitlines() if "taskkill" in line.lower()
        ]
        self.assertTrue(taskkill_lines, "GAME_MODE_ON.cmd must contain a taskkill of open-terminal.exe")
        for line in taskkill_lines:
            self.assertIn("/im open-terminal.exe", line)
            self.assertNotRegex(
                line,
                r"/im\s+(python|pythonw|cmd|powershell|explorer|steam|discord|obs64|icue|tailscaled?)",
            )

        guarded = (KIT_ROOT / "gaming-pc" / "START_AGENT_GUARDED.cmd").read_text(encoding="ascii")
        self.assertLess(guarded.index('if exist "%LOCK_FILE%"'), guarded.index("--isolated --config"))

        installer = (KIT_ROOT / "gaming-pc" / "INSTALL_GAMING_PC.cmd").read_text(encoding="ascii")
        self.assertIn("Ctrl+Alt+G", installer)
        self.assertIn("100.64.0.0/10", installer)
        self.assertIn("remoteip=%AI_TS_IP%", installer)
        self.assertIn("icacls", installer)
        self.assertIn(r"C:\SonnyWork\code", installer)
        self.assertIn(r"C:\SonnyWork\media", installer)


if __name__ == "__main__":
    unittest.main()
