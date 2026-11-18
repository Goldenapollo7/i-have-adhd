"""
title: Sonny Auto Router
author: Sonny Homelab
version: 1.0.0
description: Automatically routes Sonny turns across models already installed on the AI PC's Ollama (vision, coding, and general chat/tools) with two-stage vision-to-action delegation. Never downloads models and never routes chat inference to the Gaming PC.
"""

from __future__ import annotations

import asyncio
import json
import re
import urllib.error
import urllib.request
from typing import Any, AsyncGenerator, Awaitable, Callable, Optional
from urllib.parse import urlparse

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


EMBEDDING_NAME_RE = re.compile(r"(embed|bge-|minilm|nomic-embed|mxbai-embed|snowflake-arctic-embed)", re.IGNORECASE)
VISION_NAME_RE = re.compile(r"(vision|\bvl\b|-vl|llava|minicpm-v|moondream|pixtral|bakllava|gemma3)", re.IGNORECASE)
CODING_NAME_RE = re.compile(r"(coder|codestral|devstral|starcoder|codellama|deepseek-coder|qwen.*coder)", re.IGNORECASE)

CODING_PROMPT_RE = re.compile(
    r"(\brefactor\b|\btraceback\b|\bstack\s*trace\b|\bunit\s*test\b|\bpytest\b|\btypescript\b"
    r"|\bdockerfile\b|\bsql\s+query\b|\bgit\s+(diff|rebase|bisect|commit)\b|\bcompile\s+error\b"
    r"|\bfix\s+(this|the)\s+(bug|code|build|script|function)\b|```)",
    re.IGNORECASE,
)

ACTION_WITH_IMAGE_RE = re.compile(
    r"(\brun\b|\bexecute\b|\bfix\b|\bkill\b|\bstop\b|\bstart\b|\brestart\b|\bdeploy\b"
    r"|\bbuild\b|\brender\b|\bgenerate\b|\bcomfyui\b|\bffmpeg\b|\bblender\b"
    r"|\bgaming\s*pc\b|\bai\s*pc\b|\bcheck\s+(status|gpu|vram|processes)\b|\bsave\b|\bedit\b)",
    re.IGNORECASE,
)

RESERVED_SELF_IDS: frozenset[str] = frozenset({"sonny", "sonny_auto_router"})

ALLOWED_OLLAMA_HOSTS: frozenset[str] = frozenset(
    {"127.0.0.1", "localhost", "::1", "host.docker.internal", "ollama"}
)


def _is_embedding_model(model_id: str) -> bool:
    return bool(EMBEDDING_NAME_RE.search(model_id))


def _is_vision_model(model_id: str, meta: Optional[dict[str, Any]] = None) -> bool:
    if meta:
        caps = meta.get("capabilities")
        if isinstance(caps, dict) and caps.get("vision") is True:
            return True
        if isinstance(caps, list) and "vision" in caps:
            return True
    return bool(VISION_NAME_RE.search(model_id))


def _is_coding_model(model_id: str) -> bool:
    return bool(CODING_NAME_RE.search(model_id))


def _extract_text_from_content(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, dict) and item.get("type") == "text":
                parts.append(str(item.get("text", "")))
            elif isinstance(item, str):
                parts.append(item)
        return "\n".join(p for p in parts if p)
    return ""


def _content_has_image(content: Any) -> bool:
    if not isinstance(content, list):
        return False
    for item in content:
        if not isinstance(item, dict):
            continue
        item_type = str(item.get("type", "")).lower()
        if item_type in ("image_url", "image", "input_image"):
            return True
    return False


def _files_have_image(files: Optional[list[Any]]) -> bool:
    if not files:
        return False
    for entry in files:
        if not isinstance(entry, dict):
            continue
        ftype = str(entry.get("type") or entry.get("content_type") or "").lower()
        mime = str((entry.get("file") or {}).get("meta", {}).get("content_type", "")).lower()
        name = str(entry.get("name") or (entry.get("file") or {}).get("filename", "")).lower()
        if ftype == "image" or ftype.startswith("image/") or mime.startswith("image/"):
            return True
        if name.endswith((".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp")):
            return True
    return False


