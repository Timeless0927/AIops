from pathlib import Path
import subprocess

import yaml

IMAGE_DIGESTS = {
    "aiops-gateway": "registry.cn-hangzhou.aliyuncs.com/timelessmao/hub@sha256:fa0f193634bebad053923ad62cbecce3c252deb8df4f5d242375582ec231c184",
    "aiops-connector": "registry.cn-hangzhou.aliyuncs.com/timelessmao/hub@sha256:11ae97e1088d83e4759b5a9214027f8f8ac3c30920ef93aa0c5670ad64701565",
    "aiops-hermes": "registry.cn-hangzhou.aliyuncs.com/timelessmao/hub@sha256:aa48f083ddf9dd5813cd438f1fc6c5aaa14f4076796e65216f60176a7a0b1503",
    "aiops-mcp-prometheus": "registry.cn-hangzhou.aliyuncs.com/timelessmao/hub@sha256:f1d54690485ffe9bfde6d049cce270716f867532b7d324f886f055d9bebefc89",
    "aiops-mcp-loki": "registry.cn-hangzhou.aliyuncs.com/timelessmao/hub@sha256:710f09a154ecab9481585913eddc84bfdbab4c12d05330706e0cfc2187f242fe",
    "aiops-dev-prometheus": "registry.cn-hangzhou.aliyuncs.com/timelessmao/hub@sha256:f1d54690485ffe9bfde6d049cce270716f867532b7d324f886f055d9bebefc89",
    "aiops-dev-loki": "registry.cn-hangzhou.aliyuncs.com/timelessmao/hub@sha256:710f09a154ecab9481585913eddc84bfdbab4c12d05330706e0cfc2187f242fe",
    "payment-api": "registry.cn-hangzhou.aliyuncs.com/timelessmao/hub@sha256:f1d54690485ffe9bfde6d049cce270716f867532b7d324f886f055d9bebefc89",
}

RC_IMAGE_SOURCE_HEAD = "62805af81175d12f45eb49b695895e9268ef77f9"
RC_IMAGE_SOURCE_SHORT_SHA = "62805af"
RC_IMAGE_SOURCE_RUN = "https://github.com/Timeless0927/AIops/actions/runs/27187891609"
RC_JOB_NAME = f"aiops-loki-synthetic-log-rc-{RC_IMAGE_SOURCE_SHORT_SHA}"
LEGACY_AIOPS_DIGEST = "sha256:a4fdfb98b22a5f3194933d6d1acccd77d5f2375cf7b59bc3ddb864da544c00cb"
OLD_RC_IMAGE_SOURCE_HEAD = "c534da7e949c7b9adc9bdd832c61894068acada4"
STALE_RC_IMAGE_SOURCE_HEAD = "751ad23453eb329d5412dcec9054993ae306dfdd"
STALE_FAB2E7C_IMAGE_SOURCE_HEAD = "fab2e7c15eea5a0cfc334485bbcd8ef3d4230dee"


def _docs(path: str) -> list[dict]:
    return [doc for doc in yaml.safe_load_all(Path(path).read_text(encoding="utf-8")) if doc]


def _kustomize_docs(path: str) -> list[dict]:
    result = subprocess.run(
        ["kubectl", "kustomize", path],
        check=True,
        capture_output=True,
        text=True,
    )
    return [doc for doc in yaml.safe_load_all(result.stdout) if doc]


def _by_kind_name(docs: list[dict]) -> dict[tuple[str, str], dict]:
    return {(doc["kind"], doc["metadata"]["name"]): doc for doc in docs}


