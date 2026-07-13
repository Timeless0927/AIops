from __future__ import annotations

from pathlib import Path

import pytest

from diagnosis_service.model_provider import (
    ModelProviderConfiguration,
    ModelProviderError,
    VerificationResult,
)
from tests.model_provider_support import build_test_model_provider


def _configuration(tmp_path: Path) -> ModelProviderConfiguration:
    key = tmp_path / "key"
    key.write_bytes(b"k" * 32)
    return build_test_model_provider(
        tmp_path / "diagnosis.db",
        key,
        clock=lambda: 1_700_000_000.0,
    )


def test_saved_provider_revision_is_masked_encrypted_and_unverified(tmp_path: Path) -> None:
    configuration = _configuration(tmp_path)

    detail = configuration.save(
        {
            "endpoint": "https://models.example.test/v1",
            "endpoint_scope": "external",
            "model": "ops-model",
            "timeout_seconds": 30,
            "api_key": "secret-provider-key",
        },
        actor_id="user:admin",
    )

    assert detail["configuration"] == {
        "endpoint": "https://models.example.test/v1",
        "endpoint_scope": "external",
        "model": "ops-model",
        "timeout_seconds": 30,
        "credential_configured": True,
    }
    assert detail["verification"]["state"] == "unverified"
    assert detail["readiness"] == "not_ready"
    assert b"secret-provider-key" not in (tmp_path / "diagnosis.db").read_bytes()
    with pytest.raises(ModelProviderError, match="not ready"):
        configuration.provider_for_new_job()


def test_exact_revision_verification_is_durable_and_enables_new_jobs(tmp_path: Path) -> None:
    configuration = _configuration(tmp_path)
    revision = str(
        configuration.save(
            {
                "endpoint": "https://models.example.test/v1",
                "endpoint_scope": "external",
                "model": "ops-model",
                "timeout_seconds": 30,
                "api_key": "secret-provider-key",
            },
            actor_id="user:admin",
        )["configuration_revision"]
    )

    operation = configuration.start_verification(
        expected_revision=revision,
        actor_id="user:admin",
        operation_id="verify:request-1",
    )

    assert operation == {"operation_id": "verify:request-1", "revision": revision, "state": "verifying"}
    assert configuration.detail()["verification"]["operation_id"] == "verify:request-1"

    observed: list[tuple[str, str, str]] = []

    def probe(provider, nonce: str) -> VerificationResult:
        observed.append((provider.revision, provider.api_key, nonce))
        return VerificationResult.succeeded(latency_ms=42, provider_summary="OpenAI-compatible tool probe")

    assert configuration.run_verification_once(probe) is True
    detail = configuration.detail()
    assert detail["readiness"] == "ready"
    assert detail["verification"] == {
        "operation_id": "verify:request-1",
        "state": "verified",
        "revision": revision,
        "checked_at": 1_700_000_000.0,
        "reason_code": None,
    }
    assert detail["availability"] == {
        "state": "available",
        "observed_at": 1_700_000_000.0,
        "reason_code": None,
    }
    assert observed[0][0:2] == (revision, "secret-provider-key")
    assert len(observed[0][2]) >= 32
    assert configuration.provider_for_new_job().revision == revision


def test_configuration_change_stales_verification_but_keeps_frozen_revision(tmp_path: Path) -> None:
    configuration = _configuration(tmp_path)
    first = configuration.save(
        {
            "endpoint": "https://models.example.test/v1",
            "endpoint_scope": "external",
            "model": "ops-model-v1",
            "timeout_seconds": 30,
            "api_key": "first-provider-key",
        },
        actor_id="user:admin",
    )
    first_revision = str(first["configuration_revision"])
    configuration.start_verification(
        expected_revision=first_revision,
        actor_id="user:admin",
        operation_id="verify:first",
    )
    configuration.run_verification_once(
        lambda _provider, _nonce: VerificationResult.succeeded(
            latency_ms=10,
            provider_summary="verified",
        )
    )

    second = configuration.save(
        {
            "endpoint": "https://models.example.test/v1",
            "endpoint_scope": "external",
            "model": "ops-model-v2",
            "timeout_seconds": 45,
            "api_key": "second-provider-key",
        },
        actor_id="user:admin",
        expected_revision=first_revision,
    )

    assert second["verification"] == {
        "operation_id": "verify:first",
        "state": "stale",
        "revision": first_revision,
        "checked_at": 1_700_000_000.0,
        "reason_code": "configuration_changed",
    }
    assert second["availability"]["reason_code"] == "configuration_changed"
    assert configuration.provider_for_revision(first_revision).api_key == "first-provider-key"
    with pytest.raises(ModelProviderError, match="not ready"):
        configuration.provider_for_new_job()
    with pytest.raises(ModelProviderError, match="revision has changed"):
        configuration.start_verification(
            expected_revision=first_revision,
            actor_id="user:admin",
            operation_id="verify:stale",
        )


