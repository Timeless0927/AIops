# implement.md - Alertmanager automatic route and bearer auth

## Checklist

1. Implement Gateway Alertmanager bearer auth:
   - add `AIOPS_ALERTMANAGER_WEBHOOK_TOKEN`
   - only check it inside `/webhooks/alertmanager`
   - when configured, require `Authorization: Bearer <token>`
   - bad/missing token returns controlled `401`
   - do not change `/api/*`, `/k8s/read`, `/diagnosis/writeback`, or writeback
     HMAC
2. Read relevant specs:
   - backend API routes, authorization, error handling, logging/audit, testing,
     quality
   - deploy/dev-external observability contract if deployment docs or monitoring
     namespace assets change
3. Gateway tests:
   - existing unsigned behavior still works when no bearer token and no HMAC is
     configured
   - missing/bad bearer token fails when
     `AIOPS_ALERTMANAGER_WEBHOOK_TOKEN` is set
   - correct bearer token passes
   - HMAC tests remain passing
4. Deployment assets:
   - add AlertmanagerConfig receiver/route that points directly to Gateway and
     uses Alertmanager native bearer auth Secret reference
   - include a low-noise test matcher that can be enabled on demand
   - add secret example and README commands for real secret creation
   - document disable/rollback commands
5. Validation:
   - `python3 -m pytest -q tests/test_gateway_alertmanager_webhook.py`
   - `python3 -m compileall -q apps hermes toolsets aiops`
   - render/apply docs check where feasible:
     `kubectl apply --dry-run=client -f deploy/k8s/alertmanager/...`
6. Live validation if cluster access/secrets are available:
   - configure real token in Gateway and monitoring namespace Secret
   - apply AlertmanagerConfig
   - fire one matching test alert
   - confirm Gateway incident creation/reuse and Hermes handoff
   - confirm missing/bad bearer token returns 401

## Rollback Points

- Before applying AlertmanagerConfig: no traffic changes.
- After applying route: delete AlertmanagerConfig to stop automatic delivery.
- After enabling Gateway token: unset only for intentional unsigned manual
  debugging.