def test_deployment_manifest_references_split_service_images_and_health() -> None:
    deployments = {doc["metadata"]["name"]: doc for doc in _docs("deploy/k8s/deployment.yaml")}

    expected = {
        "aiops-gateway": ("gateway", "registry.cn-hangzhou.aliyuncs.com/timelessmao/hub:gateway-latest", 8080),
        "aiops-connector": ("connector", "registry.cn-hangzhou.aliyuncs.com/timelessmao/hub:connectors-latest", 8081),
        "aiops-hermes": ("hermes", "registry.cn-hangzhou.aliyuncs.com/timelessmao/hub:hermes-latest", 8082),
        "aiops-mcp-prometheus": (
            "mcp-prometheus",
            "registry.cn-hangzhou.aliyuncs.com/timelessmao/hub:mcp-prometheus-latest",
            8083,
        ),
        "aiops-mcp-loki": ("mcp-loki", "registry.cn-hangzhou.aliyuncs.com/timelessmao/hub:mcp-loki-latest", 8084),
    }

    assert set(expected) <= set(deployments)
    for name, (container_name, image, port) in expected.items():
        pod_spec = deployments[name]["spec"]["template"]["spec"]
        container = pod_spec["containers"][0]
        assert container["name"] == container_name
        assert container["image"] == image
        assert container["ports"][0]["containerPort"] == port
        assert container["readinessProbe"]["httpGet"]["path"] == "/readyz"
        assert container["livenessProbe"]["httpGet"]["path"] == "/healthz"
        assert {"configMapRef": {"name": "aiops-runtime-config"}} in container["envFrom"]

    assert deployments["aiops-connector"]["spec"]["template"]["spec"]["serviceAccountName"] == "aiops-connector"
    hermes_volume = deployments["aiops-hermes"]["spec"]["template"]["spec"]["volumes"][0]
    assert hermes_volume["persistentVolumeClaim"]["claimName"] == "aiops-hermes-data"


def test_configmap_contains_runtime_authorization_and_service_routing() -> None:
    configmap = yaml.safe_load(Path("deploy/k8s/configmap.yaml").read_text(encoding="utf-8"))
    data = configmap["data"]

    for key in (
        "AIOPS_SRE_ADMIN_NAME",
        "AIOPS_SRE_ADMIN_OPEN_ID",
        "AIOPS_SRE_OPERATOR_NAME",
        "AIOPS_SRE_OPERATOR_OPEN_ID",
        "AIOPS_APPROVAL_ALLOW_SELF_APPROVAL_LOW_RISK",
        "AIOPS_APPROVAL_REQUIRE_ADMIN_FOR_EXEC",
        "AIOPS_APPROVAL_REQUIRE_ADMIN_FOR_DANGEROUS",
        "FEISHU_GROUP_POLICY",
        "FEISHU_ALLOWED_USERS",
        "HERMES_HOME",
        "HERMES_CONFIG",
        "AIOPS_DATA_DIR",
        "AIOPS_CONNECTOR_URL",
        "AIOPS_GATEWAY_URL",
        "PROMETHEUS_URL",
        "LOKI_URL",
    ):
        assert key in data

    assert data["HERMES_HOME"] == "/data/hermes"
    assert data["HERMES_CONFIG"] == "/data/hermes/config.yaml"
    assert data["AIOPS_DATA_DIR"] == "/data/aiops"


def test_service_manifest_exposes_split_service_ports() -> None:
    services = {doc["metadata"]["name"]: doc for doc in _docs("deploy/k8s/service.yaml")}

    assert services["aiops-gateway"]["spec"]["ports"][0]["port"] == 8080
    assert services["aiops-connector"]["spec"]["ports"][0]["port"] == 8081
    assert services["aiops-hermes"]["spec"]["ports"][0]["port"] == 8082
    assert services["aiops-mcp-prometheus"]["spec"]["ports"][0]["port"] == 8083
    assert services["aiops-mcp-loki"]["spec"]["ports"][0]["port"] == 8084


def test_kustomize_overlays_define_observability_profiles_and_images() -> None:
    bundled = yaml.safe_load(Path("deploy/k8s/overlays/dev-bundled/kustomization.yaml").read_text(encoding="utf-8"))
    external = yaml.safe_load(Path("deploy/k8s/overlays/dev-external/kustomization.yaml").read_text(encoding="utf-8"))
    disabled = yaml.safe_load(Path("deploy/k8s/overlays/dev-disabled/kustomization.yaml").read_text(encoding="utf-8"))

    assert bundled["namespace"] == "aiops-dev"
    assert "../../base" in bundled["resources"]
    assert "../../bundled" in bundled["resources"]
    assert "http://aiops-dev-prometheus:9090" in bundled["patches"][0]["patch"]
    assert "http://aiops-dev-loki:3100" in bundled["patches"][0]["patch"]
    assert "http://prometheus.monitoring.svc.cluster.local:9090" in external["patches"][0]["patch"]
    assert "http://loki.monitoring.svc.cluster.local:3100" in external["patches"][0]["patch"]
    assert 'path: /data/PROMETHEUS_URL\n  value: ""' in disabled["patches"][0]["patch"]
    assert 'path: /data/LOKI_URL\n  value: ""' in disabled["patches"][0]["patch"]


