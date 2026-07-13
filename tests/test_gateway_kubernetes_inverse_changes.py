"""K05 frozen inverse Kubernetes Change contract tests."""

from __future__ import annotations

from apps.aiops_k8s_gateway.kubernetes_inverse_changes import (
    bind_inverse_change,
    freeze_inverse_change,
)


def _review_change(operation: str = "patch") -> dict[str, object]:
    canonical = {
        "target": {
            "api_version": "apps/v1", "kind": "Deployment", "namespace": "payments",
            "name": "checkout-api", "uid": "uid-1", "resource_version": "41",
        },
        "operation": operation,
        "payload": [
            {"op": "test", "path": "/metadata/uid", "value": "uid-1"},
            {"op": "test", "path": "/metadata/resourceVersion", "value": "41"},
            {"op": "test", "path": "/spec/replicas", "value": 3},
            {"op": "replace", "path": "/spec/replicas", "value": 5},
        ],
        "post_checks": [
            {"type": "json_pointer", "path": "/spec/replicas", "operator": "eq", "value": 5},
        ],
    }
    return {
        "canonical_change": canonical,
        "diff": [{"op": "replace", "path": "/spec/replicas", "before": 3, "after": 5}],
    }


def test_patch_inverse_freezes_expected_after_and_restores_before() -> None:
    inverse = freeze_inverse_change(_review_change())

    assert inverse is not None
    assert inverse["payload"] == [
        {"op": "test", "path": "/metadata/uid", "value": "$forward.uid"},
        {"op": "test", "path": "/metadata/resourceVersion", "value": "$forward.resource_version"},
        {"op": "test", "path": "/spec/replicas", "value": 5},
        {"op": "replace", "path": "/spec/replicas", "value": 3},
    ]
    assert inverse["post_checks"] == [
        {"type": "json_pointer", "path": "/spec/replicas", "operator": "eq", "value": 3},
    ]


def test_create_inverse_binds_trustworthy_created_identity() -> None:
    change = _review_change("create")
    canonical = change["canonical_change"]
    canonical["target"]["uid"] = None  # type: ignore[index]
    canonical["target"]["resource_version"] = None  # type: ignore[index]
    canonical["payload"] = {
        "apiVersion": "apps/v1", "kind": "Deployment",
        "metadata": {"namespace": "payments", "name": "checkout-api"},
        "spec": {"replicas": 1},
    }
    inverse = freeze_inverse_change(change)

    assert inverse is not None and inverse["operation"] == "delete"
    bound = bind_inverse_change(
        inverse,
        {"target": {"exists": True, "uid": "created-uid", "resource_version": "7"}},
    )
    assert bound["target"]["uid"] == "created-uid"  # type: ignore[index]
    assert bound["payload"]["preconditions"] == {  # type: ignore[index]
        "uid": "created-uid", "resourceVersion": "7",
    }


def test_delete_and_redacted_diff_have_no_reliable_inverse() -> None:
    deleted = _review_change("delete")
    redacted = _review_change()
    redacted["diff"][0]["before"] = {"redacted": True, "sha256": "a" * 64}  # type: ignore[index]

    assert freeze_inverse_change(deleted) is None
    assert freeze_inverse_change(redacted) is None


def test_ambiguous_null_diff_has_no_reliable_inverse() -> None:
    ambiguous = _review_change()
    ambiguous["diff"][0].update({"before": None, "after": None})  # type: ignore[index]

    assert freeze_inverse_change(ambiguous) is None


def test_removed_path_has_no_exact_absence_precondition_for_inverse_add() -> None:
    removed = _review_change()
    removed["diff"][0].update({"before": 3, "after": None})  # type: ignore[index]

    assert freeze_inverse_change(removed) is None


def test_null_to_value_diff_cannot_distinguish_null_from_absent() -> None:
    null_or_absent = _review_change()
    null_or_absent["diff"][0].update({"before": None, "after": 5})  # type: ignore[index]

    assert freeze_inverse_change(null_or_absent) is None
