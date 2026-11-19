"""Static integrity, Windows compatibility, and security validator for sonny-webui.

Verifies:
  - All expected kit files exist and are non-empty
  - No PowerShell (.ps1) scripts, ExecutionPolicy bypasses, `tailscale funnel`, or 0.0.0.0 binds
  - No hardcoded live secrets and no tracked private config files
  - All .cmd scripts use Windows CRLF line endings, start with `@echo off`, and have balanced quotes
  - All .py files compile cleanly and parse via AST
  - Open WebUI plugins have valid frontmatter, zero offline-breaking pip requirements, and valid classes
  - ai-pc/compose.yaml binds strictly to 127.0.0.1:3000, enforces auth/signup lock, and points only to AI PC Ollama
  - Gaming PC never ships an Open WebUI compose file and binds strictly to Tailscale CGNAT 100.64.0.0/10
"""

from __future__ import annotations

import ast
import ipaddress
import py_compile
import re
import sys
import tomllib
from pathlib import Path

DEFAULT_ROOT = Path(__file__).resolve().parent.parent

REQUIRED_FILES: tuple[str, ...] = (
    ".gitattributes",
    ".gitignore",
    "SETUP.md",
    "ai-pc/compose.yaml",
    "ai-pc/.env.example",
    "ai-pc/ai-pc.toml.example",
    "ai-pc/INSTALL_AI_PC.cmd",
    "ai-pc/START_AI_PC_TERMINAL.cmd",
    "ai-pc/STOP_AI_PC_TERMINAL.cmd",
    "ai-pc/ENABLE_TAILSCALE_HTTPS.cmd",
    "gaming-pc/gaming-pc.toml.example",
    "gaming-pc/media_stop_list.txt",
    "gaming-pc/media_stop_helper.py",
    "gaming-pc/INSTALL_GAMING_PC.cmd",
    "gaming-pc/START_AGENT_GUARDED.cmd",
    "gaming-pc/GAME_MODE_ON.cmd",
    "gaming-pc/GAME_MODE_OFF.cmd",
    "gaming-pc/GAME_MODE_STATUS.cmd",
    "openwebui/sonny_auto_router.py",
    "openwebui/sonny_pc_ops.py",
    "openwebui/sonny_system_prompt.md",
    "scripts/init_config.py",
    "scripts/configure_webui.py",
    "scripts/smoke_test_live.py",
    "scripts/validate_kit.py",
    "START_HERE.txt",
    "tests/__init__.py",
    "tests/__main__.py",
    "tests/test_auto_router.py",
    "tests/test_gaming_mode.py",
    "tests/test_pc_ops.py",
    "tests/test_scripts_and_config.py",
)

FRONTMATTER_KV = re.compile(r"^\s*([a-z_]+):\s*(.*)\s*$", re.IGNORECASE)


def parse_frontmatter(text: str) -> dict[str, str]:
    lines = text.splitlines()
    if not lines or lines[0].strip() != '"""':
        return {}
    out: dict[str, str] = {}
    for line in lines[1:]:
        if '"""' in line:
            break
        match = FRONTMATTER_KV.match(line)
        if match:
            out[match.group(1).strip().lower()] = match.group(2).strip()
    return out


