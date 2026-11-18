"""
title: Sonny PC Ops
author: Sonny Homelab
version: 1.0.0
description: Guarded dual-PC control toolkit for Sonny. Manages AI PC (main AI + hub) and Gaming PC (coding + media generation worker), with instant Gaming Mode stop/kill controls, protected-process blocklists, and explicit Confirm/Deny prompts for consequential actions.
"""

from __future__ import annotations

import asyncio
import csv
import io
import json
import re
import socket
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Awaitable, Callable, Optional

try:
    from pydantic import BaseModel, Field
except ImportError:  # Fallback for bare stdlib test environments without pydantic

    def Field(default: Any = None, **_: Any) -> Any:  # type: ignore[misc]
        return default

    class BaseModel:  # type: ignore[no-redef]
        def __init__(self, **kwargs: Any) -> None:
            for cls in reversed(type(self).__mro__):
                for key, val in cls.__dict__.items():
                    if not key.startswith("_") and not callable(val):
                        setattr(self, key, val)
            for key, val in kwargs.items():
                setattr(self, key, val)

        def model_dump(self) -> dict[str, Any]:
            return dict(self.__dict__)


# Consistent Open Terminal session namespaces so Sonny can list and kill every job
# it launched across ANY chat or device (desktop, iPhone, Xeneon Edge).
AI_OPS_SESSION = "sonny-ai-ops"
GAMING_CODING_SESSION = "sonny-gaming-coding"
GAMING_MEDIA_SESSION = "sonny-gaming-media"
GAMING_OPS_SESSION = "sonny-gaming-ops"
ALL_GAMING_SESSIONS: tuple[str, ...] = (
    GAMING_CODING_SESSION,
    GAMING_MEDIA_SESSION,
    GAMING_OPS_SESSION,
)

CONTAINER_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")

# Strict single-command read-only patterns allowed without confirmation nagging.
# Rejects any command containing shell chaining, pipes, redirects, or subshells.
SHELL_METACHAR_RE = re.compile(r"[&|;><`\r\n]|\$\(")
SAFE_READ_ONLY_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"^nvidia-smi(\s+--(query-gpu|query-compute-apps|format)=[A-Za-z0-9_,.-]+|\s+-L)*$", re.IGNORECASE),
    re.compile(r"^(hostname|whoami|ver|systeminfo)$", re.IGNORECASE),
    re.compile(r"^git\s+(status|branch|log(\s+-\d+|\s+--oneline){0,2}|diff(\s+--stat)?)$", re.IGNORECASE),
    re.compile(r"^docker\s+(ps(\s+-a)?|stats\s+--no-stream)$", re.IGNORECASE),
    re.compile(r"^dir(\s+\"[A-Za-z0-9_:\\.\s-]+\"|\s+[A-Za-z0-9_:\\.-]+)?$", re.IGNORECASE),
)

# Commands Sonny is NEVER permitted to run on either PC, even with confirmation.
HARD_BLOCKED_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (
        re.compile(r"(-executionpolicy\s+(bypass|unrestricted)|set-executionpolicy\b)", re.IGNORECASE),
        "PowerShell ExecutionPolicy changes/bypasses are strictly forbidden.",
    ),
    (
        re.compile(r"gaming_mode\.lock", re.IGNORECASE),
        "Tampering with GAMING_MODE.lock is forbidden; only local GAME_MODE_OFF.cmd on the Gaming PC may clear it.",
    ),
    (
        re.compile(r"game_mode_off\.cmd", re.IGNORECASE),
        "Turning Gaming Mode OFF remotely is forbidden; run ALLOW SONNY directly on the Gaming PC.",
    ),
    # The kill-switch must stay inviolable. Sonny may TRIGGER GAME_MODE_ON.cmd (that is the
    # safe direction and is done internally by stop_gaming_pc_work), but must never modify,
    # rename, delete, re-ACL, or otherwise neuter the switch or its scheduled task. Without
    # this, chat could permanently disable the owner's <1s escape hatch.
    (
        re.compile(
            r"(del|erase|rmdir|rd|ren|rename|move|copy|replace|attrib|icacls|takeown|cacls|truncate)"
            r"[^&|\r\n]*sonnyagent",
            re.IGNORECASE,
        ),
        "Modifying anything in C:\\SonnyAgent (Gaming Mode switch, config, helper) is forbidden.",
    ),
    (
        re.compile(
            r"(del|erase|rmdir|rd|ren|rename|move|copy|replace|attrib|icacls|takeown|cacls|truncate)"
            r"[^&|\r\n]*game_mode_(on|off|status)\.cmd",
            re.IGNORECASE,
        ),
        "Modifying, renaming, or deleting the Gaming Mode scripts is forbidden.",
    ),
    (
        re.compile(
            r"(^|[&|\s])(del|erase|copy|move|ren|rename|attrib)\b[^&|\r\n]*\bcon\b",
            re.IGNORECASE,
        ),
        "Overwriting files with the null device (`copy nul <file>`) is forbidden.",
    ),
    (
        re.compile(
            r"schtasks[^&|\r\n]*(/(change|delete|end)\b[^&|\r\n]*sonnyterminal_gamingpc"
            r"|/create\b[^&|\r\n]*sonnyterminal_gamingpc)",
            re.IGNORECASE,
        ),
        "Modifying the SonnyTerminal_GamingPC scheduled task is forbidden; GAME_MODE_ON/OFF manage it locally.",
    ),
    (
        re.compile(
            r"reg(\.exe)?\s+(add|delete)\b[^&|\r\n]*(\\services\\)?(tailscale|ollama|docker)",
            re.IGNORECASE,
        ),
        "Modifying Tailscale/Ollama/Docker service registry keys is forbidden.",
    ),
    (
        re.compile(
            r"sc(\.exe)?\s+(config|delete)\b[^&|\r\n]*(tailscale|ollama|docker)",
            re.IGNORECASE,
        ),
        "Reconfiguring or deleting the Tailscale/Ollama/Docker services is forbidden.",
    ),
    (
        # Quoted absolute path to the switch as the FIRST token executes it directly.
        re.compile(r"^\s*\"?[a-z]:\\[^\"]*game_mode_on\.cmd", re.IGNORECASE),
        "Launching the Gaming Mode switch directly from chat is forbidden; use the locked stop action instead.",
    ),
    (
        re.compile(r"\btailscale(\.exe)?\s+funnel\b", re.IGNORECASE),
        "Public exposure via `tailscale funnel` is forbidden; Sonny stays strictly private.",
    ),
    (
        re.compile(r"\b(format\s+[a-z]:|diskpart\b|bcdedit\b|cipher\s+/w|vssadmin\s+delete)", re.IGNORECASE),
        "Destructive disk/boot commands are blocked.",
    ),
    (
        re.compile(
            r"(taskkill\s+[^&\r\n]*/im\s+(python|python3|pythonw|cmd|powershell|pwsh|explorer|steam|discord|obs64|icue|tailscaled?|ollama|dockerd|easyanticheat|easyanticheat_eos|beservice|vgc|svchost|lsass|csrss|dwm|winlogon)(\.exe)?\b"
            r"|stop-process\s+[^&\r\n]*-name\s+['\"]?(python|python3|pythonw|cmd|powershell|pwsh|explorer|steam|discord|obs64|icue|tailscaled?|ollama|dockerd|easyanticheat|beservice|vgc|svchost|lsass|dwm)\b"
            r"|(net\s+stop|sc(\.exe)?\s+stop|stop-service)\s+[^&\r\n]*(tailscale|ollama|docker))",
            re.IGNORECASE,
        ),
        "Blanket kills or stops of python, shell, Windows core, anti-cheat, Steam, Discord, OBS, iCUE, Ollama, Docker, or Tailscale are blocked.",
    ),
    (
        re.compile(r"\b(ollama\s+(rm|delete|pull)\b)", re.IGNORECASE),
        "Downloading or deleting Ollama models via Sonny is blocked unless run manually by the owner.",
    ),
)

