"""Coverage for the multimodal vision tool."""

from __future__ import annotations

import base64
from pathlib import Path

from successor.providers.llama import LlamaCppRuntimeCapabilities
from successor.web.vision import (
    VisionConfig,
    VisionRuntimeStatus,
    _ResolvedVisionEndpoint,
    run_vision_analysis,
    vision_preview_card,
    vision_runtime_status,
)


_PNG_1X1 = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+j6uoAAAAASUVORK5CYII="
)


def test_vision_preview_card_uses_metadata() -> None:
    card = vision_preview_card(
        {
            "path": "/tmp/ui.png",
            "prompt": "Check whether the CTA is clipped.",
            "detail": "high",
        },
        tool_call_id="call_vision_1",
    )
    assert card.tool_name == "vision"
    assert card.tool_call_id == "call_vision_1"
    assert card.verb == "vision-inspect"
    assert ("path", "/tmp/ui.png") in card.params


def test_vision_runtime_status_rejects_llama_when_vision_is_disabled(monkeypatch) -> None:
    monkeypatch.setattr("successor.web.vision.LlamaCppClient.health", lambda self: True)
    monkeypatch.setattr(
        "successor.web.vision.LlamaCppClient.detect_runtime_capabilities",
        lambda self: LlamaCppRuntimeCapabilities(
            supports_vision=False,
            supports_typed_content=False,
        ),
    )
    status = vision_runtime_status(
        VisionConfig(
            mode="endpoint",
            provider_type="llamacpp",
            base_url="http://127.0.0.1:8090",
            model="vision-local",
        ),
    )
    assert status.tool_available is False
    assert "vision=false" in status.reason


def test_vision_runtime_status_allows_llama_when_vision_is_enabled_even_if_typed_flag_is_false(
    monkeypatch,
) -> None:
    monkeypatch.setattr("successor.web.vision.LlamaCppClient.health", lambda self: True)
    monkeypatch.setattr(
        "successor.web.vision.LlamaCppClient.detect_runtime_capabilities",
        lambda self: LlamaCppRuntimeCapabilities(
            supports_vision=True,
            supports_typed_content=False,
        ),
    )
    status = vision_runtime_status(
        VisionConfig(
            mode="endpoint",
            provider_type="llamacpp",
            base_url="http://127.0.0.1:8090",
            model="vision-local",
        ),
    )
    assert status.tool_available is True
    assert "verified at call time" in status.reason


def test_run_vision_analysis_formats_result(monkeypatch, tmp_path: Path) -> None:
    image = tmp_path / "shot.png"
    image.write_bytes(_PNG_1X1)

    monkeypatch.setattr(
        "successor.web.vision.vision_runtime_status",
        lambda *_args, **_kwargs: VisionRuntimeStatus(
            tool_available=True,
            mode="endpoint",
            provider_type="openai_compat",
            base_url="http://127.0.0.1:8090",
            model="vision-local",
            reason="ready",
        ),
    )
    monkeypatch.setattr(
        "successor.web.vision._resolve_vision_endpoint",
        lambda *_args, **_kwargs: _ResolvedVisionEndpoint(
            provider_type="openai_compat",
            base_url="http://127.0.0.1:8090",
            model="vision-local",
            api_key="",
            timeout_s=30.0,
            max_tokens=512,
            detail="auto",
            mode="endpoint",
        ),
    )

    captured: dict[str, object] = {}

    def _fake_post_json(*, url, body, timeout_s, api_key):  # noqa: ANN001
        captured["url"] = url
        captured["body"] = body
        captured["timeout_s"] = timeout_s
        captured["api_key"] = api_key
        return {
            "choices": [{"message": {"content": "The primary CTA is clipped on the right edge."}}],
            "usage": {"prompt_tokens": 123, "completion_tokens": 45},
        }

    monkeypatch.setattr("successor.web.vision._post_json", _fake_post_json)

    result = run_vision_analysis(
        {"path": str(image), "prompt": "Find the main visual issue."},
        VisionConfig(mode="endpoint", provider_type="openai_compat", base_url="http://127.0.0.1:8090", model="vision-local"),
    )

    assert result.exit_code == 0
    assert "Vision analysis completed." in result.output
    assert "The primary CTA is clipped on the right edge." in result.output
    body = captured["body"]
    assert isinstance(body, dict)
    content = body["messages"][1]["content"]
    assert content[1]["type"] == "image_url"
    assert content[1]["image_url"]["url"].startswith("data:image/png;base64,")


