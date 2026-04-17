"""Optional multimodal image inspection tool."""

from __future__ import annotations

import base64
import json
import mimetypes
import os
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..bash.cards import ToolCard
from ..providers.llama import LlamaCppClient
from ..providers.openai_compat import OpenAICompatClient
from ..tool_runner import ToolExecutionResult, ToolProgress
from .config import VisionConfig


_VISION_SYSTEM_PROMPT = """\
You are a precise visual inspector.

Describe what is actually visible in the supplied image. For UI
screenshots, focus on layout, hierarchy, clipping, spacing, contrast,
alignment, state, and obvious interaction cues. Do not invent DOM
structure you cannot see. If the screenshot alone is insufficient, say
what is missing instead of guessing.
"""


@dataclass(frozen=True, slots=True)
class VisionRuntimeStatus:
    tool_available: bool
    mode: str
    provider_type: str
    base_url: str
    model: str
    reason: str
    supports_vision: bool | None = None
    supports_typed_content: bool | None = None


@dataclass(frozen=True, slots=True)
class _ResolvedVisionEndpoint:
    provider_type: str
    base_url: str
    model: str
    api_key: str
    timeout_s: float
    max_tokens: int
    detail: str
    mode: str


def vision_preview_card(arguments: dict[str, Any], *, tool_call_id: str) -> ToolCard:
    path = str(arguments.get("path", "") or "").strip()
    prompt = " ".join(str(arguments.get("prompt", "") or "").split()).strip()
    detail = str(arguments.get("detail", "") or "").strip()
    params: list[tuple[str, str]] = []
    if path:
        params.append(("path", path))
    if detail:
        params.append(("detail", detail))
    if prompt:
        params.append(("prompt", prompt[:64] + ("…" if len(prompt) > 64 else "")))
    raw = " ".join(bit for bit in (path, prompt) if bit) or "vision"
    return ToolCard(
        verb="vision-inspect",
        params=tuple(params),
        risk="safe",
        raw_command=raw,
        confidence=1.0,
        parser_name="native-vision",
        tool_name="vision",
        tool_arguments={
            key: value
            for key, value in arguments.items()
            if value not in (None, "", False)
        },
        raw_label_prefix="◍",
        tool_call_id=tool_call_id,
    )


def vision_runtime_status(
    config: VisionConfig,
    *,
    client: Any | None = None,
) -> VisionRuntimeStatus:
    endpoint = _resolve_vision_endpoint(config, client=client)
    if endpoint is None:
        return VisionRuntimeStatus(
            tool_available=False,
            mode=config.mode,
            provider_type=config.provider_type,
            base_url=config.base_url.strip(),
            model=config.model.strip(),
            reason="no multimodal runtime is configured",
        )

    if endpoint.provider_type == "llamacpp":
        probe = LlamaCppClient(
            base_url=endpoint.base_url,
            model=endpoint.model,
        )
        if not probe.health():
            return VisionRuntimeStatus(
                tool_available=False,
                mode=endpoint.mode,
                provider_type=endpoint.provider_type,
                base_url=endpoint.base_url,
                model=endpoint.model,
                reason="llama.cpp vision endpoint is unreachable",
            )
        caps = probe.detect_runtime_capabilities()
        if not caps.supports_vision:
            return VisionRuntimeStatus(
                tool_available=False,
                mode=endpoint.mode,
                provider_type=endpoint.provider_type,
                base_url=endpoint.base_url,
                model=endpoint.model,
                reason="llama.cpp endpoint reports vision=false",
                supports_vision=False,
                supports_typed_content=caps.supports_typed_content,
            )
        if not caps.supports_typed_content:
            return VisionRuntimeStatus(
                tool_available=True,
                mode=endpoint.mode,
                provider_type=endpoint.provider_type,
                base_url=endpoint.base_url,
                model=endpoint.model,
                reason=(
                    "llama.cpp multimodal endpoint is reachable. "
                    "typed-content capability was not advertised in /props, "
                    "so image support will be verified at call time."
                ),
                supports_vision=True,
                supports_typed_content=False,
            )
        return VisionRuntimeStatus(
            tool_available=True,
            mode=endpoint.mode,
            provider_type=endpoint.provider_type,
            base_url=endpoint.base_url,
            model=endpoint.model,
            reason="llama.cpp multimodal endpoint is ready",
            supports_vision=True,
            supports_typed_content=True,
        )

    probe = OpenAICompatClient(
        base_url=endpoint.base_url,
        model=endpoint.model,
        api_key=endpoint.api_key or None,
    )
    if not probe.health():
        return VisionRuntimeStatus(
            tool_available=False,
            mode=endpoint.mode,
            provider_type=endpoint.provider_type,
            base_url=endpoint.base_url,
            model=endpoint.model,
            reason="OpenAI-compatible vision endpoint is unreachable",
        )
    return VisionRuntimeStatus(
        tool_available=True,
        mode=endpoint.mode,
        provider_type=endpoint.provider_type,
        base_url=endpoint.base_url,
        model=endpoint.model,
        reason=(
            "OpenAI-compatible endpoint is reachable. "
            "Image capability will be verified at call time."
        ),
    )


