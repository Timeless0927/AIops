"""Knowledge-only Chat policy over the configured Diagnosis model."""

from __future__ import annotations

from typing import Any


class KnowledgeChatError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


async def answer_knowledge_chat(messages: list[dict[str, str]], provider: Any) -> str:
    """Answer ordinary AIOps questions while exposing no tool surface."""
    if not messages or len(messages) > 100:
        raise KnowledgeChatError("invalid_messages", "messages are required")
    accepted: list[dict[str, str]] = []
    for message in messages:
        if (
            not isinstance(message, dict)
            or message.get("role") not in {"user", "assistant"}
            or not isinstance(message.get("content"), str)
            or not message["content"].strip()
            or len(message["content"]) > 16_000
        ):
            raise KnowledgeChatError("invalid_messages", "messages are invalid")
        accepted.append({"role": message["role"], "content": message["content"].strip()})
    result = await provider.chat_with_tools(
        [{
            "role": "system",
            "content": (
                "Answer the AIOps knowledge question without tools. Do not claim current live-environment facts, "
                "request credentials, or propose that an action is approved."
            ),
        }, *accepted],
        [],
    )
    content = result.message.get("content")
    if result.tool_calls or not isinstance(content, str) or not content.strip():
        raise KnowledgeChatError("invalid_model_response", "knowledge model response is invalid")
    return content.strip()