def test_base_kustomize_files_match_root_auditable_yaml() -> None:
    for name in ("configmap.yaml", "serviceaccount.yaml", "rbac.yaml", "pvc.yaml", "deployment.yaml", "service.yaml"):
        assert Path(f"deploy/k8s/base/{name}").read_text(encoding="utf-8") == Path(f"deploy/k8s/{name}").read_text(
            encoding="utf-8"
        )

    assert Path("deploy/k8s/base/secret.example.yaml").read_text(encoding="utf-8") == Path(
        "deploy/k8s/secret.example.yaml"
    ).read_text(encoding="utf-8")

    assert Path("deploy/k8s/bundled/observability-bundled.yaml").read_text(encoding="utf-8") == Path(
        "deploy/k8s/observability-bundled.yaml"
    ).read_text(encoding="utf-8")


def test_bundled_observability_manifest_contains_prometheus_loki_and_payment_api() -> None:
    docs = _docs("deploy/k8s/observability-bundled.yaml")
    by_name = {(doc["kind"], doc["metadata"]["name"]) for doc in docs}

    assert ("Deployment", "aiops-dev-prometheus") in by_name
    assert ("Service", "aiops-dev-prometheus") in by_name
    assert ("Deployment", "aiops-dev-loki") in by_name
    assert ("Service", "aiops-dev-loki") in by_name
    assert ("Deployment", "payment-api") in by_name
    assert ("Job", "aiops-loki-synthetic-log") in by_name
    bundled = Path("deploy/k8s/observability-bundled.yaml").read_text(encoding="utf-8")
    assert "registry.cn-hangzhou.aliyuncs.com/timelessmao/hub:mcp-prometheus-latest" in bundled
    assert "registry.cn-hangzhou.aliyuncs.com/timelessmao/hub:mcp-loki-latest" in bundled
    assert "payment-api synthetic checkout error" in Path("deploy/k8s/observability-bundled.yaml").read_text(
        encoding="utf-8"
    )


def test_k8s_readme_mentions_profiles_image_digest_validation_and_retention() -> None:
    readme = Path("deploy/k8s/README.md").read_text(encoding="utf-8")
    assert "kubectl apply -k deploy/k8s/overlays/dev-bundled" in readme
    assert "kubectl apply -k deploy/k8s/overlays/dev-external" in readme
    assert "kubectl apply -k deploy/k8s/overlays/dev-disabled" in readme
    assert "kubectl delete -k deploy/k8s/overlays/dev-bundled" in readme
    assert "kubectl -n aiops-dev create secret generic aiops-runtime-secret" in readme
    assert "deploy/k8s/overlays/dev-remediation-rbac" in readme
    assert "registry.cn-hangzhou.aliyuncs.com/timelessmao/hub@sha256:<gateway-digest>" in readme
    assert "kubectl apply -k deploy/k8s/overlays/rc-bundled-digest" in readme
    assert RC_IMAGE_SOURCE_HEAD in readme
    assert RC_IMAGE_SOURCE_SHORT_SHA in readme
    assert RC_IMAGE_SOURCE_RUN in readme
    assert "aiops-loki-synthetic-log-rc" in readme
    assert RC_JOB_NAME in readme
    assert LEGACY_AIOPS_DIGEST in readme
    assert "prints `replace-me`" in readme
    assert "A retained placeholder Secret is not a valid real configuration" in readme
    assert "<real-feishu-app-id>" in readme
    assert "<real-model-api-key>" in readme
    assert "backend_unavailable" in readme
    assert "do not clean up the namespace after smoke" in readme