def run_vision_analysis(
    arguments: dict[str, Any],
    config: VisionConfig,
    *,
    client: Any | None = None,
    progress: ToolProgress | None = None,
) -> ToolExecutionResult:
    path_text = str(arguments.get("path", "") or "").strip()
    if not path_text:
        return ToolExecutionResult(stderr="vision requires a local image path", exit_code=1)
    prompt = " ".join(str(arguments.get("prompt", "") or "").split()).strip()
    if not prompt:
        prompt = (
            "Describe the image accurately. If it is a UI screenshot, call out "
            "the most important visible issue or confirm that it looks correct."
        )
    detail = str(arguments.get("detail", "") or config.detail).strip().lower() or "auto"
    if detail not in {"auto", "low", "high", "original"}:
        detail = config.detail

    image_path = Path(os.path.expanduser(os.path.expandvars(path_text)))
    if not image_path.is_file():
        return ToolExecutionResult(
            stderr=f"vision image path does not exist: {image_path}",
            exit_code=1,
        )

    endpoint = _resolve_vision_endpoint(config, client=client)
    if endpoint is None:
        return ToolExecutionResult(
            stderr=(
                "vision runtime is not configured. Enable a multimodal primary "
                "model or configure a dedicated vision endpoint."
            ),
            exit_code=1,
        )

    status = vision_runtime_status(config, client=client)
    if not status.tool_available:
        return ToolExecutionResult(stderr=status.reason, exit_code=1)

    if detail not in {"auto", "low", "high", "original"}:
        detail = endpoint.detail

    if progress is not None:
        progress.stdout(f"vision: analyzing {image_path.name}")

    try:
        image_url = _image_path_to_data_url(image_path)
        body = _vision_request_body(
            prompt=prompt,
            image_url=image_url,
            detail=detail,
            model=endpoint.model,
            max_tokens=endpoint.max_tokens,
        )
        payload = _post_json(
            url=f"{_api_root(endpoint.base_url)}/chat/completions",
            body=body,
            timeout_s=endpoint.timeout_s,
            api_key=endpoint.api_key,
        )
        answer = _extract_chat_content(payload)
        usage = payload.get("usage")
    except Exception as exc:  # noqa: BLE001
        return ToolExecutionResult(
            stderr=f"vision analysis failed: {type(exc).__name__}: {exc}",
            exit_code=1,
        )

    lines = [
        "Vision analysis completed.",
        f"Path: {image_path}",
        f"Model: {endpoint.model}",
        f"Detail: {detail}",
        "",
        answer.strip(),
    ]
    if isinstance(usage, dict):
        prompt_tokens = usage.get("prompt_tokens")
        completion_tokens = usage.get("completion_tokens")
        if prompt_tokens is not None or completion_tokens is not None:
            lines.extend(
                [
                    "",
                    "Usage:",
                    f"  prompt_tokens: {prompt_tokens if prompt_tokens is not None else '?'}",
                    f"  completion_tokens: {completion_tokens if completion_tokens is not None else '?'}",
                ]
            )
    return ToolExecutionResult(
        output="\n".join(lines).rstrip(),
        exit_code=0,
        metadata={
            "path": str(image_path),
            "detail": detail,
            "provider_type": endpoint.provider_type,
            "base_url": endpoint.base_url,
            "model": endpoint.model,
        },
    )