def _strip_images_and_inject_brief(messages: list[dict[str, Any]], vision_brief: str) -> list[dict[str, Any]]:
    """Strip image parts for text-only execution models and attach the vision subagent's findings."""
    cleaned: list[dict[str, Any]] = []
    last_user_idx = -1
    for idx, msg in enumerate(messages):
        if msg.get("role") == "user":
            last_user_idx = idx

    for idx, msg in enumerate(messages):
        copy_msg = dict(msg)
        content = copy_msg.get("content")
        if isinstance(content, list):
            text_only = _extract_text_from_content(content)
            copy_msg["content"] = text_only or "(Image attached by user)"
        if idx == last_user_idx:
            base_text = str(copy_msg.get("content") or "").strip()
            copy_msg["content"] = (
                f"{base_text}\n\n"
                f"[Sonny Vision Inspection (from AI-PC vision model)]\n"
                f"{vision_brief.strip()}"
            ).strip()
        cleaned.append(copy_msg)
    return cleaned


def _validate_ai_pc_ollama_url(url: str, extra_allowed_host: str = "") -> str:
    """Guarantee Ollama requests stay on the AI PC and never target public or Gaming PC hosts."""
    cleaned = url.strip().rstrip("/")
    parsed = urlparse(cleaned)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise ValueError(f"Invalid AI-PC Ollama URL: {url!r}")
    host = parsed.hostname.lower()
    allowed = set(ALLOWED_OLLAMA_HOSTS)
    if extra_allowed_host.strip():
        allowed.add(extra_allowed_host.strip().lower())
    if host not in allowed:
        raise ValueError(
            f"Refused Ollama host '{host}': Sonny primary inference must run on the AI PC only "
            f"(expected localhost / 127.0.0.1 / host.docker.internal)."
        )
    return cleaned


def _http_json(url: str, payload: Optional[dict[str, Any]] = None, timeout: int = 30) -> dict[str, Any]:
    data = None
    headers = {"Accept": "application/json"}
    method = "GET"
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"
        method = "POST"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read().decode("utf-8", errors="replace")
        return json.loads(raw) if raw.strip() else {}


def _stream_ollama_sse(url: str, payload: dict[str, Any], timeout: int = 120):
    """Synchronous line reader for Ollama /v1/chat/completions SSE stream."""
    data = json.dumps({**payload, "stream": True}).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        headers={"Content-Type": "application/json", "Accept": "text/event-stream"},
        method="POST",
    )
    chunks: list[str] = []
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        for raw_line in resp:
            line = raw_line.decode("utf-8", errors="replace").strip()
            if line.startswith("data:"):
                chunks.append(line)
    return chunks


