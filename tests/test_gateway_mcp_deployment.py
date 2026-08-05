from pathlib import Path

import yaml

from bootstrap_service import SECRET_KEYS
from tests.pilot_manifest_support import resources


def test_mcp_registry_secret_is_bootstrapped_and_required_by_gateway() -> None:
    assert SECRET_KEYS["aiops-mcp-encryption"] == ("key",)

    pilot = resources()
    gateway = pilot[("Deployment", "aiops-gateway")]["spec"]["template"]["spec"]
    mounted = {
        volume["secret"]["secretName"]: {item["key"] for item in volume["secret"].get("items", [])}
        for volume in gateway["volumes"]
        if "secret" in volume
    }
    assert mounted["aiops-mcp-encryption"] == {"key"}
    readiness = " ".join(gateway["containers"][0]["readinessProbe"]["exec"]["command"])
    assert "aiops-mcp-encryption" in readiness
    assert ".data.key" in readiness

    role = pilot[("Role", "aiops-gateway-secret-readiness")]
    assert "aiops-mcp-encryption" in role["rules"][0]["resourceNames"]
    bootstrap = pilot[("Role", "aiops-bootstrap")]
    assert "aiops-mcp-encryption" in bootstrap["rules"][0]["resourceNames"]

    examples = {
        doc["metadata"]["name"]: doc
        for doc in yaml.safe_load_all(Path("deploy/k8s/base/secret.example.yaml").read_text(encoding="utf-8"))
        if doc
    }
    assert set(examples["aiops-mcp-encryption"]["stringData"]) == {"key"}


def test_mcp_network_policies_allow_gateway_registry_and_diagnosis_reads() -> None:
    pilot = resources()
    for name in ("prometheus", "loki", "topology"):
        policy = pilot[("NetworkPolicy", f"aiops-mcp-{name}-internal")]
        callers = {
            source["podSelector"]["matchLabels"]["app.kubernetes.io/name"]
            for source in policy["spec"]["ingress"][0]["from"]
        }
        assert callers == {"aiops-diagnosis", "aiops-gateway"}
