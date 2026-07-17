"""Durable Console mutation identity binding for headless browser adapters."""

from __future__ import annotations

import json
import re
import secrets
import threading
from dataclasses import dataclass
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from itertools import islice
from typing import Callable, Iterable

from .credentials import CredentialError, assert_public_payload
from .evidence import AcceptanceEvidence


_REQUEST_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9:._-]{0,127}")
_MAX_CALLBACK_BYTES = 32 * 1024
_MUTATION_METHODS = frozenset({"POST", "PATCH", "PUT", "DELETE"})


class BrowserMutationError(ValueError):
    pass


@dataclass(frozen=True)
class BrowserMutationFact:
    request_id: str
    method: str
    path: str
    status: int
    response_request_id: str
    identities: dict[str, str | int]
    error_code: str | None = None


class BrowserMutationBinding:
    """Bind browser request IDs before route continuation and persist terminal response facts."""

    def __init__(self, evidence: AcceptanceEvidence, gate_id: str) -> None:
        self._evidence = evidence
        self._gate_id = gate_id
        self._token = secrets.token_urlsafe(32)
        self._bound: dict[str, tuple[str, str]] = {}
        self._facts: list[BrowserMutationFact] = []
        self._lock = threading.Lock()
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    def __enter__(self) -> "BrowserMutationBinding":
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:  # noqa: N802
                if self.headers.get("Authorization") != f"Bearer {owner._token}":
                    self.send_error(HTTPStatus.UNAUTHORIZED)
                    return
                try:
                    length = int(self.headers.get("Content-Length", "0"))
                    if length <= 0 or length > _MAX_CALLBACK_BYTES:
                        raise BrowserMutationError("browser mutation callback is unbounded")
                    payload = json.loads(self.rfile.read(length))
                    if self.path == "/intent":
                        owner._bind(payload)
                    elif self.path == "/result":
                        owner._record(payload)
                    else:
                        self.send_error(HTTPStatus.NOT_FOUND)
                        return
                except (ValueError, json.JSONDecodeError) as exc:
                    body = json.dumps({"error": str(exc)}, separators=(",", ":")).encode()
                    self.send_response(HTTPStatus.BAD_REQUEST)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                    return
                self.send_response(HTTPStatus.NO_CONTENT)
                self.end_headers()

            def log_message(self, _format: str, *_args: object) -> None:
                return

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()
        return self

    def __exit__(self, *_args: object) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
        if self._thread is not None:
            self._thread.join(timeout=2)

    @property
    def callback(self) -> dict[str, str]:
        if self._server is None:
            raise BrowserMutationError("browser mutation binding is not active")
        return {
            "url": f"http://127.0.0.1:{self._server.server_address[1]}",
            "token": self._token,
        }

    @property
    def facts(self) -> tuple[BrowserMutationFact, ...]:
        with self._lock:
            return tuple(self._facts)

    def _bind(self, payload: object) -> None:
        if not isinstance(payload, dict):
            raise BrowserMutationError("browser mutation intent must be an object")
        request_id = _request_id(payload.get("request_id"))
        method = str(payload.get("method") or "").upper()
        path = str(payload.get("path") or "")
        if method not in _MUTATION_METHODS or not path.startswith("/api/v1/") or len(path) > 500:
            raise BrowserMutationError("browser mutation intent is outside the Console API boundary")
        with self._lock:
            if request_id in self._bound:
                raise BrowserMutationError("browser mutation request identity was already bound")
            self._evidence.bind_operation(
                self._gate_id,
                kind="console_mutation",
                operation_id=request_id,
            )
            self._bound[request_id] = (method, path)

    def _record(self, payload: object) -> None:
        if not isinstance(payload, dict):
            raise BrowserMutationError("browser mutation result must be an object")
        request_id = _request_id(payload.get("request_id"))
        response_request_id = _request_id(payload.get("response_request_id"))
        if response_request_id != request_id:
            raise BrowserMutationError("browser mutation response request identity mismatch")
        status = payload.get("status")
        identities = payload.get("identities")
        error_code = payload.get("error_code")
        if not isinstance(status, int) or isinstance(status, bool) or not 100 <= status <= 599:
            raise BrowserMutationError("browser mutation result status is invalid")
        if not isinstance(identities, dict) or len(identities) > 32:
            raise BrowserMutationError("browser mutation response identities are invalid")
        normalized: dict[str, str | int] = {}
        for key, value in identities.items():
            if not isinstance(key, str) or len(key) > 100:
                raise BrowserMutationError("browser mutation identity key is invalid")
            if not isinstance(value, (str, int)) or isinstance(value, bool):
                raise BrowserMutationError("browser mutation identity value is invalid")
            if isinstance(value, str) and (not value or len(value) > 300):
                raise BrowserMutationError("browser mutation identity value is invalid")
            normalized[key] = value
        if error_code is not None and (
            not isinstance(error_code, str)
            or not _REQUEST_ID.fullmatch(error_code)
        ):
            raise BrowserMutationError("browser mutation response error code is invalid")
        if 200 <= status < 300:
            if error_code is not None:
                raise BrowserMutationError("successful browser mutation returned an error code")
            if not _has_object_revision(normalized):
                raise BrowserMutationError(
                    "browser mutation response lacks object and revision identity"
                )
        elif error_code is None and not _has_object_revision(normalized):
            raise BrowserMutationError(
                "failed browser mutation lacks an error code or object identity"
            )
        fact = BrowserMutationFact(
            request_id, *self._bound_fact(request_id), status, response_request_id,
            normalized, error_code,
        )
        public_fact = {
            "request_id": fact.request_id,
            "method": fact.method,
            "path": fact.path,
            "status": fact.status,
            "response_request_id": fact.response_request_id,
            "identities": fact.identities,
        }
        if fact.error_code is not None:
            public_fact["error_code"] = fact.error_code
        assert_public_payload(public_fact)
        with self._lock:
            if any(item.request_id == request_id for item in self._facts):
                raise BrowserMutationError("browser mutation result was already recorded")
            self._evidence.reconcile_operation(
                self._gate_id,
                operation_id=request_id,
                outcome="succeeded" if 200 <= status < 300 else "failed",
                public_fact=public_fact,
            )
            self._facts.append(fact)

    def _bound_fact(self, request_id: str) -> tuple[str, str]:
        try:
            return self._bound[request_id]
        except KeyError as exc:
            raise BrowserMutationError("browser mutation result lacks a durable intent") from exc


