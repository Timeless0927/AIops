"""T09 Diagnosis-owned durable Job behavior."""

from __future__ import annotations

from pathlib import Path
import threading

import pytest

from diagnosis_service.jobs import DiagnosisJobError, DiagnosisJobs, start_workers
from diagnosis_service.diagnosis_provider import ProviderUnavailable


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def _request(request_id: str = "diagnosis-request-1") -> dict[str, object]:
    return {
        "request_id": request_id,
        "session_id": request_id,
        "incident_id": "incident-1",
        "investigation_id": "investigation-1",
        "source": "gateway",
        "alert": {
            "alertname": "HighErrorRate",
            "cluster": "cluster-prod",
            "namespace": "payments",
            "status": "firing",
        },
    }


def test_accept_persists_idempotent_job_before_returning(tmp_path: Path) -> None:
    db_path = tmp_path / "diagnosis.db"
    jobs = DiagnosisJobs(db_path)

    assert jobs.accept(_request()) == {"status": "accepted", "request_id": "diagnosis-request-1", "duplicate": False}
    assert jobs.accept(_request()) == {"status": "accepted", "request_id": "diagnosis-request-1", "duplicate": True}
    assert DiagnosisJobs(db_path).get("diagnosis-request-1")["status"] == "queued"  # type: ignore[index]

    conflicting = _request()
    conflicting["incident_id"] = "incident-other"
    with pytest.raises(DiagnosisJobError) as error:
        jobs.accept(conflicting)
    assert error.value.code == "request_conflict"


def test_accepted_job_resumes_once_after_store_restart(tmp_path: Path) -> None:
    db_path = tmp_path / "diagnosis.db"
    DiagnosisJobs(db_path).accept(_request())
    executions = 0

    def execute(payload: dict[str, object]) -> dict[str, object]:
        nonlocal executions
        executions += 1
        return {
            "session_id": payload["session_id"],
            "incident_id": payload["incident_id"],
            "status": "completed",
            "diagnosis": {"summary": "recovered after restart"},
        }

    reopened = DiagnosisJobs(db_path)
    assert reopened.run_execution_once(execute) is True
    assert reopened.run_execution_once(execute) is False
    assert executions == 1


def test_writeback_failure_does_not_repeat_completed_diagnosis(tmp_path: Path) -> None:
    jobs = DiagnosisJobs(tmp_path / "diagnosis.db", retry_base_seconds=0)
    jobs.accept(_request())
    executions = 0
    writebacks = 0

    def execute(payload: dict[str, object]) -> dict[str, object]:
        nonlocal executions
        executions += 1
        return {
            "session_id": payload["session_id"],
            "incident_id": payload["incident_id"],
            "status": "needs_human",
            "diagnosis": {"summary": "metrics unavailable"},
            "missing_evidence": [{"source_type": "prometheus"}],
            "state_transitions": ["running", "needs_human"],
        }

    def fail_writeback(_: dict[str, object]) -> tuple[int, dict[str, object]]:
        nonlocal writebacks
        writebacks += 1
        return 503, {"status": "unavailable"}

    assert jobs.run_execution_once(execute) is True
    assert jobs.run_writeback_once(fail_writeback) is True
    assert jobs.run_execution_once(execute) is False
    assert executions == 1
    assert jobs.get("diagnosis-request-1")["status"] == "completed"  # type: ignore[index]

    assert jobs.run_writeback_once(lambda _: (200, {"ok": True})) is True
    assert jobs.get("diagnosis-request-1")["writeback_status"] == "succeeded"  # type: ignore[index]
    assert executions == 1
    assert writebacks == 1


def test_cleanup_expires_only_terminal_jobs_safely_retained_by_gateway(tmp_path: Path) -> None:
    clock = Clock()
    jobs = DiagnosisJobs(tmp_path / "diagnosis.db", clock=clock)

    def complete(payload: dict[str, object]) -> dict[str, object]:
        return {
            "session_id": payload["session_id"],
            "incident_id": payload["incident_id"],
            "status": "completed",
            "diagnosis": {"summary": "retained by Gateway"},
        }

    jobs.accept(_request("old-retained"))
    jobs.run_execution_once(complete)
    jobs.run_writeback_once(lambda _: (200, {"ok": True}))
    jobs.accept(_request("old-pending-writeback"))
    jobs.run_execution_once(complete)

    clock.now += 30 * 24 * 60 * 60 + 1
    jobs.accept(_request("recent-retained"))
    jobs.run_execution_once(complete)
    jobs.run_writeback_once(lambda _: (503, {"status": "unavailable"}))
    jobs.run_writeback_once(lambda _: (200, {"ok": True}))

    assert jobs.cleanup_expired() == 1
    assert jobs.get("old-retained") is None
    assert jobs.get("old-pending-writeback") is not None
    assert jobs.get("recent-retained") is not None


