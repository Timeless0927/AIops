# Toolsets

Backend-local tool implementations used by Gateway, diagnosis, Connector, and MCP services.

- Register local tools through `toolsets.registry`.
- Keep runtime process code in `apps/`, `diagnosis_service/`, and `runtime/`.
- Keep Kubernetes, observability, incident, approval, audit, and notification helpers here only when a current backend path imports them.

Smoke:

```bash
SERVICE_NAME=gateway python3 -m runtime.service_image_smoke
```
