# OpenObserve 替代 Prometheus、Loki、Tempo 评估

评估日期：2026-07-13
评估版本：OpenObserve `v0.91.1`（2026-07-02 发布，非 prerelease）
范围：只依据 OpenObserve 官方文档/源码、Prometheus/Loki/Tempo 官方协议文档和 MCP 规范；不把产品宣传中的性能或成本数字当作本项目容量结论。

## 结论

OpenObserve 可以作为统一 logs/metrics/traces **功能后端候选**，但不能在当前项目中 drop-in 替换 Prometheus、Loki 或 Tempo。

| 被替代项 | Drop-in replacement | Functional replacement | 本项目结论 |
| --- | --- | --- | --- |
| Prometheus | 否 | 有条件是 | 支持 Prometheus remote-write 和 Prometheus HTTP query API/PromQL，但采集仍需 Prometheus 或 OpenTelemetry Collector；规则、Alertmanager payload 与 `send_resolved` 路径需要迁移。当前 Pilot 不替换。 |
| Loki | 否 | 是 | Alloy 可继续使用 Loki push API 写入；OpenObserve 日志查询使用 SQL/Search API，没有 Loki `query_range`/LogQL 查询兼容承诺，现有 Loki MCP 不能只换 URL。当前 Pilot 不替换。 |
| Tempo | 否 | 是 | 支持 OTLP traces、trace UI 和 trace/search API，但不是 Tempo HTTP/TraceQL API。当前项目按 ADR-0047 不部署 tracing，因此 Pilot 中没有 Tempo 可替换。 |

对当前 `.scratch/aiops-pilot-ready/issues/04-define-bundled-observability-and-alert-path.md` 和 `tickets.md` 的决定是：**保持 Prometheus + Alertmanager + Loki + kube-state-metrics + Alloy 的 canonical bundle 不变**。OpenObserve 只进入 Pilot 后的 external backend spike；完成真实兼容、资源、许可证和证据契约验收前，不加入 release artifact。

OpenObserve 自带 MCP 也不能直接接入本项目 Diagnosis/MCP。它是 Enterprise-only 的通用管理面，暴露 133 个查询和管理工具；本项目需要的是 `query_metrics`、`query_logs` 等少数受限证据工具，以及固定的 `ToolEnvelope`、`EvidenceRef`、审计和 Evidence Gate。最小可行路径是保留现有 MCP Module，只替换其内部 backend Adapter。

## 1. 部署、存储、保留与 HA

OpenObserve 有两种本项目相关的部署形态：

