# OpenObserve 替代 Prometheus / Loki / Tempo 评估

日期：2026-07-13

## 结论

OpenObserve 能统一保存和查看 metrics、logs、traces，但对本项目不是 Prometheus、Loki、Tempo 的无改造替代品。

| 目标 | Drop-in replacement | Functional replacement | 本项目建议 |
| --- | --- | --- | --- |
| Prometheus | 否 | 是，需 Collector | Pilot 保留 Prometheus；OpenObserve 仍需要 OTel Collector 负责 scrape，并需要重做和验证 rule/alert path。 |
| Loki | 否 | 是 | 可以通过新的 OpenObserve Logs MCP Adapter 替代，但现有 LogQL/Loki query Adapter 不能直接复用。 |
| Tempo | 否 | 是 | 当前没有 Tempo。以后启用 tracing 时可以选择 OpenObserve 作为 OTLP trace backend，但仍需 instrumentation/Collector。 |
| 整套 LGTM backend | 否 | 是，需改造 | 先做 bounded spike；不直接修改当前 Pilot 架构。 |

## 官方事实

### Metrics

- OpenObserve 官方 ingestion 文档要求通过 Prometheus `remote_write` 写入 `/api/{org}/prometheus/api/v1/write`。这说明 OpenObserve 可以保存 Prometheus metrics，但 scrape 仍由 Prometheus或 Collector 完成。[Prometheus ingestion](https://github.com/openobserve/openobserve-docs/blob/main/docs/ingestion/metrics/prometheus.md)
- OpenObserve 源码实现 Prometheus-compatible instant/range/exemplar query endpoints，并返回 Prometheus response shape。[PromQL HTTP handler](https://github.com/openobserve/openobserve/blob/main/src/handler/http/request/promql/mod.rs)
- 官方 Kubernetes Collector 使用 OTel Collector Prometheus receiver 抓取 kube-state-metrics、kubelet、control-plane 和 annotated Pod；OpenObserve binary 本身不是该采集器。[Collector metrics](https://github.com/openobserve/openobserve-helm-chart/blob/main/charts/openobserve-collector/docs/metrics.md)
- OpenObserve 有自己的 scheduled/real-time alert、PromQL/SQL condition 和 webhook destination。官方文档没有声明 Alertmanager webhook payload、fingerprint 或 `send_resolved` contract 兼容，因此本项目必须把它视为新的 Alert Adapter。[OpenObserve alerts](https://github.com/openobserve/openobserve-docs/blob/main/docs/user-guide/analytics/alerts/index.md)

对本项目的影响：现有 Prometheus MCP 的 query path 接近可适配，但需增加 OpenObserve organization path 和 authentication。现有 Prometheus scrape、native rule file、Alertmanager -> Gateway contract 不能仅改 URL 后删除。

### Logs

- OpenObserve 支持 OTLP/HTTP、OTLP/gRPC、Fluent Bit、Vector 等 log ingestion，查询和 UI 使用 field search 与 SQL。[OTLP ingestion](https://github.com/openobserve/openobserve-docs/blob/main/docs/ingestion/logs/otlp.md) [Logs](https://github.com/openobserve/openobserve-docs/blob/main/docs/features/logs.md)
- OpenObserve 源码实现 Loki-compatible `/api/{org}/loki/api/v1/push` ingestion，接受 Loki JSON/protobuf push shape。[Loki push handler](https://github.com/openobserve/openobserve/blob/main/src/handler/http/request/logs/loki.rs)
- 官方源码和文档没有提供 Loki `query_range`/LogQL compatibility contract；OpenObserve log search 使用自己的 SQL/Search interface。

对本项目的影响：Alloy 可以改为向 OpenObserve push，但当前 Loki MCP 依赖 Loki `query_range` 和 LogQL，必须替换成新的 OpenObserve Logs Adapter，并重新固定 query guard、result limit、evidence reference 和 error taxonomy。

### Traces

- OpenObserve 接收标准 OTLP traces：`/api/{org}/v1/traces`，支持 SDK 或 OTel Collector。[Trace ingestion](https://github.com/openobserve/openobserve-docs/blob/main/docs/ingestion/traces/opentelemetry.md)
- Trace 查询使用 OpenObserve `/api/{org}/{stream}/traces/latest` 与 SQL Search，不是 Tempo query contract。[Trace Search API](https://github.com/openobserve/openobserve-docs/blob/main/docs/reference/api/traces/trace-search-api.md)
- 官方 Collector 由 gateway StatefulSet 接收 OTLP；auto-instrumentation 依赖 OpenTelemetry Operator，部分语言还需要 privileged/eBPF。[Collector traces](https://github.com/openobserve/openobserve-helm-chart/blob/main/charts/openobserve-collector/docs/traces.md)

对本项目的影响：OpenObserve 可以作为以后新增 tracing 时的 backend，但不能消除 SDK/Collector、sampling、context propagation、retention 和敏感 span attribute 治理。当前 ADR-0047 的“metrics/logs 证明不足后再引入 tracing”仍成立。

### Deployment and storage

- Single-node mode 使用 SQLite 和 local disk，官方定位为 light usage/testing 或不要求 HA；也可以单节点配 object storage。[Architecture](https://github.com/openobserve/openobserve-docs/blob/main/docs/architecture.md)
- Standalone Helm chart 默认一个 `10Gi` RWO volume、local mode/disk，resource request/limit 默认为空；默认使用 version tag 而非 digest。本项目若采用，必须自行 vendor Kustomize manifest、固定 digest 和资源门限。[Standalone values](https://github.com/openobserve/openobserve-helm-chart/blob/main/charts/openobserve-standalone/values.yaml)
- HA mode 不支持 local disk，要求 object storage、PostgreSQL/MySQL 与 NATS；官方 Kubernetes 安装使用 Helm，参考拓扑约 12 Pods。[HA deployment](https://github.com/openobserve/openobserve-docs/blob/main/docs/administration/deployment/ha-deployment.md)
- Stream data 使用 Parquet；local mode metadata 使用 SQLite，cluster mode 使用 PostgreSQL。[Storage](https://github.com/openobserve/openobserve-docs/blob/main/docs/administration/maintenance/storage-management/storage.md)
- OpenObserve OSS 使用 AGPL-3.0；分发进 Pilot Release Bundle 前需要完成组织的 license review。[License](https://github.com/openobserve/openobserve/blob/main/LICENSE)
- 调查时 GitHub latest release 为 `v0.91.1`（2026-07-02），项目活跃但尚未达到 `1.0`。[Release](https://github.com/openobserve/openobserve/releases/tag/v0.91.1)

## 与 AIOps 证据边界的关系

把三类数据放进一个 backend 可以减少跨存储时间/label 对齐成本，但不会自动提高 Evidence 可信度。可信度仍来自 AIOps-owned Evidence Reference：

- exact source、normalized query/query hash；
- Cluster/namespace/workload/Pod scope；
- time range、observed time、expiry；
- backend/config revision；
- result digest、count/truncation 和 bounded redacted samples；
- request/correlation identity。

浏览器仍应只访问 Gateway，Gateway 做 actor/resource authorization，MCP Adapter 做 query guard并访问 OpenObserve。直接嵌入或开放 OpenObserve UI 会增加第二套登录/RBAC，并违反当前“browser only talks to Gateway”边界。OpenObserve native UI 可以作为 Operator-only 外部工具，但不能替代产品内 Evidence contract。

## 推荐的 Bounded Spike

在开始 O01/O02 前增加一个有退出条件的 spike，不改变生产决策：

1. 用 version-pinned standalone OpenObserve + OTel Collector 在测试 namespace 采集真实 AIOps/kube-state metrics 和 Pod logs。
2. 证明 PromQL range query、SQL log query、相同 scope/time window 关联以及 restart 后 retained data。
3. 实现临时 OpenObserve Logs Adapter，确认现有 MCP ToolEnvelope/query guard/evidence ref 能保持；不得改 Diagnosis contract。
4. 用 OpenObserve alert webhook 完成 firing/resolved -> Gateway 映射，证明 fingerprint、labels、dedup 和 recovery 语义。
5. 测量 steady-state CPU/memory/PVC、ingestion lag、query latency 和 7d retention；总资源不得高于当前 Prometheus + Alertmanager + Loki + Alloy 预算才算“简化”。
6. 完成 AGPL 分发审查和 immutable Kustomize packaging proof。

Spike 全部通过后，才重开 issue 04/ADR-0047：可以考虑用 OpenObserve + Collector 功能替代 Prometheus/Loki backend，同时保留 typed Metrics/Logs MCP Interface。任一 alert/recovery、query guard、资源、license 或 packaging gate 失败，则维持当前 Prometheus/Alertmanager/Loki/Alloy 方案。Tracing 不进入该 spike。
