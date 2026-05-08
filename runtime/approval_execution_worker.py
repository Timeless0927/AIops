"""Background worker for approved approval execution."""

from __future__ import annotations

import asyncio
import importlib.util
import logging
import os
import sys
import threading
import time
import types
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

DEFAULT_INTERVAL_SECONDS = 5.0
DEFAULT_LIMIT = 10


def _project_root() -> Path:
    """Return the project root for local module loading."""
    return Path(__file__).resolve().parent.parent


def _load_project_module(relative_path: str, alias: str):
    """Load a project module by path to avoid hermes-agent import collisions."""
    if alias in sys.modules:
        return sys.modules[alias]

    module_path = _project_root() / relative_path
    spec = importlib.util.spec_from_file_location(alias, module_path)
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load module: {module_path}")

    module = importlib.util.module_from_spec(spec)
    sys.modules[alias] = module
    spec.loader.exec_module(module)
    return module


def _load_toolset_module(module_basename: str, alias: str):
    """Load root toolsets modules while avoiding hermes-agent/toolsets.py."""
    if alias in sys.modules:
        return sys.modules[alias]

    previous_toolsets = sys.modules.get("toolsets")
    local_toolsets = types.ModuleType("toolsets")
    local_toolsets.__path__ = [str(_project_root() / "toolsets")]  # type: ignore[attr-defined]
    local_toolsets.__package__ = "toolsets"
    sys.modules["toolsets"] = local_toolsets
    try:
        return _load_project_module(f"toolsets/{module_basename}.py", alias)
    finally:
        if previous_toolsets is None:
            sys.modules.pop("toolsets", None)
        else:
            sys.modules["toolsets"] = previous_toolsets


approval_execution = _load_toolset_module("approval_execution", "aiops_toolsets_approval_execution")

ProcessPending = Callable[..., Awaitable[dict[str, Any]]]
AdapterFactory = Callable[[], approval_execution.ApprovalExecutionAdapter]


def create_approval_execution_adapter() -> approval_execution.ApprovalExecutionAdapter:
    """Create the production remediation execution adapter."""
    module = getattr(approval_execution, "remediation_execution", None)
    if module is None or not hasattr(module, "create_approval_execution_adapter"):
        module = sys.modules.get("toolsets.remediation_execution")
    if module is None or not hasattr(module, "create_approval_execution_adapter"):
        module = _load_toolset_module(
            "remediation_execution",
            "toolsets.remediation_execution",
        )
    return module.create_approval_execution_adapter()


class ApprovalExecutionWorker:
    """Poll approved approvals and hand execution to the coordinator."""

    def __init__(
        self,
        *,
        interval_seconds: float = DEFAULT_INTERVAL_SECONDS,
        limit: int = DEFAULT_LIMIT,
        approved_after: float | None = None,
        process_pending: ProcessPending | None = None,
        adapter_factory: AdapterFactory | None = None,
    ) -> None:
        if interval_seconds <= 0:
            raise ValueError("interval_seconds must be positive")
        if limit <= 0:
            raise ValueError("limit must be positive")

        self.interval_seconds = interval_seconds
        self.limit = limit
        self.approved_after = approved_after
        self._process_pending = process_pending or approval_execution.process_pending_executions
        self._adapter_factory = adapter_factory or create_approval_execution_adapter
        self._stop_event = threading.Event()
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None

    async def tick(self) -> dict[str, Any]:
        """Process one batch of approved approvals."""
        adapter = self._adapter_factory()
        approved_after = self._ensure_approved_after()
        return await self._process_pending(
            limit=self.limit,
            adapter=adapter,
            approved_after=approved_after,
        )

    def start(self) -> "ApprovalExecutionWorker":
        """Start the polling loop in a daemon thread."""
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return self
            self._stop_event.clear()
            self._ensure_approved_after()
            self._thread = threading.Thread(
                target=self._run_thread,
                name="aiops-approval-execution-worker",
                daemon=True,
            )
            self._thread.start()
        return self

    def stop(self, *, timeout: float | None = 5.0) -> bool:
        """Stop the polling loop and return whether it exited."""
        self._stop_event.set()
        with self._lock:
            thread = self._thread

        if thread is None:
            return True
        if threading.current_thread() is not thread:
            thread.join(timeout=timeout)

        stopped = not thread.is_alive()
        if stopped:
            with self._lock:
                if self._thread is thread:
                    self._thread = None
        return stopped

    def _ensure_approved_after(self) -> float:
        if self.approved_after is None:
            self.approved_after = time.time()
        return self.approved_after

    def _run_thread(self) -> None:
        asyncio.run(self._run_loop())

    async def _run_loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                await self.tick()
            except Exception:
                logger.exception("approval execution worker tick failed")
            await asyncio.to_thread(self._stop_event.wait, self.interval_seconds)


def start_approval_execution_worker() -> ApprovalExecutionWorker:
    """Create and start the production approval execution worker."""
    interval_seconds = _float_env(
        "AIOPS_APPROVAL_EXECUTION_WORKER_INTERVAL_SECONDS",
        DEFAULT_INTERVAL_SECONDS,
    )
    limit = _int_env("AIOPS_APPROVAL_EXECUTION_WORKER_LIMIT", DEFAULT_LIMIT)
    return ApprovalExecutionWorker(interval_seconds=interval_seconds, limit=limit).start()


def _float_env(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None:
        return default
    try:
        value = float(raw)
    except ValueError:
        logger.warning("invalid %s=%r, using default %s", name, raw, default)
        return default
    if value <= 0:
        logger.warning("invalid %s=%r, using default %s", name, raw, default)
        return default
    return value


def _int_env(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None:
        return default
    try:
        value = int(raw)
    except ValueError:
        logger.warning("invalid %s=%r, using default %s", name, raw, default)
        return default
    if value <= 0:
        logger.warning("invalid %s=%r, using default %s", name, raw, default)
        return default
    return value
