from __future__ import annotations

import re
import yaml

from tests.pilot_manifest_support import rendered_text, resources as _resources



def test_canonical_bundle_runs_real_immutable_metrics_workloads() -> None:
    resources = _resources()
    expected_images = {
        "aiops-prometheus": "quay.io/prometheus/prometheus@sha256:63805ebb8d2b3920190daf1cb14a60871b16fd38bed42b857a3182bc621f4996",
        "aiops-alertmanager": "quay.io/prometheus/alertmanager@sha256:27c475db5fb156cab31d5c18a4251ac7ed567746a2483ff264516437a39b15ba",
        "aiops-kube-state-metrics": "registry.k8s.io/kube-state-metrics/kube-state-metrics@sha256:2bbc915567334b13632bf62c0a97084aff72a36e13c4dabd5f2f11c898c5bacd",
    }
    for name, image in expected_images.items():
        deployment = resources[("Deployment", name)]
        assert deployment["spec"]["replicas"] == 1
        assert deployment["spec"]["template"]["spec"]["containers"][0]["image"] == image
        assert re.search(r"@sha256:[0-9a-f]{64}$", image)

    prometheus = resources[("Deployment", "aiops-prometheus")]
    assert prometheus["spec"]["strategy"]["type"] == "Recreate"
    assert prometheus["spec"]["template"]["spec"]["containers"][0]["resources"] == {
        "requests": {"cpu": "500m", "memory": "1Gi"},
        "limits": {"cpu": "1", "memory": "2Gi"},
    }
    claim = resources[("PersistentVolumeClaim", "aiops-prometheus-data")]
    assert claim["spec"]["accessModes"] == ["ReadWriteOnce"]
    assert claim["spec"]["resources"]["requests"]["storage"] == "10Gi"

    kube_state_metrics = resources[("Deployment", "aiops-kube-state-metrics")]
    probes = kube_state_metrics["spec"]["template"]["spec"]["containers"][0]
    assert probes["readinessProbe"]["httpGet"] == {"path": "/readyz", "port": "telemetry"}
    assert probes["livenessProbe"]["httpGet"] == {"path": "/livez", "port": "http"}


def _config_map(name: str, key: str) -> dict:
    return yaml.safe_load(_resources()[("ConfigMap", name)]["data"][key])


def test_prometheus_uses_native_bounded_discovery_and_exact_cluster_identity() -> None:
    resources = _resources()
    config = _config_map("aiops-prometheus-config", "prometheus.yml")
    runtime = resources[("ConfigMap", "aiops-runtime-config")]["data"]

    assert config["global"] == {
        "scrape_interval": "15s",
        "evaluation_interval": "15s",
        "external_labels": {"cluster": "pilot-cluster"},
    }
    assert runtime["AIOPS_CLUSTER_ID"] == "pilot-cluster"
    assert runtime["PROMETHEUS_URL"] == "http://aiops-prometheus:9090"
    assert config["rule_files"] == ["/etc/prometheus/rules/*.yml"]
    assert config["alerting"]["alertmanagers"] == [
        {"static_configs": [{"targets": ["aiops-alertmanager:9093"]}]}
    ]

    jobs = {job["job_name"]: job for job in config["scrape_configs"]}
    assert set(jobs) == {
        "prometheus",
        "alertmanager",
        "kube-state-metrics",
        "loki",
        "loki-storage",
        "aiops-gateway",
        "aiops-connector",
        "aiops-diagnosis",
        "aiops-notification",
        "aiops-mcp-prometheus",
        "aiops-mcp-loki",
        "aiops-mcp-topology",
        "annotated-pods",
    }
    for name in set(jobs) - {"annotated-pods"}:
        static = jobs[name]["static_configs"]
        assert len(static) == 1 and len(static[0]["targets"]) == 1
        identity = "aiops-loki" if name == "loki-storage" else name
        expected_labels = {"service": identity, "workload": identity}
        if name != "kube-state-metrics":
            expected_labels["namespace"] = "aiops-system"
        assert static[0]["labels"] == expected_labels

    discovery = jobs["annotated-pods"]
    assert discovery["kubernetes_sd_configs"] == [{"role": "pod"}]
    relabel = discovery["relabel_configs"]
    assert {
        "source_labels": ["__meta_kubernetes_pod_annotation_prometheus_io_scrape"],
        "action": "keep",
        "regex": "true",
    } in relabel
    target_labels = {
        item["target_label"]
        for item in relabel
        if item.get("action", "replace") == "replace"
        and "target_label" in item
        and not item["target_label"].startswith("__")
    }
    assert target_labels == {"namespace", "pod", "container", "app", "service", "workload"}


