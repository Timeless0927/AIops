from pathlib import Path
import subprocess

import yaml

IMAGE_DIGESTS = {
    "aiops-gateway": "registry.cn-hangzhou.aliyuncs.com/timelessmao/aiops-gateway@sha256:5c16f49e64df93199397395c32d055e5bdc7b03f802fe79ffcfb3e417130a9f6",
    "aiops-connector": "registry.cn-hangzhou.aliyuncs.com/timelessmao/aiops-connectors@sha256:b5b50502628a38ddc170c50fb73180487bf31d15bcf200da42c2d7a35310403e",
    "aiops-hermes": "registry.cn-hangzhou.aliyuncs.com/timelessmao/aiops-hermes@sha256:b88d7557b5491d0178126c5643cc5795e8e47dabbdef978f3e68c473e66504a0",
    "aiops-mcp-prometheus": "registry.cn-hangzhou.aliyuncs.com/timelessmao/aiops-mcp-prometheus@sha256:81589cb7eb50e0f244fdef1ed202fca189fa5d11e4f337f602d0fdb5c32d27bc",
    "aiops-mcp-loki": "registry.cn-hangzhou.aliyuncs.com/timelessmao/aiops-mcp-loki@sha256:2e38540cdd0c9ad6e552090072fd1adf5a38d6e347c32b31a2b256334f2e699b",
    "aiops-dev-prometheus": "registry.cn-hangzhou.aliyuncs.com/timelessmao/aiops-mcp-prometheus@sha256:81589cb7eb50e0f244fdef1ed202fca189fa5d11e4f337f602d0fdb5c32d27bc",
    "aiops-dev-loki": "registry.cn-hangzhou.aliyuncs.com/timelessmao/aiops-mcp-loki@sha256:2e38540cdd0c9ad6e552090072fd1adf5a38d6e347c32b31a2b256334f2e699b",
    "payment-api": "registry.cn-hangzhou.aliyuncs.com/timelessmao/aiops-mcp-prometheus@sha256:81589cb7eb50e0f244fdef1ed202fca189fa5d11e4f337f602d0fdb5c32d27bc",
}
SERVICE_REPOSITORIES = {
    "aiops-gateway": "registry.cn-hangzhou.aliyuncs.com/timelessmao/aiops-gateway",
    "aiops-connector": "registry.cn-hangzhou.aliyuncs.com/timelessmao/aiops-connectors",
    "aiops-hermes": "registry.cn-hangzhou.aliyuncs.com/timelessmao/aiops-hermes",
    "aiops-mcp-prometheus": "registry.cn-hangzhou.aliyuncs.com/timelessmao/aiops-mcp-prometheus",
    "aiops-mcp-loki": "registry.cn-hangzhou.aliyuncs.com/timelessmao/aiops-mcp-loki",
}
SHARED_HUB_REPOSITORY = "registry.cn-hangzhou.aliyuncs.com/timelessmao/hub"

RC_IMAGE_SOURCE_HEAD = "9f9aafd941cb47b61a955ecb8f868e7a53b5c77d"
RC_IMAGE_SOURCE_SHORT_SHA = "9f9aafd"
RC_IMAGE_SOURCE_RUN = "https://github.com/Timeless0927/AIops/actions/runs/27344249151"
RC_JOB_NAME = f"aiops-loki-synthetic-log-rc-{RC_IMAGE_SOURCE_SHORT_SHA}"
LEGACY_AIOPS_DIGEST = "sha256:6df1cbdcf6cc53d4ef6c64565b4b6a7bf0f41f0e29153765edddeacf2491c053"
PREVIOUS_RC_IMAGE_SOURCE_HEAD = "e3f08110e27ba2a65504bae0b12350b56f0f8c5e"
OLD_RC_IMAGE_SOURCE_HEAD = "c534da7e949c7b9adc9bdd832c61894068acada4"
STALE_RC_IMAGE_SOURCE_HEAD = "751ad23453eb329d5412dcec9054993ae306dfdd"
STALE_FAB2E7C_IMAGE_SOURCE_HEAD = "fab2e7c15eea5a0cfc334485bbcd8ef3d4230dee"
STALE_62805AF_IMAGE_SOURCE_HEAD = "62805af81175d12f45eb49b695895e9268ef77f9"
STALE_C63496F_IMAGE_SOURCE_HEAD = "c63496f84b67da88d5c999c83e6835beecd65e9a"
STALE_RC_JOB_NAMES = {
    "aiops-loki-synthetic-log-rc-e3f0811",
    "aiops-loki-synthetic-log-rc-fab2e7c",
    "aiops-loki-synthetic-log-rc-62805af",
    "aiops-loki-synthetic-log-rc-c63496f",
}
STALE_RC_DIGESTS = {
    "sha256:76a61bcf5109b3bb3d1b23574857a20a651cd34914aa911571c250e2355832c6",
    "sha256:95eaa23f79aa43c2e54cee86549f3b3bcf32e5fd8740849a76bf07afce18bde1",
    "sha256:531321894b90c2dd2f670bd53561d48e915e407b80121917c71bde25355dc4e6",
    "sha256:f71bce14a8c1191ac13c97d70642fa8576b5f06a8e6b7355a90a5d90543f7589",
    "sha256:90a8dcfc7800e266006cb7adce996990f33578bfc23a03acbb58b230eed14c20",
    "sha256:3ea47706bb2f799a9b7d25c9d16b9129b883f3e4f7ba1ad9cc26fa45030956b6",
    "sha256:1c0cd5dc8b2f1e16c59951df8b6f089a3efbd89098340f51d685dc56176c0a94",
    "sha256:936eeaf9949c184c9aa64a2ea693f7093437971b8b3f4a6046668f84bbb550f7",
    "sha256:11cf3858fd7f8af0809ae5535f14d8b84cc345c4f4713393ec81f140076a3a54",
    "sha256:40739bb03bc97f9e2acf696abf6882c687011d3a65c07a7125da68e04a0214de",
    "sha256:da67f99f8aa6d299f24c82d858ccc32038f62746dcdba536cbf7c791dd53f3e1",
    "sha256:eb1ec02561f938125be28872ed78c3e91cf7f5283f0dcfcc421e88929c33dab9",
}


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
        "aiops-gateway": ("gateway", "registry.cn-hangzhou.aliyuncs.com/timelessmao/aiops-gateway:latest", 8080),
        "aiops-connector": ("connector", "registry.cn-hangzhou.aliyuncs.com/timelessmao/aiops-connectors:latest", 8081),
        "aiops-hermes": ("hermes", "registry.cn-hangzhou.aliyuncs.com/timelessmao/aiops-hermes:latest", 8082),
        "aiops-mcp-prometheus": (
            "mcp-prometheus",
            "registry.cn-hangzhou.aliyuncs.com/timelessmao/aiops-mcp-prometheus:latest",
            8083,
        ),
        "aiops-mcp-loki": ("mcp-loki", "registry.cn-hangzhou.aliyuncs.com/timelessmao/aiops-mcp-loki:latest", 8084),
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
    assert "registry.cn-hangzhou.aliyuncs.com/timelessmao/aiops-mcp-prometheus:latest" in bundled
    assert "registry.cn-hangzhou.aliyuncs.com/timelessmao/aiops-mcp-loki:latest" in bundled
    assert SHARED_HUB_REPOSITORY not in bundled
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
    assert "registry.cn-hangzhou.aliyuncs.com/timelessmao/aiops-gateway@sha256:<gateway-digest>" in readme
    for repository in SERVICE_REPOSITORIES.values():
        assert repository in readme
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
    assert "Bundled dev/test observability components intentionally reuse" in readme


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
        assert not image.startswith(f"{SHARED_HUB_REPOSITORY}@")

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
    assert not any(image.startswith(f"{SHARED_HUB_REPOSITORY}@") for image in images)


