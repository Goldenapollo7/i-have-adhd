"""One-command Open WebUI bootstrapper for Sonny (runs on AI PC, zero pip dependencies).

After creating your single owner login at http://127.0.0.1:3000, run:
    python scripts\\configure_webui.py

Idempotently configures:
  1. Function `sonny_auto_router` (active Pipe for automatic AI PC Ollama model selection & vision delegation)
  2. Tool `sonny_pc_ops` + private Valves loaded directly from local ai-pc.toml & gaming-pc.toml
  3. Open Terminal integrations: `AI-PC` (127.0.0.1/host.docker.internal:8001) and `Gaming-PC` (<tailscale-ip>:8002)
  4. Workspace Model `sonny` ("Sonny") with system prompt, tool attached, vision + subagent delegation enabled
  5. Sets `sonny` as default & pinned model so every new chat on PC, Xeneon Edge, or iPhone starts as Sonny.
"""

from __future__ import annotations

import argparse
import getpass
import json
import os
import re
import sys
import tomllib
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

DEFAULT_ROOT = Path(__file__).resolve().parent.parent
LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})
EMBEDDING_RE = re.compile(r"(embed|bge-|minilm|nomic-embed|mxbai-embed)", re.IGNORECASE)


def validate_webui_url(url: str) -> str:
    cleaned = (url or "").strip().rstrip("/")
    parsed = urllib.parse.urlparse(cleaned)
    host = (parsed.hostname or "").lower()
    if parsed.scheme == "https" and host:
        return cleaned
    if parsed.scheme == "http" and host in LOOPBACK_HOSTS:
        return cleaned
    raise ValueError(
        f"Refused insecure Open WebUI URL '{url}'. Use http://127.0.0.1:3000 locally or https://<host>.ts.net."
    )


