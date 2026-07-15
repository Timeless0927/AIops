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

import http.client
import ipaddress
import json
import logging
import os
import socket
import ssl
import time
from dataclasses import dataclass, field
from typing import Any, Callable
from urllib import error, request
from urllib.parse import urlparse

from diagnosis_service.model_provider import ProviderRevision, VerificationResult, bounded_reason_code

logger = logging.getLogger(__name__)

# provider 不可达用 *Error(ValueError) 带 code,式样见 approval_service.ApprovalServiceError;
# provider 进程内、不对外开 HTTP 路由,故不映射 HTTP status(降级归 child 2 的 _derive_session_status)。
PROVIDER_UNAVAILABLE = "provider_unavailable"
PROVIDER_BAD_RESPONSE = "provider_bad_response"

_READINESS_PROBE_TOOL = [
    {
        "type": "function",
        "function": {
            "name": "readiness_probe",
            "description": "Return the supplied readiness nonce without performing external work.",
            "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
        },
    }
]


class ProviderUnavailable(ValueError):
    """Provider endpoint unreachable / timed out / returned a non-conforming body."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.no_retry = True


@dataclass
class ProviderConfig:
    base_url: str
    api_key: str = field(repr=False)
    model: str
    timeout_s: float
    extra_headers: dict[str, str] = field(default_factory=dict)
    endpoint_scope: str | None = None
    revision: str | None = None
    resolver: Callable[[str, int], list[str]] | None = field(default=None, repr=False)
    transport: Callable[..., tuple[int, dict[str, Any]]] | None = field(default=None, repr=False)
    observer: Callable[[str | None], None] | None = field(default=None, repr=False)

    async def chat_with_tools(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
    ) -> ProviderResult:
        """Post one tool-use turn as a bound method so ProviderConfig and ScriptedProvider
        share the same ``provider.chat_with_tools(messages, tools)`` surface that
        ``run_diagnosis_session`` calls. The module-level ``chat_with_tools(cfg, ...)``
        stays for direct callers/tests; this method binds ``self`` as that cfg.

        (ADR-0003 parent-level fix: child-1 exposed chat_with_tools as a module-level
        function taking cfg as arg 1, but child-2's tool-use loop treats ``provider`` as
        an object with a ``.chat_with_tools(messages, tools)`` method — like ScriptedProvider.
        The unit tests passed because they injected ScriptedProvider, never the real
        ProviderConfig returned by _resolve_diagnosis_provider. The live cluster path
        raised ``'ProviderConfig' object has no attribute 'chat_with_tools'`` and fell
        back to keyword. This method closes that seam.)"""
        return await chat_with_tools(self, messages, tools)


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


def configured_provider(
    revision: ProviderRevision,
    *,
    resolver: Callable[[str, int], list[str]] | None = None,
    transport: Callable[..., tuple[int, dict[str, Any]]] | None = None,
    observer: Callable[[str | None], None] | None = None,
) -> ProviderConfig:
    """Bind an immutable owner revision to the hardened product transport."""
    return ProviderConfig(
        base_url=revision.endpoint,
        api_key=revision.api_key,
        model=revision.model,
        timeout_s=float(revision.timeout_seconds),
        endpoint_scope=revision.endpoint_scope,
        revision=revision.revision,
        resolver=resolver,
        transport=transport,
        observer=observer,
    )


def run_readiness_probe(provider: Any, nonce: str) -> VerificationResult:
    """Run the harmless two-turn tool-use and structured JSON readiness probe."""
    import asyncio

    started = time.monotonic()

    async def run() -> None:
        messages: list[dict[str, Any]] = [
            {
                "role": "system",
                "content": (
                    "Call readiness_probe exactly once. After its tool result, return only "
                    'a JSON object with the exact nonce in the field "nonce". '
                    "Do not use Markdown, prose, or code fences."
                ),
            },
            {"role": "user", "content": "Run the readiness probe."},
        ]
        first = await provider.chat_with_tools(messages, _READINESS_PROBE_TOOL)
        if (
            len(first.tool_calls) != 1
            or first.tool_calls[0].name != "readiness_probe"
            or first.tool_calls[0].arguments != {}
        ):
            raise ValueError("first turn did not produce the required readiness_probe call")
        call = first.tool_calls[0]
        messages.extend(
            [
                first.message,
                {
                    "role": "tool",
                    "tool_call_id": call.id,
                    "content": json.dumps({"nonce": nonce}, separators=(",", ":")),
                },
            ]
        )
        second = await provider.chat_with_tools(messages, _READINESS_PROBE_TOOL)
        if second.tool_calls:
            raise ValueError("second turn unexpectedly requested another tool")
        content = second.message.get("content")
        if not isinstance(content, str):
            raise ValueError("second turn did not return structured JSON")
        decoded = json.loads(content)
        if not isinstance(decoded, dict) or decoded.get("nonce") != nonce:
            raise ValueError("second turn nonce did not match")

    try:
        asyncio.run(run())
    except ProviderUnavailable as exc:
        return VerificationResult.failed(bounded_reason_code(exc.code), latency_ms=_elapsed_ms(started))
    except (ValueError, TypeError, json.JSONDecodeError):
        return VerificationResult.failed("invalid_response", latency_ms=_elapsed_ms(started))
    return VerificationResult.succeeded(
        latency_ms=_elapsed_ms(started),
        provider_summary="tool_use_and_structured_json_verified",
    )


def _elapsed_ms(started: float) -> int:
    return max(0, int((time.monotonic() - started) * 1000))


def _is_internal_host(host: str) -> bool:
    host = (host or "").lower()
    return host.endswith(".svc.cluster.local") or host.endswith(".svc") or host.endswith(".local") or host in {"", "localhost"}


def _provider_timeout(default: float = 3.0) -> float:
    try:
        return max(0.1, float(os.getenv("AIOPS_DIAGNOSIS_TOOL_TIMEOUT_SECONDS", str(default))))
    except ValueError:
        return default


def load_from_env_for_test() -> ProviderConfig:
    """Build a test-only ProviderConfig from env and emit an outgress log.

    Product assembly resolves only encrypted owner revisions. Directed Adapter
    tests retain this helper for the historical environment contract.
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
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise ValueError("tool_calls must be a list")
    calls: list[ToolCall] = []
    for item in raw:
        if not isinstance(item, dict) or item.get("type") != "function":
            raise ValueError("tool call must be a function object")
        call_id = item.get("id")
        fn = item.get("function")
        if not isinstance(call_id, str) or not call_id or not isinstance(fn, dict):
            raise ValueError("tool call id and function are required")
        name = fn.get("name")
        args_raw = fn.get("arguments")
        if not isinstance(name, str) or not name or not isinstance(args_raw, str):
            raise ValueError("tool call name and JSON arguments are required")
        args = json.loads(args_raw)
        if not isinstance(args, dict):
            raise ValueError("tool call arguments must decode to an object")
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