def _resolve_vision_endpoint(
    config: VisionConfig,
    *,
    client: Any | None = None,
) -> _ResolvedVisionEndpoint | None:
    if config.mode == "inherit":
        if client is None:
            return None
        provider_type = str(getattr(client, "provider_type", "") or "").strip().lower()
        if provider_type in {"llamacpp", "llama", "llama.cpp"}:
            return _ResolvedVisionEndpoint(
                provider_type="llamacpp",
                base_url=str(getattr(client, "base_url", "") or "").strip(),
                model=str(getattr(client, "model", "") or "").strip() or "local",
                api_key="",
                timeout_s=config.timeout_s,
                max_tokens=config.max_tokens,
                detail=config.detail,
                mode="inherit",
            )
        if provider_type in {"openai_compat", "openai", "openai-compat"}:
            return _ResolvedVisionEndpoint(
                provider_type="openai_compat",
                base_url=str(getattr(client, "base_url", "") or "").strip(),
                model=str(getattr(client, "model", "") or "").strip(),
                api_key=str(getattr(client, "api_key", "") or "").strip(),
                timeout_s=config.timeout_s,
                max_tokens=config.max_tokens,
                detail=config.detail,
                mode="inherit",
            )
        return None

    if not config.base_url.strip() or not config.model.strip():
        return None
    return _ResolvedVisionEndpoint(
        provider_type=config.provider_type,
        base_url=config.base_url.strip(),
        model=config.model.strip(),
        api_key=config.effective_api_key(),
        timeout_s=config.timeout_s,
        max_tokens=config.max_tokens,
        detail=config.detail,
        mode="endpoint",
    )


def _api_root(base_url: str) -> str:
    base = base_url.rstrip("/")
    if base.endswith("/v1") or "/v1/" in base:
        return base
    return f"{base}/v1"


def _image_path_to_data_url(path: Path) -> str:
    mime = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    if not mime.startswith("image/"):
        raise ValueError(f"{path} is not an image file")
    raw = path.read_bytes()
    encoded = base64.b64encode(raw).decode("ascii")
    return f"data:{mime};base64,{encoded}"


def _vision_request_body(
    *,
    prompt: str,
    image_url: str,
    detail: str,
    model: str,
    max_tokens: int,
) -> dict[str, Any]:
    image_part: dict[str, Any] = {
        "type": "image_url",
        "image_url": {"url": image_url},
    }
    if detail in {"auto", "low", "high"}:
        image_part["image_url"]["detail"] = detail
    return {
        "model": model,
        "stream": False,
        "max_tokens": max_tokens,
        "temperature": 0.1,
        "messages": [
            {"role": "system", "content": _VISION_SYSTEM_PROMPT},
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": prompt},
                    image_part,
                ],
            },
        ],
    }


def _post_json(
    *,
    url: str,
    body: dict[str, Any],
    timeout_s: float,
    api_key: str,
) -> dict[str, Any]:
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    req = urllib.request.Request(
        url,
        data=json.dumps(body).encode("utf-8"),
        headers=headers,
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout_s) as resp:
            text = resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        text = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"HTTP {exc.code}: {text}") from exc
    data = json.loads(text)
    if not isinstance(data, dict):
        raise ValueError("vision endpoint returned a non-object JSON payload")
    return data


def _extract_chat_content(payload: dict[str, Any]) -> str:
    choices = payload.get("choices")
    if not isinstance(choices, list) or not choices:
        raise ValueError("vision endpoint returned no choices")
    message = (choices[0] or {}).get("message") or {}
    content = message.get("content", "")
    if isinstance(content, str):
        text = content.strip()
        if text:
            return text
    if isinstance(content, list):
        out: list[str] = []
        for part in content:
            if not isinstance(part, dict):
                continue
            text = part.get("text")
            if isinstance(text, str) and text.strip():
                out.append(text.strip())
        if out:
            return "\n".join(out)
    # Thinking models (Qwen3.x, etc.) may put the analysis in
    # reasoning_content and leave content empty — especially when
    # max_tokens is too low for the model to finish thinking and
    # start producing visible output.
    reasoning = message.get("reasoning_content", "")
    if isinstance(reasoning, str) and reasoning.strip():
        return reasoning.strip()
    raise ValueError("vision endpoint returned no readable content")
