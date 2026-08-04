"""Internal HTTP adapter for knowledge-only Chat."""

from __future__ import annotations

import asyncio
import json
from http import HTTPStatus

from diagnosis_service.diagnosis_provider import ProviderUnavailable
from diagnosis_service.knowledge_chat import KnowledgeChatError, answer_knowledge_chat
from diagnosis_service.model_provider import ModelProviderError


def dispatch(handler, path: str, provider_factory, authorize_gateway) -> bool:
    if path != "/chat/knowledge" or handler.command != "POST":
        return False
    if authorize_gateway(handler) is None:
        return True
    try:
        payload = handler.read_json_body()
        if set(payload) != {"messages"} or not isinstance(payload["messages"], list):
            raise KnowledgeChatError("invalid_messages", "messages are required")
        messages = payload["messages"]
        if any(not isinstance(item, dict) for item in messages):
            raise KnowledgeChatError("invalid_messages", "messages are invalid")
        answer = asyncio.run(answer_knowledge_chat(messages, provider_factory()))
    except KnowledgeChatError as exc:
        code = exc.code
        status = HTTPStatus.BAD_GATEWAY if code == "invalid_model_response" else HTTPStatus.BAD_REQUEST
        handler.write_json(status, {"status": "rejected", "error": {"code": code, "message": str(exc)}})
        return True
    except (ModelProviderError, ProviderUnavailable) as exc:
        handler.write_json(
            HTTPStatus.SERVICE_UNAVAILABLE,
            {"status": "blocked", "error": {"code": exc.code, "message": "Model Provider is not ready"}},
        )
        return True
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        handler.write_json(
            HTTPStatus.BAD_REQUEST,
            {"status": "rejected", "error": {"code": "invalid_request", "message": str(exc)}},
        )
        return True
    handler.write_json(HTTPStatus.OK, {"status": "ok", "answer": answer})
    return True
