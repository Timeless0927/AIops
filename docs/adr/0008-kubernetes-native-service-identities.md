# Internal services use Kubernetes-native identities

Status: accepted

AIOps V1 officially supports Kubernetes deployment only. Each internal process runs under a dedicated ServiceAccount and calls peers with a short-lived projected ServiceAccount token scoped to the `aiops-internal` audience; receivers authenticate it through Kubernetes TokenReview and authorize the resulting ServiceAccount identity. Kubernetes owns token issuance and rotation, so AIOps does not distribute static internal service secrets. Browser sessions, Alertmanager ingress, and per-Connector Enrollment remain separate identity mechanisms. Docker Compose may remain as a development or image smoke tool but is not a supported product deployment mode.