# Extra hard blocks specific to the Gaming PC (role: coding + media generation ONLY).
GAMING_PC_ROLE_BLOCKS: tuple[tuple[re.Pattern[str], str], ...] = (
    (
        re.compile(r"(open-webui|sonny-webui|ghcr\.io/open-webui/open-webui)", re.IGNORECASE),
        "Open WebUI runs on the AI PC only — never deploy or start it on the Gaming PC.",
    ),
    (
        re.compile(r"\bollama(\.exe)?\s+(serve|run|pull|create)\b", re.IGNORECASE),
        "Primary AI (Ollama) runs on the AI PC only — Gaming PC is reserved for coding and media generation.",
    ),
)

# Protected host executables that `kill_host_process` refuses to terminate.
PROTECTED_HOST_EXES: frozenset[str] = frozenset(
    {
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
        "audiodg.exe",
        "msmpeng.exe",
        "securityhealthservice.exe",
        "tailscale.exe",
        "tailscaled.exe",
        "tailscale-ipn.exe",
        "dockerd.exe",
        "com.docker.backend.exe",
        "com.docker.service",
        "docker desktop.exe",
        "ollama.exe",
        "ollama app.exe",
        "nvcontainer.exe",
        "nvdisplay.container.exe",
        "icue.exe",
        "icuedevicepluginhost.exe",
        "icueupdateservice.exe",
        "corsair.service.exe",
        "corsaircpuidservice.exe",
        "steam.exe",
        "steamwebhelper.exe",
        "steamservice.exe",
        "epicgameslauncher.exe",
        "battle.net.exe",
        "eadesktop.exe",
        "xboxpcapp.exe",
        "gamingservices.exe",
        "easyanticheat.exe",
        "easyanticheat_eos.exe",
        "beservice.exe",
        "vgc.exe",
        "vgtray.exe",
        "discord.exe",
        "obs64.exe",
    }
)


class TerminalOfflineError(Exception):
    """Raised when an Open Terminal host is unreachable or closed (e.g. Gaming Mode ON)."""


class TerminalAPIError(Exception):
    """Raised on HTTP errors returned by Open Terminal."""

    def __init__(self, status: int, message: str) -> None:
        super().__init__(f"HTTP {status}: {message}")
        self.status = status
        self.message = message


def _fmt_gib(num_bytes: Any) -> str:
    if not isinstance(num_bytes, (int, float)) or num_bytes < 0:
        return "n/a"
    return f"{num_bytes / (1024 ** 3):.1f} GiB"


def _flatten_output(payload: dict[str, Any], max_chars: int = 4000) -> str:
    entries = payload.get("output")
    if not isinstance(entries, list):
        return ""
    text = "".join(str(item.get("data", "")) for item in entries if isinstance(item, dict)).strip()
    if len(text) > max_chars:
        return text[-max_chars:] + "\n...[truncated]"
    return text


def _validate_command(target_key: str, command: str) -> Optional[str]:
    cmd = command.strip()
    if not cmd:
        return "Command cannot be empty."
    for pattern, reason in HARD_BLOCKED_PATTERNS:
        if pattern.search(cmd):
            return f"BLOCKED: {reason}"
    if target_key == "gaming":
        for pattern, reason in GAMING_PC_ROLE_BLOCKS:
            if pattern.search(cmd):
                return f"BLOCKED: {reason}"
    return None


def _is_safe_read_only(command: str) -> bool:
    cmd = command.strip()
    if not cmd or SHELL_METACHAR_RE.search(cmd):
        return False
    return any(pattern.match(cmd) for pattern in SAFE_READ_ONLY_PATTERNS)