def test_rc_digest_overlay_and_readme_reference_current_head_digest_evidence() -> None:
    overlay = Path("deploy/k8s/overlays/rc-bundled-digest/kustomization.yaml").read_text(encoding="utf-8")
    readme = Path("deploy/k8s/README.md").read_text(encoding="utf-8")
    rendered = subprocess.run(
        ["kubectl", "kustomize", "deploy/k8s/overlays/rc-bundled-digest"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    combined = f"{overlay}\n{readme}\n{rendered}"

    assert PREVIOUS_RC_IMAGE_SOURCE_HEAD not in combined
    assert OLD_RC_IMAGE_SOURCE_HEAD not in combined
    assert STALE_RC_IMAGE_SOURCE_HEAD not in combined
    assert STALE_FAB2E7C_IMAGE_SOURCE_HEAD not in combined
    assert STALE_62805AF_IMAGE_SOURCE_HEAD not in combined
    assert STALE_C63496F_IMAGE_SOURCE_HEAD not in combined
    for stale_job_name in STALE_RC_JOB_NAMES:
        assert stale_job_name not in combined
    for stale_digest in STALE_RC_DIGESTS:
        assert stale_digest not in combined
    assert f"{SHARED_HUB_REPOSITORY}@" not in combined
    assert "aiops-loki-synthetic-log-rc\"" not in overlay
    for image in IMAGE_DIGESTS.values():
        assert image in overlay
        assert image.split("@", 1)[1] in readme
        assert image in rendered
    assert LEGACY_AIOPS_DIGEST in readme

    assert RC_IMAGE_SOURCE_HEAD in overlay
    assert RC_IMAGE_SOURCE_HEAD in readme
    assert RC_IMAGE_SOURCE_HEAD in rendered
    assert RC_IMAGE_SOURCE_SHORT_SHA in overlay
    assert RC_IMAGE_SOURCE_SHORT_SHA in readme
    assert RC_IMAGE_SOURCE_SHORT_SHA in rendered
    assert RC_IMAGE_SOURCE_RUN in overlay
    assert RC_IMAGE_SOURCE_RUN in readme
    assert RC_IMAGE_SOURCE_RUN in rendered


def test_rc_digest_job_name_is_head_scoped_for_retained_namespace_reapply() -> None:
    rendered = _by_kind_name(_kustomize_docs("deploy/k8s/overlays/rc-bundled-digest"))
    job = rendered[("Job", RC_JOB_NAME)]

    assert job["metadata"]["name"] == RC_JOB_NAME
    assert job["metadata"]["labels"]["app.kubernetes.io/name"] == RC_JOB_NAME
    assert job["spec"]["template"]["metadata"]["labels"]["app.kubernetes.io/name"] == RC_JOB_NAME
    assert RC_IMAGE_SOURCE_SHORT_SHA in job["metadata"]["name"]
    assert ("Job", "aiops-loki-synthetic-log-rc") not in rendered
