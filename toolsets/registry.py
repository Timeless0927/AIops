"""Small local registry for repo-owned tool modules."""

from __future__ import annotations

import asyncio
import json
import logging
import threading
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ToolEntry:
    name: str
    toolset: str
    schema: dict[str, Any]
    handler: Callable[..., Any]
    check_fn: Callable[[], bool] | None = None
    requires_env: list[str] | None = None
    is_async: bool = False
    description: str = ""
    emoji: str = ""
    max_result_size_chars: int | float | None = None


class ToolRegistry:
    """Collect tool metadata from repo-owned tool modules."""

    def __init__(self) -> None:
        self._tools: dict[str, ToolEntry] = {}
        self._lock = threading.RLock()

    def register(
        self,
        *,
        name: str,
        toolset: str,
        schema: dict[str, Any],
        handler: Callable[..., Any],
        check_fn: Callable[[], bool] | None = None,
        requires_env: list[str] | None = None,
        is_async: bool = False,
        description: str = "",
        emoji: str = "",
        max_result_size_chars: int | float | None = None,
    ) -> None:
        with self._lock:
            self._tools[name] = ToolEntry(
                name=name,
                toolset=toolset,
                schema=schema,
                handler=handler,
                check_fn=check_fn,
                requires_env=requires_env or [],
                is_async=is_async,
                description=description or str(schema.get("description", "")),
                emoji=emoji,
                max_result_size_chars=max_result_size_chars,
            )

    def get_entry(self, name: str) -> ToolEntry | None:
        with self._lock:
            return self._tools.get(name)

    def get_all_tool_names(self) -> list[str]:
        with self._lock:
            return sorted(self._tools)

    def get_schema(self, name: str) -> dict[str, Any] | None:
        entry = self.get_entry(name)
        return entry.schema if entry is not None else None

    def get_toolset_for_tool(self, name: str) -> str | None:
        entry = self.get_entry(name)
        return entry.toolset if entry is not None else None

    def dispatch(self, name: str, args: dict[str, Any], **kwargs: Any) -> str:
        entry = self.get_entry(name)
        if entry is None:
            return tool_error(f"Unknown tool: {name}")
        try:
            result = entry.handler(args, **kwargs)
            if entry.is_async:
                result = _run_async(result)
            return str(result)
        except Exception as exc:
            logger.exception("Tool %s dispatch error: %s", name, exc)
            return tool_error(f"Tool execution failed: {type(exc).__name__}: {exc}")


def _run_async(awaitable: Any) -> Any:
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(awaitable)
    if loop.is_running():
        raise RuntimeError("cannot synchronously dispatch async tool inside a running event loop")
    return loop.run_until_complete(awaitable)


def tool_error(message: Any, **extra: Any) -> str:
    payload = {"error": str(message)}
    payload.update(extra)
    return json.dumps(payload, ensure_ascii=False)


def tool_result(data: Any = None, **kwargs: Any) -> str:
    return json.dumps(data if data is not None else kwargs, ensure_ascii=False)


registry = ToolRegistry()
