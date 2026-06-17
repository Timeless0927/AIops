window.AIOPS_INCIDENT_FIXTURES = {
  "complete": {
    "label": "Complete",
    "incident": {
      "incident_id": "inc-payment-5xx-001",
      "title": "payment-api 5xx spike",
      "status": "diagnosing",
      "severity": "high",
      "source": {
        "kind": "alertmanager",
        "alert_id": "alert-payment-5xx",
        "labels": {
          "service": "payment-api",
          "namespace": "payments",
          "cluster": "prod-a"
        }
      },
      "service": {
        "service_id": "svc_payment_api",
        "service_name": "payment-api",
        "owner_team_id": "team_payments",
        "owner_team_name": "Payments",
        "ownership_status": "owned"
      },
      "latest_session_id": "sess-payment-001",
      "created_at": "2026-06-12T08:00:00Z",
      "updated_at": "2026-06-12T08:06:30Z",
      "permissions": {
        "can_view": true,
        "can_view_raw_evidence": false,
        "can_view_cost": true,
        "can_approve": false,
        "blocked_reason": "readonly_slice"
      }
    },
    "diagnosis": {
      "session_id": "sess-payment-001",
      "status": "succeeded",
      "summary": "High 5xx rate correlates with billing-api timeout logs while payment-api pods remain ready.",
      "root_cause": {
        "category": "upstream_dependency",
        "statement": "payment-api is failing calls to billing-api, producing elevated 5xx responses.",
        "confidence": 0.86
      },
      "diagnosed_at": "2026-06-12T08:06:10Z",
      "redactions": {
        "chain_of_thought_hidden": true,
        "raw_evidence_restricted": true
      }
    },
    "timeline": [
      {
        "event_id": "evt-001",
        "occurred_at": "2026-06-12T08:00:00Z",
        "type": "alert",
        "status": "succeeded",
        "title": "Alertmanager incident created",
        "summary": "PaymentErrorRateHigh opened for payment-api in payments/prod-a.",
        "refs": {"request_id": "req-alert-001", "incident_id": "inc-payment-5xx-001"}
      },
      {
        "event_id": "evt-002",
        "occurred_at": "2026-06-12T08:01:04Z",
        "type": "session",
        "status": "succeeded",
        "title": "Hermes diagnosis session started",
        "summary": "Gateway handed off diagnosis session sess-payment-001.",
        "refs": {"request_id": "req-handoff-001", "session_id": "sess-payment-001"}
      },
      {
        "event_id": "evt-003",
        "occurred_at": "2026-06-12T08:03:18Z",
        "type": "evidence",
        "status": "succeeded",
        "title": "Prometheus and Loki evidence collected",
        "summary": "Metrics and logs both show elevated failures around the same time window.",
        "refs": {"request_id": "req-evidence-001", "evidence_id": "ev-prom-001"}
      },
      {
        "event_id": "evt-004",
        "occurred_at": "2026-06-12T08:06:10Z",
        "type": "diagnosis",
        "status": "succeeded",
        "title": "Diagnosis persisted",
        "summary": "Hermes writeback stored summary, confidence, diagnosis JSON, and timeline refs.",
        "refs": {"request_id": "req-writeback-001", "session_id": "sess-payment-001"}
      }
    ],
    "evidence": [
      {
        "evidence_id": "ev-prom-001",
        "kind": "prometheus",
        "status": "succeeded",
        "summary": "5xx rate rose to 8 percent for payment-api over 10 minutes.",
        "collected_at": "2026-06-12T08:02:10Z",
        "query": {
          "display": "sum(rate(http_requests_total{app=\"payment-api\",status=~\"5..\"}[5m]))",
          "time_range": {"from": "2026-06-12T07:50:00Z", "to": "2026-06-12T08:05:00Z"}
        },
        "result_ref": "prom-ref-001",
        "raw_available": false,
        "failure": null
      },
      {
        "evidence_id": "ev-loki-001",
        "kind": "loki",
        "status": "succeeded",
        "summary": "Sampled logs contain billing-api timeout and checkout 502 errors.",
        "collected_at": "2026-06-12T08:02:48Z",
        "query": {
          "display": "{app=\"payment-api\"} |= \"error\"",
          "time_range": {"from": "2026-06-12T07:50:00Z", "to": "2026-06-12T08:05:00Z"}
        },
        "result_ref": "loki-ref-001",
        "raw_available": false,
        "failure": null
      },
      {
        "evidence_id": "ev-k8s-001",
        "kind": "k8s",
        "status": "succeeded",
        "summary": "payment-api deployment has 2/2 ready pods; no restart storm detected.",
        "collected_at": "2026-06-12T08:03:04Z",
        "query": {
          "display": "kubectl get pods -n payments -l app=payment-api",
          "time_range": {"from": "2026-06-12T08:00:00Z", "to": "2026-06-12T08:03:04Z"}
        },
        "result_ref": "audit-payment-read",
        "raw_available": false,
        "failure": null
      },
      {
        "evidence_id": "ev-topology-001",
        "kind": "topology",
        "status": "succeeded",
        "summary": "payment-api depends on billing-api in the same namespace.",
        "collected_at": "2026-06-12T08:03:30Z",
        "query": {
          "display": "service=payment-api namespace=payments",
          "time_range": {"from": "2026-06-12T08:00:00Z", "to": "2026-06-12T08:03:30Z"}
        },
        "result_ref": "topology-ref-001",
        "raw_available": false,
        "failure": null
      }
    ],
    "actions": [
      {
        "action_proposal_id": "act-read-billing",
        "summary": "Query billing-api latency and errors before any remediation.",
        "risk_level": "low",
        "approval_required": false,
        "approval_id": null,
        "execution_enabled": false
      },
      {
        "action_proposal_id": "act-rollback-payment",
        "summary": "Rollback payment-api only if deployment regression is confirmed.",
        "risk_level": "high",
        "approval_required": true,
        "approval_id": "appr-payment-001",
        "execution_enabled": false
      }
    ],
    "audit": {
      "status": "available",
      "summary": "4 timeline events, 4 evidence refs, writeback succeeded, no mutation executed.",
      "refs": ["req-alert-001", "req-handoff-001", "req-writeback-001"]
    }
  },
  "empty": {
    "label": "No data",
    "incident": {
      "incident_id": "inc-empty-001",
      "title": "checkout latency warning",
      "status": "new",
      "severity": "medium",
      "source": {
        "kind": "alertmanager",
        "alert_id": "alert-checkout-latency",
        "labels": {"service": "checkout-api", "namespace": "payments", "cluster": "prod-a"}
      },
      "service": {
        "service_id": "svc_checkout_api",
        "service_name": "checkout-api",
        "owner_team_id": "team_payments",
        "owner_team_name": "Payments",
        "ownership_status": "owned"
      },
      "latest_session_id": null,
      "created_at": "2026-06-12T09:00:00Z",
      "updated_at": "2026-06-12T09:00:00Z",
      "permissions": {
        "can_view": true,
        "can_view_raw_evidence": false,
        "can_view_cost": true,
        "can_approve": false,
        "blocked_reason": "no_approval_request"
      }
    },
    "diagnosis": null,
    "timeline": [
      {
        "event_id": "evt-empty-001",
        "occurred_at": "2026-06-12T09:00:00Z",
        "type": "alert",
        "status": "succeeded",
        "title": "Incident created",
        "summary": "No diagnosis session has started yet.",
        "refs": {"request_id": "req-empty-alert", "incident_id": "inc-empty-001"}
      }
    ],
    "evidence": [],
    "actions": [],
    "audit": {
      "status": "empty",
      "summary": "No diagnosis, evidence, approval, or execution audit records yet.",
      "refs": ["req-empty-alert"]
    }
  },
  "partial": {
    "label": "Partial evidence",
    "incident": {
      "incident_id": "inc-partial-001",
      "title": "inventory-api error budget burn",
      "status": "diagnosing",
      "severity": "high",
      "source": {
        "kind": "alertmanager",
        "alert_id": "alert-inventory-burn",
        "labels": {"service": "inventory-api", "namespace": "supply", "cluster": "prod-a"}
      },
      "service": {
        "service_id": "svc_inventory_api",
        "service_name": "inventory-api",
        "owner_team_id": "team_supply",
        "owner_team_name": "Supply",
        "ownership_status": "owned"
      },
      "latest_session_id": "sess-partial-001",
      "created_at": "2026-06-12T10:00:00Z",
      "updated_at": "2026-06-12T10:07:00Z",
      "permissions": {
        "can_view": true,
        "can_view_raw_evidence": false,
        "can_view_cost": false,
        "can_approve": false,
        "blocked_reason": "cost_scope_restricted"
      }
    },
    "diagnosis": {
      "session_id": "sess-partial-001",
      "status": "partial",
      "summary": "Prometheus confirms burn rate, but Loki is unavailable and K8s selector returned no matching pods.",
      "root_cause": {
        "category": "insufficient_evidence",
        "statement": "Likely service-level degradation, but root cause is not confirmed.",
        "confidence": 0.52
      },
      "diagnosed_at": "2026-06-12T10:06:55Z",
      "missing_evidence": ["loki", "k8s"],
      "redactions": {
        "chain_of_thought_hidden": true,
        "raw_evidence_restricted": true
      }
    },
    "timeline": [
      {
        "event_id": "evt-partial-001",
        "occurred_at": "2026-06-12T10:00:00Z",
        "type": "alert",
        "status": "succeeded",
        "title": "Alertmanager incident created",
        "summary": "Inventory burn-rate alert opened.",
        "refs": {"request_id": "req-partial-alert", "incident_id": "inc-partial-001"}
      },
      {
        "event_id": "evt-partial-002",
        "occurred_at": "2026-06-12T10:03:44Z",
        "type": "evidence",
        "status": "partial",
        "title": "Evidence collection partially completed",
        "summary": "Prometheus succeeded, Loki failed, K8s selector returned empty results.",
        "refs": {"request_id": "req-partial-evidence", "session_id": "sess-partial-001"}
      },
      {
        "event_id": "evt-partial-003",
        "occurred_at": "2026-06-12T10:06:55Z",
        "type": "diagnosis",
        "status": "partial",
        "title": "Partial diagnosis persisted",
        "summary": "Diagnosis is readable, but missing Loki and K8s evidence is marked.",
        "refs": {"request_id": "req-partial-writeback", "session_id": "sess-partial-001"}
      }
    ],
    "evidence": [
      {
        "evidence_id": "ev-partial-prom",
        "kind": "prometheus",
        "status": "succeeded",
        "summary": "Error budget burn rate is above 14x for inventory-api.",
        "collected_at": "2026-06-12T10:02:12Z",
        "query": {
          "display": "burn_rate:inventory_api:5m",
          "time_range": {"from": "2026-06-12T09:45:00Z", "to": "2026-06-12T10:05:00Z"}
        },
        "result_ref": "prom-ref-partial",
        "raw_available": false,
        "failure": null
      },
      {
        "evidence_id": "ev-partial-loki",
        "kind": "loki",
        "status": "failed",
        "summary": "Loki query failed before log samples were collected.",
        "collected_at": "2026-06-12T10:02:50Z",
        "query": {
          "display": "{app=\"inventory-api\"} |= \"error\"",
          "time_range": {"from": "2026-06-12T09:45:00Z", "to": "2026-06-12T10:05:00Z"}
        },
        "result_ref": null,
        "raw_available": false,
        "failure": {"code": "backend_unavailable", "message": "Loki returned 503.", "retryable": true}
      },
      {
        "evidence_id": "ev-partial-k8s",
        "kind": "k8s",
        "status": "empty",
        "summary": "Selector returned zero resources.",
        "collected_at": "2026-06-12T10:03:02Z",
        "query": {
          "display": "namespace=supply selector=app=inventory-api",
          "time_range": {"from": "2026-06-12T10:00:00Z", "to": "2026-06-12T10:03:02Z"}
        },
        "result_ref": "audit-empty-k8s",
        "raw_available": false,
        "failure": {"code": "no_matching_resources", "message": "No pods matched selector.", "retryable": false}
      },
      {
        "evidence_id": "ev-partial-topology",
        "kind": "topology",
        "status": "partial",
        "summary": "Service node exists, but dependency edges are stale.",
        "collected_at": "2026-06-12T10:03:20Z",
        "query": {
          "display": "service=inventory-api namespace=supply",
          "time_range": {"from": "2026-06-12T10:00:00Z", "to": "2026-06-12T10:03:20Z"}
        },
        "result_ref": "topology-ref-partial",
        "raw_available": false,
        "failure": {"code": "stale_topology", "message": "Topology was older than freshness threshold.", "retryable": true}
      }
    ],
    "actions": [
      {
        "action_proposal_id": "act-partial-read",
        "summary": "Retry Loki and refine the K8s selector before proposing remediation.",
        "risk_level": "low",
        "approval_required": false,
        "approval_id": null,
        "execution_enabled": false
      }
    ],
    "audit": {
      "status": "partial",
      "summary": "Writeback succeeded with partial evidence. No execution audit exists.",
      "refs": ["req-partial-alert", "req-partial-evidence", "req-partial-writeback"]
    }
  },
  "failed": {
    "label": "Diagnosis failed",
    "incident": {
      "incident_id": "inc-failed-001",
      "title": "search-api availability drop",
      "status": "abnormal",
      "severity": "critical",
      "source": {
        "kind": "alertmanager",
        "alert_id": "alert-search-availability",
        "labels": {"service": "search-api", "namespace": "search", "cluster": "prod-a"}
      },
      "service": {
        "service_id": "svc_search_api",
        "service_name": "search-api",
        "owner_team_id": "team_search",
        "owner_team_name": "Search",
        "ownership_status": "owned"
      },
      "latest_session_id": "sess-failed-001",
      "created_at": "2026-06-12T11:00:00Z",
      "updated_at": "2026-06-12T11:04:00Z",
      "permissions": {
        "can_view": true,
        "can_view_raw_evidence": false,
        "can_view_cost": true,
        "can_approve": false,
        "blocked_reason": "diagnosis_failed"
      }
    },
    "diagnosis": {
      "session_id": "sess-failed-001",
      "status": "failed",
      "summary": "Diagnosis session failed before a conclusion could be produced.",
      "root_cause": {
        "category": "diagnosis_failed",
        "statement": "No root cause conclusion is available.",
        "confidence": 0
      },
      "diagnosed_at": null,
      "failure": {"code": "writeback_timeout", "message": "Gateway writeback timed out after Hermes export.", "retryable": true},
      "redactions": {
        "chain_of_thought_hidden": true,
        "raw_evidence_restricted": true
      }
    },
    "timeline": [
      {
        "event_id": "evt-failed-001",
        "occurred_at": "2026-06-12T11:00:00Z",
        "type": "alert",
        "status": "succeeded",
        "title": "Alertmanager incident created",
        "summary": "Search availability alert opened.",
        "refs": {"request_id": "req-failed-alert", "incident_id": "inc-failed-001"}
      },
      {
        "event_id": "evt-failed-002",
        "occurred_at": "2026-06-12T11:04:00Z",
        "type": "diagnosis",
        "status": "failed",
        "title": "Diagnosis session failed",
        "summary": "Hermes export remained available, but durable writeback did not complete.",
        "refs": {"request_id": "req-failed-writeback", "session_id": "sess-failed-001"}
      }
    ],
    "evidence": [
      {
        "evidence_id": "ev-failed-prom",
        "kind": "prometheus",
        "status": "failed",
        "summary": "Prometheus query did not return before the session failed.",
        "collected_at": "2026-06-12T11:02:30Z",
        "query": {
          "display": "availability:search_api:5m",
          "time_range": {"from": "2026-06-12T10:45:00Z", "to": "2026-06-12T11:03:00Z"}
        },
        "result_ref": null,
        "raw_available": false,
        "failure": {"code": "timeout", "message": "Query timed out.", "retryable": true}
      },
      {
        "evidence_id": "ev-failed-loki",
        "kind": "loki",
        "status": "skipped",
        "summary": "Log collection was skipped after session failure.",
        "collected_at": null,
        "query": {
          "display": "{app=\"search-api\"}",
          "time_range": {"from": "2026-06-12T10:45:00Z", "to": "2026-06-12T11:03:00Z"}
        },
        "result_ref": null,
        "raw_available": false,
        "failure": {"code": "session_failed", "message": "Diagnosis session failed first.", "retryable": true}
      },
      {
        "evidence_id": "ev-failed-k8s",
        "kind": "k8s",
        "status": "skipped",
        "summary": "K8s read was not attempted.",
        "collected_at": null,
        "query": {
          "display": "kubectl get pods -n search -l app=search-api",
          "time_range": {"from": "2026-06-12T11:00:00Z", "to": "2026-06-12T11:04:00Z"}
        },
        "result_ref": null,
        "raw_available": false,
        "failure": {"code": "session_failed", "message": "Diagnosis session failed first.", "retryable": true}
      },
      {
        "evidence_id": "ev-failed-topology",
        "kind": "topology",
        "status": "skipped",
        "summary": "Topology lookup was not attempted.",
        "collected_at": null,
        "query": {
          "display": "service=search-api namespace=search",
          "time_range": {"from": "2026-06-12T11:00:00Z", "to": "2026-06-12T11:04:00Z"}
        },
        "result_ref": null,
        "raw_available": false,
        "failure": {"code": "session_failed", "message": "Diagnosis session failed first.", "retryable": true}
      }
    ],
    "actions": [],
    "audit": {
      "status": "failed",
      "summary": "Diagnosis failed. No approval request or mutation execution was created.",
      "refs": ["req-failed-alert", "req-failed-writeback"]
    }
  }
};