- single-node 默认使用 SQLite 保存 metadata、本地 disk 保存 stream data；官方称适合 light usage/testing 或不要求 HA 的场景。官方 standalone Helm chart 默认一个 `10Gi` RWO PVC，但主进程资源默认为空，需要使用方自行定额。[Architecture](https://github.com/openobserve/openobserve-docs/blob/main/docs/architecture.md) [standalone values](https://github.com/openobserve/openobserve-helm-chart/blob/main/charts/openobserve-standalone/values.yaml)
- HA 模式不支持本地 disk，要求 object storage、PostgreSQL/MySQL 和 NATS；官方 Kubernetes 安装路径是 Helm，默认还要求 CloudNativePG Operator。官方验证示例约有 12 个 Pod，包括 NATS、PostgreSQL、OpenFGA、router、ingester、querier、compactor 和 alertmanager。[HA deployment](https://github.com/openobserve/openobserve-docs/blob/main/docs/administration/deployment/ha-deployment.md) [Architecture](https://github.com/openobserve/openobserve-docs/blob/main/docs/architecture.md)
- 数据以 Parquet 保存；compactor 执行 retention 和 stream deletion。stream 可以覆盖全局 retention，当前官方文档写明未配置时全局默认是 3650 天，因此若做 7 天 Pilot 必须显式设置并验证，而不能沿用默认值。[Storage](https://github.com/openobserve/openobserve-docs/blob/main/docs/administration/maintenance/storage-management/storage.md) [Stream details](https://github.com/openobserve/openobserve-docs/blob/main/docs/user-guide/data-processing/streams/stream-details.md)

这两种形态都不直接满足现有 Pilot contract：single-node 把 metrics/logs/traces 和 metadata 放在同一故障域、共享一个 PVC 且没有 HA；HA 又引入 Helm、Operator、PostgreSQL、NATS 和 object store，违反当前 self-contained native Kustomize、无 Operator 前置依赖的边界。是否改变该边界属于新的架构和成本决策。

## 2. Metrics 与 Prometheus

### 官方能力

- OpenObserve 接收 Prometheus remote-write：`POST /api/{organization}/prometheus/api/v1/write`。[OpenObserve Prometheus ingestion](https://github.com/openobserve/openobserve-docs/blob/main/docs/ingestion/metrics/prometheus.md) [Prometheus remote_write](https://prometheus.io/docs/prometheus/latest/configuration/configuration/#remote_write)
- 它实现带 organization 前缀的 Prometheus instant/range query、metadata、series 和 labels endpoints，并返回 Prometheus 风格结果；源码路由包括 `/api/{org}/prometheus/api/v1/query_range`。[OpenObserve PromQL handler](https://github.com/openobserve/openobserve/blob/v0.91.1/src/handler/http/request/promql/mod.rs) [Prometheus HTTP API](https://prometheus.io/docs/prometheus/latest/querying/api/)
- 官方 Kubernetes collector 是 OpenTelemetry Collector chart，可抓取 host、kubelet、kube-state-metrics、control-plane 和 annotated Pod metrics。该 chart 要求 cert-manager 与 OpenTelemetry Operator；OpenObserve server 本身不是现有 Prometheus scrape 配置的直接宿主。[Collector README](https://github.com/openobserve/openobserve-helm-chart/blob/main/charts/openobserve-collector/README.md) [Collector metrics](https://github.com/openobserve/openobserve-helm-chart/blob/main/charts/openobserve-collector/docs/metrics.md)

### 不兼容点

现有 Pilot 要求 Prometheus 自己完成 target discovery/scrape、rule evaluation，并向标准 Alertmanager 发送 firing/resolved。OpenObserve 官方 alert 是自己的 scheduled/realtime alert 和 webhook template；没有声明兼容 Prometheus rule files、Alertmanager API、标准 webhook payload/fingerprint 或 `send_resolved`。[OpenObserve alerts](https://github.com/openobserve/openobserve-docs/blob/main/docs/user-guide/analytics/alerts/index.md) [OpenObserve alert destinations](https://github.com/openobserve/openobserve-docs/blob/main/docs/user-guide/account-administration/management/alert-destinations.md) [Alertmanager webhook config](https://prometheus.io/docs/alerting/latest/configuration/#webhook_config)

因此它只能在以下迁移后功能替代 Prometheus：vendor 一个最小 OTel Collector/native collector deployment、迁移 scrape 和 labels、迁移 rules，并增加 OpenObserve alert 到 Gateway Alert Signal 的新 Adapter 与 firing/resolved 合同测试。只设置 `PROMETHEUS_URL` 不够；现有 client 还没有 OpenObserve Basic Auth/organization 配置。

## 3. Logs 与 Loki

### 官方能力

- OpenObserve 兼容 Loki **push** API：`POST /api/{organization}/loki/api/v1/push`，支持 JSON/Protobuf、stream labels 和 structured metadata。Alloy 的写入侧可以改 endpoint/credential 后继续使用。[Loki ingestion API](https://github.com/openobserve/openobserve-docs/blob/main/docs/reference/api/ingestion/logs/loki.md) [OpenObserve Loki handler](https://github.com/openobserve/openobserve/blob/v0.91.1/src/handler/http/request/logs/loki.rs)
- 官方日志查询接口是 `POST /api/{organization}/_search`，查询体使用 SQL、明确的 microsecond time range、offset/size；UI 也以 SQL/full-text 查询为主。[Search API](https://github.com/openobserve/openobserve-docs/blob/main/docs/reference/api/search/search.md) [Logs](https://github.com/openobserve/openobserve-docs/blob/main/docs/features/logs.md)

### 不兼容点

官方文档和 `v0.91.1` 路由只给出 Loki push compatibility，没有 Loki `/loki/api/v1/query_range` 或 LogQL query compatibility。现有 `HttpLokiRunner` 固定调用该 endpoint 并解析 Loki stream response；OpenObserve 不能只替换 `LOKI_URL`。[OpenObserve router](https://github.com/openobserve/openobserve/blob/v0.91.1/src/handler/http/router/mod.rs) [Loki query_range](https://grafana.com/docs/loki/latest/reference/loki-http-api/#query-logs-within-a-range-of-time)

功能替代需要一个 OpenObserve log Adapter：把已批准的 cluster/namespace/workload/time 范围编译为参数化、bounded SQL，调用 Search API，再归一化为现有 `query_logs` 结果。不要让模型生成任意 SQL，也不要在迁移期间保留 LogQL/SQL 双 owner。

## 4. Traces 与 Tempo

OpenObserve 支持 OTLP HTTP/gRPC traces，提供 trace list/detail、service map、logs-to-trace 双向跳转；相关性依赖应用把 `trace_id`/`span_id` 写入日志并传播 trace context。[OTLP traces](https://github.com/openobserve/openobserve-docs/blob/main/docs/ingestion/traces/opentelemetry.md) [Trace API](https://github.com/openobserve/openobserve-docs/blob/main/docs/reference/api/traces/trace-search-api.md) [Trace/log correlation](https://github.com/openobserve/openobserve-docs/blob/main/docs/user-guide/data-exploration/traces/traces.md)

这足以成为 Tempo 的功能替代候选，但不是 Tempo API/TraceQL 的 drop-in replacement。[Tempo HTTP API](https://grafana.com/docs/tempo/latest/api_docs/) 本项目 ADR-0047 明确先使用 metrics 与 structured logs，尚不部署 OpenTelemetry SDK、Collector 或 tracing backend。加入 OpenObserve 不能绕过该 ADR；只有 metrics/logs 对跨进程因果多次不足，并完成 instrumentation、sampling、敏感字段和 retention 设计后，才重新评估 traces。

## 5. Auth、多租户、API 与许可证

- 所有 OpenObserve API 使用 Authorization，官方 REST 文档主要示例为 HTTP Basic；organization 位于 API path。organization 是 stream/user/function 的隔离边界。[API auth](https://github.com/openobserve/openobserve-docs/blob/main/docs/reference/api/index.md) [Organizations](https://github.com/openobserve/openobserve-docs/blob/main/docs/user-guide/account-administration/identity-and-access-management/organizations.md)
- OSS 用户角色是 root/admin/member；advanced RBAC、SSO 和 audit trail 被列为 Enterprise 功能。不能假设 OpenObserve organization/role 自动等价于本项目 Team、Resource Binding、Platform Administrator 或 Approval Authority。[Users](https://github.com/openobserve/openobserve-docs/blob/main/docs/user-guide/users.md) [OpenObserve README](https://github.com/openobserve/openobserve#security--compliance)
- OpenObserve server `v0.91.1` 是 AGPL-3.0；Helm chart 自身是 Apache-2.0。把 server binary 纳入 release artifact 前需要完成许可证清单、源代码/修改/网络服务义务和发布方式的法律审查；这里不作法律结论。[Server LICENSE](https://github.com/openobserve/openobserve/blob/v0.91.1/LICENSE) [Helm LICENSE](https://github.com/openobserve/openobserve-helm-chart/blob/main/LICENSE)
- `v0.91.1` 是正式 release，项目持续活跃，但版本号、stars 或官方 production-ready 声明都不能代替本项目 query/rule/recovery/load acceptance。[v0.91.1 release](https://github.com/openobserve/openobserve/releases/tag/v0.91.1)

## 6. OpenObserve MCP 核实

### 6.1 部署与传输

OpenObserve MCP **只在 Enterprise edition 提供**。必须设置 `O2_AI_ENABLED=true` 和 `O2_TOOL_API_URL`；OSS build 对 MCP endpoint 返回 404。[MCP docs](https://openobserve.ai/docs/integration/ai/mcp/) [MCP handler source](https://github.com/openobserve/openobserve/blob/v0.91.1/src/handler/http/request/mcp/mod.rs)

endpoint 是 `https://host/api/{org_id}/mcp`，使用 MCP `2025-11-25` Streamable HTTP：POST 请求、GET event stream、DELETE session；服务端是 stateless。非 initialize 请求应带 `MCP-Protocol-Version: 2025-11-25`，version mismatch 返回 400，notification 返回 202，DELETE 返回 204。JSON response 和 `Accept: text/event-stream` SSE 都受支持。[OpenObserve autonomous agents](https://openobserve.ai/docs/integration/ai/mcp/#building-autonomous-agents) [MCP Streamable HTTP](https://modelcontextprotocol.io/specification/2025-11-25/basic/transports#streamable-http)

### 6.2 工具清单

官方当前列出 133 个工具。证据查询相关的最小子集是：

- metrics：`PrometheusQuery`、`PrometheusRangeQuery`、`PrometheusMetadata`、`PrometheusSeries`、`PrometheusLabels`、`PrometheusLabelValues`、`PrometheusFormatQuery`；
- logs/traces：`SearchSQL`、`SearchAround`、`SearchValues`、`SearchPartition`，以及 async search job 工具；没有独立 `TraceQuery`，trace 依赖 stream/search API；
- discovery：`StreamList`、`StreamSchema`。

完整类别为 Alerts 28、Authorization 4、Dashboards 20、Enrichment Tables 2、Folders 6、Functions 6、KV Store 4、Logs ingestion 1、Organizations/System Settings 12、Patterns 1、Pipelines 7、PromQL/Metrics 7、Search 17、Service Accounts 4、Sourcemaps 4、Streams 5、Users 5。[Official tool list](https://openobserve.ai/docs/integration/ai/mcp/#available-tools)

其中包含 alert/role/user/organization/stream/pipeline/function/KV 的创建、更新、触发和删除工具。它不是只读 observability evidence server。即便客户端支持“调用前确认”，本项目也不能把这些 mutation tools 暴露给 Diagnosis；模型输出和 MCP tool call 都不构成 Kubernetes 或产品状态变更授权。

### 6.3 认证与组织隔离

官方 client 示例使用 `Authorization: Basic base64(email:password)`，并建议为 agent 建立 dedicated scoped user。每个 organization 有独立 endpoint；官方声明某 organization 的 token 不能访问另一 organization，并建议每个 org 注册一个 MCP server。[MCP auth and multi-org](https://openobserve.ai/docs/integration/ai/mcp/#multi-organization-workflows)

接入本项目时还必须额外固定：

1. OpenObserve credential 由 MCP owner 保存，不进入模型、Gateway response 或 Console；
2. URL `{org_id}` 和 tool arguments 中的 `org_id` 都由 Adapter 根据受信 `cluster_id -> organization` mapping 注入，不能接受模型自由提供；
3. OpenObserve organization/RBAC 只限制 backend data，不替代本项目 actor scope、Resource Binding、Evidence Gate 或 Approval Authority；
4. 只创建 read-only dedicated identity，并在网络层只允许本项目 MCP process 访问 endpoint。

### 6.4 查询限制与返回契约

OpenObserve MCP 对 search/list 默认做摘要：`SearchSQL`/`SearchAround` 只返回 `hits,total,took,columns,scan_size,function_error`，hits 上限 100；list tools 只返回 `summary_fields`；`detail='full'` 返回未摘要 response。[Tool response format](https://openobserve.ai/docs/integration/ai/mcp/#tool-response-format)

MCP 规范的 `tools/call` 结果是 `content`、可选 `structuredContent` 和可选 `isError`；server 可以声明 `outputSchema`，但协议本身不提供本项目所需的 evidence provenance、freshness 或 audit 语义。[MCP tools result](https://modelcontextprotocol.io/specification/2025-11-25/server/tools)

OpenObserve 官方 MCP 文档没有为每个查询工具承诺本项目需要的最大 time window、series/bytes、cluster/namespace scope、stable output schema、query digest、result digest、observed/expiry time 或 durable evidence handle。`detail='full'` 反而会绕开 100-hit 摘要，因此不能让模型控制。接入前必须由本项目 Adapter 继续执行已有的 query guard、30s timeout、line/series/byte limit、redaction、truncation 和 bounded error taxonomy。

### 6.5 能否直接接入现有 Diagnosis/MCP

**不能直接接入。** 原因不是 MCP transport 不兼容，而是 Module Interface 和信任边界不兼容：

- Diagnosis 的模型只认识 `query_metrics`、`query_logs`、`run_k8s_read`、`get_service_topology` 四个固定 tool schema；OpenObserve 暴露另一组工具名和参数。
- 本项目 MCP processes 不是通用 MCP wire server，而是 internal-auth HTTP facade，分别在 `/query_metrics` 和 `/query_logs` 返回 `ToolEnvelope`。
- `ToolEnvelope` 要求 `request_id/correlation_id/tool_name/status/summary/data/evidence_refs/audit/truncated/next_cursor/errors`；`EvidenceRef` 要求 `source/cluster_id/namespace/service/time_range/query_digest`。OpenObserve 的 generic MCP result 不产生这些产品契约。
- ADR-0028 规定 MCP 只返回 observation，Gateway 才拥有 durable Evidence Step 并确定性校验 scope、freshness、reference integrity；OpenObserve alert/incident/MCP 状态不能成为第二个 Evidence owner。
- 直接暴露 133 个工具会把产品 mutation、identity administration 和 backend deletion 带进 Diagnosis，违反最小 Interface 和显式审批边界。

推荐的最小接入形态：

```text
Diagnosis
  -> existing query_metrics/query_logs facade
     -> existing guard + scope + limits
        -> OpenObserve REST Adapter (preferred)
        -> normalize ToolEnvelope + EvidenceRef
  -> Gateway durable Evidence Step / Evidence Gate
```

OpenObserve REST API 比 Enterprise MCP 更适合这个窄 Adapter：metrics 已有 Prometheus-compatible query endpoint；logs/traces 有明确 Search API。这样不引入通用 MCP client、133-tool allowlist 或 Enterprise MCP 许可，只新增真实不兼容处需要的 auth/organization 和 SQL result normalization。只有组织已经购买 Enterprise，并且需要复用其 MCP tool schemas 时，才实现一个仅 allowlist `PrometheusRangeQuery/SearchSQL/StreamList/StreamSchema` 的 MCP client Adapter；仍不得绕过现有 facade 和 Evidence Reference。

因此无需重写现有 Diagnosis 或 MCP server；只需在既有 runner Seam 下增加薄证据适配层，并保留全部 guard、`ToolEnvelope` 和 Evidence Gate 行为。

## 7. 建议验证票（不纳入当前 Pilot）

只在决定试用 OpenObserve 后创建一个 bounded spike：

1. 在非生产 Cluster 部署 pinned standalone image，显式 7 天 retention 和 resource limits；不先设计 HA。
2. 复用 Alloy Loki push 写真实日志；用最小 OTel Collector 抓同一 AIOps/verification metrics。
3. 为现有 `PrometheusRunner` 增加 Basic Auth + organization，运行当前 PromQL guard/contract tests。
4. 实现一个 `OpenObserveLogRunner`，只接受 Adapter 生成的 bounded SQL，证明与 Loki fixture 相同的 scope、time、limit、truncation 和 redaction。
5. 将 OpenObserve webhook 映射为 Gateway Alert Signal，分别证明 firing、resolved、stable identity 和 duplicate delivery；不能只证明 webhook 200。
6. crash/restart 后验证 retained metrics/logs、metadata、credential、readiness 和 Evidence Reference 可重查；记录共享 PVC 的故障影响。
7. 让安全/法务确认 AGPL 与 Enterprise/MCP 商业条款，再决定是否写新 ADR 改 canonical bundle。

在这些 gate 通过前，OpenObserve 对本项目是值得验证的统一后端，不是已批准的发布依赖。
