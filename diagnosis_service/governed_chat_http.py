"""Internal HTTP adapter for governed knowledge and environment Chat."""

from __future__ import annotations

import asyncio
import json
from http import HTTPStatus

from diagnosis_service.chat_loop_checkpoints import ChatLoopCheckpointError
from diagnosis_service.diagnosis_provider import ProviderUnavailable
from diagnosis_service.governed_chat import GovernedChatError, answer_governed_chat
from diagnosis_service.model_provider import ModelProviderError


def dispatch(
    handler,
    path: str,
    provider_factory,
    checkpoints,
    adapters,
    max_turns: int,
    authorize_gateway,
) -> bool:
    if path != "/chat" or handler.command != "POST":
        return False
    if authorize_gateway(handler) is None:
        return True
    try:
        payload = handler.read_json_body()
        if not isinstance(payload, dict):
            raise GovernedChatError("invalid_request", "Chat execution request is invalid")
        result = asyncio.run(answer_governed_chat(
            payload,
            provider=provider_factory(),
            checkpoints=checkpoints,
            adapters=adapters,
            max_turns=max_turns,
        ))
    except (GovernedChatError, ChatLoopCheckpointError) as exc:
        code = exc.code
        status = HTTPStatus.CONFLICT if code in {"request_conflict", "image_input_unsupported"} else (
            HTTPStatus.BAD_GATEWAY if code == "invalid_model_response" else HTTPStatus.BAD_REQUEST
        )
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
    handler.write_json(HTTPStatus.OK, {"status": "ok", "result": result})
    return True
