"""Safe, allow-formatted media workload stopper for Gaming PC Gaming Mode.

Invoked by GAME_MODE_ON.cmd after open-terminal.exe and its child tree have been
terminated. Reads C:\\SonnyAgent\\media_stop_list.txt (or --list path) and stops
only explicitly listed Docker media containers or dedicated media executables,
while refusing protected OS, gaming, peripheral, networking, and broad runtime
names so Gaming Mode can never kill desktop apps, Steam, iCUE, or Tailscale.
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path
from typing import Callable

CONTAINER_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
PROCESS_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}\.exe$", re.IGNORECASE)

# Lowercase executable names that must NEVER be terminated by Gaming Mode.
PROTECTED_PROCESSES: frozenset[str] = frozenset(
    {
        # Windows core
        "system",
        "smss.exe",
        "csrss.exe",
        "wininit.exe",
        "services.exe",
        "lsass.exe",
        "svchost.exe",
        "winlogon.exe",
        "dwm.exe",
        "explorer.exe",
        "sihost.exe",
        "taskhostw.exe",
        "conhost.exe",
        "openconsole.exe",
        "windowsterminal.exe",
        "audiodg.exe",
        "fontdrvhost.exe",
        "spoolsv.exe",
        "searchhost.exe",
        "startmenuexperiencehost.exe",
        "shellexperiencehost.exe",
        "runtimebroker.exe",
        "dllhost.exe",
        "ctfmon.exe",
        "msmpeng.exe",
        "securityhealthservice.exe",
        # Broad shells & language runtimes (never blanket-kill!)
        "cmd.exe",
        "powershell.exe",
        "pwsh.exe",
        "python.exe",
        "python3.exe",
        "pythonw.exe",
        "node.exe",
        "git.exe",
        "ssh.exe",
        "wsl.exe",
        "wslservice.exe",
        # Networking, Docker engine, & NVIDIA driver services
        "tailscale.exe",
        "tailscaled.exe",
        "tailscale-ipn.exe",
        "docker.exe",
        "dockerd.exe",
        "com.docker.backend.exe",
        "com.docker.service",
        "docker desktop.exe",
        "nvcontainer.exe",
        "nvdisplay.container.exe",
        "nvidia overlay.exe",
        "nvidia share.exe",
        # Corsair iCUE & Xeneon Edge peripherals
        "icue.exe",
        "icuedevicepluginhost.exe",
        "icueupdateservice.exe",
        "corsair.service.exe",
        "corsaircpuidservice.exe",
        "corsairgamingaudiocfgservice64.exe",
        # Gaming launchers, anti-cheat, voice chat, streaming, browsers
        "steam.exe",
        "steamwebhelper.exe",
        "steamservice.exe",
        "epicgameslauncher.exe",
        "battle.net.exe",
        "eadesktop.exe",
        "galaxyclient.exe",
        "xboxpcapp.exe",
        "gamingservices.exe",
        "easyanticheat.exe",
        "easyanticheat_eos.exe",
        "beservice.exe",
        "vgk.sys",
        "vgc.exe",
        "vgtray.exe",
        "discord.exe",
        "obs64.exe",
        "chrome.exe",
        "msedge.exe",
        "firefox.exe",
        "brave.exe",
        # Code editors (don't close user's open editor unsaved)
        "code.exe",
        "cursor.exe",
        "devenv.exe",
    }
)

# Lowercase container names that should never be stopped via the Gaming PC list.
PROTECTED_CONTAINERS: frozenset[str] = frozenset(
    {
        "sonny-webui",
        "open-webui",
        "tailscale",
    }
)

RunnerFn = Callable[[list[str], int], tuple[int, str]]


def default_runner(cmd: list[str], timeout: int = 10) -> tuple[int, str]:
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
        out = (proc.stdout or proc.stderr or "").strip()
        return proc.returncode, out
    except FileNotFoundError:
        return 127, f"command not found: {cmd[0]}"
    except subprocess.TimeoutExpired:
        return 124, f"timed out after {timeout}s: {' '.join(cmd)}"


def parse_stop_list(text: str) -> tuple[list[tuple[str, str]], list[str]]:
    """Parse media_stop_list.txt into validated (kind, target) pairs and skipped reasons."""
    entries: list[tuple[str, str]] = []
    warnings: list[str] = []
    seen: set[tuple[str, str]] = set()

    for lineno, raw_line in enumerate(text.splitlines(), start=1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if ":" not in line:
            warnings.append(f"line {lineno}: ignored '{line}' (expected container:<name> or process:<name.exe>)")
            continue

        kind_raw, target_raw = line.split(":", 1)
        kind = kind_raw.strip().lower()
        target = target_raw.strip()

        if kind == "container":
            if not CONTAINER_NAME_RE.match(target):
                warnings.append(f"line {lineno}: rejected invalid container name '{target}'")
                continue
            if target.lower() in PROTECTED_CONTAINERS:
                warnings.append(f"line {lineno}: blocked protected container '{target}'")
                continue
            key = ("container", target)
        elif kind == "process":
            if not PROCESS_NAME_RE.match(target):
                warnings.append(f"line {lineno}: rejected invalid process name '{target}' (must end in .exe)")
                continue
            if target.lower() in PROTECTED_PROCESSES:
                warnings.append(f"line {lineno}: blocked protected process '{target}'")
                continue
            key = ("process", target.lower())
            target = target
        else:
            warnings.append(f"line {lineno}: unknown prefix '{kind}' (use 'container' or 'process')")
            continue

        if key not in seen:
            seen.add(key)
            entries.append((kind, target))

    return entries, warnings


def execute_stop_list(
    list_path: Path,
    *,
    dry_run: bool = False,
    runner: RunnerFn = default_runner,
) -> dict[str, list[str]]:
    result: dict[str, list[str]] = {
        "stopped": [],
        "not_running": [],
        "warnings": [],
        "errors": [],
    }
    if not list_path.is_file():
        return result

    text = list_path.read_text(encoding="utf-8", errors="replace")
    entries, warnings = parse_stop_list(text)
    result["warnings"].extend(warnings)

    for kind, target in entries:
        if kind == "container":
            cmd = ["docker", "stop", "-t", "3", target]
        else:
            cmd = ["taskkill", "/F", "/T", "/IM", target]

        if dry_run:
            result["stopped"].append(f"[dry-run] {kind}:{target} -> {' '.join(cmd)}")
            continue

        code, output = runner(cmd, 12)
        low = output.lower()
        if code == 0:
            result["stopped"].append(f"{kind}:{target}")
        elif (
            "no such container" in low
            or "is not running" in low
            or "not found" in low
            or "no running instance" in low
            or code == 128
        ):
            result["not_running"].append(f"{kind}:{target}")
        else:
            result["errors"].append(f"{kind}:{target} (exit {code}: {output})")

    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Stop opt-in Gaming PC media workloads safely.")
    parser.add_argument(
        "--list",
        default=r"C:\SonnyAgent\media_stop_list.txt",
        help="Path to media_stop_list.txt",
    )
    parser.add_argument("--dry-run", action="store_true", help="Print actions without running them")
    args = parser.parse_args(argv)

    summary = execute_stop_list(Path(args.list), dry_run=args.dry_run)
    for item in summary["stopped"]:
        print(f"  [STOPPED] {item}")
    for warn in summary["warnings"]:
        print(f"  [SKIPPED] {warn}")
    for err in summary["errors"]:
        print(f"  [WARN]    {err}", file=sys.stderr)
    # Never fail GAME_MODE_ON.cmd if an optional media container was already off.
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
