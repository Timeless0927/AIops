from __future__ import annotations

import pytest

from toolsets.service_ownership import (
    CMDBClientUnavailable,
    CMDBServiceOwner,
    ServiceOwnershipStore,
    resolve_alert_ownership,
)


class FakeCMDBClient:
    def __init__(self, responses=None, exc: Exception | None = None) -> None:
        self.responses = responses or {}
        self.exc = exc
        self.queries = []

    async def lookup_service_owner(self, candidates):
        self.queries.append(list(candidates))
        if self.exc is not None:
            raise self.exc
        for candidate in candidates:
            if candidate in self.responses:
                return {"service_key": candidate, "owner": self.responses[candidate]}
        return None


@pytest.mark.asyncio
async def test_resolve_alert_ownership_hits_cmdb_and_caches_team_route(tmp_path, **_kwargs):
    store = ServiceOwnershipStore(tmp_path / "ownership.db", stale_after_seconds=300)
    cmdb = FakeCMDBClient(
        {
            "prod-a/payments/checkout-api": CMDBServiceOwner(
                service_id="svc-checkout",
                service_name="checkout-api",
                owner_team="payments-dev",
                notification_channel="oc_payments",
                rbac_scope="team:payments-dev",
                approval_scope="payments-prod",
                source="bk_cmdb",
            )
        }
    )

    result = await resolve_alert_ownership(
        {
            "cluster": "prod-a",
            "namespace": "payments",
            "workload_name": "checkout-api",
            "alertname": "HighErrorRate",
        },
        config={"cmdb": {"default_team": "sre", "default_notification_channel": "oc_sre"}},
        store=store,
        cmdb_client=cmdb,
    )

    assert result["service_id"] == "svc-checkout"
    assert result["owner_team"] == "payments-dev"
    assert result["notification_channel"] == "oc_payments"
    assert result["rbac_scope"] == "team:payments-dev"
    assert result["approval_scope"] == "payments-prod"
    assert result["ownership_source"] == "bk_cmdb"
    assert result["ownership_status"] == "owned"
    assert result["confidence"] == 0.95

    cached = store.get_cached_ownership("prod-a/payments/checkout-api")
    assert cached is not None
    assert cached["owner_team"] == "payments-dev"

    store.close()


@pytest.mark.asyncio
async def test_resolve_alert_ownership_uses_matched_cmdb_candidate_for_result_and_cache(tmp_path, **_kwargs):
    store = ServiceOwnershipStore(tmp_path / "ownership.db", stale_after_seconds=300)
    cmdb = FakeCMDBClient(
        {
            "prod-a/payments/checkout-api": CMDBServiceOwner(
                service_id="svc-checkout",
                service_name="checkout-api",
                owner_team="payments-dev",
                notification_channel="oc_payments",
                source="bk_cmdb",
            )
        }
    )

    result = await resolve_alert_ownership(
        {
            "cluster": "prod-a",
            "namespace": "payments",
            "service": "missing-service",
            "workload_name": "checkout-api",
        },
        config={"cmdb": {"default_team": "sre"}},
        store=store,
        cmdb_client=cmdb,
        now=1000.0,
    )

    assert cmdb.queries == [["prod-a/payments/missing-service", "prod-a/payments/checkout-api"]]
    assert result["service_key"] == "prod-a/payments/checkout-api"
    assert result["owner_team"] == "payments-dev"
    assert store.get_cached_ownership("prod-a/payments/missing-service") is None
    cached = store.get_cached_ownership("prod-a/payments/checkout-api")
    assert cached is not None
    assert cached["owner_team"] == "payments-dev"

    store.close()


@pytest.mark.asyncio
async def test_resolve_alert_ownership_marks_unowned_and_uses_default_team(tmp_path, **_kwargs):
    store = ServiceOwnershipStore(tmp_path / "ownership.db")
    cmdb = FakeCMDBClient()

    result = await resolve_alert_ownership(
        {
            "cluster": "prod-a",
            "namespace": "payments",
            "service": "checkout",
            "alertname": "HighErrorRate",
        },
        config={"cmdb": {"default_team": "sre", "default_notification_channel": "oc_sre"}},
        store=store,
        cmdb_client=cmdb,
    )

    assert result["owner_team"] == "sre"
    assert result["service_id"] == "prod-a/payments/checkout"
    assert result["ownership_status"] == "unowned"
    assert result["ownership_source"] == "default_team"
    assert result["confidence"] == 0.2
    assert result["notification_channel"] == "oc_sre"
    assert "cmdb_owner_missing" in result["warnings"]

    store.close()


