from http import HTTPStatus

import json

import pytest

from apps.internal_auth import (
    INTERNAL_AUDIENCE,
    authorize_request,
    enforce_internal_auth,
    internal_auth_headers,
)


def test_internal_request_accepts_expected_service_account() -> None:
    reviewed: list[tuple[str, tuple[str, ...]]] = []

    def review(token: str, audiences: tuple[str, ...]) -> dict[str, object]:
        reviewed.append((token, audiences))
        return {
            "authenticated": True,
            "audiences": [INTERNAL_AUDIENCE],
            "username": "system:serviceaccount:aiops-dev:aiops-diagnosis",
        }

    result = authorize_request(
        {"Authorization": "Bearer projected-token"},
        namespace="aiops-dev",
        allowed_service_account="aiops-diagnosis",
        review_token=review,
    )

    assert result.status == HTTPStatus.OK
    assert result.identity == "system:serviceaccount:aiops-dev:aiops-diagnosis"
    assert reviewed == [("projected-token", ("aiops-internal",))]


@pytest.mark.parametrize(
    ("headers", "review", "expected_status", "expected_reason"),
    [
        ({}, None, HTTPStatus.UNAUTHORIZED, "missing_bearer_token"),
        (
            {"Authorization": "Bearer forged"},
            {"authenticated": False},
            HTTPStatus.UNAUTHORIZED,
            "invalid_token",
        ),
        (
            {"Authorization": "Bearer valid-other-service"},
            {
                "authenticated": True,
                "audiences": [INTERNAL_AUDIENCE],
                "username": "system:serviceaccount:aiops-dev:aiops-gateway",
            },
            HTTPStatus.FORBIDDEN,
            "service_account_forbidden",
        ),
    ],
)
def test_internal_request_fails_closed(
    headers: dict[str, str],
    review: dict[str, object] | None,
    expected_status: HTTPStatus,
    expected_reason: str,
) -> None:
    def reviewer(_token: str, _audiences: tuple[str, ...]) -> dict[str, object]:
        assert review is not None
        return review

    result = authorize_request(
        headers,
        namespace="aiops-dev",
        allowed_service_account="aiops-diagnosis",
        review_token=reviewer,
    )

    assert result.status == expected_status
    assert result.reason == expected_reason


def test_token_review_failure_is_unavailable() -> None:
    def unavailable(_token: str, _audiences: tuple[str, ...]) -> dict[str, object]:
        raise OSError("api server unavailable")

    result = authorize_request(
        {"Authorization": "Bearer projected-token"},
        namespace="aiops-dev",
        allowed_service_account="aiops-diagnosis",
        review_token=unavailable,
    )

    assert result.status == HTTPStatus.SERVICE_UNAVAILABLE
    assert result.reason == "token_review_unavailable"


def test_internal_auth_headers_reread_projected_token(tmp_path) -> None:
    token_path = tmp_path / "token"
    token_path.write_text("first-token\n", encoding="utf-8")
    assert internal_auth_headers(token_path) == {"Authorization": "Bearer first-token"}

    token_path.write_text("rotated-token\n", encoding="utf-8")
    assert internal_auth_headers(token_path) == {"Authorization": "Bearer rotated-token"}


def test_denial_audit_does_not_log_token(capsys) -> None:
    writes: list[tuple[int, dict[str, object]]] = []

    class Handler:
        path = "/query_metrics"
        headers = {"Authorization": "Bearer must-not-be-logged", "X-Request-ID": "req-1"}

        def write_json(self, status: int, payload: dict[str, object]) -> None:
            writes.append((status, payload))

    identity = enforce_internal_auth(
        Handler(),
        service_name="mcp-prometheus",
        allowed_service_account="aiops-diagnosis",
        namespace="aiops-dev",
        review_token=lambda _token, _audiences: {
            "authenticated": True,
            "audiences": [INTERNAL_AUDIENCE],
            "username": "system:serviceaccount:aiops-dev:aiops-gateway",
        },
    )

    audit = json.loads(capsys.readouterr().out)
    assert identity is None
    assert writes == [(HTTPStatus.FORBIDDEN, {"status": "forbidden", "reason": "service_account_forbidden"})]
    assert audit["event"] == "internal_auth_denied"
    assert audit["identity"] == "system:serviceaccount:aiops-dev:aiops-gateway"
    assert "must-not-be-logged" not in json.dumps(audit)
