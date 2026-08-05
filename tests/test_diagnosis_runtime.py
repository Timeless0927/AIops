"""Diagnosis runtime revision and Provider failure behavior."""

from __future__ import annotations

from pathlib import Path

import pytest

from diagnosis_service.diagnosis_provider import ProviderResult, ScriptedProvider
from diagnosis_service.jobs import DiagnosisJobs
from diagnosis_service.model_provider import VerificationResult
from tests.model_provider_support import build_test_model_provider
from diagnosis_service.runtime import DiagnosisRuntime, run_diagnosis_job


async def _unused_adapter(_args):
    raise AssertionError("unexpected evidence tool call")


def _payload(revision: str) -> dict[str, object]:
    return {
        "request_id": "diagnosis-runtime-1",
        "session_id": "diagnosis-runtime-1",
        "incident_id": "incident-1",
        "investigation_id": "investigation-1",
        "source": "gateway",
        "provider_revision": revision,
        "alert": {
            "alertname": "HighErrorRate",
            "cluster": "cluster-prod",
            "namespace": "payments",
            "status": "firing",
        },
    }


class _FinalProvider:
    async def chat_with_tools(self, _messages, _tools):
        return ProviderResult(
            {
                "role": "assistant",
                "content": (
                    '{"root_cause_candidates":[{"cause":"证据不足",'
                    '"confidence":0,"evidence_refs":[]}],'
                    '"recommended_actions":[],"confidence":{"score":0,"level":"low"}}'
                ),
            },
            [],
            "stop",
            {},
        )


@pytest.mark.asyncio
async def test_investigation_baselines_require_registry_capabilities() -> None:
    calls: list[dict[str, object]] = []

    async def must_not_run(args):
        calls.append(args)
        raise AssertionError("unverified baseline executed")

    result = await run_diagnosis_job(
        {**_payload("model-provider:verified"), "capabilities": {}},
        provider=_FinalProvider(), metrics_adapter=must_not_run, logs_adapter=must_not_run,
        k8s_read_adapter=must_not_run, topology_adapter=must_not_run, max_turns=1,
    )

    assert calls == []
    assert [item["status"] for item in result["tool_activity"][:2]] == ["skipped", "skipped"]
    assert all(
        item["summary"] == "tool capability is not enabled"
        for item in result["tool_activity"][:2]
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "content",
    [
        "not valid JSON",
        '{"root_cause_candidates":[],"recommended_actions":[],"confidence":"high"}',
    ],
)
async def test_invalid_final_json_returns_safe_completion_without_poisoning_provider(
    tmp_path: Path,
    content: str,
    **_: object,
) -> None:
    key = tmp_path / "model-key"
    key.write_bytes(b"k" * 32)
    owner = build_test_model_provider(tmp_path / "diagnosis.db", key)
    revision = str(
        owner.save(
            {
                "endpoint": "https://models.example.test/v1",
                "endpoint_scope": "external",
                "model": "ops-model",
                "timeout_seconds": 30,
                "api_key": "provider-key",
            },
            actor_id="user:admin",
        )["configuration_revision"]
    )
    owner.start_verification(
        expected_revision=revision,
        actor_id="user:admin",
        operation_id="verify:runtime",
    )
    owner.run_verification_once(
        lambda _provider, _nonce: VerificationResult.succeeded(
            latency_ms=10,
            provider_summary="verified",
        )
    )
    runtime = DiagnosisRuntime(
        DiagnosisJobs(tmp_path / "diagnosis.db"),
        lambda: owner,
        metrics_adapter=_unused_adapter,
        logs_adapter=_unused_adapter,
        k8s_read_adapter=_unused_adapter,
        topology_adapter=_unused_adapter,
    )
    provider = ScriptedProvider(
        [
            {
                "choices": [
                    {
                        "finish_reason": "stop",
                        "message": {"role": "assistant", "content": content},
                    }
                ]
            }
        ]
    )
    runtime.resolve_provider = lambda _revision=None: provider  # type: ignore[method-assign]

    result = await runtime.run_job(_payload(revision))

    detail = owner.detail()
    assert result["completion_validation"]["status"] == "safe_partial"
    assert result["diagnosis"]["recommended_actions"] == []
    assert detail["availability"]["state"] == "available"
    assert detail["availability"]["reason_code"] is None
