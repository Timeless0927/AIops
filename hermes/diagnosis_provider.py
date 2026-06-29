"""OpenAI-compatible diagnosis provider layer.

Thin LLM call layer for ADR-0003: drives tool-use against an OpenAI-compatible
``/chat/completions`` endpoint over stdlib ``urllib`` (no SDK, no httpx), reads its
config from env, emits a startup outgress log, and degrades loudly when the
provider is unreachable.

Scope (child 1 of ADR-0003): this module is a *single step* of a tool-use loop. It
posts the accumulated ``messages`` to the provider and returns the next assistant
turn (content / tool_calls / finish_reason / usage). The caller (child 2) drives
the loop — dispatches tool_calls to MCP adapters, reinjects tool results, calls
us again — because this layer does not know about adapters or evidence collection.

# ponytail: 与现有 _http_tool_adapter 同 stdlib urllib,不引 httpx/openai/litellm;
#           每会话调用次数低,高并发需切 httpx(届时连同 adapter 一起换)。
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field
from typing import Any, Callable
from urllib import error, request

logger = logging.getLogger(__name__)

# provider 不可达用 *Error(ValueError) 带 code,式样见 approval_service.ApprovalServiceError;
# provider 进程内、不对外开 HTTP 路由,故不映射 HTTP status(降级归 child 2 的 _derive_session_status)。
PROVIDER_UNAVAILABLE = "provider_unavailable"
PROVIDER_BAD_RESPONSE = "provider_bad_response"


class ProviderUnavailable(ValueError):
    """Provider endpoint unreachable / timed out / returned a non-conforming body."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass
class ProviderConfig:
    base_url: str
    api_key: str
    model: str
    timeout_s: float
    extra_headers: dict[str, str] = field(default_factory=dict)


@dataclass
class ToolCall:
    id: str
    name: str
    arguments: dict[str, Any]


@dataclass
class ProviderResult:
    """One assistant turn from the provider."""

    message: dict[str, Any]          # OpenAI-shape assistant message (role/content/tool_calls)
    tool_calls: list[ToolCall]      # parsed tool calls (arguments 是已 parse 的 dict)
    finish_reason: str
    usage: dict[str, Any]           # {prompt_tokens, completion_tokens}


def _is_internal_host(host: str) -> bool:
    host = (host or "").lower()
    return host.endswith(".svc.cluster.local") or host.endswith(".svc") or host.endswith(".local") or host in {"", "localhost"}


def _provider_timeout(default: float = 3.0) -> float:
    # ponytail: 复用 aiops hermes 既有的 timeout env 约定,不新造 AIOPS_MODEL_TIMEOUT_SECONDS(deploy 侧零改动)。
    raw = os.getenv("AIOPS_HERMES_TOOL_TIMEOUT_SECONDS", "").strip()
    if not raw:
        return default
    try:
        return max(0.1, float(raw))
    except ValueError:
        return default


def load_from_env() -> ProviderConfig:
    """Build a ProviderConfig from env and emit a startup outgress log.

    The log is the ADR-0005 Issue D audit surface: provider base_url is recorded at
    process load so a data-egress base_url is *visible* to anyone reading stdout
    (which alloy scrapes to Loki per Issue A) without duplicating it into audit_log.
    """
    base_url = os.getenv("AIOPS_MODEL_BASE_URL", "").strip()
    api_key = os.getenv("AIOPS_MODEL_API_KEY", "").strip()
    model = os.getenv("AIOPS_MODEL_NAME", "").strip()

    if not base_url or not model:
        raise ProviderUnavailable(
            PROVIDER_BAD_RESPONSE,
            "AIOPS_MODEL_BASE_URL and AIOPS_MODEL_NAME must be set to load the diagnosis provider",
        )

    # base_url host 解析:保守地取 scheme://host[:port] 之后的 host 段判定内外网。
    host = base_url
    for sep in ("://",):
        if sep in host:
            host = host.split(sep, 1)[1]
    host = host.split("/", 1)[0].split("?", 1)[0]
    # 去掉 port / userinfo
    if "@" in host:
        host = host.rsplit("@", 1)[1]
    host = host.rsplit(":", 1)[0]
    external = not _is_internal_host(host)

    key_tail = api_key[-4:] if len(api_key) >= 4 else (api_key or "<empty>")
    # 仅放 base_url 尾(model-service.../v1)不暴露 entry;api_key 只露尾 4 位,never 全文。
    url_tail = base_url.split("://", 1)[-1] if "://" in base_url else base_url
    if external:
        logger.warning(
            "diagnosis provider outgress: base_url=...%s model=%s api_key=...%s — 数据出境,确认在允许范围内",
            url_tail, model, key_tail,
        )
    else:
        logger.warning(
            "diagnosis provider outgress: base_url=...%s model=%s api_key=...%s — internal provider",
            url_tail, model, key_tail,
        )

    return ProviderConfig(
        base_url=base_url,
        api_key=api_key,
        model=model,
        timeout_s=_provider_timeout(),
    )


