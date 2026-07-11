"""Resource Binding guard for legacy grant paths pending T13 replacement."""

from __future__ import annotations

from http import HTTPStatus
from typing import Callable

from apps.service_http import JsonHandler

from .resource_catalog import ResourceCatalog


def proposal_denial(
    catalog: ResourceCatalog,
    target: dict[str, object],
    request_id: str,
    error_payload: Callable[[str, str, str], dict[str, object]],
) -> tuple[HTTPStatus, dict[str, object]] | None:
    error = catalog.execution_target_error(target)
    if error is None:
        return None
    return HTTPStatus.CONFLICT, error_payload(error.code, error.message, request_id)


def deny_approval(
    handler: JsonHandler,
    catalog: ResourceCatalog,
    action_record: dict[str, object] | None,
    request_id: str,
    error_payload: Callable[[str, str, str], dict[str, object]],
) -> bool:
    denial = proposal_denial(catalog, action_record["target"], request_id, error_payload) if action_record else None
    if denial is None:
        return False
    handler.write_json(*denial)
    return True