def _post_configured_chat(
    cfg: ProviderConfig,
    target: str,
    body: dict[str, Any],
    headers: dict[str, str],
) -> dict[str, Any]:
    parsed = urlparse(target)
    host = parsed.hostname or ""
    try:
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
    except ValueError as exc:
        raise ProviderUnavailable("provider_rejected", "provider endpoint has an invalid port") from exc
    if cfg.endpoint_scope == "external":
        if parsed.scheme != "https":
            raise ProviderUnavailable("provider_rejected", "external provider endpoint must use HTTPS")
    elif cfg.endpoint_scope == "cluster_internal":
        if parsed.scheme not in {"http", "https"} or not (
            host.endswith(".svc") or host.endswith(".svc.cluster.local")
        ):
            raise ProviderUnavailable("provider_rejected", "cluster-internal provider must use Kubernetes service DNS")
    else:
        raise ProviderUnavailable("provider_rejected", "provider endpoint scope is invalid")
    resolver = cfg.resolver or _resolve_addresses
    try:
        addresses = list(dict.fromkeys(resolver(host, port)))
    except (OSError, ValueError) as exc:
        raise ProviderUnavailable("provider_unavailable", "provider DNS resolution failed") from exc
    if not addresses:
        raise ProviderUnavailable("provider_unavailable", "provider DNS resolution returned no addresses")
    for address in addresses:
        try:
            ip = ipaddress.ip_address(address)
        except ValueError as exc:
            raise ProviderUnavailable("provider_rejected", "provider DNS returned an invalid address") from exc
        allowed = ip.is_global if cfg.endpoint_scope == "external" else (
            (ip.is_private or ip.is_loopback or ip.is_link_local)
            and not (ip.is_multicast or ip.is_unspecified)
        )
        if not allowed:
            raise ProviderUnavailable("provider_rejected", "provider DNS address violates endpoint scope")
    transport = cfg.transport or _pinned_transport
    try:
        status, data = transport(target, addresses[0], body, headers, cfg.timeout_s)
    except (TimeoutError, socket.timeout) as exc:
        raise ProviderUnavailable("timeout", "provider request timed out") from exc
    except ssl.SSLError as exc:
        raise ProviderUnavailable("provider_rejected", "provider TLS verification failed") from exc
    except (OSError, http.client.HTTPException) as exc:
        raise ProviderUnavailable("provider_unavailable", "provider endpoint is unavailable") from exc
    if status in {401, 403}:
        raise ProviderUnavailable("authentication_failed", "provider rejected authentication")
    if status == 429:
        raise ProviderUnavailable("rate_limited", "provider rate limited the request")
    if status == 408:
        raise ProviderUnavailable("timeout", "provider request timed out")
    if 300 <= status < 400:
        raise ProviderUnavailable("provider_rejected", "provider redirects are not allowed")
    if 400 <= status < 500:
        raise ProviderUnavailable("provider_rejected", "provider rejected the request")
    if status >= 500:
        raise ProviderUnavailable("provider_unavailable", "provider endpoint is unavailable")
    if not isinstance(data, dict):
        raise ProviderUnavailable("invalid_response", "provider returned a non-object response")
    return data


