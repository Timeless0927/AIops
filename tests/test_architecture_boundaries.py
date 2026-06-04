"""Architecture boundary fitness tests."""

from __future__ import annotations

import ast
import importlib
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def _python_files(path: Path) -> list[Path]:
    return sorted(p for p in path.rglob("*.py") if p.is_file())


def _import_roots(path: Path) -> set[str]:
    roots: set[str] = set()
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                roots.add(alias.name.split(".", 1)[0])
        elif isinstance(node, ast.ImportFrom) and node.module:
            roots.add(node.module.split(".", 1)[0])
    return roots


def test_new_platform_packages_are_importable() -> None:
    modules = [
        "apps.aiops_k8s_gateway",
        "apps.cluster_connector",
        "apps.mcp_prometheus.facade",
        "apps.mcp_loki.facade",
        "apps.mcp_topology.facade",
        "aiops.contracts",
        "aiops.domain",
        "aiops.k8s",
        "toolsets",
    ]
    for module in modules:
        importlib.import_module(module)


def test_domain_layer_does_not_depend_on_legacy_runtime_or_infrastructure() -> None:
    forbidden = {
        "hooks",
        "httpx",
        "kubernetes",
        "prometheus_api_client",
        "runtime",
        "toolsets",
        "tools",
    }
    violations: list[str] = []
    for path in _python_files(ROOT / "aiops" / "domain"):
        roots = _import_roots(path)
        for name in sorted(roots & forbidden):
            violations.append(f"{path.relative_to(ROOT)} imports {name}")

    assert violations == []


def test_contracts_layer_stays_pure() -> None:
    forbidden = {
        "apps",
        "hooks",
        "httpx",
        "kubernetes",
        "prometheus_api_client",
        "runtime",
        "toolsets",
        "tools",
    }
    violations: list[str] = []
    for path in _python_files(ROOT / "aiops" / "contracts"):
        roots = _import_roots(path)
        for name in sorted(roots & forbidden):
            violations.append(f"{path.relative_to(ROOT)} imports {name}")

    assert violations == []


def test_gateway_does_not_import_connector_internals() -> None:
    violations: list[str] = []
    for path in _python_files(ROOT / "apps" / "aiops_k8s_gateway"):
        roots = _import_roots(path)
        if "cluster_connector" in roots:
            violations.append(f"{path.relative_to(ROOT)} imports cluster_connector")

        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name.startswith("apps.cluster_connector"):
                        violations.append(f"{path.relative_to(ROOT)} imports {alias.name}")
            elif isinstance(node, ast.ImportFrom) and node.module:
                if node.module.startswith("apps.cluster_connector"):
                    violations.append(f"{path.relative_to(ROOT)} imports {node.module}")

    assert violations == []
