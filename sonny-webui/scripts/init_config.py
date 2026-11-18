"""Generate private local secrets and machine configs for Sonny (zero dependencies).

Creates (all gitignored):
  - ai-pc/.env               (WEBUI_SECRET_KEY for Open WebUI on AI PC)
  - ai-pc/ai-pc.toml         (Open Terminal config on AI PC, bound to 127.0.0.1:8001)
  - gaming-pc/gaming-pc.toml (Open Terminal config on Gaming PC, bound ONLY to Gaming PC Tailscale IP:8002)
  - gaming-pc/firewall_rule.txt (AI PC Tailscale IP so Gaming PC firewall restricts port 8002 to AI PC only)

Never prints secret values to the console and refuses to overwrite existing secrets unless --rotate is passed.
"""

from __future__ import annotations

import argparse
import ipaddress
import os
import secrets
import subprocess
import sys
from pathlib import Path

TAILSCALE_CGNAT = ipaddress.ip_network("100.64.0.0/10")
DEFAULT_ROOT = Path(__file__).resolve().parent.parent


def validate_tailscale_ipv4(value: str, label: str) -> str:
    cleaned = (value or "").strip()
    try:
        addr = ipaddress.ip_address(cleaned)
    except ValueError as exc:
        raise ValueError(f"{label} '{cleaned}' is not a valid IP address.") from exc
    if not isinstance(addr, ipaddress.IPv4Address) or addr not in TAILSCALE_CGNAT:
        raise ValueError(
            f"{label} '{cleaned}' must be a private Tailscale IPv4 address in 100.64.0.0/10 "
            f"(run `tailscale ip -4` on that PC). Public, LAN, 0.0.0.0, and loopback IPs are refused."
        )
    return str(addr)


