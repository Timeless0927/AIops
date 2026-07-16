"""Real MCP Loki probes for R04 without synthetic log injection."""

from __future__ import annotations

import hashlib
import json
import math
import time
from datetime import datetime, timezone

from .command import CommandExecutor, CommandResult
from .http import GatewaySession
from .recovery import NAMESPACE, RecoveryScope
from .run_one_decisions import valid_run_id


_MCP_SCRIPT = """import json,sys,urllib.request
token=open('/var/run/secrets/aiops-internal/token',encoding='utf-8').read().strip()
body=sys.argv[1].encode()
request=urllib.request.Request('http://127.0.0.1:8084/query_logs',data=body,headers={'Authorization':'Bearer '+token,'Content-Type':'application/json'},method='POST')
with urllib.request.urlopen(request,timeout=30) as response: print(response.read().decode())
"""


class KubectlLokiDependencyProbe:
    """Calls the real MCP and creates one fresh log through a read-only Gateway request."""

    def __init__(
        self,
        commands: CommandExecutor,
        gateway: GatewaySession,
        *,
        kube_context: str,
        sleep=time.sleep,
        monotonic=time.monotonic,
    ) -> None:
        if not kube_context:
            raise ValueError("Loki dependency probe requires an exact kube context")
        self.commands = commands
        self.gateway = gateway
        self.kube_context = kube_context
        self.sleep = sleep
        self.monotonic = monotonic

    def unavailable(self, scope: RecoveryScope, *, request_id: str) -> dict[str, object]:
        envelope = self._query(
            scope,
            request_id=self._request_id(request_id, "unavailable"),
            query=self._retained_query(scope),
            time_range=self._retained_window(scope),
            namespace="aiops-verification",
            service="verification-api",
        )
        errors = envelope.get("errors")
        evidence_refs = envelope.get("evidence_refs", [])
        error = errors[0] if isinstance(errors, list) and len(errors) == 1 else None
        if (
            envelope.get("status") != "failed"
            or not isinstance(error, dict)
            or error.get("code") != "backend_unavailable"
            or evidence_refs not in ([], None)
        ):
            raise ValueError("R04 MCP did not return bounded Loki unavailability")
        return {
            "status": "failed", "error_code": "backend_unavailable", "evidence_refs": [],
        }

    def recovered(self, scope: RecoveryScope, *, request_id: str) -> dict[str, object]:
        marker = self._request_id(request_id, "fresh")
        response = self.gateway.request("GET", "/healthz", request_id=marker)
        if response.status != 200 or response.body.get("status") != "ok":
            raise RuntimeError("R04 fresh Gateway log probe failed")
        retained = self._query_until_match(
            scope,
            request_id=self._request_id(request_id, "retained"),
            query=self._retained_query(scope),
            time_range=self._retained_window(scope),
            namespace="aiops-verification",
            service="verification-api",
        )
        fresh = self._query_until_match(
            scope,
            request_id=self._request_id(request_id, "fresh-query"),
            query=(
                '{namespace="aiops-system",app="aiops-gateway"} '
                f'|= "\\\"request_id\\\":\\\"{marker}\\\""'
            ),
            time_range={"type": "relative", "value": "5m"},
            namespace="aiops-system",
            service="aiops-gateway",
        )
        return {
            "retained_log_refs_sha256": scope.recovery_log_refs_sha256,
            "retained_query": retained,
            "fresh_probe_request_id": marker,
            "fresh_query": fresh,
        }

    def _query_until_match(
        self,
        scope: RecoveryScope,
        *,
        request_id: str,
        query: str,
        time_range: dict[str, str],
        namespace: str,
        service: str,
    ) -> dict[str, object]:
        deadline = self.monotonic() + 120
        while True:
            envelope = self._query(
                scope,
                request_id=request_id,
                query=query,
                time_range=time_range,
                namespace=namespace,
                service=service,
            )
            normalized = self._success(envelope)
            if normalized is not None:
                return normalized
            remaining = deadline - self.monotonic()
            if remaining <= 0:
                raise TimeoutError("R04 real Loki probe log did not become queryable")
            self.sleep(min(2.0, remaining))

    def _query(
        self,
        scope: RecoveryScope,
        *,
        request_id: str,
        query: str,
        time_range: dict[str, str],
        namespace: str,
        service: str,
    ) -> dict[str, object]:
        if not valid_run_id(request_id):
            raise ValueError("R04 MCP request identity is invalid")
        body = {
            "request_id": request_id,
            "correlation_id": scope.run_id,
            "cluster_id": scope.cluster_id,
            "namespace": namespace,
            "service": service,
            "reason": "R04 verify real Loki dependency recovery",
            "query": query,
            "time_range": time_range,
            "response_mode": "summary_samples",
            "max_lines": 20,
            "sample_size": 1,
        }
        result = self.commands.run([
            "kubectl", "--context", self.kube_context, "-n", NAMESPACE,
            "exec", "deployment/aiops-mcp-loki", "--",
            "python3", "-c", _MCP_SCRIPT,
            json.dumps(body, sort_keys=True, separators=(",", ":")),
        ], timeout=60)
        return self._json(result)

    @staticmethod
    def _success(envelope: dict[str, object]) -> dict[str, object] | None:
        data = envelope.get("data")
        refs = envelope.get("evidence_refs")
        if envelope.get("status") != "succeeded" or not isinstance(data, dict):
            return None
        matched = data.get("total_matched")
        ref = refs[0] if isinstance(refs, list) and len(refs) == 1 else None
        if (
            not isinstance(matched, int)
            or matched < 1
            or not isinstance(ref, dict)
            or ref.get("source") != "loki"
            or not ref.get("ref_id")
        ):
            return None
        return {
            "status": "succeeded", "matched": matched,
            "evidence_ref": {"source": "loki", "ref_id": ref["ref_id"]},
        }

    @staticmethod
    def _retained_query(scope: RecoveryScope) -> str:
        return (
            '{namespace="aiops-verification",container="verification-api"} '
            f'|= "{scope.run_id}" |= "verification_fault_recovered"'
        )

    @staticmethod
    def _retained_window(scope: RecoveryScope) -> dict[str, str]:
        observed_at = float(scope.recovery_log_observed_at)
        if not math.isfinite(observed_at) or observed_at < 0:
            raise ValueError("R04 retained log timestamp is invalid")
        start = datetime.fromtimestamp(max(0.0, observed_at - 1), tz=timezone.utc)
        end = datetime.fromtimestamp(observed_at + 1, tz=timezone.utc)
        return {
            "type": "absolute",
            "value": f"{start.isoformat().replace('+00:00', 'Z')}/{end.isoformat().replace('+00:00', 'Z')}",
        }

    @staticmethod
    def _request_id(operation_id: str, suffix: str) -> str:
        digest = hashlib.sha256(f"{operation_id}:{suffix}".encode()).hexdigest()[:24]
        return f"h20-r04-{suffix}-{digest}"

    @staticmethod
    def _json(result: CommandResult) -> dict[str, object]:
        if result.exit_code != 0:
            raise RuntimeError("R04 MCP query command failed")
        value = json.loads(result.stdout)
        if not isinstance(value, dict):
            raise RuntimeError("R04 MCP query returned a non-object")
        return value
