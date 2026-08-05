"""Diagnosis Job admission and execution against a frozen Provider revision."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from typing import Any

import diagnosis_service.diagnosis_provider as diagnosis_provider
from aiops.contracts.governed_tools import capability_binding
from diagnosis_service.handoff import incident_from_handoff
from diagnosis_service.jobs import DiagnosisJobs
from diagnosis_service.loop_checkpoint import GovernedLoopCheckpoint
from diagnosis_service.model_provider import (
    ModelProviderConfiguration,
    ProviderRevision,
    VerificationResult,
)
from toolsets.diagnosis_session import ModelResponseError, run_diagnosis_session


JSON = dict[str, object]
Adapter = Callable[[dict[str, Any]], Any]


class DiagnosisRuntime:
    """Binds durable Jobs to the exact verified Model Provider revision."""

    def __init__(
        self,
        jobs: DiagnosisJobs,
        model_provider: Callable[[], ModelProviderConfiguration],
        *,
        metrics_adapter: Adapter,
        logs_adapter: Adapter,
        k8s_read_adapter: Adapter,
        topology_adapter: Adapter,
        max_turns: int = 6,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._jobs = jobs
        self._model_provider = model_provider
        self._adapters = {
            "metrics_adapter": metrics_adapter,
            "logs_adapter": logs_adapter,
            "k8s_read_adapter": k8s_read_adapter,
            "topology_adapter": topology_adapter,
        }
        self._max_turns = max(1, max_turns)
        self._clock = clock

    def accept(self, payload: JSON) -> JSON:
        revision = self._model_provider().provider_for_new_job().revision
        return self._jobs.accept(payload, provider_revision=revision)

    def resolve_provider(self, revision: str | None = None) -> diagnosis_provider.ProviderConfig:
        owner = self._model_provider()
        bound = (
            owner.provider_for_revision(revision)
            if revision
            else owner.provider_for_new_job()
        )
        return diagnosis_provider.configured_provider(
            bound,
            observer=lambda reason: owner.record_call_result(bound.revision, reason),
        )

    async def run_job(self, payload: JSON) -> JSON:
        revision = str(payload.get("provider_revision") or "")
        if not revision:
            raise diagnosis_provider.ProviderUnavailable(
                "provider_unavailable",
                "Diagnosis Job has no provider revision",
            )
        request_id = str(payload.get("request_id") or "")
        checkpoint = (
            GovernedLoopCheckpoint(self._jobs, request_id, max_turns=self._max_turns)
            if request_id and self._jobs.get(request_id) is not None
            else None
        )
        if checkpoint is not None:
            completed = checkpoint.completed_result()
            if completed is not None:
                return completed
        try:
            return await run_diagnosis_job(
                payload,
                provider=self.resolve_provider(revision),
                **self._adapters,
                max_turns=self._max_turns,
                clock=self._clock,
                loop_checkpoint=checkpoint,
            )
        except ModelResponseError:
            self._model_provider().record_call_result(revision, "invalid_response")
            raise

    def execute_job(self, payload: JSON) -> JSON:
        return asyncio.run(self.run_job(payload))

    def verify_provider(self, provider: ProviderRevision, nonce: str) -> VerificationResult:
        owner = self._model_provider()
        configured = diagnosis_provider.configured_provider(
            provider,
            observer=lambda reason: owner.record_call_result(provider.revision, reason),
        )
        return diagnosis_provider.run_readiness_probe(configured, nonce)


async def run_diagnosis_job(
    payload: JSON,
    *,
    provider: Any,
    metrics_adapter: Adapter,
    logs_adapter: Adapter,
    k8s_read_adapter: Adapter,
    topology_adapter: Adapter,
    max_turns: int = 6,
    clock: Callable[[], float] = time.monotonic,
    loop_checkpoint: GovernedLoopCheckpoint | None = None,
) -> JSON:
    """Execute one already-persisted Job with an exact Provider binding."""
    revision = str(payload.get("provider_revision") or "")
    incident = incident_from_handoff(payload)
    if loop_checkpoint is not None:
        provider = loop_checkpoint.provider(provider)
        metrics_adapter = loop_checkpoint.adapter("query_metrics", metrics_adapter)
        logs_adapter = loop_checkpoint.adapter("query_logs", logs_adapter)
        k8s_read_adapter = loop_checkpoint.adapter("run_k8s_read", k8s_read_adapter)
        topology_adapter = loop_checkpoint.adapter("get_service_topology", topology_adapter)

    def authorize(tool: str, args: dict[str, Any], _evidence_refs: list[dict[str, Any]]) -> tuple[dict[str, Any], str | None]:
        binding, denied = capability_binding(tool, payload.get("capabilities"))
        if binding is not None:
            args = {**args, "_mcp": binding}
        return args, denied

    session = await run_diagnosis_session(
        incident,
        metrics_adapter=metrics_adapter,
        logs_adapter=logs_adapter,
        k8s_read_adapter=k8s_read_adapter,
        topology_adapter=topology_adapter,
        provider=provider,
        incident_store=False,
        max_turns=max_turns,
        clock=clock,
        tool_authorizer=authorize,
    )
    diagnosis = session.get("diagnosis")
    if isinstance(diagnosis, dict) and incident["human_input_event_ids"]:
        diagnosis["human_input_event_ids"] = incident["human_input_event_ids"]
    session["provider_revision"] = revision
    if loop_checkpoint is not None:
        loop_checkpoint.complete(session)
    return session
