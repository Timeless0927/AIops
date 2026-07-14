from __future__ import annotations

import re

import yaml

from tests.pilot_manifest_support import rendered_text, resources as _resources



def test_canonical_bundle_runs_real_immutable_loki_and_per_node_alloy() -> None:
    resources = _resources()
    loki = resources[("Deployment", "aiops-loki")]
    loki_container = loki["spec"]["template"]["spec"]["containers"][0]
    assert loki["spec"]["replicas"] == 1
    assert loki["spec"]["strategy"]["type"] == "Recreate"
    assert loki_container["image"] == (
        "docker.m.daocloud.io/grafana/loki@"
        "sha256:cd6e176883a90c21755f0315688668991634143423f75bdedfef41441b0fdc3c"
    )
    assert re.search(r"@sha256:[0-9a-f]{64}$", loki_container["image"])
    assert loki_container["resources"] == {
        "requests": {"cpu": "500m", "memory": "512Mi"},
        "limits": {"cpu": "1", "memory": "1Gi"},
    }
    claim = resources[("PersistentVolumeClaim", "aiops-loki-data")]
    assert claim["spec"]["accessModes"] == ["ReadWriteOnce"]
    assert claim["spec"]["resources"]["requests"]["storage"] == "10Gi"
    assert "storageClassName" not in claim["spec"]
    assert loki["spec"]["template"]["spec"]["automountServiceAccountToken"] is False
    storage_metrics = loki["spec"]["template"]["spec"]["containers"][1]
    assert storage_metrics["name"] == "storage-metrics"
    assert storage_metrics["image"] == (
        "registry.cn-hangzhou.aliyuncs.com/timelessmao/aiops-gateway@"
        "sha256:680cda91c8d5625976c7d4bf5f956bd42954e31d93b0441bbad9ebedf8215d24"
    )
    assert {item["name"]: item.get("value") for item in storage_metrics["env"]}[
        "AIOPS_DATA_DIR"
    ] == "/var/loki"
    data_mount = next(item for item in storage_metrics["volumeMounts"] if item["name"] == "data")
    assert data_mount == {"name": "data", "mountPath": "/var/loki", "readOnly": True}
    assert "write_metrics(\"aiops-loki\")" in storage_metrics["args"][0]

    alloy = resources[("DaemonSet", "aiops-alloy")]
    alloy_container = alloy["spec"]["template"]["spec"]["containers"][0]
    assert alloy_container["image"] == (
        "docker.m.daocloud.io/grafana/alloy@"
        "sha256:85e4a706181741dd735d9a69bea81f9e03e16d5349bff46dd9640379f143c007"
    )
    assert re.search(r"@sha256:[0-9a-f]{64}$", alloy_container["image"])
    assert alloy_container["resources"] == {
        "requests": {"cpu": "50m", "memory": "128Mi"},
        "limits": {"cpu": "300m", "memory": "256Mi"},
    }
    node_name = next(item for item in alloy_container["env"] if item["name"] == "NODE_NAME")
    assert node_name["valueFrom"]["fieldRef"]["fieldPath"] == "spec.nodeName"
    assert all("hostPort" not in port for port in alloy_container.get("ports", []))
    assert all("hostPath" not in volume for volume in alloy["spec"]["template"]["spec"].get("volumes", []))


def _config_map(name: str, key: str) -> str:
    return _resources()[("ConfigMap", name)]["data"][key]


