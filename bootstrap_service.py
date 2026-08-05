from __future__ import annotations

import base64
import json
import os
import secrets
import ssl
import sys
from collections.abc import Callable
from pathlib import Path
from urllib import error, parse, request


RUNTIME_SECRET = "aiops-runtime-secret"
MARKER = "aiops-bootstrap-state"
SECRET_KEYS = {
    RUNTIME_SECRET: ("AIOPS_BOOTSTRAP_ADMIN_PASSWORD", "AIOPS_ALERTMANAGER_WEBHOOK_TOKEN"),
    "aiops-model-encryption": ("key",),
    "aiops-notification-encryption": ("key",),
    "aiops-change-encryption": ("key",),
    "aiops-mcp-encryption": ("key",),
}


class BootstrapError(RuntimeError):
    pass


class KubernetesApi:
    def __init__(self, namespace: str, *, host: str, token: str, ca_file: str) -> None:
        self._base = f"{host}/api/v1/namespaces/{parse.quote(namespace, safe='')}"
        self._token = token
        self._context = ssl.create_default_context(cafile=ca_file)

    @classmethod
    def from_cluster(cls, namespace: str) -> KubernetesApi:
        host = os.environ["KUBERNETES_SERVICE_HOST"]
        port = os.environ.get("KUBERNETES_SERVICE_PORT_HTTPS", "443")
        service_account = Path("/var/run/secrets/kubernetes.io/serviceaccount")
        return cls(
            namespace,
            host=f"https://{host}:{port}",
            token=(service_account / "token").read_text(encoding="utf-8").strip(),
            ca_file=str(service_account / "ca.crt"),
        )

    def get_secret(self, name: str) -> dict | None:
        return self._get("secrets", name)

    def create_secret(self, body: dict) -> bool:
        return self._create("secrets", body)

    def get_marker(self) -> dict | None:
        return self._get("configmaps", MARKER)

    def create_marker(self, body: dict) -> bool:
        return self._create("configmaps", body)

    def _get(self, resource: str, name: str) -> dict | None:
        try:
            return self._send("GET", f"{resource}/{parse.quote(name, safe='')}")
        except error.HTTPError as exc:
            if exc.code == 404:
                return None
            raise BootstrapError(f"kubernetes_api_error:{exc.code}") from exc

    def _create(self, resource: str, body: dict) -> bool:
        try:
            self._send("POST", resource, body)
        except error.HTTPError as exc:
            if exc.code == 409:
                return False
            raise BootstrapError(f"kubernetes_api_error:{exc.code}") from exc
        return True

    def _send(self, method: str, path: str, body: dict | None = None) -> dict:
        payload = json.dumps(body).encode() if body is not None else None
        req = request.Request(
            f"{self._base}/{path}",
            data=payload,
            method=method,
            headers={"Authorization": f"Bearer {self._token}", "Content-Type": "application/json"},
        )
        with request.urlopen(req, context=self._context, timeout=10) as response:
            return json.load(response)


def reconcile(api: KubernetesApi, namespace: str, *, random_bytes: Callable[[int], bytes] = secrets.token_bytes) -> None:
    marker = api.get_marker()
    if marker is not None and not _valid_marker(marker):
        raise BootstrapError("bootstrap_marker_invalid")
    completed = marker is not None
    existing = {name: api.get_secret(name) for name in SECRET_KEYS}

    invalid = [
        name
        for name, keys in SECRET_KEYS.items()
        if existing[name] is None or not all(existing[name].get("data", {}).get(key) for key in keys)
    ]
    if completed:
        if invalid:
            raise BootstrapError("bootstrap_secret_lost")
        return

    for name in invalid:
        if existing[name] is not None:
            raise BootstrapError("bootstrap_secret_incomplete")
        if not api.create_secret(_new_secret(name, namespace, random_bytes)):
            raced = api.get_secret(name)
            if raced is None or not all(raced.get("data", {}).get(key) for key in SECRET_KEYS[name]):
                raise BootstrapError("bootstrap_secret_incomplete")

    marker_body = {
        "apiVersion": "v1",
        "kind": "ConfigMap",
        "metadata": {"name": MARKER, "namespace": namespace},
        "immutable": True,
        "data": {"state": "complete"},
    }
    if not api.create_marker(marker_body) and not _valid_marker(api.get_marker()):
        raise BootstrapError("bootstrap_marker_invalid")


def _valid_marker(marker: dict | None) -> bool:
    return bool(marker and marker.get("immutable") is True and marker.get("data") == {"state": "complete"})


def _new_secret(name: str, namespace: str, random_bytes: Callable[[int], bytes]) -> dict:
    if name == RUNTIME_SECRET:
        values = {
            key: base64.urlsafe_b64encode(random_bytes(32)).rstrip(b"=")
            for key in SECRET_KEYS[name]
        }
    else:
        values = {"key": random_bytes(32)}
    return {
        "apiVersion": "v1",
        "kind": "Secret",
        "metadata": {"name": name, "namespace": namespace},
        "type": "Opaque",
        "data": {key: base64.b64encode(value).decode("ascii") for key, value in values.items()},
    }


def main() -> int:
    namespace = os.environ.get("POD_NAMESPACE", "aiops-system")
    try:
        reconcile(KubernetesApi.from_cluster(namespace), namespace)
    except Exception as exc:
        code = str(exc) if isinstance(exc, BootstrapError) else "bootstrap_failed"
        print(code, file=sys.stderr)
        return 1
    print("bootstrap_complete")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
