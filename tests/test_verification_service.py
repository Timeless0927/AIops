from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request
from http import HTTPStatus

import pytest

from verification_service import FaultLatch, TriggerOutcome
from verification_service.server import create_servers
from verification_service.trigger import MAX_ACK_BYTES, read_ack


RUN_ID = "7a708c9f-7252-40d6-9410-f0d38b8818e7"


def _request(url: str, *, body: dict[str, object] | None = None) -> tuple[int, bytes]:
    request = urllib.request.Request(
        url,
        data=json.dumps(body).encode() if body is not None else None,
        headers={"Content-Type": "application/json"} if body is not None else {},
        method="POST" if body is not None else "GET",
    )
    try:
        with urllib.request.urlopen(request, timeout=2) as response:
            return response.status, response.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()


def test_fault_latch_accepts_one_bounded_run_and_replays_only_that_run() -> None:
    latch = FaultLatch()

    assert latch.trigger(RUN_ID) == TriggerOutcome.ACTIVATED
    assert latch.trigger(RUN_ID) == TriggerOutcome.REPLAYED
    assert latch.trigger("89b7c017-7b5d-40cd-b676-9fbbe35b9e47") == TriggerOutcome.CONFLICT
    assert latch.snapshot().active is True
    assert latch.snapshot().run_id == RUN_ID

    for invalid in ("", "UPPERCASE", "contains/slash", "x" * 65):
        with pytest.raises(ValueError):
            FaultLatch().trigger(invalid)


def test_rollout_starts_ready_with_the_recovered_run_identity() -> None:
    snapshot = FaultLatch(RUN_ID).snapshot()

    assert snapshot.run_id == RUN_ID
    assert snapshot.active is False


def test_http_ports_expose_live_ready_metrics_and_idempotent_internal_trigger() -> None:
    latch = FaultLatch()
    servers = create_servers(latch, "127.0.0.1", (0, 0, 0))
    threads = [threading.Thread(target=server.serve_forever, daemon=True) for server in servers]
    for thread in threads:
        thread.start()
    live = f"http://127.0.0.1:{servers[0].server_port}"
    trigger = f"http://127.0.0.1:{servers[1].server_port}/trigger"
    metrics = f"http://127.0.0.1:{servers[2].server_port}/metrics"
    try:
        assert _request(f"{live}/livez") == (HTTPStatus.OK, b"live\n")
        assert _request(f"{live}/readyz") == (HTTPStatus.OK, b"ready\n")

        activated, body = _request(trigger, body={"run_id": RUN_ID})
        replayed, replay_body = _request(trigger, body={"run_id": RUN_ID})
        conflict, _ = _request(
            trigger,
            body={"run_id": "89b7c017-7b5d-40cd-b676-9fbbe35b9e47"},
        )

        assert activated == replayed == HTTPStatus.OK
        assert json.loads(body)["outcome"] == "activated"
        assert json.loads(replay_body)["outcome"] == "replayed"
        assert conflict == HTTPStatus.CONFLICT
        assert _request(f"{live}/livez")[0] == HTTPStatus.OK
        assert _request(f"{live}/readyz")[0] == HTTPStatus.SERVICE_UNAVAILABLE
        metric_status, metric_body = _request(metrics)
        assert metric_status == HTTPStatus.OK
        assert (
            f'aiops_verification_fault_active{{service="verification-api",run_id="{RUN_ID}"}} 1'
            in metric_body.decode()
        )
    finally:
        for server in servers:
            server.shutdown()
            server.server_close()


def test_trigger_acknowledgement_is_bounded_and_exact() -> None:
    class Response:
        def __init__(self, body: bytes) -> None:
            self.body = body

        def read(self, limit: int) -> bytes:
            assert limit == MAX_ACK_BYTES + 1
            return self.body[:limit]

    assert read_ack(
        Response(json.dumps({"run_id": RUN_ID, "outcome": "activated"}).encode()), RUN_ID
    ) == {"run_id": RUN_ID, "outcome": "activated"}
    for body in (
        b"[]",
        b'{"run_id":"wrong","outcome":"activated"}',
        b'{"run_id":"' + RUN_ID.encode() + b'","outcome":42}',
        b"x" * (MAX_ACK_BYTES + 1),
    ):
        with pytest.raises(RuntimeError):
            read_ack(Response(body), RUN_ID)
