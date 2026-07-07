# Console Next responsibility-chain audit implementation

## Steps

1. Add a small Gateway-owned audit-chain projection that reads existing durable stores.
   - Use `audit_log`, action proposals, approval requests, executions, agent runs,
     notifications, and conversation delete events as refs.
   - Keep responsibility records immutable; deletion only creates chat tombstones.
2. Add Gateway routes.
   - `GET /api/audit/chains`
   - `GET /api/audit/chains/{chain_id}`
   - `GET /api/audit/raw`
   - `GET /api/audit/tombstones`
   - conversation delete must create a tombstone visible through audit.
3. Add Console Next routes.
   - `/audit` defaults to chains with secondary raw-log and tombstone tabs.
   - `/audit/:chainId` shows chain detail.
   - Browser calls same-origin Gateway `/api/audit*` only.
4. Add focused tests.
   - Gateway HTTP tests for chain construction, filtering, raw refs,
     notification refs, tombstones, immutability, and hidden objects.
   - Console contract test for Gateway-only audit calls and route labels.

## Validation

- `rtk python3 -m py_compile apps/aiops_k8s_gateway/main.py apps/aiops_k8s_gateway/audit_chain_service.py`
- `rtk test pytest -q tests/test_gateway_audit_chains.py`
- `rtk test pytest -q tests/test_aiops_console_web.py`
- `rtk npm run build`
- `rtk git diff --check`
