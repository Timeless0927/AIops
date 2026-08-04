"""Durable, safe-boundary checkpoints for one Diagnosis tool loop."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, is_dataclass
from typing import Any, Awaitable, Callable

from diagnosis_service.diagnosis_provider import ProviderResult, ToolCall
from diagnosis_service.jobs import DiagnosisJobs
from toolsets.diagnosis_observation import normalize_observation
from toolsets.diagnosis_session import MAX_EVIDENCE_STEPS_TOTAL, MAX_TOOL_CALLS_PER_TURN


JSON = dict[str, Any]
Adapter = Callable[[JSON], Awaitable[Any]]
_SENSITIVE = re.compile(
    r"authorization|credential|password|secret|token|api[_-]?key|secure[_-]?input|system[_-]?prompt|chain[_-]?of[_-]?thought|raw[_-]?payload",
    re.IGNORECASE,
)
_SOURCE_TYPES = {
    "query_metrics": "metrics",
    "query_logs": "logs",
    "run_k8s_read": "k8s_read",
    "get_service_topology": "topology",
}
_ALLOWED_ARGS = {
    "query_metrics": {"cluster_id", "namespace", "service", "query", "start", "end", "step"},
    "query_logs": {"cluster_id", "namespace", "service", "query", "time_range", "max_lines"},
    "run_k8s_read": {"cluster_id", "namespace", "argv", "selector"},
    "get_service_topology": {"cluster_id", "namespace", "service"},
}


class DiagnosisLoopCheckpoint:
    """Wrap model/tool calls so accepted work survives a process restart."""

    def __init__(self, jobs: DiagnosisJobs, request_id: str, *, max_turns: int) -> None:
        self._jobs = jobs
        self._request_id = request_id
        self._max_turns = max_turns
        self._state = jobs.load_loop_checkpoint(request_id) or {
            "version": 1,
            "model_turn": 0,
            "model_turns": [],
            "tool_calls": {},
            "proposed_tool_calls": [],
            "normalized_observations": [],
            "evidence_references": [],
            "remaining_budget": {"model_turns": max_turns, "tool_calls": MAX_EVIDENCE_STEPS_TOTAL},
            "completion_state": "running",
        }
        self._provider_turn = 0
        self._tool_index = 0

    def completed_result(self) -> JSON | None:
        result = self._state.get("result")
        if self._state.get("completion_state") != "completed" or not isinstance(result, dict):
            return None
        return json.loads(json.dumps(result, ensure_ascii=False))

    def provider(self, provider: Any) -> Any:
        checkpoint = self

        class CheckpointedProvider:
            async def chat_with_tools(self, messages: list[JSON], tools: list[JSON]) -> ProviderResult:
                return await checkpoint._model_turn(provider, messages, tools)

        return CheckpointedProvider()

    def adapter(self, tool: str, adapter: Adapter | None) -> Adapter | None:
        if adapter is None:
            return None

        async def checkpointed(args: JSON) -> Any:
            return await self._tool_call(tool, args, adapter)

        return checkpointed

    def complete(self, result: JSON) -> None:
        self._state["completion_state"] = "completed"
        self._state["result"] = result
        self._sync_summary()
        self._save()

    async def _model_turn(self, provider: Any, messages: list[JSON], tools: list[JSON]) -> ProviderResult:
        self._provider_turn += 1
        stored_turns = self._state.get("model_turns")
        if not isinstance(stored_turns, list):
            stored_turns = []
            self._state["model_turns"] = stored_turns
        if self._provider_turn <= len(stored_turns):
            stored = stored_turns[self._provider_turn - 1]
            calls = stored.get("tool_calls") if isinstance(stored, dict) else None
            if isinstance(calls, list) and calls:
                return _replayed_model_turn(calls)

        self._state["completion_state"] = "model_in_flight"
        self._state["model_turn"] = self._provider_turn
        self._sync_summary()
        self._save()
        result = await provider.chat_with_tools(messages, tools)
        calls = [_safe_proposal(call) for call in result.tool_calls[:MAX_TOOL_CALLS_PER_TURN]]
        turn = {
            "turn": self._provider_turn,
            "completion_state": "tool_proposed" if calls else "final_proposed",
            "tool_calls": calls,
        }
        if self._provider_turn <= len(stored_turns):
            stored_turns[self._provider_turn - 1] = turn
        else:
            stored_turns.append(turn)
        self._state["completion_state"] = turn["completion_state"]
        self._sync_summary()
        self._save()
        return result

    async def _tool_call(self, tool: str, args: JSON, adapter: Adapter) -> Any:
        self._tool_index += 1
        call_key = _call_key(self._tool_index, tool, args)
        calls = self._state.setdefault("tool_calls", {})
        assert isinstance(calls, dict)
        existing = calls.get(call_key)
        if isinstance(existing, dict) and existing.get("state") == "completed":
            return _envelope_from_observation(existing["observation"])
        if isinstance(existing, dict) and existing.get("state") == "in_flight":
            observation = await _unknown_observation(tool, args)
            existing.update({"state": "completed", "observation": observation})
            self._state["completion_state"] = "needs_human"
            self._sync_summary()
            self._save()
            return _envelope_from_observation(observation)

        calls[call_key] = {
            "sequence": self._tool_index,
            "state": "in_flight",
            "proposal": _safe_call(tool, args, call_key),
        }
        self._state["completion_state"] = "tool_in_flight"
        self._sync_summary()
        self._save()
        try:
            envelope = await adapter(args)
        except Exception as exc:
            observation = await _failed_observation(tool, args, exc)
            calls[call_key].update({"state": "completed", "observation": observation})
            self._state["completion_state"] = "running"
            self._sync_summary()
            self._save()
            raise
        observation = await _normalized_envelope(tool, args, envelope)
        calls[call_key].update({"state": "completed", "observation": observation})
        self._state["completion_state"] = "running"
        self._sync_summary()
        self._save()
        return envelope

    def _sync_summary(self) -> None:
        calls = self._state.get("tool_calls")
        records = sorted(
            calls.values(),
            key=lambda record: int(record.get("sequence") or 0),
        ) if isinstance(calls, dict) else []
        observations = [
            record["observation"]
            for record in records
            if isinstance(record, dict) and isinstance(record.get("observation"), dict)
        ]
        refs = [
            str(observation["evidence_ref"])
            for observation in observations
            if observation.get("evidence_ref")
        ]
        proposals = [
            record["proposal"]
            for record in records
            if isinstance(record, dict) and isinstance(record.get("proposal"), dict)
        ]
        self._state["normalized_observations"] = observations
        self._state["evidence_references"] = list(dict.fromkeys(refs))
        self._state["proposed_tool_calls"] = proposals
        self._state["remaining_budget"] = {
            "model_turns": max(0, self._max_turns - int(self._state.get("model_turn") or 0)),
            "tool_calls": max(0, MAX_EVIDENCE_STEPS_TOTAL - len(records)),
        }

    def _save(self) -> None:
        self._jobs.save_loop_checkpoint(self._request_id, self._state)


def _replayed_model_turn(calls: list[Any]) -> ProviderResult:
    tool_calls = [
        ToolCall(str(call["id"]), str(call["name"]), dict(call.get("arguments") or {}))
        for call in calls
        if isinstance(call, dict) and call.get("id") and call.get("name")
    ]
    return ProviderResult(
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": call.id,
                    "type": "function",
                    "function": {
                        "name": call.name,
                        "arguments": json.dumps(call.arguments, ensure_ascii=False),
                    },
                }
                for call in tool_calls
            ],
        },
        tool_calls,
        "tool_calls",
        {},
    )


def _safe_proposal(call: ToolCall) -> JSON:
    return {
        "id": str(call.id)[:160],
        "name": str(call.name)[:160],
        "arguments": _safe_args(call.name, call.arguments),
    }


def _safe_call(tool: str, args: JSON, call_key: str) -> JSON:
    return {"id": call_key, "name": tool[:160], "arguments": _safe_args(tool, args)}


def _safe_args(tool: str, args: JSON) -> JSON:
    allowed = _ALLOWED_ARGS.get(tool, set())
    return {
        key: _safe_value(value)
        for key, value in args.items()
        if key in allowed and not _SENSITIVE.search(key)
    }


def _safe_value(value: Any) -> Any:
    if isinstance(value, str):
        return "[REDACTED]" if _SENSITIVE.search(value) else value[:512]
    if isinstance(value, dict):
        return {
            str(key): _safe_value(child)
            for key, child in list(value.items())[:16]
            if not _SENSITIVE.search(str(key))
        }
    if isinstance(value, list):
        return [_safe_value(child) for child in value[:32]]
    return value if isinstance(value, (int, float, bool)) or value is None else str(value)[:512]


def _call_key(index: int, tool: str, args: JSON) -> str:
    stable_args = _safe_args(tool, args)
    stable_args.pop("start", None)
    stable_args.pop("end", None)
    encoded = json.dumps([index, tool, stable_args], ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


async def _normalized_envelope(tool: str, args: JSON, envelope: Any) -> JSON:
    data = _mapping(envelope)
    evidence_ref = _evidence_ref(data)
    return await normalize_observation(
        {
            "tool": tool,
            "status": str(data.get("status") or "failed"),
            "source_type": _SOURCE_TYPES.get(tool, tool),
            "evidence_ref": evidence_ref,
            "summary": str(data.get("summary") or f"{tool} returned {data.get('status') or 'failed'}"),
            "missing_reason": None if evidence_ref else _error(data),
            "payload": dict(data.get("data") or {}),
            "audit": dict(data.get("audit") or {}),
        },
        args,
    )


async def _unknown_observation(tool: str, args: JSON) -> JSON:
    reason = "tool outcome is unknown after restart; reconciliation required"
    return await normalize_observation(
        {
            "tool": tool,
            "status": "failed",
            "source_type": _SOURCE_TYPES.get(tool, tool),
            "evidence_ref": None,
            "summary": reason,
            "missing_reason": reason,
            "payload": {},
            "audit": {"status": "failed", "tool_name": tool, "reason_code": "unknown_tool_outcome"},
        },
        args,
    )


async def _failed_observation(tool: str, args: JSON, exc: Exception) -> JSON:
    reason = f"{tool} adapter raised {type(exc).__name__}: {exc}"[:256]
    return await normalize_observation(
        {
            "tool": tool,
            "status": "failed",
            "source_type": _SOURCE_TYPES.get(tool, tool),
            "evidence_ref": None,
            "summary": reason,
            "missing_reason": reason,
            "payload": {},
            "audit": {"status": "failed", "tool_name": tool, "reason_code": "adapter_error"},
        },
        args,
    )


def _envelope_from_observation(observation: JSON) -> JSON:
    facts = observation.get("key_facts")
    data = {
        str(fact["name"]): fact.get("value")
        for fact in facts or []
        if isinstance(fact, dict) and fact.get("name")
    }
    samples = observation.get("representative_samples")
    if isinstance(samples, list) and samples:
        data["samples"] = samples
    evidence_ref = observation.get("evidence_ref")
    return {
        "status": observation.get("status") or "failed",
        "summary": observation.get("summary") or "checkpointed tool result",
        "data": data,
        "evidence_refs": [{"ref_id": evidence_ref}] if evidence_ref else [],
        "errors": [] if evidence_ref else [{"message": observation.get("missing_reason") or observation.get("summary")}],
        "audit": {"reason_code": "checkpoint_replay"},
    }


def _mapping(value: Any) -> JSON:
    if isinstance(value, dict):
        return value
    if is_dataclass(value):
        return asdict(value)
    return {
        "status": getattr(value, "status", "failed"),
        "summary": getattr(value, "summary", ""),
        "data": getattr(value, "data", {}),
        "evidence_refs": getattr(value, "evidence_refs", ()),
        "audit": getattr(value, "audit", {}),
        "errors": getattr(value, "errors", ()),
    }


def _evidence_ref(data: JSON) -> str | None:
    refs = data.get("evidence_refs") or ()
    if not refs:
        return None
    first = refs[0]
    if isinstance(first, dict):
        return str(first.get("ref_id") or "") or None
    return str(getattr(first, "ref_id", "") or "") or None


def _error(data: JSON) -> str:
    errors = data.get("errors") or ()
    if errors:
        first = errors[0]
        if isinstance(first, dict):
            return str(first.get("message") or data.get("summary") or "tool failed")[:256]
        return str(getattr(first, "message", data.get("summary") or "tool failed"))[:256]
    return str(data.get("summary") or "tool returned no Evidence")[:256]