def test_diagnosis_workers_run_periodic_cleanup_before_execution() -> None:
    stop = threading.Event()

    class Jobs:
        cleanup_calls = 0

        def cleanup_expired(self) -> None:
            self.cleanup_calls += 1

        def run_execution_once(self, _runner) -> bool:
            stop.set()
            return False

        def run_writeback_once(self, _sender) -> bool:
            return False

    jobs = Jobs()
    workers = start_workers(
        jobs,  # type: ignore[arg-type]
        runner=lambda _: {},
        sender=lambda _: (200, {}),
        interval_seconds=0.01,
        stop_event=stop,
    )
    for worker in workers:
        worker.join(timeout=1)

    assert jobs.cleanup_calls == 1


def test_expired_execution_leases_stop_at_bounded_attempts(tmp_path: Path) -> None:
    clock = Clock()
    db_path = tmp_path / "diagnosis.db"
    jobs = DiagnosisJobs(db_path, clock=clock, execution_lease_seconds=1, max_execution_attempts=2)
    jobs.accept(_request())

    def crash(_: dict[str, object]) -> dict[str, object]:
        raise SystemExit("process crashed")

    with pytest.raises(SystemExit):
        jobs.run_execution_once(crash)
    clock.now += 2
    with pytest.raises(SystemExit):
        DiagnosisJobs(
            db_path,
            clock=clock,
            execution_lease_seconds=1,
            max_execution_attempts=2,
        ).run_execution_once(crash)

    clock.now += 2
    executions = 0

    def should_not_run(_: dict[str, object]) -> dict[str, object]:
        nonlocal executions
        executions += 1
        return {}

    assert jobs.run_execution_once(should_not_run) is True
    assert executions == 0
    assert jobs.get("diagnosis-request-1")["status"] == "failed"  # type: ignore[index]
    assert jobs.get("diagnosis-request-1")["writeback_status"] == "pending"  # type: ignore[index]


def test_result_must_match_persisted_job_identity(tmp_path: Path) -> None:
    jobs = DiagnosisJobs(tmp_path / "diagnosis.db", max_execution_attempts=1)
    jobs.accept(_request())

    jobs.run_execution_once(
        lambda _: {
            "session_id": "another-session",
            "incident_id": "incident-1",
            "status": "completed",
            "diagnosis": {"summary": "wrong job"},
        }
    )

    job = jobs.get("diagnosis-request-1")
    assert job["status"] == "failed"  # type: ignore[index]
    assert "does not match" in str(job["error"])  # type: ignore[index]


def test_terminal_provider_failure_writeback_keeps_partial_evidence_steps(tmp_path: Path) -> None:
    jobs = DiagnosisJobs(tmp_path / "diagnosis.db")
    jobs.accept(_request())

    def fail_after_evidence(_: dict[str, object]) -> dict[str, object]:
        failure = ProviderUnavailable("provider_unavailable", "endpoint down")
        failure.partial_result = {
            "status": "failed",
            "steps": [
                {
                    "tool": "query_metrics",
                    "status": "succeeded",
                    "evidence_ref": "evidence:metrics:1",
                }
            ],
            "tool_activity": [
                {
                    "tool": "query_metrics",
                    "status": "succeeded",
                    "evidence_ref": "evidence:metrics:1",
                }
            ],
            "missing_evidence": [],
            "state_transitions": ["running", "failed"],
        }
        raise failure

    assert jobs.run_execution_once(fail_after_evidence) is True

    result = jobs.export("diagnosis-request-1")
    assert result is not None
    assert result["status"] == "failed"
    assert result["steps"][0]["evidence_ref"] == "evidence:metrics:1"  # type: ignore[index]
    assert result["tool_activity"][0]["tool"] == "query_metrics"  # type: ignore[index]
    writebacks: list[dict[str, object]] = []
    assert jobs.run_writeback_once(
        lambda payload: (writebacks.append(payload) or 200, {"ok": True})
    ) is True
    assert writebacks[0]["steps"] == result["steps"]
    assert writebacks[0]["tool_activity"] == result["tool_activity"]


def test_provider_failure_ends_frozen_revision_without_automatic_retry(tmp_path: Path) -> None:
    jobs = DiagnosisJobs(tmp_path / "diagnosis.db", retry_base_seconds=0)
    jobs.accept(_request(), provider_revision="model-provider:revision-1")
    executions = 0

    def fail_provider(payload: dict[str, object]) -> dict[str, object]:
        nonlocal executions
        executions += 1
        assert payload["provider_revision"] == "model-provider:revision-1"
        raise ProviderUnavailable("rate_limited", "provider rate limited the request")

    assert jobs.run_execution_once(fail_provider) is True
    assert jobs.run_execution_once(fail_provider) is False

    job = jobs.get("diagnosis-request-1")
    assert job["status"] == "failed"  # type: ignore[index]
    assert job["writeback_status"] == "pending"  # type: ignore[index]
    assert job["provider_revision"] == "model-provider:revision-1"  # type: ignore[index]
    assert executions == 1