class Pipe:
    """Automatic local model router and vision-to-action delegator for Sonny (AI PC only)."""

    class Valves(BaseModel):
        ollama_url: str = Field(
            default="http://host.docker.internal:11434",
            description="AI PC Ollama base URL (must remain on the AI PC; never point at Gaming PC).",
        )
        ai_pc_allowed_host: str = Field(
            default="",
            description="Optional AI PC hostname/Tailscale IP if Ollama is reached by host IP instead of host.docker.internal.",
        )
        preferred_primary_model: str = Field(
            default="sonny7-uncensored-8b:latest",
            description="Preferred AI PC Ollama model for everyday chat, planning, and tool execution.",
        )
        preferred_vision_model: str = Field(
            default="sonny7-vision-4b-64k:latest",
            description="Preferred AI PC Ollama model for images, screenshots, and visual analysis.",
        )
        preferred_coding_model: str = Field(
            default="",
            description="Optional dedicated AI PC coding model ID. Leave blank to auto-detect or fall back to primary model.",
        )
        enable_vision_delegation: bool = Field(
            default=True,
            description="When a turn includes an image AND an action/coding/tool task, analyze the image with the vision model first and hand findings to the primary/coding model.",
        )
        show_route_status: bool = Field(
            default=True,
            description="Show a brief status indicator in chat naming the model selected on the AI PC.",
        )
        request_timeout_seconds: int = Field(
            default=120,
            description="Timeout in seconds for direct Ollama requests.",
        )

    def __init__(self) -> None:
        self.type = "pipe"
        self.id = "sonny_auto_router"
        self.name = "Sonny Auto (AI PC)"
        self.valves = self.Valves()

    def _discover_from_request(self, request: Any) -> list[dict[str, Any]]:
        """Read installed Ollama models directly from Open WebUI's in-memory registry."""
        models_map = getattr(getattr(getattr(request, "app", None), "state", None), "MODELS", None)
        if not isinstance(models_map, dict):
            return []
        discovered: list[dict[str, Any]] = []
        for mid, entry in models_map.items():
            if not isinstance(entry, dict):
                continue
            low = str(mid).strip().lower()
            if not low or low in RESERVED_SELF_IDS or low.startswith("sonny_auto_router"):
                continue
            if entry.get("pipe") or entry.get("owned_by") != "ollama":
                continue
            if _is_embedding_model(mid):
                continue
            info_meta = ((entry.get("info") or {}).get("meta") or {}) if isinstance(entry.get("info"), dict) else {}
            discovered.append({"id": str(mid), "vision": _is_vision_model(str(mid), info_meta)})
        return discovered

    async def _discover_from_ollama(self) -> list[dict[str, Any]]:
        """Query AI PC Ollama /api/tags directly (read-only; never pulls or modifies models)."""
        base = _validate_ai_pc_ollama_url(self.valves.ollama_url, self.valves.ai_pc_allowed_host)
        payload = await asyncio.to_thread(_http_json, f"{base}/api/tags", None, 8)
        models = payload.get("models")
        if not isinstance(models, list):
            return []
        out: list[dict[str, Any]] = []
        for row in models:
            if not isinstance(row, dict):
                continue
            name = str(row.get("name") or row.get("model") or "").strip()
            low = name.lower()
            if not name or low in RESERVED_SELF_IDS or _is_embedding_model(name):
                continue
            families = (row.get("details") or {}).get("families") or []
            is_vis = _is_vision_model(name) or any("clip" in str(f).lower() or "vl" in str(f).lower() for f in families)
            out.append({"id": name, "vision": is_vis})
        return out

    async def discover_models(self, request: Any = None) -> list[dict[str, Any]]:
        from_req = self._discover_from_request(request)
        if from_req:
            return from_req
        return await self._discover_from_ollama()

    @staticmethod
    def _match_model(available_ids: list[str], wanted: str) -> Optional[str]:
        target = wanted.strip()
        if not target:
            return None
        for mid in available_ids:
            if mid == target:
                return mid
        # Match bare name against `:latest`
        target_base = target.split(":", 1)[0].lower()
        for mid in available_ids:
            if mid.lower() == f"{target_base}:latest" or mid.split(":", 1)[0].lower() == target_base:
                return mid
        return None

    def select_route(
        self,
        body: dict[str, Any],
        catalog: list[dict[str, Any]],
        *,
        files: Optional[list[Any]] = None,
        task: Optional[str] = None,
    ) -> dict[str, Any]:
        """Choose the best AI PC model (and optional vision delegation) without asking the user."""
        if not catalog:
            raise RuntimeError(
                "No local chat models found on the AI PC's Ollama. Confirm Ollama is running on port 11434."
            )

        all_ids = [m["id"] for m in catalog if m["id"].lower() not in RESERVED_SELF_IDS]
        vision_ids = [m["id"] for m in catalog if m.get("vision")]
        non_vision_ids = [m["id"] for m in catalog if not m.get("vision")]
        coding_ids = [m["id"] for m in catalog if _is_coding_model(m["id"])]

        primary = (
            self._match_model(all_ids, self.valves.preferred_primary_model)
            or (non_vision_ids[0] if non_vision_ids else all_ids[0])
        )
        vision = (
            self._match_model(all_ids, self.valves.preferred_vision_model)
            or (vision_ids[0] if vision_ids else None)
        )
        coding = (
            self._match_model(all_ids, self.valves.preferred_coding_model)
            or (coding_ids[0] if coding_ids else primary)
        )

        # Background housekeeping tasks (chat title, tags, follow-ups) always use primary directly.
        if task:
            return {"model": primary, "delegate_vision": None, "reason": f"background:{task}"}

        messages = body.get("messages") or []
        last_user = next((m for m in reversed(messages) if m.get("role") == "user"), {})
        last_text = _extract_text_from_content(last_user.get("content"))

        # Check current user turn for images first, then recent history.
        inline_has_image = _content_has_image(last_user.get("content"))
        current_has_image = inline_has_image or _files_have_image(files)
        history_has_image = any(_content_has_image(m.get("content")) for m in messages[-6:])
        has_image = current_has_image or history_has_image

        wants_coding = bool(CODING_PROMPT_RE.search(last_text))
        has_tools = bool(body.get("tools"))
        wants_action = bool(ACTION_WITH_IMAGE_RE.search(last_text))

        if has_image and vision:
            executor = coding if wants_coding else primary
            # Two-stage delegation when the user sent an inline image AND wants an action/coding step,
            # and the vision model is distinct from the executor model.
            if (
                inline_has_image
                and self.valves.enable_vision_delegation
                and executor != vision
                and (wants_action or wants_coding)
            ):
                return {
                    "model": executor,
                    "delegate_vision": vision,
                    "reason": "vision+action delegation",
                }
            # Direct vision turn (pure image Q&A, OCR, screenshot explanation, or follow-up).
            if current_has_image or not (wants_action and has_tools):
                return {"model": vision, "delegate_vision": None, "reason": "vision"}

        if wants_coding and coding != primary:
            return {"model": coding, "delegate_vision": None, "reason": "coding"}

        return {"model": primary, "delegate_vision": None, "reason": "primary"}

    async def _emit_status(
        self,
        emitter: Optional[Callable[[dict[str, Any]], Awaitable[Any]]],
        description: str,
        *,
        done: bool = True,
    ) -> None:
        if not emitter or not self.valves.show_route_status:
            return
        try:
            await emitter(
                {
                    "type": "status",
                    "data": {"description": description, "done": done},
                }
            )
        except Exception:
            pass

    async def _run_vision_subagent(
        self,
        vision_model: str,
        messages: list[dict[str, Any]],
        request: Any = None,
        user: Optional[dict[str, Any]] = None,
    ) -> str:
        """Delegate visual inspection to the AI PC vision model and return structured findings."""
        last_user = next((m for m in reversed(messages) if m.get("role") == "user"), None)
        if not last_user:
            return ""
        user_text = _extract_text_from_content(last_user.get("content"))
        inspection_system = {
            "role": "system",
            "content": (
                "You are Sonny's visual inspection subagent on the AI PC. "
                "Examine the attached image(s) accurately and concisely: extract exact visible text, "
                "error messages, file paths, UI state, numbers, and key visual details relevant to the "
                "user's request. Do not speculate or invent details."
            ),
        }
        vision_user = dict(last_user)
        if isinstance(vision_user.get("content"), list) and not user_text:
            vision_user["content"] = [
                *vision_user["content"],
                {"type": "text", "text": "Extract all key details and visible text from this image."},
            ]

        sub_payload: dict[str, Any] = {
            "model": vision_model,
            "messages": [inspection_system, vision_user],
            "stream": False,
        }

        res = await self._dispatch_completion(sub_payload, request=request, user=user, metadata={})
        if isinstance(res, dict):
            choices = res.get("choices") or []
            if choices and isinstance(choices[0], dict):
                msg = choices[0].get("message") or {}
                return str(msg.get("content") or "").strip()
        return ""

    async def _dispatch_completion(
        self,
        payload: dict[str, Any],
        *,
        request: Any = None,
        user: Optional[dict[str, Any]] = None,
        metadata: Optional[dict[str, Any]] = None,
    ) -> Any:
        """Use Open WebUI's internal completion pipeline when available; fall back to AI PC Ollama HTTP."""
        chosen = str(payload.get("model") or "").strip()
        if not chosen or chosen.lower() in RESERVED_SELF_IDS or chosen.lower().startswith("sonny_auto_router"):
            raise RuntimeError(f"Blocked recursive route to '{chosen}'.")

        if request is not None and user:
            try:
                from open_webui.models.users import UserModel  # type: ignore[import-not-found]
                from open_webui.utils.chat import generate_chat_completion  # type: ignore[import-not-found]

                user_obj = (
                    user
                    if isinstance(user, UserModel)
                    else UserModel(**{k: v for k, v in user.items() if k != "valves"})
                )
                call_body = dict(payload)
                if metadata:
                    clean_meta = {k: v for k, v in metadata.items() if k != "selected_model_id"}
                    call_body["metadata"] = clean_meta
                return await generate_chat_completion(
                    request,
                    call_body,
                    user_obj,
                    bypass_filter=True,
                    bypass_system_prompt=True,
                )
            except ImportError:
                pass

        # Standalone / direct AI PC Ollama OpenAI-compatible endpoint
        base = _validate_ai_pc_ollama_url(self.valves.ollama_url, self.valves.ai_pc_allowed_host)
        endpoint = f"{base}/v1/chat/completions"
        clean_payload = {k: v for k, v in payload.items() if k != "metadata"}
        timeout = max(10, int(self.valves.request_timeout_seconds))

        if clean_payload.get("stream"):
            lines = await asyncio.to_thread(_stream_ollama_sse, endpoint, clean_payload, timeout)

            async def _gen() -> AsyncGenerator[str, None]:
                for line in lines:
                    yield line

            return _gen()

        return await asyncio.to_thread(_http_json, endpoint, clean_payload, timeout)

    async def pipe(
        self,
        body: dict[str, Any],
        __user__: Optional[dict[str, Any]] = None,
        __request__: Any = None,
        __event_emitter__: Optional[Callable[[dict[str, Any]], Awaitable[Any]]] = None,
        __metadata__: Optional[dict[str, Any]] = None,
        __files__: Optional[list[Any]] = None,
        __task__: Optional[str] = None,
    ) -> Any:
        catalog = await self.discover_models(__request__)
        route = self.select_route(body, catalog, files=__files__, task=__task__)
        target_model: str = route["model"]
        vision_subagent: Optional[str] = route.get("delegate_vision")
        reason: str = route["reason"]

        forward_body = dict(body)
        forward_body["model"] = target_model

        if vision_subagent:
            await self._emit_status(
                __event_emitter__,
                f"Inspecting image with {vision_subagent} (AI PC)...",
                done=False,
            )
            try:
                brief = await self._run_vision_subagent(
                    vision_subagent,
                    list(forward_body.get("messages") or []),
                    request=__request__,
                    user=__user__,
                )
            except Exception:
                brief = ""

            if brief:
                forward_body["messages"] = _strip_images_and_inject_brief(
                    list(forward_body.get("messages") or []),
                    brief,
                )
                await self._emit_status(
                    __event_emitter__,
                    f"Delegated image findings ({vision_subagent}) -> {target_model}",
                    done=True,
                )
            else:
                # Fail gracefully back to the vision model directly so image context is never dropped.
                forward_body["model"] = vision_subagent
                target_model = vision_subagent
                await self._emit_status(
                    __event_emitter__,
                    f"Routed to {vision_subagent} (vision fallback)",
                    done=True,
                )
        elif not __task__:
            await self._emit_status(
                __event_emitter__,
                f"Using {target_model} ({reason})",
                done=True,
            )

        return await self._dispatch_completion(
            forward_body,
            request=__request__,
            user=__user__,
            metadata=__metadata__,
        )