def validate_kit(root: Path) -> list[str]:
    errors: list[str] = []

    # 1. Required files present & non-empty
    for rel in REQUIRED_FILES:
        p = root / rel
        if not p.is_file():
            errors.append(f"Missing required file: {rel}")
        elif p.stat().st_size == 0:
            errors.append(f"Empty file: {rel}")

    # 2. Zero .ps1 scripts anywhere (respect Windows RemoteSigned without signing friction)
    for ps1 in root.rglob("*.ps1"):
        errors.append(f"Unexpected PowerShell script found: {ps1.relative_to(root)}")

    # 3. Gaming PC must never contain a Docker Compose file for Open WebUI
    for bad in ("compose.yaml", "compose.yml", "docker-compose.yaml", "docker-compose.yml"):
        if (root / "gaming-pc" / bad).exists():
            errors.append(f"Gaming PC must not host Open WebUI compose file: gaming-pc/{bad}")

    # 4. .gitignore coverage
    gitignore_path = root / ".gitignore"
    if gitignore_path.is_file():
        gi = gitignore_path.read_text(encoding="utf-8")
        for must_ignore in ("ai-pc/.env", "ai-pc/ai-pc.toml", "gaming-pc/gaming-pc.toml", "*.lock"):
            if must_ignore not in gi:
                errors.append(f".gitignore missing pattern: {must_ignore}")

    # 5. Validate all .py files
    for py_file in sorted(root.rglob("*.py")):
        if "__pycache__" in py_file.parts:
            continue
        rel = str(py_file.relative_to(root))
        try:
            source = py_file.read_text(encoding="utf-8")
            ast.parse(source, filename=rel)
            py_compile.compile(str(py_file), doraise=True)
        except Exception as exc:
            errors.append(f"Python compile/AST failed for {rel}: {exc}")

    # 6. Validate Open WebUI plugins
    for rel_plugin, expected_cls in (
        ("openwebui/sonny_auto_router.py", "Pipe"),
        ("openwebui/sonny_pc_ops.py", "Tools"),
    ):
        p = root / rel_plugin
        if not p.is_file():
            continue
        text = p.read_text(encoding="utf-8")
        fm = parse_frontmatter(text)
        for key in ("title", "version", "description"):
            if not fm.get(key):
                errors.append(f"{rel_plugin}: missing Open WebUI frontmatter key '{key}'")
        if fm.get("requirements"):
            errors.append(f"{rel_plugin}: must not declare pip 'requirements' (breaks offline HF_HUB_OFFLINE installs)")
        tree = ast.parse(text)
        class_names = {n.name for n in tree.body if isinstance(n, ast.ClassDef)}
        if expected_cls not in class_names:
            errors.append(f"{rel_plugin}: missing top-level class '{expected_cls}'")

    # 7. Validate all .cmd batch scripts (CRLF, pure ASCII, @echo off, balanced quotes, block-safe echo, safety rules)
    for cmd_file in sorted(root.rglob("*.cmd")):
        rel = str(cmd_file.relative_to(root))
        raw = cmd_file.read_bytes()
        # Strip CRLF pairs; any remaining bare \n means mixed/LF endings.
        if b"\n" in raw.replace(b"\r\n", b""):
            errors.append(f"{rel}: contains bare LF line endings (Windows .cmd requires CRLF)")
        try:
            text = raw.decode("ascii")
        except UnicodeDecodeError as exc:
            errors.append(f"{rel}: contains non-ASCII bytes that corrupt in CP437 cmd.exe ({exc})")
            text = raw.decode("utf-8", errors="replace")
        lines = text.split("\r\n")
        if not lines or lines[0].strip().lower() != "@echo off":
            errors.append(f"{rel}: must begin with '@echo off'")
        block_depth = 0
        for idx, line in enumerate(lines, start=1):
            stripped = line.strip()
            if not stripped or stripped.upper().startswith("REM "):
                continue
            if stripped.startswith(") else ("):
                continue
            if stripped == ")":
                block_depth = max(0, block_depth - 1)
                continue
            if stripped.count('"') % 2 != 0:
                errors.append(f"{rel}:{idx}: unbalanced double quotes: {stripped}")
            if block_depth > 0 and stripped.lower().startswith("echo "):
                for c_idx, ch in enumerate(stripped):
                    prev = stripped[c_idx - 1] if c_idx > 0 else ""
                    if ch in "()&|<>" and prev != "^":
                        errors.append(f"{rel}:{idx}: unescaped '{ch}' in echo inside parenthesized block")
            low = stripped.lower()
            if "tailscale funnel" in low or ("tailscale" in low and " funnel " in low):
                errors.append(f"{rel}:{idx}: forbidden `tailscale funnel` usage")
            if "executionpolicy" in low:
                errors.append(f"{rel}:{idx}: forbidden PowerShell ExecutionPolicy flag")
            if re.search(r"taskkill\s+.*(/im\s+python(\.exe)?)", low):
                errors.append(f"{rel}:{idx}: forbidden blanket taskkill of python.exe")
            if "0.0.0.0" in low:
                errors.append(f"{rel}:{idx}: forbidden 0.0.0.0 network bind")
            if stripped.endswith("("):
                block_depth += 1
        if block_depth != 0:
            errors.append(f"{rel}: unbalanced parenthesized block depth ({block_depth})")

    # 7b. Verify non-.cmd text files use LF endings and zero generated secret files exist in clean kit
    for secret_rel in ("ai-pc/.env", "ai-pc/ai-pc.toml", "gaming-pc/gaming-pc.toml", "gaming-pc/firewall_rule.txt"):
        if (root / secret_rel).exists():
            errors.append(f"Generated private file present in clean kit tree: {secret_rel}")
    for txt_file in sorted(root.rglob("*")):
        if not txt_file.is_file() or "__pycache__" in txt_file.parts or txt_file.suffix == ".cmd":
            continue
        if txt_file.suffix in {".py", ".md", ".yaml", ".yml", ".toml", ".example", ".txt"} or txt_file.name in {
            ".gitignore",
            ".gitattributes",
        }:
            if b"\r\n" in txt_file.read_bytes():
                errors.append(f"{txt_file.relative_to(root)}: unexpected CRLF in non-.cmd file")

    # 8. Validate example TOMLs & .env.example
    ai_ex = root / "ai-pc" / "ai-pc.toml.example"
    if ai_ex.is_file():
        cfg = tomllib.loads(ai_ex.read_text(encoding="utf-8"))
        if cfg.get("host") != "127.0.0.1":
            errors.append(f"ai-pc.toml.example must bind host='127.0.0.1', got {cfg.get('host')!r}")
        if cfg.get("port") != 8001:
            errors.append(f"ai-pc.toml.example must use port=8001, got {cfg.get('port')!r}")
        if not str(cfg.get("api_key", "")).startswith("REPLACE_"):
            errors.append("ai-pc.toml.example must contain only a REPLACE_ placeholder api_key")

    g_ex = root / "gaming-pc" / "gaming-pc.toml.example"
    if g_ex.is_file():
        cfg = tomllib.loads(g_ex.read_text(encoding="utf-8"))
        host = str(cfg.get("host", ""))
        try:
            if ipaddress.ip_address(host) not in ipaddress.ip_network("100.64.0.0/10"):
                errors.append(f"gaming-pc.toml.example host must be in 100.64.0.0/10, got {host!r}")
        except ValueError:
            errors.append(f"gaming-pc.toml.example invalid host IP: {host!r}")
        if cfg.get("port") != 8002:
            errors.append(f"gaming-pc.toml.example must use port=8002, got {cfg.get('port')!r}")
        if not str(cfg.get("api_key", "")).startswith("REPLACE_"):
            errors.append("gaming-pc.toml.example must contain only a REPLACE_ placeholder api_key")

    env_ex = root / "ai-pc" / ".env.example"
    if env_ex.is_file():
        env_txt = env_ex.read_text(encoding="utf-8")
        if "WEBUI_SECRET_KEY=REPLACE_" not in env_txt:
            errors.append("ai-pc/.env.example must contain WEBUI_SECRET_KEY=REPLACE_ placeholder")

    # 9. Validate ai-pc/compose.yaml security & architecture invariants
    compose = root / "ai-pc" / "compose.yaml"
    if compose.is_file():
        ctext = compose.read_text(encoding="utf-8")
        required_snippets = (
            '"127.0.0.1:3000:8080"',
            'OLLAMA_BASE_URL: "http://host.docker.internal:11434"',
            'WEBUI_AUTH: "true"',
            'ENABLE_SIGNUP: "false"',
            'JWT_EXPIRES_IN: "4w"',
            'HF_HUB_OFFLINE: "1"',
            "ghcr.io/open-webui/open-webui:main",
        )
        for snip in required_snippets:
            if snip not in ctext:
                errors.append(f"ai-pc/compose.yaml missing required setting: {snip}")
        if "0.0.0.0" in ctext:
            errors.append("ai-pc/compose.yaml must not bind 0.0.0.0")
        if "ollama/ollama" in ctext:
            errors.append("ai-pc/compose.yaml must use host's existing Ollama, not start a second Ollama container")

    # 10. Validate Gaming Mode lock guard contracts in batch files
    guarded = root / "gaming-pc" / "START_AGENT_GUARDED.cmd"
    if guarded.is_file():
        gt = guarded.read_text(encoding="utf-8")
        if 'if exist "%LOCK_FILE%"' not in gt:
            errors.append("START_AGENT_GUARDED.cmd missing GAMING_MODE.lock fail-closed check")
    gm_on = root / "gaming-pc" / "GAME_MODE_ON.cmd"
    if gm_on.is_file():
        ot = gm_on.read_text(encoding="utf-8")
        if "taskkill /F /T /IM open-terminal.exe" not in ot:
            errors.append("GAME_MODE_ON.cmd missing scoped tree-kill of open-terminal.exe")
        if "GAMING_MODE=ON" not in ot:
            errors.append("GAME_MODE_ON.cmd missing GAMING_MODE.lock write")

    # 11. Guard the line endings of every file against silent normalization.
    #     A workspace snapshot / editor round-trip can quietly rewrite CRLF to LF, which
    #     breaks cmd.exe label parsing in .cmd files. Catch that here instead of on the PC.
    gitattr = root / ".gitattributes"
    if gitattr.is_file():
        ga = gitattr.read_text(encoding="utf-8")
        if "*.cmd text eol=crlf" not in ga:
            errors.append(".gitattributes must pin '*.cmd text eol=crlf' to protect batch files")
        if "*.py text eol=lf" not in ga:
            errors.append(".gitattributes must pin '*.py text eol=lf'")
    else:
        errors.append("Missing .gitattributes (cmd.exe CRLF protection)")

    for cmd_file in sorted(root.rglob("*.cmd")):
        raw = cmd_file.read_bytes()
        if b"\r\n" not in raw:
            rel = cmd_file.relative_to(root)
            errors.append(f"{rel}: has no CRLF at all; batch file would break under cmd.exe")

    return errors


def main(argv: list[str] | None = None) -> int:
    root = Path(argv[0]).resolve() if argv else DEFAULT_ROOT
    errors = validate_kit(root)
    if errors:
        print(f"[FAIL] {len(errors)} validation issue(s):")
        for err in errors:
            print(f"  - {err}")
        return 1
    print(f"[PASS] Sonny kit validated cleanly ({len(REQUIRED_FILES)} files checked, 0 issues).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
