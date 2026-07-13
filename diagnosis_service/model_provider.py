"""Diagnosis-owned Model Provider configuration and readiness state."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlparse

from diagnosis_service.model_provider_crypto import CredentialCipher
from diagnosis_service.model_provider_repository import ModelProviderRepository, RepositoryConflict


JSON = dict[str, object]


class ModelProviderError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True)
class ProviderRevision:
    revision: str
    endpoint: str
    endpoint_scope: str
    model: str
    timeout_seconds: int
    api_key: str = field(repr=False)


@dataclass(frozen=True)
class VerificationResult:
    ok: bool
    reason_code: str | None
    latency_ms: int
    provider_summary: str

    @classmethod
    def succeeded(cls, *, latency_ms: int, provider_summary: str) -> VerificationResult:
        return cls(True, None, latency_ms, provider_summary)

    @classmethod
    def failed(
        cls,
        reason_code: str,
        *,
        latency_ms: int = 0,
        provider_summary: str = "",
    ) -> VerificationResult:
        return cls(False, reason_code, latency_ms, provider_summary)


class ModelProviderConfiguration:
    """Owns immutable provider revisions and exposes masked readiness projections."""

    def __init__(
        self,
        repository: ModelProviderRepository,
        cipher: CredentialCipher,
        *,
        clock: Callable[[], float],
        revision_id: Callable[[], str],
        verification_nonce: Callable[[], str],
    ) -> None:
        self.db_path = repository.db_path
        self._repository = repository
        self._cipher = cipher
        self._clock = clock
        self._revision_id = revision_id
        self._verification_nonce = verification_nonce

    def save(
        self,
        payload: JSON,
        *,
        actor_id: str,
        expected_revision: str | None = None,
        operation_id: str | None = None,
    ) -> JSON:
        endpoint, endpoint_scope, model, timeout_seconds, api_key = _validate_configuration(payload)
        mutation_hash = _hash(
            {
                "action": "save",
                "endpoint": endpoint,
                "endpoint_scope": endpoint_scope,
                "model": model,
                "timeout_seconds": timeout_seconds,
                "api_key": api_key,
                "expected_revision": expected_revision,
            }
        )
        revision = self._revision_id()
        now = self._clock()
        try:
            self._repository.save_revision(
                {
                    "revision": revision,
                    "endpoint": endpoint,
                    "endpoint_scope": endpoint_scope,
                    "model": model,
                    "timeout_seconds": timeout_seconds,
                    "credential_ciphertext": self._cipher.encrypt(api_key),
                    "actor_id": actor_id,
                    "created_at": now,
                },
                expected_revision=expected_revision,
                operation_id=operation_id,
                mutation_hash=mutation_hash,
            )
        except RepositoryConflict as exc:
            raise _repository_error(exc) from exc
        return self.detail()

    def detail(self) -> JSON:
        row, verification, availability, effective = self._repository.snapshot()
        if (
            verification is not None
            and str(verification["state"]) == "failed"
            and _is_transient_reason(str(verification["reason_code"] or ""))
            and effective is not None
        ):
            verification = effective
        if row is None:
            return {
                "readiness": "not_ready",
                "configuration_revision": None,
                "configuration": None,
                "verification": _unverified(None, "not_applicable"),
                "availability": {"state": "unavailable", "observed_at": None, "reason_code": "not_configured"},
            }
        revision = str(row["revision"])
        verification_view = _verification_view(verification, revision)
        availability_view = _availability_view(availability, revision, verification_view)
        ready = verification_view["state"] == "verified" and availability_view["state"] == "available"
        return {
            "readiness": "ready" if ready else "not_ready",
            "configuration_revision": revision,
            "configuration": {
                "endpoint": str(row["endpoint"]),
                "endpoint_scope": str(row["endpoint_scope"]),
                "model": str(row["model"]),
                "timeout_seconds": int(row["timeout_seconds"]),
                "credential_configured": True,
            },
            "verification": verification_view,
            "availability": availability_view,
        }

    def start_verification(
        self,
        *,
        expected_revision: str,
        actor_id: str,
        operation_id: str,
    ) -> JSON:
        now = self._clock()
        try:
            existing_state = self._repository.start_verification(
                {
                    "operation_id": operation_id,
                    "revision": expected_revision,
                    "nonce": self._verification_nonce(),
                    "actor_id": actor_id,
                    "created_at": now,
                }
            )
        except RepositoryConflict as exc:
            raise _repository_error(exc) from exc
        if existing_state is not None:
            return {
                "operation_id": operation_id,
                "revision": expected_revision,
                "state": _public_verification_state(existing_state),
            }
        return {"operation_id": operation_id, "revision": expected_revision, "state": "verifying"}

    def delete(self, *, expected_revision: str, operation_id: str | None = None) -> JSON:
        mutation_hash = _hash({"action": "delete", "expected_revision": expected_revision})
        try:
            self._repository.delete_current(
                expected_revision=expected_revision,
                operation_id=operation_id,
                mutation_hash=mutation_hash,
                now=self._clock(),
            )
        except RepositoryConflict as exc:
            raise _repository_error(exc) from exc
        return self.detail()

    def run_verification_once(
        self,
        probe: Callable[[ProviderRevision, str], VerificationResult],
        *,
        lease_seconds: float = 300.0,
    ) -> bool:
        now = self._clock()
        row = self._repository.claim_verification(
            now=now,
            lease_until=now + max(1.0, lease_seconds),
        )
        if row is None:
            return False
        operation_id = str(row["operation_id"])
        try:
            result = probe(self.provider_for_revision(str(row["revision"])), str(row["nonce"]))
            if not isinstance(result, VerificationResult):
                raise TypeError("Model Provider probe must return VerificationResult")
        except Exception:
            result = VerificationResult.failed("provider_unavailable")
        checked_at = self._clock()
        state = "verified" if result.ok else "failed"
        reason = None if result.ok else bounded_reason_code(result.reason_code)
        availability_state = _availability_state(reason)
        self._repository.complete_verification(
            operation_id,
            {
                "revision": str(row["revision"]),
                "state": state,
                "reason_code": reason,
                "checked_at": checked_at,
                "latency_ms": max(0, result.latency_ms),
                "provider_summary": result.provider_summary[:200],
                "availability_state": availability_state,
            },
        )
        return True

    def provider_for_new_job(self) -> ProviderRevision:
        detail = self.detail()
        if detail["readiness"] != "ready":
            raise ModelProviderError("provider_not_ready", "Model Provider is not ready")
        return self.provider_for_revision(str(detail["configuration_revision"]))

    def provider_for_revision(self, revision: str) -> ProviderRevision:
        row = self._repository.revision(revision)
        if row is None:
            raise ModelProviderError("revision_not_found", "Model Provider revision was not found")
        return ProviderRevision(
            revision=str(row["revision"]),
            endpoint=str(row["endpoint"]),
            endpoint_scope=str(row["endpoint_scope"]),
            model=str(row["model"]),
            timeout_seconds=int(row["timeout_seconds"]),
            api_key=self._cipher.decrypt(str(row["credential_ciphertext"])),
        )

    def record_call_result(self, revision: str, reason_code: str | None) -> None:
        reason = None if reason_code is None else bounded_reason_code(reason_code)
        now = self._clock()
        self._repository.record_availability(
            {
                "revision": revision,
                "availability_state": _availability_state(reason),
                "observed_at": now,
                "reason_code": reason,
            },
            invalidate_verification=reason in {"authentication_failed", "provider_rejected"},
        )

    def public_status(self) -> JSON:
        detail = self.detail()
        return {
            "readiness": detail["readiness"],
            "configuration": "present" if detail["configuration_revision"] is not None else "absent",
            "configuration_revision": detail["configuration_revision"],
            "verification": detail["verification"],
            "availability": detail["availability"],
        }

def _unverified(revision: str | None, state: str) -> JSON:
    return {
        "operation_id": None,
        "state": state,
        "revision": revision,
        "checked_at": None,
        "reason_code": None if state == "not_applicable" else "test_required",
    }


def _verification_view(row: JSON | None, revision: str) -> JSON:
    if row is None:
        return _unverified(revision, "unverified")
    operation_id = str(row["operation_id"])
    row_revision = str(row["revision"])
    if row_revision != revision:
        return {
            "operation_id": operation_id,
            "state": "stale",
            "revision": row_revision,
            "checked_at": row["checked_at"],
            "reason_code": "configuration_changed",
        }
    state = _public_verification_state(str(row["state"]))
    return {
        "operation_id": operation_id,
        "state": state,
        "revision": row_revision,
        "checked_at": row["checked_at"],
        "reason_code": row["reason_code"],
    }


def _availability_view(row: JSON | None, revision: str, verification: JSON) -> JSON:
    if row is None:
        reason = "test_required" if verification["state"] != "stale" else "configuration_changed"
        return {"state": "unavailable", "observed_at": None, "reason_code": reason}
    if str(row["revision"]) != revision:
        return {"state": "unavailable", "observed_at": row["observed_at"], "reason_code": "configuration_changed"}
    return {
        "state": str(row["state"]),
        "observed_at": float(row["observed_at"]),
        "reason_code": row["reason_code"],
    }


def _public_verification_state(state: str) -> str:
    return "verifying" if state in {"queued", "running"} else state


def bounded_reason_code(reason: str | None) -> str:
    allowed = {
        "authentication_failed",
        "rate_limited",
        "timeout",
        "provider_unavailable",
        "provider_rejected",
        "invalid_response",
    }
    return reason if reason in allowed else "invalid_response"


def _availability_state(reason: str | None) -> str:
    if reason is None:
        return "available"
    if reason in {"rate_limited", "timeout"}:
        return "degraded"
    return "unavailable"


def _is_transient_reason(reason: str) -> bool:
    return reason in {"rate_limited", "timeout", "provider_unavailable"}


def _validate_configuration(payload: JSON) -> tuple[str, str, str, int, str]:
    allowed = {"endpoint", "endpoint_scope", "model", "timeout_seconds", "api_key"}
    if set(payload) - allowed:
        raise ModelProviderError("invalid_configuration", "unsupported Model Provider field")
    endpoint = _text(payload, "endpoint", 2048).rstrip("/")
    endpoint_scope = str(payload.get("endpoint_scope") or "")
    parsed = urlparse(endpoint)
    if endpoint_scope not in {"external", "cluster_internal"}:
        raise ModelProviderError("invalid_configuration", "endpoint_scope must be external or cluster_internal")
    if not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ModelProviderError("invalid_configuration", "invalid Model Provider endpoint")
    if endpoint_scope == "external" and parsed.scheme != "https":
        raise ModelProviderError("invalid_configuration", "external Model Provider endpoint must use HTTPS")
    if endpoint_scope == "cluster_internal" and (
        parsed.scheme not in {"http", "https"}
        or not (parsed.hostname.endswith(".svc") or parsed.hostname.endswith(".svc.cluster.local"))
    ):
        raise ModelProviderError("invalid_configuration", "cluster-internal endpoint must use Kubernetes service DNS")
    model = _text(payload, "model", 200)
    api_key = _text(payload, "api_key", 4096)
    timeout_seconds = payload.get("timeout_seconds")
    if not isinstance(timeout_seconds, int) or isinstance(timeout_seconds, bool) or not 5 <= timeout_seconds <= 120:
        raise ModelProviderError("invalid_configuration", "timeout_seconds must be an integer between 5 and 120")
    return endpoint, endpoint_scope, model, timeout_seconds, api_key


def _text(payload: JSON, field: str, maximum: int) -> str:
    value = payload.get(field)
    if not isinstance(value, str) or not value.strip() or len(value.strip()) > maximum:
        raise ModelProviderError("invalid_configuration", f"{field} is required")
    return value.strip()


def _hash(value: JSON) -> str:
    canonical = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()


def _repository_error(error: RepositoryConflict) -> ModelProviderError:
    messages = {
        "not_configured": "Model Provider is not configured",
        "revision_conflict": "Model Provider revision has changed",
        "operation_conflict": "operation_id is already used",
    }
    return ModelProviderError(error.code, messages.get(error.code, "Model Provider persistence failed"))
