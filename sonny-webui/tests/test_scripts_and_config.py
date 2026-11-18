"""End-to-end tests for init_config.py, configure_webui.py, smoke_test_live.py, and validate_kit.py."""

from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import os
import shutil
import stat
import tempfile
import threading
import tomllib
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

KIT_ROOT = Path(__file__).resolve().parent.parent


def load_script(name: str):
    path = KIT_ROOT / "scripts" / f"{name}.py"
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class _MockOpenWebUIAdminServer:
    """Faithful mock of Open WebUI v0.13.4 admin REST endpoints used by configure_webui.py + smoke_test_live.py."""

    def __init__(self, *, admin_email: str = "owner@sonny.local", admin_pass: str = "s3cr3t-pass!") -> None:
        self.admin_email = admin_email
        self.admin_pass = admin_pass
        self.valid_token = "JWT_ADMIN_TOKEN_DO_NOT_PRINT_12345"
        self.functions: dict[str, dict[str, Any]] = {}
        self.function_valves: dict[str, dict[str, Any]] = {}
        self.tools: dict[str, dict[str, Any]] = {}
        self.tool_valves: dict[str, dict[str, Any]] = {}
        self.terminal_servers: list[dict[str, Any]] = [
            {"id": "user-existing", "name": "Existing", "enabled": True, "url": "http://127.0.0.1:9999"}
        ]
        self.workspace_models: dict[str, dict[str, Any]] = {}
        self.models_config: dict[str, Any] = {
            "DEFAULT_MODELS": None,
            "DEFAULT_PINNED_MODELS": None,
            "MODEL_ORDER_LIST": None,
            "DEFAULT_MODEL_METADATA": None,
            "DEFAULT_MODEL_PARAMS": None,
        }
        self.gaming_online = True
        self.ai_api_key = ""
        self.gaming_api_key = ""

        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
                return

            def _send(self, code: int, body: Any) -> None:
                raw = json.dumps(body).encode("utf-8")
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def _authed(self) -> bool:
                if self.headers.get("Authorization") != f"Bearer {outer.valid_token}":
                    self._send(401, {"detail": "Not authenticated"})
                    return False
                return True

            def _read_json(self) -> dict[str, Any]:
                n = int(self.headers.get("Content-Length", "0"))
                return json.loads(self.rfile.read(n).decode("utf-8")) if n else {}

            def do_GET(self) -> None:  # noqa: N802
                # Public health & mock Ollama / Open Terminal routes for smoke_test_live.py
                if self.path == "/health":
                    self._send(200, {"status": "ok"})
                    return
                if self.path == "/api/tags":
                    self._send(
                        200,
                        {
                            "models": [
                                {"name": "sonny7-uncensored-8b:latest"},
                                {"name": "sonny7-vision-4b-64k:latest"},
                            ]
                        },
                    )
                    return
                if self.path == "/system/info":
                    if self.headers.get("Authorization") == f"Bearer {outer.ai_api_key}":
                        self._send(200, {"hostname": "JOSHAIPC"})
                    else:
                        self._send(401, {"detail": "bad key"})
                    return

                if not self._authed():
                    return

                if self.path == "/api/models":
                    self._send(
                        200,
                        {
                            "data": [
                                {"id": "sonny7-uncensored-8b:latest", "owned_by": "ollama"},
                                {"id": "sonny7-vision-4b-64k:latest", "owned_by": "ollama"},
                                {"id": "nomic-embed-text:latest", "owned_by": "ollama"},
                                {"id": "sonny_auto_router", "owned_by": "openai", "pipe": {"type": "pipe"}},
                            ]
                        },
                    )
                elif self.path == "/api/v1/functions/id/sonny_auto_router":
                    fn = outer.functions.get("sonny_auto_router")
                    self._send(200, fn) if fn else self._send(404, {"detail": "Not found"})
                elif self.path == "/api/v1/tools/id/sonny_pc_ops":
                    t = outer.tools.get("sonny_pc_ops")
                    self._send(200, t) if t else self._send(404, {"detail": "Not found"})
                elif self.path == "/api/v1/configs/terminal_servers":
                    self._send(200, {"TERMINAL_SERVER_CONNECTIONS": outer.terminal_servers})
                elif self.path == "/api/v1/models/model?id=sonny":
                    m = outer.workspace_models.get("sonny")
                    self._send(200, m) if m else self._send(404, {"detail": "Not found"})
                elif self.path == "/api/v1/configs/models":
                    self._send(200, outer.models_config)
                else:
                    self._send(404, {"detail": f"Unhandled GET {self.path}"})

            def do_POST(self) -> None:  # noqa: N802
                payload = self._read_json()
                if self.path == "/api/v1/auths/signin":
                    if payload.get("email") == outer.admin_email and payload.get("password") == outer.admin_pass:
                        self._send(200, {"token": outer.valid_token, "role": "admin", "email": outer.admin_email})
                    else:
                        self._send(400, {"detail": "Invalid credentials"})
                    return

                if not self._authed():
                    return

                if self.path in ("/api/v1/functions/create", "/api/v1/functions/id/sonny_auto_router/update"):
                    prev_active = outer.functions.get("sonny_auto_router", {}).get("is_active", False)
                    rec = {**payload, "is_active": prev_active, "is_global": False}
                    outer.functions["sonny_auto_router"] = rec
                    self._send(200, rec)
                elif self.path == "/api/v1/functions/id/sonny_auto_router/toggle":
                    fn = outer.functions["sonny_auto_router"]
                    fn["is_active"] = not fn["is_active"]
                    self._send(200, fn)
                elif self.path == "/api/v1/functions/id/sonny_auto_router/valves/update":
                    outer.function_valves["sonny_auto_router"] = payload
                    self._send(200, payload)
                elif self.path in ("/api/v1/tools/create", "/api/v1/tools/id/sonny_pc_ops/update"):
                    outer.tools["sonny_pc_ops"] = payload
                    self._send(200, payload)
                elif self.path == "/api/v1/tools/id/sonny_pc_ops/valves/update":
                    outer.tool_valves["sonny_pc_ops"] = payload
                    self._send(200, payload)
                elif self.path == "/api/v1/configs/terminal_servers":
                    conns = payload["TERMINAL_SERVER_CONNECTIONS"]
                    assert isinstance(conns, list)
                    for c in conns:
                        assert "url" in c
                    outer.terminal_servers = conns
                    self._send(200, {"TERMINAL_SERVER_CONNECTIONS": conns})
                elif self.path in ("/api/v1/models/create", "/api/v1/models/model/update"):
                    assert payload["id"] == "sonny"
                    assert payload["base_model_id"] == "sonny_auto_router"
                    outer.workspace_models["sonny"] = payload
                    self._send(200, payload)
                elif self.path == "/api/v1/configs/models":
                    for req_key in ("DEFAULT_MODELS", "DEFAULT_PINNED_MODELS", "MODEL_ORDER_LIST"):
                        assert req_key in payload
                    outer.models_config = payload
                    self._send(200, payload)
                else:
                    self._send(404, {"detail": f"Unhandled POST {self.path}"})

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.httpd.server_address[1]
        self.url = f"http://127.0.0.1:{self.port}"
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)

    def __enter__(self) -> "_MockOpenWebUIAdminServer":
        self.thread.start()
        return self

    def __exit__(self, *_: Any) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(timeout=3)


