"""Read-only post-install smoke check for Sonny homelab (runs on AI PC, zero dependencies).

Verifies:
  1. Open WebUI is responding on http://127.0.0.1:3000/health
  2. AI PC Ollama is responding on http://127.0.0.1:11434/api/tags with local models intact
  3. AI PC Open Terminal is responding on http://127.0.0.1:8001/health and accepts its local API key
  4. Gaming PC Open Terminal (<tailscale-ip>:8002) is either ONLINE & authenticated, OR cleanly offline in Gaming Mode

Never modifies state, never toggles Gaming Mode, never downloads models, never prints secrets.
"""

from __future__ import annotations

import argparse
import json
import tomllib
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

DEFAULT_ROOT = Path(__file__).resolve().parent.parent


def http_get(url: str, *, api_key: str = "", timeout: float = 4.0) -> tuple[int, Any]:
    headers = {"Accept": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    req = urllib.request.Request(url, headers=headers, method="GET")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read().decode("utf-8", errors="replace")
        try:
            return resp.status, (json.loads(raw) if raw.strip() else {})
        except json.JSONDecodeError:
            return resp.status, raw


def run_smoke_checks(
    root_dir: Path,
    *,
    webui_url: str = "http://127.0.0.1:3000",
    ollama_url: str = "http://127.0.0.1:11434",
    require_gaming_online: bool = False,
) -> tuple[bool, list[str]]:
    lines: list[str] = []
    ok = True

    # 1. Open WebUI health (AI PC)
    try:
        st, _ = http_get(f"{webui_url.rstrip('/')}/health")
        if st == 200:
            lines.append(f"[PASS] Open WebUI healthy at {webui_url}")
        else:
            lines.append(f"[FAIL] Open WebUI returned HTTP {st} at {webui_url}")
            ok = False
    except Exception as exc:
        lines.append(f"[FAIL] Open WebUI unreachable at {webui_url}: {exc}")
        ok = False

    # 2. Ollama models (AI PC, read-only)
    try:
        st, payload = http_get(f"{ollama_url.rstrip('/')}/api/tags")
        models = [m.get("name", "") for m in (payload.get("models") or []) if isinstance(m, dict)]
        if st == 200 and models:
            lines.append(f"[PASS] AI PC Ollama online ({len(models)} model(s): {', '.join(models)})")
        elif st == 200:
            lines.append("[FAIL] AI PC Ollama is online but reports 0 installed models")
            ok = False
        else:
            lines.append(f"[FAIL] AI PC Ollama returned HTTP {st}")
            ok = False
    except Exception as exc:
        lines.append(f"[FAIL] AI PC Ollama unreachable at {ollama_url}: {exc}")
        ok = False

    # 3. AI PC Open Terminal
    ai_toml = root_dir / "ai-pc" / "ai-pc.toml"
    if ai_toml.is_file():
        cfg = tomllib.loads(ai_toml.read_text(encoding="utf-8"))
        ai_url = f"http://{cfg.get('host', '127.0.0.1')}:{cfg.get('port', 8001)}"
        ai_key = str(cfg.get("api_key", ""))
        try:
            st_h, _ = http_get(f"{ai_url}/health")
            st_i, info = http_get(f"{ai_url}/system/info", api_key=ai_key)
            host_label = info.get("hostname", "unknown") if isinstance(info, dict) else "unknown"
            if st_h == 200 and st_i == 200:
                lines.append(f"[PASS] AI PC Open Terminal online & authenticated at {ai_url} (host: {host_label})")
            else:
                lines.append(f"[FAIL] AI PC Open Terminal unexpected status (health={st_h}, info={st_i})")
                ok = False
        except urllib.error.HTTPError as exc:
            lines.append(f"[FAIL] AI PC Open Terminal auth check failed (HTTP {exc.code})")
            ok = False
        except Exception as exc:
            lines.append(f"[FAIL] AI PC Open Terminal unreachable at {ai_url}: {exc}")
            ok = False
    else:
        lines.append("[FAIL] Missing ai-pc/ai-pc.toml (run init_config.py)")
        ok = False

    # 4. Gaming PC Open Terminal (coding & media worker)
    g_toml = root_dir / "gaming-pc" / "gaming-pc.toml"
    if g_toml.is_file():
        cfg = tomllib.loads(g_toml.read_text(encoding="utf-8"))
        g_url = f"http://{cfg.get('host', '')}:{cfg.get('port', 8002)}"
        g_key = str(cfg.get("api_key", ""))
        try:
            st_h, _ = http_get(f"{g_url}/health", timeout=3.0)
            st_i, info = http_get(f"{g_url}/system/info", api_key=g_key, timeout=5.0)
            host_label = info.get("hostname", "unknown") if isinstance(info, dict) else "unknown"
            if st_h == 200 and st_i == 200:
                lines.append(
                    f"[PASS] Gaming PC worker online & authenticated at {g_url} (host: {host_label}, Gaming Mode OFF)"
                )
            else:
                lines.append(f"[FAIL] Gaming PC unexpected response (health={st_h}, info={st_i})")
                ok = False
        except urllib.error.HTTPError as exc:
            lines.append(f"[FAIL] Gaming PC responded but rejected API key (HTTP {exc.code})")
            ok = False
        except Exception:
            if require_gaming_online:
                lines.append(
                    f"[FAIL] Gaming PC unreachable at {g_url} (--require-gaming-online set; run ALLOW SONNY on Gaming PC)"
                )
                ok = False
            else:
                lines.append(
                    f"[INFO] Gaming PC port closed at {g_url} (GAMING MODE ON — protected for gaming, or not started yet)"
                )
    else:
        lines.append("[FAIL] Missing gaming-pc/gaming-pc.toml (run init_config.py)")
        ok = False

    return ok, lines


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Read-only smoke test for live Sonny AI PC + Gaming PC setup.")
    parser.add_argument("--root", default=str(DEFAULT_ROOT))
    parser.add_argument("--webui-url", default="http://127.0.0.1:3000")
    parser.add_argument("--ollama-url", default="http://127.0.0.1:11434")
    parser.add_argument(
        "--require-gaming-online",
        action="store_true",
        help="Fail if Gaming PC port 8002 is closed (use when testing with Gaming Mode OFF).",
    )
    args = parser.parse_args(argv)

    passed, report = run_smoke_checks(
        Path(args.root).resolve(),
        webui_url=args.webui_url,
        ollama_url=args.ollama_url,
        require_gaming_online=args.require_gaming_online,
    )
    for line in report:
        print(line)
    print("\nResult:", "ALL CHECKS PASSED" if passed else "ONE OR MORE CHECKS FAILED")
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
