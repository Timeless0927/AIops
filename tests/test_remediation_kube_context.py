"""Tests for remediation kube context resolution."""

from __future__ import annotations

import pytest

from toolsets import remediation_kube_context


def test_resolve_kube_context_defaults_to_in_cluster_without_mapping(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("AIOPS_KUBE_CONTEXT_MAP", raising=False)
    monkeypatch.delenv("AIOPS_CLUSTER_CONTEXT_MAP", raising=False)
    monkeypatch.delenv("HERMES_CONFIG", raising=False)
    monkeypatch.delenv("HERMES_HOME", raising=False)

    assert remediation_kube_context.resolve_kube_context({"cluster": "206K8S"}) is None


def test_resolve_kube_context_uses_explicit_action_context(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AIOPS_KUBE_CONTEXT_MAP", '{"206K8S": "mapped-context"}')

    action = {"cluster": "206K8S", "kube_context": "explicit-context"}

    assert remediation_kube_context.resolve_kube_context(action) == "explicit-context"


def test_resolve_kube_context_uses_env_mapping(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AIOPS_KUBE_CONTEXT_MAP", '{"206K8S": "mapped-context"}')

    assert remediation_kube_context.resolve_kube_context({"cluster": "206K8S"}) == "mapped-context"


def test_resolve_kube_context_uses_config_mapping(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("AIOPS_KUBE_CONTEXT_MAP", raising=False)
    monkeypatch.delenv("AIOPS_CLUSTER_CONTEXT_MAP", raising=False)
    config = {"kubernetes": {"cluster_contexts": {"206K8S": "config-context"}}}

    assert remediation_kube_context.resolve_kube_context({"cluster": "206K8S"}, config=config) == "config-context"


def test_resolve_kube_context_rejects_invalid_mapping(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AIOPS_KUBE_CONTEXT_MAP", '{"206K8S": "bad context"}')

    with pytest.raises(ValueError, match="invalid kube context"):
        remediation_kube_context.resolve_kube_context({"cluster": "206K8S"})


def test_resolve_kube_context_accepts_csv_env_mapping(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AIOPS_KUBE_CONTEXT_MAP", "206K8S=prod-context, staging=staging-context")

    assert remediation_kube_context.resolve_kube_context({"cluster": "staging"}) == "staging-context"