def _parse_tool_calls(raw: Any) -> list[ToolCall]:
    if not isinstance(raw, list):
        return []
    calls: list[ToolCall] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        call_id = str(item.get("id") or "")
        fn = item.get("function") if isinstance(item.get("function"), dict) else {}
        name = str(fn.get("name") or "")
        args_raw = fn.get("arguments")
        if isinstance(args_raw, str):
            try:
                args = json.loads(args_raw) if args_raw else {}
            except json.JSONDecodeError:
                args = {"_raw": args_raw}
        elif isinstance(args_raw, dict):
            args = args_raw
        else:
            args = {}
        calls.append(ToolCall(id=call_id, name=name, arguments=args))
    return calls


def _post_chat(target: str, body: dict[str, Any], headers: dict[str, str], timeout: float) -> dict[str, Any]:
    """Synchronous urllib POST; run via asyncio.to_thread from the async entry."""
    payload = json.dumps(body, ensure_ascii=False).encode("utf-8")
    req = request.Request(
        target,
        data=payload,
        headers=headers,
        method="POST",
    )
    with request.urlopen(req, timeout=timeout) as response:
        data = json.loads(response.read().decode("utf-8") or "{}")
        if not isinstance(data, dict):
            raise ValueError("chat completions response must be a JSON object")
        return data


async def chat_with_tools(
    cfg: ProviderConfig,
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]],
) -> ProviderResult:
    """Post one tool-use turn to the provider and return the next assistant turn.

    Single-step design: the caller owns the messages list and the tool dispatch
    loop (child 2). We never mutate ``messages``; we return the assistant turn to
    append + react to. Raises ProviderUnavailable on network/parse failure.
    """
    import asyncio

    target = f"{cfg.base_url.rstrip('/')}/chat/completions"
    headers = {"Content-Type": "application/json", "Accept": "application/json"}
    if cfg.api_key:
        headers["Authorization"] = f"Bearer {cfg.api_key}"
    headers.update(cfg.extra_headers)
    body = {
        "model": cfg.model,
        "messages": messages,
        "tools": tools,
        "tool_choice": "auto",
    }

    try:
        data = await asyncio.to_thread(_post_chat, target, body, headers, cfg.timeout_s)
    except (error.URLError, TimeoutError, OSError) as exc:
        raise ProviderUnavailable(PROVIDER_UNAVAILABLE, f"provider endpoint unreachable: {exc}") from exc
    except (ValueError, json.JSONDecodeError) as exc:
        raise ProviderUnavailable(PROVIDER_BAD_RESPONSE, f"provider returned a non-conforming body: {exc}") from exc

    choices = data.get("choices")
    if not isinstance(choices, list) or not choices:
        raise ProviderUnavailable(PROVIDER_BAD_RESPONSE, "provider response had no choices")
    choice = choices[0]
    if not isinstance(choice, dict):
        raise ProviderUnavailable(PROVIDER_BAD_RESPONSE, "provider response choice was not an object")
    message = choice.get("message")
    if not isinstance(message, dict):
        raise ProviderUnavailable(PROVIDER_BAD_RESPONSE, "provider response had no assistant message")
    finish_reason = str(choice.get("finish_reason") or "stop")
    tool_calls = _parse_tool_calls(message.get("tool_calls"))
    usage = data.get("usage")
    if not isinstance(usage, dict):
        usage = {}

    return ProviderResult(message=message, tool_calls=tool_calls, finish_reason=finish_reason, usage=usage)


class ScriptedProvider:
    """In-memory provider for tests: pops scripted responses, records the message history.

    Exposes the same ``chat_with_tools`` surface as a real ProviderConfig so child 2
    can inject it without a network. No env, no http.
    """

    def __init__(self, scripts: list[Any]) -> None:
        # Each script entry: a dict response (raw chat-completions body) or a
        # callable(messages, tools) -> dict for state-dependent scripting.
        self._scripts = list(scripts)
        self._index = 0
        self.messages_history: list[list[dict[str, Any]]] = []

    async def chat_with_tools(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
    ) -> ProviderResult:
        if self._index >= len(self._scripts):
            raise ProviderUnavailable(PROVIDER_BAD_RESPONSE, "ScriptedProvider out of scripts")
        entry = self._scripts[self._index]
        self._index += 1
        self.messages_history.append([dict(m) for m in messages])
        data = entry(messages, tools) if callable(entry) else entry
        if not isinstance(data, dict):
            raise ProviderUnavailable(PROVIDER_BAD_RESPONSE, "scripted response was not a dict")
        return _parse_chat_response(data)


def _parse_chat_response(data: dict[str, Any]) -> ProviderResult:
    """Build a ProviderResult from a raw chat-completions response dict.

    Shared by ScriptedProvider and (aligned with) chat_with_tools so the scripted
    shape matches the real wire shape.
    """
    choices = data.get("choices")
    if not isinstance(choices, list) or not choices:
        raise ProviderUnavailable(PROVIDER_BAD_RESPONSE, "scripted response had no choices")
    choice = choices[0]
    if not isinstance(choice, dict):
        raise ProviderUnavailable(PROVIDER_BAD_RESPONSE, "scripted choice was not an object")
    message = choice.get("message")
    if not isinstance(message, dict):
        raise ProviderUnavailable(PROVIDER_BAD_RESPONSE, "scripted response had no assistant message")
    return ProviderResult(
        message=message,
        tool_calls=_parse_tool_calls(message.get("tool_calls")),
        finish_reason=str(choice.get("finish_reason") or "stop"),
        usage=data.get("usage") if isinstance(data.get("usage"), dict) else {},
    )