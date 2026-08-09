"""Gateway HTTP adapter boundary checks."""

import inspect

from apps.aiops_k8s_gateway import chat_http


def test_chat_adapter_exposes_a_narrow_dispatch_seam() -> None:
    assert tuple(inspect.signature(chat_http.ChatHTTPAdapter.dispatch).parameters) == (
        "self",
        "handler",
        "route_path",
    )