def test_loki_uses_tsdb_filesystem_and_physical_seven_day_retention() -> None:
    resources = _resources()
    config = yaml.safe_load(_config_map("aiops-loki-config", "loki.yaml"))
    assert config["auth_enabled"] is False
    assert config["common"]["replication_factor"] == 1
    assert config["common"]["ring"]["kvstore"]["store"] == "inmemory"
    assert config["common"]["storage"]["filesystem"] == {
        "chunks_directory": "/var/loki/chunks",
        "rules_directory": "/var/loki/rules",
    }
    assert config["schema_config"]["configs"] == [
        {
            "from": "2024-01-01",
            "store": "tsdb",
            "object_store": "filesystem",
            "schema": "v13",
            "index": {"prefix": "index_", "period": "24h"},
        }
    ]
    assert config["limits_config"]["retention_period"] == "168h"
    assert config["compactor"]["retention_enabled"] is True
    assert config["compactor"]["delete_request_store"] == "filesystem"
    assert config["analytics"]["reporting_enabled"] is False

    container = resources[("Deployment", "aiops-loki")]["spec"]["template"]["spec"]["containers"][0]
    assert container["readinessProbe"]["httpGet"] == {"path": "/ready", "port": "http"}
    assert container["livenessProbe"]["httpGet"] == {"path": "/ready", "port": "http"}
    service = resources[("Service", "aiops-loki")]
    assert service["spec"].get("type", "ClusterIP") == "ClusterIP"
    runtime = resources[("ConfigMap", "aiops-runtime-config")]["data"]
    assert runtime["LOKI_URL"] == "http://aiops-loki:3100"
    prometheus = yaml.safe_load(_config_map("aiops-prometheus-config", "prometheus.yml"))
    storage_job = next(job for job in prometheus["scrape_configs"] if job["job_name"] == "loki-storage")
    assert storage_job["static_configs"] == [
        {
            "targets": ["aiops-loki:9100"],
            "labels": {
                "namespace": "aiops-system",
                "service": "aiops-loki",
                "workload": "aiops-loki",
            },
        }
    ]
    rules = yaml.safe_load(_config_map("aiops-prometheus-config", "aiops-rules.yml"))["groups"]
    storage_rule = next(
        rule
        for group in rules
        for rule in group["rules"]
        if rule["alert"] == "AIOpsStoragePressure"
    )
    assert storage_rule["expr"] == "aiops_storage_available_ratio < 0.1"


def test_alloy_reads_only_node_pod_logs_with_bounded_identity_labels() -> None:
    config = _config_map("aiops-alloy-config", "config.alloy")
    assert 'field = "spec.nodeName=" + sys.env("NODE_NAME")' in config
    assert 'loki.source.kubernetes "pod_logs"' in config
    assert 'url = "http://aiops-loki:3100/loki/api/v1/push"' in config
    for label in ("cluster", "namespace", "pod", "container", "app", "job"):
        assert f'target_label  = "{label}"' in config or f'target_label = "{label}"' in config
    for forbidden in (
        "host_path",
        "hostPort",
        "loki.process",
        "stage.drop",
        "debug|trace",
        "alloy-aggregator",
        "otelcol.",
        "pyroscope.",
        "prometheus.remote_write",
    ):
        assert forbidden not in config

    role = _resources()[("ClusterRole", "aiops-alloy")]
    assert role["rules"] == [
        {"apiGroups": [""], "resources": ["pods"], "verbs": ["get", "list", "watch"]},
        {"apiGroups": [""], "resources": ["pods/log"], "verbs": ["get"]},
    ]
    assert not ({"secrets", "configmaps"} & {resource for rule in role["rules"] for resource in rule["resources"]})
    assert all(
        set(rule["verbs"]) <= {"get", "list", "watch"}
        for rule in role["rules"]
    )


def test_logging_network_boundaries_and_canonical_path_exclude_synthetic_backends() -> None:
    resources = _resources()
    loki = resources[("NetworkPolicy", "aiops-loki-ingress")]
    allowed = {
        source["podSelector"]["matchLabels"]["app.kubernetes.io/name"]
        for source in loki["spec"]["ingress"][0]["from"]
    }
    assert allowed == {"aiops-alloy", "aiops-mcp-loki", "aiops-prometheus"}
    assert loki["spec"]["ingress"] == [
        {
            "from": [
                {
                    "podSelector": {
                        "matchLabels": {"app.kubernetes.io/name": "aiops-mcp-loki"}
                    }
                },
                {
                    "podSelector": {"matchLabels": {"app.kubernetes.io/name": "aiops-alloy"}}
                },
                {
                    "podSelector": {
                        "matchLabels": {"app.kubernetes.io/name": "aiops-prometheus"}
                    }
                },
            ],
            "ports": [{"protocol": "TCP", "port": 3100}],
        },
        {
            "from": [
                {
                    "podSelector": {
                        "matchLabels": {"app.kubernetes.io/name": "aiops-prometheus"}
                    }
                }
            ],
            "ports": [{"protocol": "TCP", "port": 9100}],
        },
    ]
    assert resources[("NetworkPolicy", "aiops-alloy-ingress")]["spec"]["ingress"] == [
        {
            "from": [
                {
                    "podSelector": {
                        "matchLabels": {"app.kubernetes.io/name": "aiops-prometheus"}
                    }
                }
            ],
            "ports": [{"protocol": "TCP", "port": 12345}],
        }
    ]

    rendered = rendered_text()
    for fake in ("aiops-dev-loki", "aiops-loki-synthetic-log", "payment-api synthetic"):
        assert fake not in rendered