def detect_local_tailscale_ip() -> str | None:
    try:
        proc = subprocess.run(
            ["tailscale", "ip", "-4"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        if proc.returncode != 0:
            return None
        for line in proc.stdout.splitlines():
            candidate = line.strip()
            if candidate:
                return validate_tailscale_ipv4(candidate, "Detected AI PC Tailscale IP")
    except (FileNotFoundError, subprocess.TimeoutExpired, ValueError):
        return None
    return None


def restrict_file_permissions(path: Path) -> None:
    """Restrict generated secret files to the current OS user."""
    if os.name == "nt":
        user = os.environ.get("USERNAME", "")
        domain = os.environ.get("USERDOMAIN", "")
        principal = f"{domain}\\{user}" if domain and user else user
        if principal:
            subprocess.run(
                ["icacls", str(path), "/inheritance:r", "/grant:r", f"{principal}:(R,W)"],
                capture_output=True,
                timeout=10,
                check=False,
            )
    else:
        os.chmod(path, 0o600)


def write_private_file(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if os.name != "nt":
        fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(content)
    else:
        path.write_text(content, encoding="utf-8", newline="\n")
    restrict_file_permissions(path)


def generate_configs(
    root_dir: Path,
    *,
    ai_tailscale_ip: str,
    gaming_tailscale_ip: str,
    ai_port: int = 8001,
    gaming_port: int = 8002,
    rotate: bool = False,
) -> dict[str, Path]:
    ai_ip = validate_tailscale_ipv4(ai_tailscale_ip, "AI PC Tailscale IP")
    gaming_ip = validate_tailscale_ipv4(gaming_tailscale_ip, "Gaming PC Tailscale IP")
    if ai_ip == gaming_ip:
        raise ValueError(
            f"AI PC and Gaming PC Tailscale IPs cannot be identical ({ai_ip}). "
            "Run `tailscale ip -4` on each PC to get its own 100.x.y.z address."
        )
    if not (1024 <= ai_port <= 65535 and 1024 <= gaming_port <= 65535):
        raise ValueError("Ports must be between 1024 and 65535.")

    env_path = root_dir / "ai-pc" / ".env"
    ai_toml_path = root_dir / "ai-pc" / "ai-pc.toml"
    gaming_toml_path = root_dir / "gaming-pc" / "gaming-pc.toml"
    fw_path = root_dir / "gaming-pc" / "firewall_rule.txt"

    existing = [p for p in (env_path, ai_toml_path, gaming_toml_path) if p.exists()]
    if existing and not rotate:
        names = ", ".join(str(p.relative_to(root_dir)) for p in existing)
        raise FileExistsError(
            f"Config files already exist ({names}). Pass --rotate only if you intentionally want new secrets "
            "(rotating WEBUI_SECRET_KEY revokes all active browser/iPhone sessions)."
        )

    # Three independent 288-bit URL-safe secrets (never reused across services).
    webui_secret = secrets.token_urlsafe(36)
    ai_api_key = secrets.token_urlsafe(36)
    gaming_api_key = secrets.token_urlsafe(36)

    env_content = (
        "# Auto-generated private Open WebUI secret (AI PC ONLY). Never commit or share.\n"
        "# Rotate with `python scripts/init_config.py ... --rotate` to revoke all active sessions.\n"
        f"WEBUI_SECRET_KEY={webui_secret}\n"
    )

    ai_toml_content = (
        "# Auto-generated Open Terminal config — AI PC ONLY (C:\\SonnyAgent\\ai-pc.toml)\n"
        "# Loopback-only: never reachable from LAN or internet.\n"
        'host = "127.0.0.1"\n'
        f"port = {ai_port}\n"
        f'api_key = "{ai_api_key}"\n'
        'cors_allowed_origins = "http://127.0.0.1:3000,http://localhost:3000"\n'
        'file_browser_root = "C:\\\\"\n'
        "enable_browser = true\n"
        "enable_desktop = true\n"
        'log_dir = "C:\\\\SonnyAgent\\\\logs\\\\ai-pc"\n'
    )

    gaming_toml_content = (
        "# Auto-generated Open Terminal config — GAMING PC ONLY (C:\\SonnyAgent\\gaming-pc.toml)\n"
        "# Role: Coding (C:\\SonnyWork\\code) & Media Generation (C:\\SonnyWork\\media) ONLY.\n"
        "# Bound strictly to Gaming PC Tailscale IP; killed immediately by GAME_MODE_ON.cmd.\n"
        f'host = "{gaming_ip}"\n'
        f"port = {gaming_port}\n"
        f'api_key = "{gaming_api_key}"\n'
        f'cors_allowed_origins = "http://127.0.0.1:3000,http://{ai_ip}:3000"\n'
        'file_browser_root = "C:\\\\"\n'
        "enable_browser = true\n"
        "enable_desktop = true\n"
        'log_dir = "C:\\\\SonnyAgent\\\\logs\\\\gaming-pc"\n'
    )

    write_private_file(env_path, env_content)
    write_private_file(ai_toml_path, ai_toml_content)
    write_private_file(gaming_toml_path, gaming_toml_content)
    fw_path.parent.mkdir(parents=True, exist_ok=True)
    fw_path.write_text(f"{ai_ip}\n", encoding="utf-8", newline="\n")

    return {
        "env": env_path,
        "ai_toml": ai_toml_path,
        "gaming_toml": gaming_toml_path,
        "firewall_rule": fw_path,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Generate local secrets and private configs for Sonny (AI PC + Gaming PC)."
    )
    parser.add_argument(
        "--ai-tailscale-ip",
        default="",
        help="AI PC Tailscale IPv4 (100.x.y.z). Auto-detected via `tailscale ip -4` if omitted.",
    )
    parser.add_argument(
        "--gaming-tailscale-ip",
        required=True,
        help="Gaming PC Tailscale IPv4 (100.x.y.z) — run `tailscale ip -4` on Gaming PC.",
    )
    parser.add_argument(
        "--root",
        default=str(DEFAULT_ROOT),
        help="Path to sonny-webui kit root.",
    )
    parser.add_argument(
        "--rotate",
        action="store_true",
        help="Regenerate and overwrite existing secrets (revokes current sessions).",
    )
    args = parser.parse_args(argv)

    ai_ip = args.ai_tailscale_ip.strip() or detect_local_tailscale_ip()
    if not ai_ip:
        print(
            "[ERROR] Could not auto-detect AI PC Tailscale IP. Pass --ai-tailscale-ip 100.x.y.z explicitly.",
            file=sys.stderr,
        )
        return 1

    root = Path(args.root).resolve()
    try:
        written = generate_configs(
            root,
            ai_tailscale_ip=ai_ip,
            gaming_tailscale_ip=args.gaming_tailscale_ip,
            rotate=args.rotate,
        )
    except (ValueError, FileExistsError) as exc:
        print(f"[ERROR] {exc}", file=sys.stderr)
        return 1

    print("[OK] Generated private Sonny configs (secret values hidden):")
    print(f"  - {written['env'].relative_to(root)} (Open WebUI session key, AI PC)")
    print(f"  - {written['ai_toml'].relative_to(root)} (AI PC Open Terminal -> 127.0.0.1:8001)")
    print(f"  - {written['gaming_toml'].relative_to(root)} (Gaming PC Open Terminal -> {args.gaming_tailscale_ip.strip()}:8002)")
    print(f"  - {written['firewall_rule'].relative_to(root)} (allows TCP 8002 only from AI PC {ai_ip})")
    print("\nNext: run ai-pc\\INSTALL_AI_PC.cmd on the AI PC.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