class TestScriptsAndConfig(unittest.TestCase):
    def setUp(self) -> None:
        self.init_mod = load_script("init_config")
        self.cfg_mod = load_script("configure_webui")
        self.smoke_mod = load_script("smoke_test_live")
        self.val_mod = load_script("validate_kit")

    def _copy_kit(self, tmp_dir: str) -> Path:
        dst = Path(tmp_dir) / "sonny-webui"
        shutil.copytree(
            KIT_ROOT,
            dst,
            ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
        )
        return dst

    def test_init_config_generates_unique_private_secrets_and_guards_inputs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = self._copy_kit(tmp)

            # Reject public, LAN, loopback, 0.0.0.0, outside-CGNAT, and identical IPs
            for bad_ai, bad_g in (
                ("0.0.0.0", "100.80.1.2"),
                ("100.80.1.1", "0.0.0.0"),
                ("127.0.0.1", "100.80.1.2"),
                ("100.80.1.1", "192.168.1.20"),
                ("100.80.1.1", "8.8.8.8"),
                ("100.63.255.255", "100.80.1.2"),
                ("100.80.1.1", "100.128.0.1"),
                ("100.80.1.5", "100.80.1.5"),
            ):
                with self.assertRaises(ValueError):
                    self.init_mod.generate_configs(root, ai_tailscale_ip=bad_ai, gaming_tailscale_ip=bad_g)

            # Run CLI main() and confirm zero secret leakage to stdout/stderr
            buf_out, buf_err = io.StringIO(), io.StringIO()
            with contextlib.redirect_stdout(buf_out), contextlib.redirect_stderr(buf_err):
                rc = self.init_mod.main(
                    [
                        "--root",
                        str(root),
                        "--ai-tailscale-ip",
                        "100.90.10.1",
                        "--gaming-tailscale-ip",
                        "100.90.10.2",
                    ]
                )
            self.assertEqual(rc, 0)

            env_txt = (root / "ai-pc" / ".env").read_text(encoding="utf-8")
            webui_key = env_txt.split("WEBUI_SECRET_KEY=", 1)[1].splitlines()[0].strip()
            ai_cfg = tomllib.loads((root / "ai-pc" / "ai-pc.toml").read_text(encoding="utf-8"))
            g_cfg = tomllib.loads((root / "gaming-pc" / "gaming-pc.toml").read_text(encoding="utf-8"))
            fw_ip = (root / "gaming-pc" / "firewall_rule.txt").read_text(encoding="utf-8").strip()

            # All 3 keys distinct and >= 48 chars
            keys = {webui_key, ai_cfg["api_key"], g_cfg["api_key"]}
            self.assertEqual(len(keys), 3)
            for k in keys:
                self.assertGreaterEqual(len(k), 48)
                self.assertNotIn(k, buf_out.getvalue())
                self.assertNotIn(k, buf_err.getvalue())

            self.assertEqual(ai_cfg["host"], "127.0.0.1")
            self.assertEqual(ai_cfg["port"], 8001)
            self.assertEqual(g_cfg["host"], "100.90.10.2")
            self.assertEqual(g_cfg["port"], 8002)
            self.assertEqual(fw_ip, "100.90.10.1")

            if os.name != "nt":
                for p in (root / "ai-pc" / ".env", root / "ai-pc" / "ai-pc.toml", root / "gaming-pc" / "gaming-pc.toml"):
                    self.assertEqual(stat.S_IMODE(p.stat().st_mode), 0o600)

            # Refuses overwrite without --rotate
            with self.assertRaises(FileExistsError):
                self.init_mod.generate_configs(root, ai_tailscale_ip="100.90.10.1", gaming_tailscale_ip="100.90.10.2")

            # Rotates cleanly with --rotate
            self.init_mod.generate_configs(
                root, ai_tailscale_ip="100.90.10.1", gaming_tailscale_ip="100.90.10.2", rotate=True
            )
            ai_cfg_new = tomllib.loads((root / "ai-pc" / "ai-pc.toml").read_text(encoding="utf-8"))
            self.assertNotEqual(ai_cfg_new["api_key"], ai_cfg["api_key"])

    def test_configure_webui_idempotent_bootstrap_and_smoke_test(self) -> None:
        # Plain HTTP to non-loopback must be rejected so passwords/tokens never cross cleartext
        with self.assertRaises(ValueError):
            self.cfg_mod.validate_webui_url("http://100.90.10.1:3000")
        self.assertEqual(
            self.cfg_mod.validate_webui_url("https://joshaipc.tail1234.ts.net/"),
            "https://joshaipc.tail1234.ts.net",
        )

        with tempfile.TemporaryDirectory() as tmp, _MockOpenWebUIAdminServer() as srv:
            root = self._copy_kit(tmp)
            self.init_mod.generate_configs(root, ai_tailscale_ip="100.90.10.1", gaming_tailscale_ip="100.90.10.2")
            ai_key = tomllib.loads((root / "ai-pc" / "ai-pc.toml").read_text(encoding="utf-8"))["api_key"]
            g_key = tomllib.loads((root / "gaming-pc" / "gaming-pc.toml").read_text(encoding="utf-8"))["api_key"]

            client = self.cfg_mod.WebUIClient(srv.url)
            client.sign_in("owner@sonny.local", "s3cr3t-pass!")

            # Run 1 (creates all objects) + Run 2 (idempotently updates all objects)
            summary1 = self.cfg_mod.configure_sonny(client, root, media_containers="comfyui")
            summary2 = self.cfg_mod.configure_sonny(client, root, media_containers="comfyui")
            self.assertEqual(summary1["primary_model"], "sonny7-uncensored-8b:latest")
            self.assertEqual(summary2["vision_model"], "sonny7-vision-4b-64k:latest")
            self.assertTrue(summary2["default_model_set"])

            # Verify server state
            self.assertTrue(srv.functions["sonny_auto_router"]["is_active"])
            self.assertEqual(srv.tool_valves["sonny_pc_ops"]["ai_pc_api_key"], ai_key)
            self.assertEqual(srv.tool_valves["sonny_pc_ops"]["gaming_pc_api_key"], g_key)
            self.assertEqual(srv.tool_valves["sonny_pc_ops"]["gaming_pc_url"], "http://100.90.10.2:8002")
            self.assertEqual(srv.tool_valves["sonny_pc_ops"]["gaming_media_containers"], "comfyui")

            # Existing user terminal connection preserved + both Sonny PCs registered
            conn_ids = [c["id"] for c in srv.terminal_servers]
            self.assertEqual(conn_ids, ["user-existing", "sonny-ai-pc", "sonny-gaming-pc"])

            # Sonny workspace model configured with tools, vision, native tool calling, and filtered subagents
            sonny = srv.workspace_models["sonny"]
            self.assertEqual(sonny["meta"]["toolIds"], ["sonny_pc_ops"])
            self.assertEqual(
                sonny["meta"]["subagents"]["model_ids"],
                ["sonny7-uncensored-8b:latest", "sonny7-vision-4b-64k:latest"],
            )
            self.assertEqual(sonny["params"]["function_calling"], "native")
            self.assertIn("Gaming Mode", sonny["params"]["system"])
            self.assertEqual(srv.models_config["DEFAULT_MODELS"], "sonny")
            self.assertEqual(srv.models_config["DEFAULT_PINNED_MODELS"], "sonny")

            # Test smoke_test_live.py with AI PC online & Gaming PC offline (Gaming Mode ON)
            srv.ai_api_key = ai_key
            ai_toml_p = root / "ai-pc" / "ai-pc.toml"
            ai_toml_p.write_text(
                ai_toml_p.read_text(encoding="utf-8").replace("port = 8001", f"port = {srv.port}"),
                encoding="utf-8",
            )
            # Point Gaming PC at an unused closed loopback port so test completes instantaneously
            g_toml_p = root / "gaming-pc" / "gaming-pc.toml"
            g_toml_p.write_text(
                g_toml_p.read_text(encoding="utf-8")
                .replace('host = "100.90.10.2"', 'host = "127.0.0.1"')
                .replace("port = 8002", "port = 9"),
                encoding="utf-8",
            )

            passed, lines = self.smoke_mod.run_smoke_checks(root, webui_url=srv.url, ollama_url=srv.url)
            self.assertTrue(passed, lines)
            joined = "\n".join(lines)
            self.assertIn("GAMING MODE ON", joined)
            self.assertNotIn(ai_key, joined)
            self.assertNotIn(g_key, joined)

            passed_req, _ = self.smoke_mod.run_smoke_checks(
                root, webui_url=srv.url, ollama_url=srv.url, require_gaming_online=True
            )
            self.assertFalse(passed_req)

    def test_validate_kit_passes_clean_tree_and_catches_security_regressions(self) -> None:
        self.assertEqual(self.val_mod.validate_kit(KIT_ROOT), [])

        with tempfile.TemporaryDirectory() as tmp:
            root = self._copy_kit(tmp)
            # Inject prohibited artifacts and confirm validator catches every one of them
            (root / "ai-pc" / "bad.ps1").write_text("Write-Host hi\n", encoding="utf-8")
            (root / "ai-pc" / ".env").write_text("WEBUI_SECRET_KEY=leaked\n", encoding="utf-8")
            (root / "gaming-pc" / "compose.yaml").write_text("services: {}\n", encoding="utf-8")
            errs = "\n".join(self.val_mod.validate_kit(root))
            self.assertIn("bad.ps1", errs)
            self.assertIn("ai-pc/.env", errs)
            self.assertIn("gaming-pc/compose.yaml", errs)


if __name__ == "__main__":
    unittest.main()
