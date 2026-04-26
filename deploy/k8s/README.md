# AIOps Agent Kubernetes Deployment

## Build image

```bash
docker build -f Dockerfile.aiops -t aiops-agent:latest .
```

## Prepare secrets

Copy `deploy/k8s/secret.example.yaml` to a real secret manifest and replace all placeholder values.

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

## Alertmanager target

Point Alertmanager to the webhook service URL ending with `/webhooks/alertmanager`, for example:

```text
http://aiops-agent-webhook.aiops.svc.cluster.local:8765/webhooks/alertmanager
```
