"""Unit and live-mock integration tests for openwebui/sonny_auto_router.py."""

from __future__ import annotations

import asyncio
import importlib.util
import json
import sys
import threading
import types
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

KIT_ROOT = Path(__file__).resolve().parent.parent
ROUTER_PATH = KIT_ROOT / "openwebui" / "sonny_auto_router.py"


def load_router_module():
    spec = importlib.util.spec_from_file_location("sonny_auto_router", ROUTER_PATH)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class _MockOllamaServer:
    """Thread-backed mock AI PC Ollama server (/api/tags + /v1/chat/completions)."""

    def __init__(self, models: list[dict[str, Any]], *, fail_vision: bool = False) -> None:
        self.models = models
        self.fail_vision = fail_vision
        self.calls: list[dict[str, Any]] = []

        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
                return

            def do_GET(self) -> None:  # noqa: N802
                if self.path == "/api/tags":
                    body = json.dumps({"models": outer.models}).encode("utf-8")
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                    return
                self.send_response(404)
                self.end_headers()

            def do_POST(self) -> None:  # noqa: N802
                length = int(self.headers.get("Content-Length", "0"))
                payload = json.loads(self.rfile.read(length).decode("utf-8"))
                outer.calls.append(payload)
                model = payload.get("model", "")

                if self.path == "/v1/chat/completions":
                    if outer.fail_vision and "vision" in model and not payload.get("stream"):
                        self.send_response(500)
                        self.end_headers()
                        return
                    if payload.get("stream"):
                        chunks = [
                            f'data: {json.dumps({"choices": [{"delta": {"content": f"streamed-from-{model}"}}]})}\n\n',
                            "data: [DONE]\n\n",
                        ]
                        raw = "".join(chunks).encode("utf-8")
                        self.send_response(200)
                        self.send_header("Content-Type", "text/event-stream")
                        self.end_headers()
                        self.wfile.write(raw)
                        return

                    reply_text = (
                        "Traceback in app.py line 42: ZeroDivisionError"
                        if "vision" in model
                        else f"handled-by-{model}"
                    )
                    body = json.dumps(
                        {"choices": [{"message": {"role": "assistant", "content": reply_text}}]}
                    ).encode("utf-8")
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                    return

                self.send_response(404)
                self.end_headers()

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.httpd.server_address[1]
        self.url = f"http://127.0.0.1:{self.port}"
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)

    def __enter__(self) -> "_MockOllamaServer":
        self.thread.start()
        return self

    def __exit__(self, *_: Any) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(timeout=3)