def _resolve_addresses(host: str, port: int) -> list[str]:
    return [str(item[4][0]) for item in socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)]


def _pinned_transport(
    target: str,
    address: str,
    body: dict[str, Any],
    headers: dict[str, str],
    timeout: float,
) -> tuple[int, dict[str, Any]]:
    parsed = urlparse(target)
    host = parsed.hostname or ""
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    sock = socket.create_connection((address, port), timeout=timeout)
    if parsed.scheme == "https":
        sock = ssl.create_default_context().wrap_socket(sock, server_hostname=host)
    connection = http.client.HTTPConnection(host, port, timeout=timeout)
    connection.sock = sock
    path = parsed.path or "/"
    if parsed.query:
        path = f"{path}?{parsed.query}"
    try:
        connection.request(
            "POST",
            path,
            body=json.dumps(body, ensure_ascii=False).encode(),
            headers=headers,
        )
        response = connection.getresponse()
        return response.status, _decode_transport_body(response.status, response.read())
    finally:
        connection.close()


def _decode_transport_body(status: int, raw: bytes) -> dict[str, Any]:
    if not 200 <= status < 300:
        return {}
    try:
        payload = json.loads(raw.decode())
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("provider success body was not a valid JSON object") from exc
    if not isinstance(payload, dict):
        raise ValueError("provider success body was not a valid JSON object")
    return payload


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
        if cfg.endpoint_scope is None:
            data = await asyncio.to_thread(_post_chat, target, body, headers, cfg.timeout_s)
        else:
            data = await asyncio.to_thread(_post_configured_chat, cfg, target, body, headers)
    except ProviderUnavailable as exc:
        if cfg.observer is not None:
            cfg.observer(bounded_reason_code(exc.code))
        raise
    except (error.URLError, TimeoutError, OSError) as exc:
        failure = ProviderUnavailable(PROVIDER_UNAVAILABLE, f"provider endpoint unreachable: {exc}")
        if cfg.observer is not None:
            cfg.observer(bounded_reason_code(failure.code))
        raise failure from exc
    except (ValueError, json.JSONDecodeError) as exc:
        code = "invalid_response" if cfg.endpoint_scope is not None else PROVIDER_BAD_RESPONSE
        failure = ProviderUnavailable(code, "provider returned a non-conforming body")
        if cfg.observer is not None:
            cfg.observer(bounded_reason_code(failure.code))
        raise failure from exc

    choices = data.get("choices")
    if not isinstance(choices, list) or not choices:
        _raise_response_error(cfg, "provider response had no choices")
    choice = choices[0]
    if not isinstance(choice, dict):
        _raise_response_error(cfg, "provider response choice was not an object")
    message = choice.get("message")
    if not isinstance(message, dict):
        _raise_response_error(cfg, "provider response had no assistant message")
    finish_reason = str(choice.get("finish_reason") or "stop")
    try:
        tool_calls = _parse_tool_calls(message.get("tool_calls"))
    except (TypeError, ValueError, json.JSONDecodeError):
        _raise_response_error(cfg, "provider tool call was malformed")
    usage = data.get("usage")
    if not isinstance(usage, dict):
        usage = {}

    if cfg.observer is not None:
        cfg.observer(None)
    return ProviderResult(message=message, tool_calls=tool_calls, finish_reason=finish_reason, usage=usage)


def _raise_response_error(cfg: ProviderConfig, message: str) -> None:
    code = "invalid_response" if cfg.endpoint_scope is not None else PROVIDER_BAD_RESPONSE
    if cfg.observer is not None:
        cfg.observer(bounded_reason_code(code))
    raise ProviderUnavailable(code, message)


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