def test_prometheus_loads_real_gateway_routed_rules() -> None:
    groups = _config_map("aiops-prometheus-config", "aiops-rules.yml")["groups"]
    rules = [rule for group in groups for rule in group["rules"]]
    assert {rule["alert"] for rule in rules} == {
        "AIOpsControlPlaneUnavailable",
        "AIOpsDurableWorkStalled",
        "AIOpsUnknownOutcome",
        "AIOpsStoragePressure",
        "AIOpsNotificationDeadLetter",
        "AIOpsVerificationWorkloadUnavailable",
    }
    for rule in rules:
        assert {"severity", "aiops_route", "namespace", "workload", "service"} <= set(
            rule["labels"]
        )
        assert rule["labels"]["aiops_route"] == "gateway"
        expression = str(rule["expr"])
        assert "vector(" not in expression
        assert "ALERTS{" not in expression

    verification = next(
        rule for rule in rules if rule["alert"] == "AIOpsVerificationWorkloadUnavailable"
    )
    assert "kube_deployment_status_replicas_unavailable" in verification["expr"]
    assert 'deployment="aiops-verification"' in verification["expr"]


def test_alertmanager_routes_only_explicit_aiops_alerts_with_projected_token() -> None:
    resources = _resources()
    config = _config_map("aiops-alertmanager-config", "alertmanager.yml")
    assert config["route"] == {
        "receiver": "drop",
        "routes": [
            {
                "receiver": "gateway",
                "matchers": ['aiops_route="gateway"'],
                "group_by": ["cluster", "namespace", "alertname"],
                "group_wait": "5s",
                "group_interval": "5s",
            }
        ],
    }
    receivers = {receiver["name"]: receiver for receiver in config["receivers"]}
    assert receivers["drop"] == {"name": "drop"}
    assert receivers["gateway"]["webhook_configs"] == [
        {
            "url": "http://aiops-gateway:8080/webhooks/alertmanager",
            "send_resolved": True,
            "http_config": {
                "authorization": {
                    "type": "Bearer",
                    "credentials_file": "/etc/alertmanager/secrets/token",
                }
            },
        }
    ]

    pod = resources[("Deployment", "aiops-alertmanager")]["spec"]["template"]["spec"]
    assert pod["automountServiceAccountToken"] is False
    token = next(volume["secret"] for volume in pod["volumes"] if volume["name"] == "runtime-token")
    assert token == {
        "secretName": "aiops-runtime-secret",
        "optional": False,
        "items": [{"key": "AIOPS_ALERTMANAGER_WEBHOOK_TOKEN", "path": "token"}],
    }


def test_metrics_rbac_and_ingress_are_read_only_and_bounded() -> None:
    resources = _resources()
    prometheus = resources[("ClusterRole", "aiops-prometheus")]
    assert prometheus["rules"] == [
        {"apiGroups": [""], "resources": ["pods"], "verbs": ["get", "list", "watch"]}
    ]
    kube_state_metrics = resources[("ClusterRole", "aiops-kube-state-metrics")]
    for role in (prometheus, kube_state_metrics):
        for rule in role["rules"]:
            assert not ({"secrets", "configmaps"} & set(rule["resources"]))
            assert set(rule["verbs"]) <= {"get", "list", "watch"}

    expected = {
        "aiops-gateway-internal",
        "aiops-prometheus-ingress",
        "aiops-alertmanager-ingress",
        "aiops-kube-state-metrics-ingress",
        "aiops-services-prometheus-scrape",
    }
    assert expected <= {name for kind, name in resources if kind == "NetworkPolicy"}
    assert resources[("NetworkPolicy", "aiops-prometheus-ingress")]["spec"]["ingress"][0][
        "from"
    ] == [{"podSelector": {"matchLabels": {"app.kubernetes.io/name": "aiops-mcp-prometheus"}}}]
    for name in ("aiops-alertmanager-ingress", "aiops-kube-state-metrics-ingress"):
        assert resources[("NetworkPolicy", name)]["spec"]["ingress"][0]["from"] == [
            {"podSelector": {"matchLabels": {"app.kubernetes.io/name": "aiops-prometheus"}}}
        ]

    gateway = resources[("NetworkPolicy", "aiops-gateway-internal")]
    allowed_callers = {
        source["podSelector"]["matchLabels"]["app.kubernetes.io/name"]
        for source in gateway["spec"]["ingress"][0]["from"]
    }
    assert allowed_callers == {
        "aiops-console",
        "aiops-connector",
        "aiops-diagnosis",
        "aiops-alertmanager",
        "aiops-prometheus",
    }
    assert gateway["spec"]["ingress"][0]["ports"] == [{"protocol": "TCP", "port": 8080}]


def test_canonical_metrics_path_contains_no_compatibility_artifacts() -> None:
    rendered = rendered_text()
    for fake in ("aiops-dev-prometheus", "payment-api", "aiops-loki-synthetic-log"):
        assert fake not in rendered
    assert "monitoring.coreos.com" not in rendered