def test_run_vision_analysis_requires_existing_path(tmp_path: Path) -> None:
    result = run_vision_analysis(
        {"path": str(tmp_path / "missing.png")},
        VisionConfig(mode="endpoint", provider_type="openai_compat", base_url="http://127.0.0.1:8090", model="vision-local"),
    )
    assert result.exit_code == 1
    assert "does not exist" in result.stderr


class _FakeAnthropicClient:
    """Minimal stand-in for AnthropicClient used in vision resolution tests."""

    provider_type = "anthropic"

    def __init__(self, api_key: str = "") -> None:
        self.base_url = "https://api.z.ai/api/anthropic"
        self.model = "glm-5.1"
        self.api_key = api_key


def test_vision_runtime_status_explains_why_inherit_fails_on_anthropic() -> None:
    status = vision_runtime_status(
        VisionConfig(mode="inherit", provider_type="llamacpp"),
        client=_FakeAnthropicClient(),
    )
    assert status.tool_available is False
    assert "Anthropic-protocol primary" in status.reason
    # Error message should now point at the subscription-covered path.
    assert "glm-4.6v" in status.reason
    assert "https://api.z.ai/api/anthropic" in status.reason


def test_vision_endpoint_anthropic_dispatch_format(
    monkeypatch, tmp_path: Path
) -> None:
    """Endpoint mode with provider_type=anthropic builds Anthropic Messages
    API requests (image content block with source{type,media_type,data},
    POSTs to /v1/messages, uses x-api-key header) and inherits the primary
    client's api_key when the vision block leaves it empty.

    This is the subscription-covered path for z.ai GLM Coding Plan users.
    """
    image = tmp_path / "shot.png"
    image.write_bytes(_PNG_1X1)

    captured: dict[str, object] = {}

    def _fake_post_anthropic(*, url, body, timeout_s, api_key):  # noqa: ANN001
        captured["url"] = url
        captured["api_key"] = api_key
        captured["body"] = body
        return {
            "id": "msg_test",
            "type": "message",
            "role": "assistant",
            "model": "glm-4.6v",
            "content": [{"type": "text", "text": "A small PNG, mostly black."}],
            "stop_reason": "end_turn",
            "usage": {"input_tokens": 42, "output_tokens": 10},
        }

    monkeypatch.setattr(
        "successor.web.vision._post_anthropic_messages", _fake_post_anthropic
    )

    config = VisionConfig(
        mode="endpoint",
        provider_type="anthropic",
        base_url="https://api.z.ai/api/anthropic",
        model="glm-4.6v",
        # NO api_key/api_key_file — should fall through to primary client.
    )
    client = _FakeAnthropicClient(api_key="zai-secret-key")

    result = run_vision_analysis(
        {"path": str(image), "prompt": "Describe."},
        config,
        client=client,
    )

    assert result.exit_code == 0, result.stderr
    assert "A small PNG, mostly black." in result.output

    # Wire format — these are the guarantees that must hold for a real z.ai
    # subscription request to succeed.
    assert captured["url"] == "https://api.z.ai/api/anthropic/v1/messages"
    assert captured["api_key"] == "zai-secret-key"

    body = captured["body"]
    assert isinstance(body, dict)
    assert body["model"] == "glm-4.6v"
    # system prompt lives at the top level on Anthropic, not as a role=system msg
    assert "system" in body
    assert body["messages"][0]["role"] == "user"
    content = body["messages"][0]["content"]
    assert isinstance(content, list)
    assert content[0] == {"type": "text", "text": "Describe."}
    img_block = content[1]
    assert img_block["type"] == "image"
    assert img_block["source"]["type"] == "base64"
    assert img_block["source"]["media_type"] == "image/png"
    assert img_block["source"]["data"]  # non-empty base64 payload