class WebUIClient:
    def __init__(self, base_url: str, token: str = "", timeout: int = 20) -> None:
        self.base_url = validate_webui_url(base_url)
        self.token = token
        self.timeout = timeout

    def request(self, method: str, path: str, payload: Any = None) -> tuple[int, Any]:
        headers = {"Accept": "application/json"}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        data = None
        if payload is not None:
            data = json.dumps(payload).encode("utf-8")
            headers["Content-Type"] = "application/json"

        req = urllib.request.Request(f"{self.base_url}{path}", data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                raw = resp.read().decode("utf-8", errors="replace")
                return resp.status, (json.loads(raw) if raw.strip() else None)
        except urllib.error.HTTPError as exc:
            raw = ""
            try:
                raw = exc.read().decode("utf-8", errors="replace")
            except Exception:
                pass
            try:
                parsed = json.loads(raw) if raw.strip() else {"detail": str(exc.reason)}
            except json.JSONDecodeError:
                parsed = {"detail": raw[:300] or str(exc.reason)}
            return exc.code, parsed

    def sign_in(self, email: str, password: str) -> None:
        status, data = self.request("POST", "/api/v1/auths/signin", {"email": email.strip(), "password": password})
        if status != 200 or not isinstance(data, dict) or not data.get("token"):
            detail = data.get("detail") if isinstance(data, dict) else "authentication failed"
            raise RuntimeError(f"Open WebUI sign-in failed (HTTP {status}): {detail}")
        token = str(data["token"])
        role = str(data.get("role", ""))
        if role and role != "admin":
            raise RuntimeError(f"Account role is '{role}'; owner/admin account is required to configure Sonny.")
        self.token = token


def load_local_terminal_configs(root_dir: Path) -> tuple[dict[str, Any], dict[str, Any]]:
    ai_path = root_dir / "ai-pc" / "ai-pc.toml"
    gaming_path = root_dir / "gaming-pc" / "gaming-pc.toml"
    if not ai_path.is_file() or not gaming_path.is_file():
        raise FileNotFoundError(
            "Missing ai-pc/ai-pc.toml or gaming-pc/gaming-pc.toml. Run `python scripts\\init_config.py` first."
        )
    ai_cfg = tomllib.loads(ai_path.read_text(encoding="utf-8"))
    gaming_cfg = tomllib.loads(gaming_path.read_text(encoding="utf-8"))
    for name, cfg in (("ai-pc.toml", ai_cfg), ("gaming-pc.toml", gaming_cfg)):
        key = str(cfg.get("api_key", "")).strip()
        if not key or key.startswith("REPLACE_"):
            raise ValueError(f"{name} contains a placeholder api_key. Run init_config.py to generate real secrets.")
    return ai_cfg, gaming_cfg


def discover_ollama_models(client: WebUIClient) -> list[str]:
    status, data = client.request("GET", "/api/models")
    if status != 200 or not isinstance(data, dict):
        return []
    rows = data.get("data") if isinstance(data.get("data"), list) else []
    models: list[str] = []
    for item in rows:
        if not isinstance(item, dict):
            continue
        mid = str(item.get("id", "")).strip()
        if not mid or mid in ("sonny", "sonny_auto_router") or item.get("pipe"):
            continue
        if item.get("owned_by") == "ollama" and not EMBEDDING_RE.search(mid):
            models.append(mid)
    return models


def pick_default_pair(ollama_models: list[str], primary_pref: str, vision_pref: str) -> tuple[str, str]:
    primary = primary_pref
    vision = vision_pref
    if ollama_models:
        if primary not in ollama_models:
            non_vis = [m for m in ollama_models if "vision" not in m.lower() and "llava" not in m.lower()]
            primary = non_vis[0] if non_vis else ollama_models[0]
        if vision not in ollama_models:
            vis = [m for m in ollama_models if any(k in m.lower() for k in ("vision", "-vl", "llava", "minicpm-v", "gemma3"))]
            vision = vis[0] if vis else vision_pref
    return primary, vision


def configure_sonny(
    client: WebUIClient,
    root_dir: Path,
    *,
    primary_model: str = "sonny7-uncensored-8b:latest",
    vision_model: str = "sonny7-vision-4b-64k:latest",
    coding_model: str = "",
    media_containers: str = "",
    ollama_url: str = "http://host.docker.internal:11434",
) -> dict[str, Any]:
    ai_cfg, gaming_cfg = load_local_terminal_configs(root_dir)
    router_src = (root_dir / "openwebui" / "sonny_auto_router.py").read_text(encoding="utf-8")
    tool_src = (root_dir / "openwebui" / "sonny_pc_ops.py").read_text(encoding="utf-8")
    system_prompt = (root_dir / "openwebui" / "sonny_system_prompt.md").read_text(encoding="utf-8").strip()

    ai_port = int(ai_cfg.get("port", 8001))
    ai_container_url = f"http://host.docker.internal:{ai_port}"
    ai_key = str(ai_cfg["api_key"]).strip()

    gaming_host = str(gaming_cfg["host"]).strip()
    gaming_port = int(gaming_cfg.get("port", 8002))
    gaming_url = f"http://{gaming_host}:{gaming_port}"
    gaming_key = str(gaming_cfg["api_key"]).strip()

    ollama_models = discover_ollama_models(client)
    chosen_primary, chosen_vision = pick_default_pair(ollama_models, primary_model, vision_model)

    # 1. Upsert & activate sonny_auto_router Pipe function
    fn_form = {
        "id": "sonny_auto_router",
        "name": "Sonny Auto (AI PC)",
        "content": router_src,
        "meta": {
            "description": "Automatic AI PC Ollama router (chat, coding, and vision delegation).",
        },
    }
    st, existing_fn = client.request("GET", "/api/v1/functions/id/sonny_auto_router")
    if st == 200 and isinstance(existing_fn, dict) and existing_fn.get("id") == "sonny_auto_router":
        st, resp = client.request("POST", "/api/v1/functions/id/sonny_auto_router/update", fn_form)
    else:
        st, resp = client.request("POST", "/api/v1/functions/create", fn_form)
    if st != 200 or not isinstance(resp, dict):
        raise RuntimeError(f"Failed to register sonny_auto_router (HTTP {st}): {resp}")

    if not resp.get("is_active"):
        st_tog, tog = client.request("POST", "/api/v1/functions/id/sonny_auto_router/toggle")
        if st_tog != 200:
            raise RuntimeError(f"Failed to activate sonny_auto_router (HTTP {st_tog}): {tog}")

    router_valves = {
        "ollama_url": ollama_url,
        "preferred_primary_model": chosen_primary,
        "preferred_vision_model": chosen_vision,
        "preferred_coding_model": coding_model,
        "enable_vision_delegation": True,
        "show_route_status": True,
    }
    st_rv, rv_resp = client.request("POST", "/api/v1/functions/id/sonny_auto_router/valves/update", router_valves)
    if st_rv != 200:
        raise RuntimeError(f"Failed to update sonny_auto_router valves (HTTP {st_rv}): {rv_resp}")

    # 2. Upsert sonny_pc_ops Tool + private Valves (keys never logged)
    tool_form = {
        "id": "sonny_pc_ops",
        "name": "Sonny PC Ops",
        "content": tool_src,
        "meta": {
            "description": "Guarded AI PC & Gaming PC (coding/media) control with instant Gaming Mode kill-switch.",
        },
    }
    st_t, existing_tool = client.request("GET", "/api/v1/tools/id/sonny_pc_ops")
    if st_t == 200 and isinstance(existing_tool, dict) and existing_tool.get("id") == "sonny_pc_ops":
        st_t, t_resp = client.request("POST", "/api/v1/tools/id/sonny_pc_ops/update", tool_form)
    else:
        st_t, t_resp = client.request("POST", "/api/v1/tools/create", tool_form)
    if st_t != 200:
        raise RuntimeError(f"Failed to register sonny_pc_ops tool (HTTP {st_t}): {t_resp}")

    tool_valves = {
        "ai_pc_url": ai_container_url,
        "ai_pc_api_key": ai_key,
        "gaming_pc_url": gaming_url,
        "gaming_pc_api_key": gaming_key,
        "ollama_url": ollama_url,
        "gaming_coding_dir": "C:\\SonnyWork\\code",
        "gaming_media_dir": "C:\\SonnyWork\\media",
        "gaming_media_containers": media_containers,
        "require_confirmation": True,
        "instant_gaming_stop": True,
        "confirm_gaming_lock": True,
    }
    st_tv, tv_resp = client.request("POST", "/api/v1/tools/id/sonny_pc_ops/valves/update", tool_valves)
    if st_tv != 200:
        raise RuntimeError(f"Failed to update sonny_pc_ops valves (HTTP {st_tv}): {tv_resp}")

    # 3. Upsert Open Terminal system connections (preserving any unrelated entries)
    st_ts, ts_cur = client.request("GET", "/api/v1/configs/terminal_servers")
    existing_conns: list[dict[str, Any]] = []
    if st_ts == 200 and isinstance(ts_cur, dict):
        raw_list = ts_cur.get("TERMINAL_SERVER_CONNECTIONS")
        if isinstance(raw_list, list):
            existing_conns = [
                c
                for c in raw_list
                if isinstance(c, dict) and c.get("id") not in ("sonny-ai-pc", "sonny-gaming-pc")
            ]

    managed_conns = [
        {
            "id": "sonny-ai-pc",
            "name": "AI-PC",
            "enabled": True,
            "url": ai_container_url,
            "path": "/openapi.json",
            "key": ai_key,
            "auth_type": "bearer",
            "forward_cookies": False,
            "config": {"access_grants": []},
        },
        {
            "id": "sonny-gaming-pc",
            "name": "Gaming-PC (Code & Media)",
            "enabled": True,
            "url": gaming_url,
            "path": "/openapi.json",
            "key": gaming_key,
            "auth_type": "bearer",
            "forward_cookies": False,
            "config": {"access_grants": []},
        },
    ]
    st_tsu, tsu_resp = client.request(
        "POST",
        "/api/v1/configs/terminal_servers",
        {"TERMINAL_SERVER_CONNECTIONS": [*existing_conns, *managed_conns]},
    )
    if st_tsu != 200:
        raise RuntimeError(f"Failed to register Open Terminal connections (HTTP {st_tsu}): {tsu_resp}")

    # 4. Upsert "Sonny" Workspace Model preset
    model_form: dict[str, Any] = {
        "id": "sonny",
        "base_model_id": "sonny_auto_router",
        "name": "Sonny",
        "meta": {
            "description": "Private homelab agent — AI PC brain + Gaming PC coding & media control (Gaming Mode aware).",
            "capabilities": {
                "vision": True,
                "citations": True,
                "status_updates": True,
                "builtin_tools": True,
            },
            "toolIds": ["sonny_pc_ops"],
            **({"subagents": {"model_ids": ollama_models}} if ollama_models else {}),
        },
        "params": {
            "system": system_prompt,
            "function_calling": "native",
        },
    }
    st_m, existing_m = client.request("GET", "/api/v1/models/model?id=sonny")
    if st_m == 200 and isinstance(existing_m, dict) and existing_m.get("id") == "sonny":
        st_m, m_resp = client.request("POST", "/api/v1/models/model/update", model_form)
    else:
        st_m, m_resp = client.request("POST", "/api/v1/models/create", model_form)
    if st_m != 200:
        raise RuntimeError(f"Failed to save Sonny workspace model (HTTP {st_m}): {m_resp}")

    # 5. Pin Sonny as the default model for every new chat (desktop, touchscreen, iPhone)
    st_mc, cur_mc = client.request("GET", "/api/v1/configs/models")
    models_cfg: dict[str, Any] = {
        "DEFAULT_MODELS": "sonny",
        "DEFAULT_PINNED_MODELS": "sonny",
        "MODEL_ORDER_LIST": (cur_mc.get("MODEL_ORDER_LIST") if isinstance(cur_mc, dict) else None) or ["sonny"],
        "DEFAULT_MODEL_METADATA": (cur_mc.get("DEFAULT_MODEL_METADATA") if isinstance(cur_mc, dict) else None),
        "DEFAULT_MODEL_PARAMS": (cur_mc.get("DEFAULT_MODEL_PARAMS") if isinstance(cur_mc, dict) else None),
    }
    st_ms, _ = client.request("POST", "/api/v1/configs/models", models_cfg)
    default_set = st_ms == 200

    return {
        "ollama_models": ollama_models,
        "primary_model": chosen_primary,
        "vision_model": chosen_vision,
        "ai_terminal": ai_container_url,
        "gaming_terminal": gaming_url,
        "default_model_set": default_set,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Automatically register Sonny agent, router, tools, and PCs in Open WebUI.")
    parser.add_argument("--webui-url", default="http://127.0.0.1:3000", help="Open WebUI URL (default: http://127.0.0.1:3000)")
    parser.add_argument("--email", default="", help="Owner admin email (prompts if omitted and SONNY_WEBUI_TOKEN not set)")
    parser.add_argument("--primary-model", default="sonny7-uncensored-8b:latest", help="Preferred AI PC primary Ollama model")
    parser.add_argument("--vision-model", default="sonny7-vision-4b-64k:latest", help="Preferred AI PC vision Ollama model")
    parser.add_argument("--coding-model", default="", help="Optional dedicated AI PC coding Ollama model")
    parser.add_argument(
        "--media-containers",
        default="",
        help="Optional comma-separated Docker container names on Gaming PC to stop on Gaming Mode (e.g. comfyui)",
    )
    parser.add_argument("--root", default=str(DEFAULT_ROOT), help="Path to sonny-webui root directory")
    args = parser.parse_args(argv)

    try:
        token = os.environ.get("SONNY_WEBUI_TOKEN", "").strip()
        client = WebUIClient(args.webui_url, token=token)
        if not token:
            email = args.email.strip() or input("Sonny Open WebUI admin email: ").strip()
            password = getpass.getpass("Sonny Open WebUI admin password (hidden): ")
            client.sign_in(email, password)

        summary = configure_sonny(
            client,
            Path(args.root).resolve(),
            primary_model=args.primary_model,
            vision_model=args.vision_model,
            coding_model=args.coding_model,
            media_containers=args.media_containers,
        )
    except Exception as exc:
        print(f"[ERROR] {exc}", file=sys.stderr)
        return 1

    print("[OK] Sonny configured in Open WebUI:")
    print(f"  - Discovered AI PC Ollama models: {', '.join(summary['ollama_models']) or '(none yet — check Ollama)'}")
    print(f"  - Auto-router primary model:      {summary['primary_model']}")
    print(f"  - Auto-router vision model:       {summary['vision_model']}")
    print(f"  - AI PC terminal registered:      {summary['ai_terminal']}")
    print(f"  - Gaming PC terminal registered:  {summary['gaming_terminal']} (coding & media only)")
    print(f"  - Default & pinned model:         Sonny ({'enabled' if summary['default_model_set'] else 'set manually in Admin Settings'})")
    print("\nNext: copy the gaming-pc\\ folder to your Gaming PC and run INSTALL_GAMING_PC.cmd.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