@pytest.mark.asyncio
async def test_resolve_alert_ownership_uses_fresh_cache_when_cmdb_unavailable(tmp_path, **_kwargs):
    store = ServiceOwnershipStore(tmp_path / "ownership.db", stale_after_seconds=300)
    store.upsert_ownership(
        service_key="prod-a/payments/checkout",
        service_id="svc-checkout",
        service_name="checkout",
        owner_team="payments-dev",
        notification_channel="oc_payments",
        rbac_scope="team:payments-dev",
        approval_scope="payments-prod",
        source="bk_cmdb",
        confidence=0.95,
        observed_at=1000.0,
    )
    cmdb = FakeCMDBClient(exc=CMDBClientUnavailable("timeout"))

    result = await resolve_alert_ownership(
        {
            "cluster": "prod-a",
            "namespace": "payments",
            "service": "checkout",
        },
        config={"cmdb": {"default_team": "sre", "default_notification_channel": "oc_sre"}},
        store=store,
        cmdb_client=cmdb,
        now=1100.0,
    )

    assert result["owner_team"] == "payments-dev"
    assert result["ownership_status"] == "owned"
    assert result["ownership_source"] == "cache"
    assert result["confidence"] == 0.75
    assert "cmdb_unavailable" in result["warnings"]

    store.close()


@pytest.mark.asyncio
async def test_resolve_alert_ownership_marks_stale_cache_and_defaults_when_cmdb_unavailable(tmp_path, **_kwargs):
    store = ServiceOwnershipStore(tmp_path / "ownership.db", stale_after_seconds=300)
    store.upsert_ownership(
        service_key="prod-a/payments/checkout",
        service_id="svc-checkout",
        service_name="checkout",
        owner_team="payments-dev",
        source="bk_cmdb",
        confidence=0.95,
        observed_at=1000.0,
    )
    cmdb = FakeCMDBClient(exc=CMDBClientUnavailable("timeout"))

    result = await resolve_alert_ownership(
        {
            "cluster": "prod-a",
            "namespace": "payments",
            "service": "checkout",
        },
        config={"cmdb": {"default_team": "sre", "default_notification_channel": "oc_sre"}},
        store=store,
        cmdb_client=cmdb,
        now=2000.0,
    )

    assert result["owner_team"] == "sre"
    assert result["ownership_status"] == "unowned"
    assert result["ownership_source"] == "default_team"
    assert result["confidence"] == 0.1
    assert "cmdb_unavailable" in result["warnings"]
    assert "ownership_cache_stale" in result["warnings"]

    store.close()


@pytest.mark.asyncio
async def test_resolve_alert_ownership_checks_later_cache_candidates(tmp_path, **_kwargs):
    store = ServiceOwnershipStore(tmp_path / "ownership.db", stale_after_seconds=300)
    store.upsert_ownership(
        service_key="prod-a/payments/checkout-api",
        service_id="svc-checkout",
        service_name="checkout-api",
        owner_team="payments-dev",
        source="bk_cmdb",
        confidence=0.95,
        observed_at=1000.0,
    )
    cmdb = FakeCMDBClient(exc=CMDBClientUnavailable("timeout"))

    result = await resolve_alert_ownership(
        {
            "cluster": "prod-a",
            "namespace": "payments",
            "service": "missing-service",
            "workload_name": "checkout-api",
        },
        config={"cmdb": {"default_team": "sre"}},
        store=store,
        cmdb_client=cmdb,
        now=1100.0,
    )

    assert result["service_key"] == "prod-a/payments/checkout-api"
    assert result["owner_team"] == "payments-dev"
    assert result["ownership_source"] == "cache"

    store.close()
