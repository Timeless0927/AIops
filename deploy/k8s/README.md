# AIOps Agent Kubernetes Deployment

## Build image

```bash
docker build -f Dockerfile.aiops -t aiops-agent:latest .
```

## Runtime config

`deploy/entrypoint.sh` renders `/root/.hermes/config.yaml` from `deploy/hermes-config.template.yaml`.

Required runtime envs:

- `FEISHU_APP_ID`
- `FEISHU_APP_SECRET`
- `FEISHU_MAIN_CHAT_ID`
- `AIOPS_MODEL_BASE_URL`
- `AIOPS_MODEL_API_KEY`
- `AIOPS_SRE_ADMIN_NAME`
- `AIOPS_SRE_ADMIN_OPEN_ID`
- `AIOPS_SRE_OPERATOR_NAME`
- `AIOPS_SRE_OPERATOR_OPEN_ID`
- `AIOPS_APPROVAL_ALLOW_SELF_APPROVAL_LOW_RISK`
- `AIOPS_APPROVAL_REQUIRE_ADMIN_FOR_EXEC`
- `AIOPS_APPROVAL_REQUIRE_ADMIN_FOR_DANGEROUS`

The generated runtime config carries `sre_permissions` and keeps Feishu authorization aligned with the deployment config.
`deploy/entrypoint.sh` renders the template into `/root/.hermes/config.yaml`; the image already contains the template under `/app/deploy/hermes-config.template.yaml`.

## Prepare secrets

Copy `deploy/k8s/secret.example.yaml` to a real secret manifest and replace all placeholder values.
Keep the `ConfigMap` values in `deploy/k8s/configmap.yaml` in sync with the runtime operator names, open IDs, and approval policy flags.

## Apply manifests

```bash
kubectl apply -f deploy/k8s/namespace.yaml
kubectl apply -f deploy/k8s/configmap.yaml
kubectl apply -f deploy/k8s/secret.yaml
kubectl apply -f deploy/k8s/serviceaccount.yaml
kubectl apply -f deploy/k8s/rbac.yaml
kubectl apply -f deploy/k8s/pvc.yaml
kubectl apply -f deploy/k8s/deployment.yaml
kubectl apply -f deploy/k8s/service.yaml
```

You can also apply the directory after creating `deploy/k8s/secret.yaml`:

```bash
kubectl apply -f deploy/k8s
```

## Verify runtime

```bash
kubectl -n aiops get pods
kubectl -n aiops logs deploy/aiops-agent
kubectl -n aiops exec deploy/aiops-agent -- kubectl auth can-i get pods
```

For the first cluster pass, keep `AIOPS_APPROVAL_EXECUTION_WORKER_ENABLED` unset for production behavior or set it to `0` for approval/card-only smoke tests.

## Alertmanager target

Point Alertmanager to the webhook service URL ending with `/webhooks/alertmanager`, for example:

```text
http://aiops-agent-webhook.aiops.svc.cluster.local:8765/webhooks/alertmanager
```