def _sync_request(
    method: str,
    url: str,
    *,
    api_key: str = "",
    session_id: str = "",
    body: Optional[dict[str, Any]] = None,
    timeout: float = 15.0,
) -> Any:
    headers: dict[str, str] = {"Accept": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    if session_id:
        headers["X-User-Id"] = "sonny-owner"
        headers["X-Session-Id"] = session_id

    data = None
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"

    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
            return json.loads(raw) if raw.strip() else {}
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            detail = exc.read().decode("utf-8", errors="replace")[:300]
        except Exception:
            detail = str(exc.reason)
        raise TerminalAPIError(exc.code, detail) from exc
    except (urllib.error.URLError, socket.timeout, TimeoutError, ConnectionError, OSError) as exc:
        raise TerminalOfflineError(str(exc)) from exc


class Tools:
    """Sonny's guarded dual-PC automation and Gaming Mode control toolkit."""

    class Valves(BaseModel):
        ai_pc_url: str = Field(
            default="http://host.docker.internal:8001",
            description="Open Terminal URL for the AI PC (main AI & Open WebUI host).",
        )
        ai_pc_api_key: str = Field(
            default="",
            description="Bearer API key for AI PC Open Terminal (auto-configured from ai-pc.toml).",
        )
        gaming_pc_url: str = Field(
            default="http://100.64.0.2:8002",
            description="Private Tailscale Open Terminal URL for the Gaming PC (coding & media generation worker).",
        )
        gaming_pc_api_key: str = Field(
            default="",
            description="Bearer API key for Gaming PC Open Terminal (auto-configured from gaming-pc.toml).",
        )
        ollama_url: str = Field(
            default="http://host.docker.internal:11434",
            description="AI PC Ollama URL for read-only VRAM/model status checks.",
        )
        gaming_coding_dir: str = Field(
            default="C:\\SonnyWork\\code",
            description="Default working directory for coding jobs on the Gaming PC.",
        )
        gaming_media_dir: str = Field(
            default="C:\\SonnyWork\\media",
            description="Default working directory for media generation jobs on the Gaming PC.",
        )
        gaming_media_containers: str = Field(
            default="",
            description="Optional comma-separated Docker container names on Gaming PC to stop when freeing GPU (e.g. comfyui,invokeai).",
        )
        require_confirmation: bool = Field(
            default=True,
            description="Prompt Confirm/Deny in chat before running non-read-only commands, Gaming PC jobs, or host PID kills.",
        )
        instant_gaming_stop: bool = Field(
            default=True,
            description="Allow stopping Sonny's own tracked Gaming PC jobs immediately without an extra prompt when you want to game.",
        )
        confirm_gaming_lock: bool = Field(
            default=True,
            description="Prompt Confirm/Deny before fully shutting down and locking the Gaming PC agent (GAME_MODE_ON.cmd).",
        )
        health_timeout_seconds: int = Field(
            default=3,
            description="Timeout in seconds when checking whether a PC's Open Terminal is online.",
        )

    def __init__(self) -> None:
        self.valves = self.Valves()

    def _scrub(self, text: str) -> str:
        """Never leak configured API keys into chat output or errors."""
        out = text
        for secret in (self.valves.ai_pc_api_key, self.valves.gaming_pc_api_key):
            if secret and len(secret) >= 6:
                out = out.replace(secret, "[REDACTED]")
        return out

    def _resolve_target(self, target: str) -> tuple[str, str, str, str]:
        """Return (target_key, label, base_url, api_key)."""
        norm = (target or "").strip().lower().replace("-", "").replace("_", "").replace(" ", "")
        if norm in ("ai", "aipc", "joshaipc", "main", "hub"):
            return ("ai", "AI PC", self.valves.ai_pc_url.strip().rstrip("/"), self.valves.ai_pc_api_key.strip())
        if norm in ("gaming", "gamingpc", "game", "gamepc", "worker", "media", "coding"):
            return (
                "gaming",
                "Gaming PC",
                self.valves.gaming_pc_url.strip().rstrip("/"),
                self.valves.gaming_pc_api_key.strip(),
            )
        raise ValueError(f"Unknown target '{target}'. Use 'ai' (AI PC) or 'gaming' (Gaming PC).")

    def _session_for_job(self, target_key: str, job_type: str = "ops") -> str:
        if target_key == "ai":
            return AI_OPS_SESSION
        jt = (job_type or "ops").strip().lower()
        if jt in ("coding", "code", "dev", "build", "test"):
            return GAMING_CODING_SESSION
        if jt in ("media", "image", "video", "audio", "render", "3d", "comfyui"):
            return GAMING_MEDIA_SESSION
        return GAMING_OPS_SESSION

    async def _call(
        self,
        method: str,
        base_url: str,
        path: str,
        *,
        api_key: str = "",
        session_id: str = "",
        body: Optional[dict[str, Any]] = None,
        timeout: float = 15.0,
    ) -> Any:
        url = f"{base_url}{path}"
        return await asyncio.to_thread(
            _sync_request,
            method,
            url,
            api_key=api_key,
            session_id=session_id,
            body=body,
            timeout=timeout,
        )

    async def _is_online(self, base_url: str) -> bool:
        if not base_url:
            return False
        try:
            res = await self._call(
                "GET",
                base_url,
                "/health",
                timeout=max(1, int(self.valves.health_timeout_seconds)),
            )
            return isinstance(res, dict) and res.get("status") == "ok"
        except (TerminalOfflineError, TerminalAPIError):
            return False

    async def _confirm(
        self,
        event_call: Optional[Callable[[dict[str, Any]], Awaitable[Any]]],
        title: str,
        message: str,
    ) -> tuple[bool, str]:
        """Fail-closed browser confirmation helper."""
        if not self.valves.require_confirmation:
            return True, ""
        if event_call is None:
            return (
                False,
                "Cancelled (fail-closed): action requires owner confirmation, but no interactive session was available.",
            )
        try:
            approved = await event_call(
                {
                    "type": "confirmation",
                    "data": {"title": title, "message": message},
                }
            )
        except Exception as exc:
            return False, self._scrub(f"Cancelled: confirmation dialog failed ({exc}).")
        if approved is True:
            return True, ""
        return False, "Cancelled: owner denied confirmation."

    async def _emit(
        self,
        emitter: Optional[Callable[[dict[str, Any]], Awaitable[Any]]],
        description: str,
        done: bool = True,
    ) -> None:
        if not emitter:
            return
        try:
            await emitter({"type": "status", "data": {"description": description, "done": done}})
        except Exception:
            pass

    async def _gpu_summary(self, base_url: str, api_key: str, session_id: str) -> tuple[str, Optional[int]]:
        """Query RTX 5090 stats via read-only nvidia-smi. Returns (summary_line, free_mib)."""
        cmd = (
            "nvidia-smi --query-gpu=name,temperature.gpu,utilization.gpu,memory.used,memory.total,power.draw "
            "--format=csv,noheader,nounits"
        )
        try:
            res = await self._call(
                "POST",
                base_url,
                "/execute?wait=6&tail=20",
                api_key=api_key,
                session_id=session_id,
                body={"command": cmd},
                timeout=9,
            )
            raw = _flatten_output(res).splitlines()
            if not raw or res.get("exit_code") not in (0, None):
                return "nvidia-smi unavailable", None
            parts = [p.strip() for p in raw[0].split(",")]
            if len(parts) >= 6:
                name, temp, util, used, total, power = parts[:6]
                free_mib: Optional[int] = None
                try:
                    free_mib = max(0, int(float(total)) - int(float(used)))
                except ValueError:
                    pass
                return (
                    f"{name} | {temp}C | GPU {util}% | VRAM {used}/{total} MiB | {power}W",
                    free_mib,
                )
            return raw[0], None
        except Exception:
            return "GPU query unavailable", None

    async def _list_running_jobs(
        self,
        base_url: str,
        api_key: str,
        sessions: tuple[str, ...],
    ) -> list[dict[str, Any]]:
        running: list[dict[str, Any]] = []
        for sess in sessions:
            try:
                rows = await self._call("GET", base_url, "/execute", api_key=api_key, session_id=sess, timeout=6)
                if isinstance(rows, list):
                    for row in rows:
                        if isinstance(row, dict) and row.get("status") == "running":
                            running.append({**row, "session": sess})
            except Exception:
                continue
        return running

    async def _pc_snapshot(self, target_key: str) -> str:
        _, label, base_url, api_key = self._resolve_target(target_key)
        role = "Main AI + Open WebUI Hub" if target_key == "ai" else "Coding & Media Generation Worker"

        if not base_url:
            return f"### {label} ({role})\n- Status: NOT CONFIGURED"

        online = await self._is_online(base_url)
        if not online:
            if target_key == "gaming":
                return (
                    f"### {label} ({role})\n"
                    "- Status: **GAMING MODE ON / OFFLINE** (port 8002 closed)\n"
                    "- Action rule: Protected for gaming — do not send coding or media jobs until owner runs ALLOW SONNY."
                )
            return f"### {label} ({role})\n- Status: **OFFLINE** (AI-PC Open Terminal not responding on port 8001)"

        if not api_key:
            return f"### {label} ({role})\n- Status: ONLINE, but API key Valve is empty (run configure_webui.py)"

        lines = [f"### {label} ({role})", "- Status: **ONLINE**"]
        try:
            info = await self._call("GET", base_url, "/system/info", api_key=api_key, timeout=8)
            cpu = info.get("cpu") or {}
            mem = info.get("memory") or {}
            disk = info.get("disk") or {}
            cpu_pct = f"{cpu.get('percent')}%" if cpu.get("percent") is not None else "sampled"
            lines.append(
                f"- Host: `{info.get('hostname', 'unknown')}` ({info.get('os', 'Windows')}) | "
                f"CPU: {cpu.get('cores', '?')} threads ({cpu_pct}) | "
                f"RAM: {_fmt_gib(mem.get('used'))} / {_fmt_gib(mem.get('total'))} | "
                f"Disk Free: {_fmt_gib(disk.get('free'))}"
            )
        except TerminalAPIError as exc:
            return self._scrub(f"### {label} ({role})\n- Status: AUTH ERROR ({exc.status}) — check API key Valve")
        except Exception as exc:
            lines.append(self._scrub(f"- System info unavailable: {exc}"))

        sess = AI_OPS_SESSION if target_key == "ai" else GAMING_OPS_SESSION
        gpu_line, _ = await self._gpu_summary(base_url, api_key, sess)
        lines.append(f"- GPU: {gpu_line}")

        sessions = (AI_OPS_SESSION,) if target_key == "ai" else ALL_GAMING_SESSIONS
        running = await self._list_running_jobs(base_url, api_key, sessions)
        if running:
            lines.append(f"- Active Sonny jobs ({len(running)}):")
            for job in running[:8]:
                tag = job["session"].replace("sonny-", "")
                lines.append(f"  - `{job.get('id')}` [{tag}]: `{str(job.get('command', ''))[:90]}`")
        else:
            lines.append("- Active Sonny jobs: 0 (idle)")

        if target_key == "ai" and self.valves.ollama_url.strip():
            try:
                ps = await self._call("GET", self.valves.ollama_url.strip().rstrip("/"), "/api/ps", timeout=4)
                loaded = [m.get("name", "?") for m in (ps.get("models") or []) if isinstance(m, dict)]
                lines.append(f"- Ollama VRAM loaded: {', '.join(loaded) if loaded else 'idle (loads on demand)'}")
            except Exception:
                lines.append("- Ollama status: check skipped")

        return self._scrub("\n".join(lines))

    async def check_homelab_status(
        self,
        __event_emitter__: Optional[Callable[[dict[str, Any]], Awaitable[Any]]] = None,
    ) -> str:
        """
        Read-only health, CPU/RAM/disk, RTX 5090 GPU/VRAM, Ollama, active job, and Gaming Mode check
        across both the AI PC and Gaming PC. Never modifies state or prompts for confirmation.
        """
        await self._emit(__event_emitter__, "Checking AI PC and Gaming PC status...", done=False)
        ai_report, gaming_report = await asyncio.gather(
            self._pc_snapshot("ai"),
            self._pc_snapshot("gaming"),
        )
        await self._emit(__event_emitter__, "Homelab status ready", done=True)
        return f"{ai_report}\n\n{gaming_report}"

    async def list_pc_processes(
        self,
        target: str = "gaming",
        filter_name: str = "",
        __event_emitter__: Optional[Callable[[dict[str, Any]], Awaitable[Any]]] = None,
    ) -> str:
        """
        Read-only list of active Sonny jobs and top/matching host processes on 'ai' or 'gaming'.

        :param target: Which PC to inspect: 'ai' (AI PC) or 'gaming' (Gaming PC).
        :param filter_name: Optional case-insensitive substring filter (e.g. 'comfy', 'ffmpeg', 'node').
        """
        try:
            target_key, label, base_url, api_key = self._resolve_target(target)
        except ValueError as exc:
            return str(exc)

        if not await self._is_online(base_url):
            if target_key == "gaming":
                return "Gaming PC is in GAMING MODE / OFFLINE (port 8002 closed). Zero Sonny processes are running there."
            return "AI PC Open Terminal is offline (port 8001 not responding)."

        await self._emit(__event_emitter__, f"Listing processes on {label}...", done=False)
        sessions = (AI_OPS_SESSION,) if target_key == "ai" else ALL_GAMING_SESSIONS
        jobs = await self._list_running_jobs(base_url, api_key, sessions)
        needle = filter_name.strip().lower()

        out = [f"### {label} Processes"]
        if jobs:
            out.append("**Tracked Sonny Jobs (running):**")
            for j in jobs:
                cmd = str(j.get("command", ""))
                if not needle or needle in cmd.lower():
                    out.append(f"- `{j.get('id')}` [{j['session']}]: `{cmd[:100]}`")
        else:
            out.append("**Tracked Sonny Jobs:** none running")

        try:
            info = await self._call("GET", base_url, "/system/info", api_key=api_key, timeout=8)
            procs = info.get("processes") or []
            if needle:
                procs = [p for p in procs if needle in str(p.get("name", "")).lower()]
            out.append("\n**Top Host Processes:**")
            if procs:
                for p in procs[:15]:
                    mem_mb = (p.get("memory_bytes") or 0) / (1024 * 1024)
                    cpu_s = f"{p.get('cpu_percent')}%" if p.get("cpu_percent") is not None else "-"
                    out.append(f"- PID `{p.get('pid')}` | `{p.get('name')}` | CPU {cpu_s} | RAM {mem_mb:.0f} MiB")
            else:
                out.append(f"- No top processes matched `{filter_name}`." if needle else "- None reported.")
        except Exception as exc:
            out.append(self._scrub(f"- Could not read host process list: {exc}"))

        await self._emit(__event_emitter__, f"Loaded {label} process list", done=True)
        return self._scrub("\n".join(out))

    async def start_gaming_pc_job(
        self,
        job_type: str,
        command: str,
        cwd: str = "",
        wait_seconds: int = 10,
        __event_call__: Optional[Callable[[dict[str, Any]], Awaitable[Any]]] = None,
        __event_emitter__: Optional[Callable[[dict[str, Any]], Awaitable[Any]]] = None,
    ) -> str:
        """
        Start a tracked coding or media-generation job on the Gaming PC (respects Gaming Mode and
        requires owner confirmation). Every job launched here can be stopped instantly with stop_gaming_pc_work.

        :param job_type: 'coding' (repos, builds, tests, scripts) or 'media' (ComfyUI, image/video/3D/FFmpeg generation).
        :param command: Command to run on the Gaming PC.
        :param cwd: Optional working directory (defaults to C:\\SonnyWork\\code or C:\\SonnyWork\\media).
        :param wait_seconds: Seconds (1-120) to collect initial output; longer jobs continue running in the background.
        """
        jt = (job_type or "").strip().lower()
        if jt not in ("coding", "code", "media", "image", "video", "audio", "render", "3d"):
            return "Invalid job_type. Use 'coding' or 'media' — Gaming PC is reserved for coding and media generation."

        blocked = _validate_command("gaming", command)
        if blocked:
            return blocked

        _, _, base_url, api_key = self._resolve_target("gaming")
        if not await self._is_online(base_url):
            return (
                "Gaming PC is currently in **GAMING MODE / OFFLINE** (port 8002 closed). "
                "Job was NOT started. Run ALLOW SONNY on the Gaming PC when you are done gaming."
            )
        if not api_key:
            return "Gaming PC API key Valve is not configured. Run `python scripts\\configure_webui.py` on the AI PC."

        is_media = jt in ("media", "image", "video", "audio", "render", "3d")
        category = "media" if is_media else "coding"
        session_id = GAMING_MEDIA_SESSION if is_media else GAMING_CODING_SESSION
        work_dir = cwd.strip() or (self.valves.gaming_media_dir if is_media else self.valves.gaming_coding_dir)

        gpu_note = ""
        if is_media:
            gpu_line, free_mib = await self._gpu_summary(base_url, api_key, GAMING_OPS_SESSION)
            gpu_note = f"\nRTX 5090: {gpu_line}"
            if free_mib is not None and free_mib < 2048:
                gpu_note += f" (WARNING: only {free_mib} MiB VRAM free)"

        ok, deny_msg = await self._confirm(
            __event_call__,
            f"Run {category.upper()} job on Gaming PC?",
            f"Directory: {work_dir}\nCommand: {command.strip()}{gpu_note}\n\n"
            f"(Can be stopped anytime with STOP SONNY / Ctrl+Alt+G or stop_gaming_pc_work.)",
        )
        if not ok:
            return deny_msg

        wait = max(1, min(120, int(wait_seconds)))
        await self._emit(__event_emitter__, f"Running {category} job on Gaming PC...", done=False)
        try:
            res = await self._call(
                "POST",
                base_url,
                f"/execute?wait={wait}&tail=150",
                api_key=api_key,
                session_id=session_id,
                body={"command": command.strip(), "cwd": work_dir},
                timeout=wait + 15,
            )
        except TerminalOfflineError:
            return "Gaming PC went offline (Gaming Mode activated) before the job completed."
        except TerminalAPIError as exc:
            return self._scrub(f"Gaming PC rejected job ({exc.status}): {exc.message}")

        pid_id = res.get("id", "unknown")
        status = res.get("status", "unknown")
        exit_code = res.get("exit_code")
        output = _flatten_output(res)
        await self._emit(__event_emitter__, f"Gaming PC {category} job {status} ({pid_id})", done=True)

        header = (
            f"**Gaming PC {category} job** `{pid_id}` — status: **{status}**"
            + (f" (exit `{exit_code}`)" if exit_code is not None else " (still running; stop anytime)")
            + f"\nWorking dir: `{res.get('cwd', work_dir)}`"
        )
        return self._scrub(f"{header}\n\n```\n{output}\n```" if output else header)

    async def run_pc_command(
        self,
        target: str,
        command: str,
        cwd: str = "",
        wait_seconds: int = 30,
        __event_call__: Optional[Callable[[dict[str, Any]], Awaitable[Any]]] = None,
        __event_emitter__: Optional[Callable[[dict[str, Any]], Awaitable[Any]]] = None,
    ) -> str:
        """
        Execute a shell command on 'ai' (AI PC) or 'gaming' (Gaming PC). Safe single read-only commands
        run directly; all other commands require explicit owner confirmation.

        :param target: 'ai' (AI PC) or 'gaming' (Gaming PC).
        :param command: Shell command to run.
        :param cwd: Optional working directory.
        :param wait_seconds: Seconds (1-120) to wait for inline output.
        """
        try:
            target_key, label, base_url, api_key = self._resolve_target(target)
        except ValueError as exc:
            return str(exc)

        blocked = _validate_command(target_key, command)
        if blocked:
            return blocked

        if not await self._is_online(base_url):
            if target_key == "gaming":
                return "Gaming PC is in **GAMING MODE / OFFLINE** (port 8002 closed). Command was not run."
            return "AI PC Open Terminal is offline (port 8001 not responding)."
        if not api_key:
            return f"{label} API key Valve is empty. Run `python scripts\\configure_webui.py` on the AI PC."

        if not _is_safe_read_only(command):
            dir_line = f"Directory: {cwd.strip()}\n" if cwd.strip() else ""
            ok, deny_msg = await self._confirm(
                __event_call__,
                f"Approve command on {label}?",
                f"{dir_line}Command: {command.strip()}",
            )
            if not ok:
                return deny_msg

        wait = max(1, min(120, int(wait_seconds)))
        session_id = AI_OPS_SESSION if target_key == "ai" else GAMING_OPS_SESSION
        payload: dict[str, Any] = {"command": command.strip()}
        if cwd.strip():
            payload["cwd"] = cwd.strip()

        await self._emit(__event_emitter__, f"Running command on {label}...", done=False)
        try:
            res = await self._call(
                "POST",
                base_url,
                f"/execute?wait={wait}&tail=150",
                api_key=api_key,
                session_id=session_id,
                body=payload,
                timeout=wait + 15,
            )
        except TerminalOfflineError:
            return f"{label} disconnected while executing the command."
        except TerminalAPIError as exc:
            return self._scrub(f"{label} command error ({exc.status}): {exc.message}")

        status = res.get("status", "unknown")
        exit_code = res.get("exit_code")
        output = _flatten_output(res)
        await self._emit(__event_emitter__, f"{label} command {status}", done=True)

        header = (
            f"**{label}** job `{res.get('id', 'unknown')}` — **{status}**"
            + (f" (exit `{exit_code}`)" if exit_code is not None else "")
        )
        return self._scrub(f"{header}\n\n```\n{output}\n```" if output else header)

    async def get_job_output(
        self,
        target: str,
        process_id: str,
        job_type: str = "ops",
        tail: int = 100,
    ) -> str:
        """
        Read-only check of status and recent output for a tracked job on 'ai' or 'gaming'.

        :param target: 'ai' or 'gaming'.
        :param process_id: Job ID returned by start_gaming_pc_job or run_pc_command.
        :param job_type: 'coding', 'media', or 'ops' (for Gaming PC jobs).
        :param tail: Number of recent output chunks to return (1-300).
        """
        try:
            target_key, label, base_url, api_key = self._resolve_target(target)
        except ValueError as exc:
            return str(exc)

        pid_clean = (process_id or "").strip()
        if not pid_clean or not re.match(r"^[A-Za-z0-9_-]{1,64}$", pid_clean):
            return "Invalid process_id."

        if not await self._is_online(base_url):
            if target_key == "gaming":
                return "Gaming PC is in GAMING MODE / OFFLINE (all Sonny jobs are already stopped)."
            return "AI PC Open Terminal is offline."

        bounded_tail = max(1, min(300, int(tail)))
        session_id = self._session_for_job(target_key, job_type)
        safe_id = urllib.parse.quote(pid_clean, safe="")
        try:
            res = await self._call(
                "GET",
                base_url,
                f"/execute/{safe_id}/status?tail={bounded_tail}",
                api_key=api_key,
                session_id=session_id,
                timeout=10,
            )
        except TerminalAPIError as exc:
            return self._scrub(f"Could not read job `{pid_clean}` on {label} ({exc.status}): {exc.message}")
        except TerminalOfflineError:
            return f"{label} is offline."

        output = _flatten_output(res)
        header = (
            f"**{label}** job `{pid_clean}` — status: **{res.get('status', 'unknown')}**"
            + (f" (exit `{res.get('exit_code')}`)" if res.get("exit_code") is not None else "")
        )
        return self._scrub(f"{header}\n\n```\n{output}\n```" if output else header)

    async def stop_gaming_pc_work(
        self,
        mode: str = "jobs_only",
        process_id: str = "",
        __event_call__: Optional[Callable[[dict[str, Any]], Awaitable[Any]]] = None,
        __event_emitter__: Optional[Callable[[dict[str, Any]], Awaitable[Any]]] = None,
    ) -> str:
        """
        Immediately stop Sonny's coding and media-generation workloads on the Gaming PC so the owner can game.

        :param mode: 'jobs_only' (kill all running Sonny coding/media jobs & optional media containers, keep PC available),
                     'lock_gaming_mode' (kill everything AND run GAME_MODE_ON.cmd to shut down port 8002 and lock Gaming PC until owner runs ALLOW SONNY locally),
                     or 'single' (kill only process_id).
        :param process_id: Required only when mode='single'.
        """
        chosen = (mode or "jobs_only").strip().lower()
        if chosen not in ("jobs_only", "lock_gaming_mode", "single"):
            return "Invalid mode. Use 'jobs_only', 'lock_gaming_mode', or 'single'."

        _, _, base_url, api_key = self._resolve_target("gaming")
        if not await self._is_online(base_url):
            return "Gaming PC is already in **GAMING MODE / OFFLINE** — port 8002 is closed and zero Sonny jobs are running."
        if not api_key:
            return "Gaming PC API key Valve is empty. Use the Desktop `STOP SONNY - GAME MODE` shortcut (Ctrl+Alt+G) on the Gaming PC."

        if chosen == "lock_gaming_mode" and self.valves.confirm_gaming_lock:
            ok, deny_msg = await self._confirm(
                __event_call__,
                "Lock Gaming PC in GAMING MODE?",
                "This kills all Sonny coding & media jobs on the Gaming PC, stops configured media containers, "
                "shuts down port 8002, and sets GAMING_MODE.lock.\n\n"
                "To re-enable later, click ALLOW SONNY directly on the Gaming PC.",
            )
            if not ok:
                return deny_msg
        elif chosen in ("jobs_only", "single") and not self.valves.instant_gaming_stop:
            ok, deny_msg = await self._confirm(
                __event_call__,
                "Stop Sonny jobs on Gaming PC?",
                f"Mode: {chosen}" + (f" (job {process_id})" if process_id else " (all running coding & media jobs)"),
            )
            if not ok:
                return deny_msg

        await self._emit(__event_emitter__, "Stopping Gaming PC workloads...", done=False)

        if chosen == "single":
            pid_clean = (process_id or "").strip()
            if not pid_clean or not re.match(r"^[A-Za-z0-9_-]{1,64}$", pid_clean):
                return "mode='single' requires a valid process_id."
            safe_id = urllib.parse.quote(pid_clean, safe="")
            killed = False
            for sess in ALL_GAMING_SESSIONS:
                try:
                    await self._call(
                        "DELETE",
                        base_url,
                        f"/execute/{safe_id}?force=true",
                        api_key=api_key,
                        session_id=sess,
                        timeout=8,
                    )
                    killed = True
                    break
                except TerminalAPIError as exc:
                    if exc.status == 404:
                        continue
                    return self._scrub(f"Failed to kill `{pid_clean}` ({exc.status}): {exc.message}")
            await self._emit(__event_emitter__, f"Stopped job {pid_clean}", done=True)
            return f"Force-killed Gaming PC job `{pid_clean}`." if killed else f"Job `{pid_clean}` was not running."

        # Kill ALL tracked Sonny coding, media, and ops jobs across all sessions.
        running = await self._list_running_jobs(base_url, api_key, ALL_GAMING_SESSIONS)
        killed_ids: list[str] = []
        for job in running:
            jid = str(job.get("id", ""))
            sess = str(job.get("session", GAMING_OPS_SESSION))
            if not jid:
                continue
            try:
                await self._call(
                    "DELETE",
                    base_url,
                    f"/execute/{urllib.parse.quote(jid, safe='')}?force=true",
                    api_key=api_key,
                    session_id=sess,
                    timeout=8,
                )
                killed_ids.append(jid)
            except Exception:
                continue

        # Stop any configured opt-in media generation containers on the Gaming PC.
        stopped_containers: list[str] = []
        raw_containers = [c.strip() for c in self.valves.gaming_media_containers.split(",") if c.strip()]
        for cname in raw_containers:
            if not CONTAINER_NAME_RE.match(cname) or cname.lower() in ("sonny-webui", "open-webui", "tailscale"):
                continue
            try:
                res = await self._call(
                    "POST",
                    base_url,
                    "/execute?wait=10&tail=10",
                    api_key=api_key,
                    session_id=GAMING_OPS_SESSION,
                    body={"command": f"docker stop -t 3 {cname}"},
                    timeout=14,
                )
                if res.get("exit_code") == 0:
                    stopped_containers.append(cname)
            except Exception:
                continue

        if chosen == "lock_gaming_mode":
            # Trigger local GAME_MODE_ON.cmd — the connection commonly drops mid-request as open-terminal exits!
            try:
                await self._call(
                    "POST",
                    base_url,
                    "/execute?wait=2&tail=20",
                    api_key=api_key,
                    session_id=GAMING_OPS_SESSION,
                    body={"command": r"C:\SonnyAgent\GAME_MODE_ON.cmd --nopause"},
                    timeout=5,
                )
            except (TerminalOfflineError, TerminalAPIError):
                pass  # Expected: GAME_MODE_ON.cmd terminates open-terminal.exe immediately.

            await self._emit(__event_emitter__, "Gaming Mode ON — Gaming PC locked & free", done=True)
            return (
                "**GAMING MODE IS NOW ON (Gaming PC locked & ready to game)**\n"
                f"- Killed tracked Sonny jobs: {len(killed_ids)}"
                + (f" ({', '.join(killed_ids)})" if killed_ids else "")
                + (f"\n- Stopped media containers: {', '.join(stopped_containers)}" if stopped_containers else "")
                + "\n- Triggered `C:\\SonnyAgent\\GAME_MODE_ON.cmd` (agent stopped, port 8002 closed, anti-cheat clean)."
                + "\n- Run **ALLOW SONNY** on the Gaming PC when you finish gaming."
            )

        gpu_line, _ = await self._gpu_summary(base_url, api_key, GAMING_OPS_SESSION)
        await self._emit(__event_emitter__, "Stopped all Gaming PC jobs", done=True)
        return self._scrub(
            "**Stopped all Sonny workloads on Gaming PC**\n"
            f"- Force-killed Sonny jobs: {len(killed_ids)}"
            + (f" ({', '.join(killed_ids)})" if killed_ids else " (none were running)")
            + (f"\n- Stopped media containers: {', '.join(stopped_containers)}" if stopped_containers else "")
            + f"\n- Gaming PC GPU now: {gpu_line}"
            + "\n- Tip: Say *\"lock Gaming Mode\"* or hit `Ctrl+Alt+G` on the Gaming PC to also shut down the agent completely."
        )

    async def kill_host_process(
        self,
        target: str,
        pid: int,
        reason: str = "",
        __event_call__: Optional[Callable[[dict[str, Any]], Awaitable[Any]]] = None,
        __event_emitter__: Optional[Callable[[dict[str, Any]], Awaitable[Any]]] = None,
    ) -> str:
        """
        Terminate a specific host process by numeric PID (tree kill) on 'ai' or 'gaming'.
        Refuses protected Windows, Steam, anti-cheat, Discord, OBS, Corsair iCUE, Ollama, Docker,
        and Tailscale processes, and ALWAYS requires owner confirmation.

        :param target: 'ai' or 'gaming'.
        :param pid: Numeric Windows process ID (> 4).
        :param reason: Brief explanation shown on the confirmation card.
        """
        try:
            target_key, label, base_url, api_key = self._resolve_target(target)
        except ValueError as exc:
            return str(exc)

        try:
            pid_int = int(pid)
        except (TypeError, ValueError):
            return "Invalid PID: must be an integer."
        if pid_int <= 4:
            return f"BLOCKED: Refusing to terminate reserved system PID {pid_int}."

        if not await self._is_online(base_url):
            if target_key == "gaming":
                return "Gaming PC is in GAMING MODE / OFFLINE (port 8002 closed)."
            return "AI PC Open Terminal is offline."
        if not api_key:
            return f"{label} API key Valve is empty."

        sess = AI_OPS_SESSION if target_key == "ai" else GAMING_OPS_SESSION
        try:
            lookup = await self._call(
                "POST",
                base_url,
                "/execute?wait=6&tail=20",
                api_key=api_key,
                session_id=sess,
                body={"command": f'tasklist /FI "PID eq {pid_int}" /FO CSV /NH'},
                timeout=9,
            )
        except Exception as exc:
            return self._scrub(f"Could not verify PID {pid_int} on {label}: {exc}")

        raw_line = _flatten_output(lookup).splitlines()
        first = raw_line[0].strip() if raw_line else ""
        if not first or first.lower().startswith("info:"):
            return f"No running process found with PID `{pid_int}` on {label}."

        exe_name = ""
        try:
            parsed_row = next(csv.reader(io.StringIO(first)))
            exe_name = parsed_row[0].strip() if parsed_row else ""
        except Exception:
            exe_name = ""

        if not exe_name:
            return f"Refused (fail-closed): could not verify executable name for PID `{pid_int}` on {label}."
        if exe_name.lower() in PROTECTED_HOST_EXES:
            return f"BLOCKED: `{exe_name}` (PID `{pid_int}`) is a protected system/gaming/peripheral process."

        # Always require explicit confirmation for arbitrary host PID kills, regardless of Valve defaults.
        if __event_call__ is None:
            return "Cancelled (fail-closed): killing a host PID always requires live owner confirmation."
        reason_line = f"\nReason: {reason.strip()}" if reason.strip() else ""
        try:
            approved = await __event_call__(
                {
                    "type": "confirmation",
                    "data": {
                        "title": f"Kill process on {label}?",
                        "message": f"Executable: {exe_name}\nPID: {pid_int} (including child processes){reason_line}",
                    },
                }
            )
        except Exception as exc:
            return self._scrub(f"Cancelled: confirmation dialog failed ({exc}).")
        if approved is not True:
            return f"Cancelled: left `{exe_name}` (PID `{pid_int}`) running on {label}."

        await self._emit(__event_emitter__, f"Terminating {exe_name} (PID {pid_int}) on {label}...", done=False)
        res = await self._call(
            "POST",
            base_url,
            "/execute?wait=8&tail=20",
            api_key=api_key,
            session_id=sess,
            body={"command": f"taskkill /PID {pid_int} /T /F"},
            timeout=12,
        )
        await self._emit(__event_emitter__, f"Terminated PID {pid_int}", done=True)
        out = _flatten_output(res)
        return self._scrub(
            f"Terminated `{exe_name}` (PID `{pid_int}`) on {label} (exit `{res.get('exit_code')}`).\n{out}".strip()
        )
