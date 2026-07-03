window.AIOPS_CONSOLE_OVERVIEW_FIXTURES = {
  "summary": {
    "incidents": [
      {
        "incident_id": "INC-2481",
        "title": "checkout-api p95 latency rose to 4.8s",
        "severity": "critical",
        "status": "waiting_approval",
        "service": "checkout-api",
        "impact": "18.4% checkout traffic",
        "age": "12m",
        "tags": "critical approval k8s checkout"
      },
      {
        "incident_id": "INC-2480",
        "title": "search-api pod restart rate abnormal",
        "severity": "high",
        "status": "diagnosing",
        "service": "search-api",
        "impact": "3 nodes OOMKilled",
        "age": "31m",
        "tags": "high k8s search"
      },
      {
        "incident_id": "INC-2478",
        "title": "inventory-sync Kafka lag expanding",
        "severity": "high",
        "status": "waiting_approval",
        "service": "inventory-sync",
        "impact": "replenishment delay",
        "age": "44m",
        "tags": "high approval inventory"
      }
    ],
    "tools": [
      {
        "name": "query.prometheus",
        "status": "succeeded",
        "duration_ms": 820,
        "query": "checkout-api p95 latency and payment-service 5xx",
        "observation": "Latency and 5xx rose together after rollout rev184.",
        "ref": "req-prom-2481"
      },
      {
        "name": "kubectl.rollout_history",
        "status": "succeeded",
        "duration_ms": 410,
        "query": "deployment/checkout-api revision history",
        "observation": "revision 184 reached 30% canary before the alert.",
        "ref": "audit-k8s-2481"
      },
      {
        "name": "logs.cluster_search",
        "status": "partial",
        "duration_ms": 1300,
        "query": "payment-service timeout and pool exhaustion logs",
        "observation": "Timeout samples found; Loki sampling window has gaps.",
        "ref": "loki-ref-2481"
      }
    ]
  }
};