def test_rendered_profiles_do_not_apply_placeholder_secret_but_reference_runtime_secret() -> None:
    for profile in ("dev-bundled", "dev-external", "dev-disabled"):
        rendered = _by_kind_name(_kustomize_docs(f"deploy/k8s/overlays/{profile}"))
        namespace = rendered[("Namespace", "aiops-dev")]
        assert namespace["metadata"]["name"] == "aiops-dev"

        assert ("Secret", "aiops-runtime-secret") not in rendered

        for deployment_name in (
            "aiops-gateway",
            "aiops-connector",
            "aiops-hermes",
            "aiops-mcp-prometheus",
            "aiops-mcp-loki",
        ):
            deployment = rendered[("Deployment", deployment_name)]
            assert deployment["metadata"]["namespace"] == "aiops-dev"
            env_from = deployment["spec"]["template"]["spec"]["containers"][0]["envFrom"]
            assert {"secretRef": {"name": "aiops-runtime-secret", "optional": True}} in env_from


def test_rendered_default_profiles_keep_connector_rbac_read_only() -> None:
    for profile in ("dev-bundled", "dev-external", "dev-disabled"):
        rendered = _by_kind_name(_kustomize_docs(f"deploy/k8s/overlays/{profile}"))
        role = rendered[("Role", "aiops-connector")]
        assert role["metadata"]["namespace"] == "aiops-dev"

        rules = role["rules"]
        assert not any("pods/exec" in rule.get("resources", []) for rule in rules)
        assert not any("pods/attach" in rule.get("resources", []) for rule in rules)
        assert not any("patch" in rule.get("verbs", []) for rule in rules)
        assert not any("update" in rule.get("verbs", []) for rule in rules)

        apps_rule = next(rule for rule in rules if rule.get("apiGroups") == ["apps"])
        assert set(apps_rule["verbs"]) == {"get", "list", "watch"}


def test_rendered_remediation_rbac_overlay_is_explicit_opt_in() -> None:
    rendered = _by_kind_name(_kustomize_docs("deploy/k8s/overlays/dev-remediation-rbac"))

    role = rendered[("Role", "aiops-connector-remediation")]
    assert role["metadata"]["namespace"] == "aiops-dev"
    assert any(
        set(rule.get("resources", [])) == {"pods/exec", "pods/attach"} and set(rule.get("verbs", [])) == {"create"}
        for rule in role["rules"]
    )
    assert any(
        set(rule.get("resources", [])) == {"deployments", "statefulsets", "daemonsets", "replicasets"}
        and set(rule.get("verbs", [])) == {"patch", "update"}
        for rule in role["rules"]
    )
    binding = rendered[("RoleBinding", "aiops-connector-remediation")]
    assert binding["metadata"]["namespace"] == "aiops-dev"
    assert binding["subjects"] == [{"kind": "ServiceAccount", "name": "aiops-connector"}]
    assert ("Deployment", "aiops-gateway") not in rendered


def test_rendered_profile_resource_differences_are_explicit() -> None:
    bundled = set(_by_kind_name(_kustomize_docs("deploy/k8s/overlays/dev-bundled")))
    external = set(_by_kind_name(_kustomize_docs("deploy/k8s/overlays/dev-external")))
    disabled = set(_by_kind_name(_kustomize_docs("deploy/k8s/overlays/dev-disabled")))

    bundled_only = {
        ("Deployment", "aiops-dev-prometheus"),
        ("Service", "aiops-dev-prometheus"),
        ("Deployment", "aiops-dev-loki"),
        ("Service", "aiops-dev-loki"),
        ("Deployment", "payment-api"),
        ("Service", "payment-api"),
        ("Job", "aiops-loki-synthetic-log"),
    }

    assert bundled_only <= bundled
    assert bundled_only.isdisjoint(external)
    assert bundled_only.isdisjoint(disabled)
    assert external == disabled


