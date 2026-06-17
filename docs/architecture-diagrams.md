# 架构图集

最后对齐日期：2026-06-17

这些图整理自 AIO-73、AIO-80、AIO-86、AIO-87、AIO-95 和当前代码。

## 系统上下文

```mermaid
flowchart LR
    Alertmanager[Alertmanager] --> Gateway[Gateway / control-plane]
    Console[AIOps Console] --> Gateway
    Feishu[Feishu notification-only] <-->|通知与链接| Gateway

    Gateway --> Hermes[Hermes diagnosis]
    Gateway --> Connector[Cluster Connector]
    Hermes --> Gateway
    Hermes --> PromMCP[Prometheus MCP]
    Hermes --> LokiMCP[Loki MCP]
    Hermes --> TopologyMCP[Topology MCP]

    Connector --> K8s[Kubernetes API]
    PromMCP --> Prometheus[Prometheus]
    LokiMCP --> Loki[Loki]
    TopologyMCP --> TopologyStore[Topology store]
    Gateway --> IncidentStore[(Incident / approval / audit stores)]
```

## P0 告警到诊断流程

```mermaid
sequenceDiagram
    participant AM as Alertmanager
    participant GW as Gateway
    participant Store as Incident Store
    participant H as Hermes
    participant PM as Prometheus MCP
    participant LM as Loki MCP
    participant KM as Connector / K8s
    participant TM as Topology MCP

    AM->>GW: POST /webhooks/alertmanager
    GW->>GW: validate payload and optional HMAC
    GW->>Store: create or reuse incident/session
    GW->>H: trigger diagnosis session
    H->>PM: query_metrics
    H->>LM: query_logs
    H->>KM: read-only K8s command envelope
    H->>TM: get_service_topology
    PM-->>H: evidence ref or controlled failure
    LM-->>H: evidence ref or controlled failure
    KM-->>H: result envelope / evidence
    TM-->>H: topology evidence or skipped/partial
    H-->>GW: protected POST /diagnosis/writeback
    GW->>Store: persist diagnosis, evidence summary, timeline refs
```

## Control-Plane 边界

```mermaid
flowchart TB
    subgraph Gateway["Gateway / control-plane"]
        Ingress[Alertmanager ingress]
        Incident[Incident and session state]
        Approval[Internal Approval Service]
        RBAC[LDAP/RBAC authorization]
        Notify[Notification Center]
        Audit[Audit]
        Routing[K8s command routing]
        Writeback[Diagnosis writeback]
    end

    Ingress --> Incident
    Incident --> Writeback
    Incident --> Approval
    Approval --> RBAC
    Approval --> Audit
    Approval --> Notify
    Routing --> RBAC
    Routing --> Audit

    Notify --> Feishu[Feishu notification-only]
    Routing --> Connector[Cluster Connector]
```

## 内部审批状态

```mermaid
stateDiagram-v2
    [*] --> pending: Gateway creates approval request
    pending --> approved: internal API approve
    pending --> rejected: internal API reject with reason
    pending --> expired: expiry elapsed
    pending --> cancelled: admin/system cancel
    approved --> [*]
    rejected --> [*]
    expired --> [*]
    cancelled --> [*]
```

关键边界：Feishu notification 不推动这个状态机。

## Console V1 边界

```mermaid
flowchart LR
    Browser[Browser / Console] --> GatewayAPI[Gateway /api]
    GatewayAPI --> Incidents[Incident APIs]
    GatewayAPI --> Approvals[Approval APIs]
    GatewayAPI --> Costs[Cost APIs]
    GatewayAPI --> Grafana[Grafana panel metadata]
    GatewayAPI --> Audit[Audit APIs]

    Browser -. forbidden .-> Hermes[Hermes]
    Browser -. forbidden .-> Connector[Connector]
    Browser -. forbidden .-> MCP[MCP services]
    Browser -. forbidden .-> Observability[Prometheus / Loki]
    Browser -. forbidden .-> FeishuAPI[Feishu approval API]
```

## Kubernetes 部署形态

```mermaid
flowchart TB
    subgraph Namespace["aiops-dev or selected namespace"]
        GWPod[aiops-gateway Deployment]
        HPod[aiops-hermes Deployment]
        CPod[aiops-connector Deployment]
        PPod[aiops-mcp-prometheus Deployment]
        LPod[aiops-mcp-loki Deployment]
        TPod[aiops-mcp-topology Deployment]
        PVC[(aiops-hermes-data PVC)]
        SA[aiops-connector ServiceAccount / read-only Role]
    end

    GWPod --> CPod
    GWPod --> HPod
    HPod --> PPod
    HPod --> LPod
    HPod --> TPod
    HPod --> PVC
    CPod --> SA
    SA --> K8sAPI[Kubernetes API]
```

## Evidence 完整度

```mermaid
flowchart LR
    Diagnosis[Hermes diagnosis] --> Prom[Prometheus evidence]
    Diagnosis --> Loki[Loki evidence]
    Diagnosis --> K8s[K8s read evidence]
    Diagnosis --> Topology[Topology evidence]

    Prom -->|succeeded / failed / unavailable| Confidence[Confidence and rationale]
    Loki -->|succeeded / failed / empty| Confidence
    K8s -->|matched / empty / selector issue| Confidence
    Topology -->|succeeded / skipped / stale| Confidence
```
