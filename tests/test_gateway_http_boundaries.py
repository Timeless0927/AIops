"""Gateway HTTP adapter boundary checks."""

import inspect
from pathlib import Path

from apps.aiops_k8s_gateway import chat_http
from apps.aiops_k8s_gateway import main as gateway_main


def test_chat_adapter_exposes_a_narrow_dispatch_seam() -> None:
    assert tuple(inspect.signature(chat_http.ChatHTTPAdapter.dispatch).parameters) == (
        "self",
        "handler",
        "route_path",
    )
    assert tuple(inspect.signature(gateway_main._chat_http).parameters) == ()
    dependencies = set(inspect.signature(chat_http.ChatHTTPAdapter).parameters)
    assert "sessions" not in dependencies
    assert {"actor_view", "connector_status"} <= dependencies
    assert not Path(chat_http.__file__).with_name("gateway_http_runtime.py").exists()