def test_rendered_rc_bundled_digest_profile_pins_all_images_and_uses_rc_job() -> None:
    rendered = _by_kind_name(_kustomize_docs("deploy/k8s/overlays/rc-bundled-digest"))

    assert rendered[("Namespace", "aiops-dev")]["metadata"]["name"] == "aiops-dev"
    assert rendered[("Namespace", "aiops-dev")]["metadata"]["labels"]["aiops.dev/profile"] == "rc-bundled-digest"
    annotations = rendered[("Namespace", "aiops-dev")]["metadata"]["annotations"]
    assert annotations["aiops.dev/image-source-head"] == RC_IMAGE_SOURCE_HEAD
    assert annotations["aiops.dev/image-source-short-sha"] == RC_IMAGE_SOURCE_SHORT_SHA
    assert annotations["aiops.dev/image-source-run"] == RC_IMAGE_SOURCE_RUN
    assert ("Secret", "aiops-runtime-secret") not in rendered

    for deployment_name, image in IMAGE_DIGESTS.items():
        deployment = rendered[("Deployment", deployment_name)]
        assert deployment["metadata"]["namespace"] == "aiops-dev"
        assert deployment["spec"]["template"]["spec"]["containers"][0]["image"] == image

    assert ("Job", "aiops-loki-synthetic-log") not in rendered
    assert ("Job", "aiops-loki-synthetic-log-rc") not in rendered
    job = rendered[("Job", RC_JOB_NAME)]
    assert job["metadata"]["namespace"] == "aiops-dev"
    assert job["metadata"]["name"].endswith(RC_IMAGE_SOURCE_SHORT_SHA)
    assert (
        job["spec"]["template"]["spec"]["containers"][0]["image"]
        == IMAGE_DIGESTS["aiops-mcp-loki"]
    )
    command = job["spec"]["template"]["spec"]["containers"][0]["command"][2]
    assert "aiops-rc-bundled-digest" in command
    assert "payment-api rc digest synthetic checkout error" in command


def test_rendered_rc_bundled_digest_profile_contains_no_mutable_latest_images() -> None:
    rendered = _kustomize_docs("deploy/k8s/overlays/rc-bundled-digest")
    images: list[str] = []
    for doc in rendered:
        pod_spec = doc.get("spec", {}).get("template", {}).get("spec", {})
        for container in pod_spec.get("containers", []):
            image = container.get("image")
            if image:
                images.append(image)

    assert images
    assert all("@sha256:" in image for image in images)
    assert not any(":latest" in image for image in images)


def test_rc_digest_overlay_and_readme_reference_current_head_digest_evidence() -> None:
    overlay = Path("deploy/k8s/overlays/rc-bundled-digest/kustomization.yaml").read_text(encoding="utf-8")
    readme = Path("deploy/k8s/README.md").read_text(encoding="utf-8")
    combined = f"{overlay}\n{readme}"

    assert OLD_RC_IMAGE_SOURCE_HEAD not in combined
    assert STALE_RC_IMAGE_SOURCE_HEAD not in combined
    assert STALE_FAB2E7C_IMAGE_SOURCE_HEAD not in combined
    assert "aiops-loki-synthetic-log-rc-fab2e7c" not in combined
    assert "aiops-loki-synthetic-log-rc\"" not in overlay
    for image in IMAGE_DIGESTS.values():
        assert image in overlay
        assert image.removeprefix("registry.cn-hangzhou.aliyuncs.com/timelessmao/hub@") in readme
    assert LEGACY_AIOPS_DIGEST in readme

    assert RC_IMAGE_SOURCE_HEAD in overlay
    assert RC_IMAGE_SOURCE_HEAD in readme
    assert RC_IMAGE_SOURCE_SHORT_SHA in overlay
    assert RC_IMAGE_SOURCE_SHORT_SHA in readme
    assert RC_IMAGE_SOURCE_RUN in overlay
    assert RC_IMAGE_SOURCE_RUN in readme


def test_rc_digest_job_name_is_head_scoped_for_retained_namespace_reapply() -> None:
    rendered = _by_kind_name(_kustomize_docs("deploy/k8s/overlays/rc-bundled-digest"))
    job = rendered[("Job", RC_JOB_NAME)]

    assert job["metadata"]["name"] == RC_JOB_NAME
    assert job["metadata"]["labels"]["app.kubernetes.io/name"] == RC_JOB_NAME
    assert job["spec"]["template"]["metadata"]["labels"]["app.kubernetes.io/name"] == RC_JOB_NAME
    assert RC_IMAGE_SOURCE_SHORT_SHA in job["metadata"]["name"]
    assert ("Job", "aiops-loki-synthetic-log-rc") not in rendered