def reconcile_unique_browser_operation(
    evidence: AcceptanceEvidence,
    gate_id: str,
    operation_id: str,
    lookup: Callable[[str], Iterable[dict[str, object]]],
) -> dict[str, object]:
    """Resume only when one bounded actor-scoped public fact matches the request identity."""
    matches = list(islice(lookup(operation_id), 2))
    if len(matches) != 1:
        evidence.reconcile_operation(
            gate_id,
            operation_id=operation_id,
            outcome="unprovable",
            public_fact={"request_id": operation_id, "match_count": len(matches)},
        )
        raise BrowserMutationError("browser mutation correlation must resolve exactly one public fact")
    fact = matches[0]
    try:
        assert_public_payload(fact)
        if fact.get("request_id") != operation_id:
            raise BrowserMutationError("browser mutation public fact request identity mismatch")
        if not _has_object_revision(fact):
            raise BrowserMutationError("browser mutation public fact lacks object and revision identity")
    except (BrowserMutationError, CredentialError) as exc:
        evidence.reconcile_operation(
            gate_id,
            operation_id=operation_id,
            outcome="unprovable",
            public_fact={"request_id": operation_id, "match_count": 1, "invalid_fact": True},
        )
        raise BrowserMutationError(str(exc)) from exc
    evidence.reconcile_operation(
        gate_id,
        operation_id=operation_id,
        outcome="succeeded",
        public_fact=fact,
    )
    return fact


def _request_id(value: object) -> str:
    if not isinstance(value, str) or not _REQUEST_ID.fullmatch(value):
        raise BrowserMutationError("browser mutation request identity is invalid")
    return value


def _has_object_revision(value: dict[str, object]) -> bool:
    keys = {str(key).lower() for key in value}
    object_identity = any(
        key not in {"request_id", "response_request_id"}
        and (key == "id" or key.endswith(".id") or key.endswith("_id"))
        for key in keys
    )
    revision_identity = any(
        key in {"revision", "revision_id"}
        or key == "sequence" or key.endswith((".sequence", "_sequence"))
        or key.endswith((".revision", ".revision_id", "_revision", "_revision_id"))
        or ("revision" in key and key.endswith(".id"))
        for key in keys
    )
    return object_identity and revision_identity