class TestSonnyAutoRouter(unittest.TestCase):
    def setUp(self) -> None:
        self.mod = load_router_module()
        self.pipe = self.mod.Pipe()
        self.default_catalog = [
            {"id": "sonny7-uncensored-8b:latest", "vision": False},
            {"id": "sonny7-vision-4b-64k:latest", "vision": True},
        ]

    def test_routes_general_and_tool_turns_to_primary(self) -> None:
        body = {
            "model": "sonny",
            "messages": [{"role": "user", "content": "Check homelab status on both PCs"}],
            "tools": [{"type": "function", "function": {"name": "check_homelab_status"}}],
        }
        route = self.pipe.select_route(body, self.default_catalog)
        self.assertEqual(route["model"], "sonny7-uncensored-8b:latest")
        self.assertIsNone(route["delegate_vision"])
        self.assertEqual(route["reason"], "primary")

    def test_routes_pure_image_question_directly_to_vision_model(self) -> None:
        body = {
            "model": "sonny",
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "What cable connector is shown in this photo?"},
                        {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}},
                    ],
                }
            ],
        }
        route = self.pipe.select_route(body, self.default_catalog)
        self.assertEqual(route["model"], "sonny7-vision-4b-64k:latest")
        self.assertIsNone(route["delegate_vision"])
        self.assertEqual(route["reason"], "vision")

    def test_routes_file_attachment_image_to_vision(self) -> None:
        body = {"messages": [{"role": "user", "content": "Describe this screenshot"}]}
        files = [{"type": "image", "name": "screen.png"}]
        route = self.pipe.select_route(body, self.default_catalog, files=files)
        self.assertEqual(route["model"], "sonny7-vision-4b-64k:latest")
        self.assertIsNone(route["delegate_vision"])

    def test_delegates_vision_to_executor_when_image_plus_action(self) -> None:
        body = {
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "Fix this bug and run pytest on Gaming PC"},
                        {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}},
                    ],
                }
            ],
            "tools": [{"type": "function", "function": {"name": "start_gaming_pc_job"}}],
        }
        route = self.pipe.select_route(body, self.default_catalog)
        self.assertEqual(route["model"], "sonny7-uncensored-8b:latest")
        self.assertEqual(route["delegate_vision"], "sonny7-vision-4b-64k:latest")
        self.assertEqual(route["reason"], "vision+action delegation")

    def test_uses_dedicated_coding_model_when_available_and_falls_back_cleanly(self) -> None:
        body = {"messages": [{"role": "user", "content": "Refactor this function and write a pytest unit test"}]}
        # Without dedicated coder -> falls back to primary automatically
        r1 = self.pipe.select_route(body, self.default_catalog)
        self.assertEqual(r1["model"], "sonny7-uncensored-8b:latest")

        # With dedicated coder installed on AI PC -> automatically routes to coder
        extended = [*self.default_catalog, {"id": "qwen2.5-coder:32b", "vision": False}]
        r2 = self.pipe.select_route(body, extended)
        self.assertEqual(r2["model"], "qwen2.5-coder:32b")
        self.assertEqual(r2["reason"], "coding")

    def test_background_task_uses_primary_without_status_events(self) -> None:
        ollama_models = [{"name": "sonny7-uncensored-8b:latest"}, {"name": "sonny7-vision-4b-64k:latest"}]
        events: list[dict[str, Any]] = []

        async def emitter(ev: dict[str, Any]) -> None:
            events.append(ev)

        with _MockOllamaServer(ollama_models) as srv:
            self.pipe.valves.ollama_url = srv.url
            res = asyncio.run(
                self.pipe.pipe(
                    {"messages": [{"role": "user", "content": "Generate a title"}], "stream": False},
                    __event_emitter__=emitter,
                    __task__="title_generation",
                )
            )
            self.assertEqual(res["choices"][0]["message"]["content"], "handled-by-sonny7-uncensored-8b:latest")
            self.assertEqual(events, [])

    def test_discovery_filters_embeddings_self_and_non_ollama(self) -> None:
        fake_req = types.SimpleNamespace(
            app=types.SimpleNamespace(
                state=types.SimpleNamespace(
                    MODELS={
                        "sonny": {"id": "sonny", "owned_by": "ollama"},
                        "sonny_auto_router": {"id": "sonny_auto_router", "pipe": {"type": "pipe"}, "owned_by": "openai"},
                        "nomic-embed-text:latest": {"id": "nomic-embed-text:latest", "owned_by": "ollama"},
                        "external-gpt": {"id": "external-gpt", "owned_by": "openai"},
                        "sonny7-uncensored-8b:latest": {"id": "sonny7-uncensored-8b:latest", "owned_by": "ollama"},
                        "custom-model:latest": {
                            "id": "custom-model:latest",
                            "owned_by": "ollama",
                            "info": {"meta": {"capabilities": {"vision": True}}},
                        },
                    }
                )
            )
        )
        found = asyncio.run(self.pipe.discover_models(fake_req))
        self.assertEqual(
            found,
            [
                {"id": "sonny7-uncensored-8b:latest", "vision": False},
                {"id": "custom-model:latest", "vision": True},
            ],
        )

    def test_rejects_gaming_pc_or_public_ollama_hosts_and_recursion(self) -> None:
        for bad_url in (
            "http://100.88.10.20:11434",  # Gaming PC Tailscale IP
            "http://192.168.1.50:11434",  # LAN host
            "https://api.openai.com",  # Public internet
            "ftp://127.0.0.1:11434",  # Bad scheme
        ):
            self.pipe.valves.ollama_url = bad_url
            with self.assertRaises(ValueError):
                asyncio.run(self.pipe.discover_models(None))

        with self.assertRaises(RuntimeError):
            asyncio.run(self.pipe._dispatch_completion({"model": "sonny", "messages": []}))
        with self.assertRaises(RuntimeError):
            asyncio.run(self.pipe._dispatch_completion({"model": "sonny_auto_router", "messages": []}))

    def test_end_to_end_vision_delegation_and_streaming_via_mock_ollama(self) -> None:
        models = [
            {"name": "sonny7-uncensored-8b:latest", "details": {"families": ["llama"]}},
            {"name": "sonny7-vision-4b-64k:latest", "details": {"families": ["qwen2vl", "clip"]}},
            {"name": "mxbai-embed-large:latest", "details": {"families": ["bert"]}},
        ]
        events: list[dict[str, Any]] = []

        async def emitter(ev: dict[str, Any]) -> None:
            events.append(ev)

        with _MockOllamaServer(models) as srv:
            self.pipe.valves.ollama_url = srv.url
            body = {
                "model": "sonny",
                "stream": True,
                "messages": [
                    {
                        "role": "user",
                        "content": [
                            {"type": "text", "text": "Fix this error on Gaming PC"},
                            {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}},
                        ],
                    }
                ],
                "tools": [{"type": "function", "function": {"name": "start_gaming_pc_job"}}],
            }

            async def consume() -> list[str]:
                gen = await self.pipe.pipe(body, __event_emitter__=emitter)
                return [chunk async for chunk in gen]

            chunks = asyncio.run(consume())
            # Call 1: vision subagent (non-streaming), Call 2: primary executor (streaming, image stripped, tools intact)
            self.assertEqual(len(srv.calls), 2)
            self.assertEqual(srv.calls[0]["model"], "sonny7-vision-4b-64k:latest")
            self.assertFalse(srv.calls[0]["stream"])
            self.assertEqual(srv.calls[1]["model"], "sonny7-uncensored-8b:latest")
            self.assertTrue(srv.calls[1]["stream"])
            self.assertIn("tools", srv.calls[1])
            injected_msg = srv.calls[1]["messages"][-1]["content"]
            self.assertIsInstance(injected_msg, str)
            self.assertIn("ZeroDivisionError", injected_msg)
            self.assertTrue(any("streamed-from-sonny7-uncensored-8b:latest" in c for c in chunks))
            self.assertGreaterEqual(len(events), 2)

    def test_vision_subagent_failure_falls_back_directly_to_vision_model(self) -> None:
        models = [
            {"name": "sonny7-uncensored-8b:latest"},
            {"name": "sonny7-vision-4b-64k:latest"},
        ]
        with _MockOllamaServer(models, fail_vision=True) as srv:
            self.pipe.valves.ollama_url = srv.url
            body = {
                "stream": True,
                "messages": [
                    {
                        "role": "user",
                        "content": [
                            {"type": "text", "text": "Run fix for this screenshot"},
                            {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}},
                        ],
                    }
                ],
            }

            async def consume() -> list[str]:
                gen = await self.pipe.pipe(body)
                return [c async for c in gen]

            chunks = asyncio.run(consume())
            self.assertEqual(srv.calls[-1]["model"], "sonny7-vision-4b-64k:latest")
            self.assertTrue(any("streamed-from-sonny7-vision-4b-64k:latest" in c for c in chunks))

    def test_internal_open_webui_dispatch_path_strips_valves_and_selected_model(self) -> None:
        captured: dict[str, Any] = {}

        class FakeUserModel:
            def __init__(self, **kwargs: Any) -> None:
                if "valves" in kwargs:
                    raise TypeError("Unexpected valves field on UserModel")
                self.data = kwargs

        async def fake_generate_chat_completion(
            request: Any,
            form_data: dict[str, Any],
            user_obj: Any,
            bypass_filter: bool = False,
            bypass_system_prompt: bool = False,
        ) -> dict[str, Any]:
            captured["form_data"] = form_data
            captured["user"] = user_obj.data
            captured["bypass_filter"] = bypass_filter
            captured["bypass_system_prompt"] = bypass_system_prompt
            return {"choices": [{"message": {"role": "assistant", "content": "ok-internal"}}]}

        ow_pkg = types.ModuleType("open_webui")
        ow_models = types.ModuleType("open_webui.models")
        ow_users = types.ModuleType("open_webui.models.users")
        ow_users.UserModel = FakeUserModel  # type: ignore[attr-defined]
        ow_utils = types.ModuleType("open_webui.utils")
        ow_chat = types.ModuleType("open_webui.utils.chat")
        ow_chat.generate_chat_completion = fake_generate_chat_completion  # type: ignore[attr-defined]

        saved = {
            k: sys.modules.get(k)
            for k in (
                "open_webui",
                "open_webui.models",
                "open_webui.models.users",
                "open_webui.utils",
                "open_webui.utils.chat",
            )
        }
        try:
            sys.modules["open_webui"] = ow_pkg
            sys.modules["open_webui.models"] = ow_models
            sys.modules["open_webui.models.users"] = ow_users
            sys.modules["open_webui.utils"] = ow_utils
            sys.modules["open_webui.utils.chat"] = ow_chat

            fake_req = types.SimpleNamespace(
                app=types.SimpleNamespace(
                    state=types.SimpleNamespace(
                        MODELS={"sonny7-uncensored-8b:latest": {"id": "sonny7-uncensored-8b:latest", "owned_by": "ollama"}}
                    )
                )
            )
            res = asyncio.run(
                self.pipe.pipe(
                    {"model": "sonny", "messages": [{"role": "user", "content": "hello"}]},
                    __user__={"id": "u1", "email": "owner@local", "role": "admin", "valves": {"x": 1}},
                    __request__=fake_req,
                    __metadata__={"chat_id": "c1", "selected_model_id": "sonny"},
                )
            )
            self.assertEqual(res["choices"][0]["message"]["content"], "ok-internal")
            self.assertEqual(captured["form_data"]["model"], "sonny7-uncensored-8b:latest")
            self.assertEqual(captured["form_data"]["metadata"], {"chat_id": "c1"})
            self.assertTrue(captured["bypass_filter"])
            self.assertTrue(captured["bypass_system_prompt"])
        finally:
            for k, old in saved.items():
                if old is None:
                    sys.modules.pop(k, None)
                else:
                    sys.modules[k] = old


if __name__ == "__main__":
    unittest.main()