def test_runtime_error_taxonomy_updates_availability_without_false_verification(tmp_path: Path) -> None:
    configuration = _configuration(tmp_path)
    revision = str(
        configuration.save(
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
    configuration.start_verification(
        expected_revision=revision,
        actor_id="user:admin",
        operation_id="verify:taxonomy",
    )
    configuration.run_verification_once(
        lambda _provider, _nonce: VerificationResult.succeeded(
            latency_ms=10,
            provider_summary="verified",
        )
    )

    configuration.record_call_result(revision, "timeout")
    assert configuration.detail()["verification"]["state"] == "verified"
    assert configuration.detail()["availability"]["state"] == "degraded"

    configuration.record_call_result(revision, None)
    assert configuration.detail()["readiness"] == "ready"

    configuration.record_call_result(revision, "authentication_failed")
    assert configuration.detail()["verification"]["state"] == "failed"
    assert configuration.detail()["verification"]["reason_code"] == "authentication_failed"
    status = configuration.public_status()
    assert status["configuration"] == "present"
    assert "endpoint" not in str(status)
    assert "ops-model" not in str(status)


@pytest.mark.parametrize("reason_code", ["timeout", "rate_limited"])
def test_transient_retest_keeps_effective_verified_revision(
    tmp_path: Path,
    reason_code: str,
) -> None:
    configuration = _configuration(tmp_path)
    revision = str(
        configuration.save(
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
    configuration.start_verification(
        expected_revision=revision,
        actor_id="user:admin",
        operation_id="verify:initial",
    )
    configuration.run_verification_once(
        lambda _provider, _nonce: VerificationResult.succeeded(
            latency_ms=10,
            provider_summary="verified",
        )
    )
    configuration.start_verification(
        expected_revision=revision,
        actor_id="user:admin",
        operation_id="verify:transient-retest",
    )

    configuration.run_verification_once(
        lambda _provider, _nonce: VerificationResult.failed(reason_code)
    )

    detail = configuration.detail()
    assert detail["verification"]["state"] == "verified"
    assert detail["verification"]["operation_id"] == "verify:initial"
    assert detail["availability"] == {
        "state": "degraded",
        "observed_at": 1_700_000_000.0,
        "reason_code": reason_code,
    }
    assert detail["readiness"] == "not_ready"


def test_delete_removes_current_configuration_without_breaking_frozen_revision(tmp_path: Path) -> None:
    configuration = _configuration(tmp_path)
    revision = str(
        configuration.save(
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

    deleted = configuration.delete(expected_revision=revision)

    assert deleted["configuration"] is None
    assert deleted["configuration_revision"] is None
    assert configuration.public_status()["configuration"] == "absent"
    assert configuration.provider_for_revision(revision).api_key == "provider-key"
    with pytest.raises(ModelProviderError, match="not ready"):
        configuration.provider_for_new_job()


def test_verification_worker_reclaims_expired_operation_after_restart(tmp_path: Path) -> None:
    now = [1_700_000_000.0]
    key = tmp_path / "key"
    key.write_bytes(b"k" * 32)
    configuration = build_test_model_provider(tmp_path / "diagnosis.db", key, clock=lambda: now[0])
    revision = str(
        configuration.save(
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
    configuration.start_verification(
        expected_revision=revision,
        actor_id="user:admin",
        operation_id="verify:restart",
    )

    with pytest.raises(SystemExit):
        configuration.run_verification_once(
            lambda _provider, _nonce: (_ for _ in ()).throw(SystemExit("worker stopped")),
            lease_seconds=5,
        )
    now[0] += 6
    reopened = build_test_model_provider(tmp_path / "diagnosis.db", key, clock=lambda: now[0])

    assert reopened.run_verification_once(
        lambda _provider, _nonce: VerificationResult.succeeded(
            latency_ms=12,
            provider_summary="verified after restart",
        )
    ) is True
    assert reopened.detail()["verification"]["operation_id"] == "verify:restart"
    assert reopened.detail()["readiness"] == "ready"
