"""Exact Kubernetes Cluster identity source for Acceptance initialization."""

from __future__ import annotations

import hashlib
import json

from .command import CommandExecutor


class KubernetesClusterIdentitySource:
    def __init__(self, commands: CommandExecutor) -> None:
        self.commands = commands

    def read(self) -> tuple[str, str]:
        context_result = self.commands.run(
            ["kubectl", "config", "current-context"], timeout=15,
        )
        config_result = self.commands.run(
            ["kubectl", "config", "view", "--minify", "-o", "json"], timeout=15,
        )
        if context_result.exit_code != 0 or config_result.exit_code != 0:
            raise RuntimeError("exact Kubernetes Cluster identity is unavailable")
        context = context_result.stdout.strip()
        try:
            config = json.loads(config_result.stdout)
            clusters = config.get("clusters")
            cluster = clusters[0]["cluster"] if isinstance(clusters, list) and len(clusters) == 1 else None
        except (KeyError, TypeError, json.JSONDecodeError) as exc:
            raise ValueError("Kubernetes Cluster identity response is invalid") from exc
        if (
            not context or not isinstance(cluster, dict)
            or not isinstance(cluster.get("server"), str) or not cluster["server"]
            or not isinstance(cluster.get("certificate-authority-data", ""), str)
        ):
            raise ValueError("current kube context must resolve exactly one Cluster")
        identity = {
            "server": cluster["server"],
            "certificate_authority_data": cluster.get("certificate-authority-data", ""),
        }
        digest = hashlib.sha256(
            json.dumps(identity, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        return context, digest
