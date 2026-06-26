# aiops-dev ServiceMonitor for metrics evidence

## Background / Problem

ADR-0005 Issue A `dev-external` smoke 中 metrics 路无数据:kube-prometheus-stack 默认只
scrape k8s 系统组件,不 scrape 业务 ns。Prometheus `up{namespace="aiops-dev"}` 为空 →
`aiops-mcp-prometheus` 查 `up` 返回空 → metrics observation `skipped`/空 → 四路 evidence
metrics 路结构性缺失。详见 `.trellis/spec/deploy/dev-external-observability-contract.md` §5
Bad case 2。

## Root Cause(已读码确认)

1. **6 个真实 AIOps 服务都不暴露 `/metrics`**:`grep` 全仓 `apps/` `hermes/` 无
   `prometheus_client` / `make_asgi_app` / `generate_latest` / `/metrics` 命中。唯一带 `/metrics`
   路由的是已经废弃的 bundled `payment-api` inline mock(`deploy/k8s/bundled/observability-bundled.yaml:329`),
   与 6 个真实服务无关。
2. **6 个Deployment/Service 都只有单 `http` 端口,无 metrics 端口,无 `prometheus.io/scrape`
   annotation**:`deploy/k8s/base/deployment.yaml` 每服务一个 `containerPort` 808x、port name
   `http`;`deploy/k8s/base/service.yaml` 同样只有 `http` port。全仓 `deploy/` 仅 bundled 那条
   mock 带 `prometheus.io/scrape`(legacy,dev-external 不带 bundled)。
3. **结论**:光加 ServiceMonitor YAML 是空 scrape —— Prometheus 会发现服务但 endpoints 都没
   `/metrics`,scrape 全 404/失败,`up` 仍 0。必须**先让服务暴露 `/metrics`,再开 metrics 端口,
   再加 ServiceMonitor**,三步联动。

本 child 范围(用户批准):**全做 —— 暴露 /metrics + 端口 + ServiceMonitor**。

## Requirements

1. 6 个 AIOps 服务在代码层暴露 `GET /metrics`,返回 Prometheus exposition format 文本
   (`text/plain; version=0.0.4`)。Phase-1 最小面 = 至少一个恒定 metric(如
   `aiops_service_up{service="..." } 1` / `aiops_http_requests_total`)让 scrape 不空;
   完整 RED 指标(request/error/duration)非 AC 必需,可后续加。
2. 复用现有 HTTP 框架:6 个服务都走 `apps/service_http.py` `JsonHandler`;`/metrics` 路由尽量
   在 shared 层(`JsonHandler` / `observability_http.py` / 新共享 helper)实现一次,6 个服务
   同时获得,**不逐服务复制粘贴**。`/metrics` 不依赖 `audit_log`/durable channel。
3. 不引第三方依赖:`prometheus_client` 是可选耦合点 —— 决策见 design.md(用 prometheus_client
   vs 手写最小 exposition)。两者都接受;倾向手写最小 exposition 以保持 Docker image copy scope
   不扩大、不增依赖(aligns with `Dockerfile.aiops` runtime copy scope 约束)。
4. 6 个 Deployment 暴露 metrics 端口(命名 `metrics`),Service 增加同名 port;`/metrics` 与
   现有 `/healthz`/`/readyz` 共服务端口也允许(单 http 端口 + ServiceMonitor 指向它)——
   design.md 决定「单端口共用」还是「独立 metrics 端口」,倾向最小改动(共用 http 端口)。
5. 新增 ServiceMonitor(CRD 来自 kube-prometheus-stack),selector 命中 6 个服务,被
   `loki` ns 的 Prometheus 栈 pick up。ServiceMonitor selector label 必须在集群确认
   (kube-prometheus-stack 默认 selector 通常为 `release: <release-name>` 或自定义),implement
   第一步先 manifest→实地查。
6. 不破坏现有 `/healthz`/`/readyz`、`/connectors`、`/webhooks/alertmanager` 等业务路由。
7. 不改 `dev-bundled` / `rc-bundled-digest` 现有行为 —— ServiceMonitor 作为 dev-external 的
   增量资源,放 overlay 或 base 由 design 决定。

## Acceptance Criteria

- [ ] 6 个服务的 entrypoint 都有可达的 `GET /metrics` 返回 `text/plain` exposition 文本,
      `curl http://aiops-<svc>:<port>/metrics` 200 且 body 含至少一行 `<metric_name> ... <value>`。
- [ ] `deploy/k8s/base/service.yaml` 或 ServiceMonitor 指向的端口能被 Prometheus 发现:
      集群内 `kubectl -n loki exec deploy/prometheus-stack-...` 查 `up{namespace="aiops-dev"}`
      不为空,含 6 个 AIOps 服务的 target。
- [ ] 新增 ServiceMonitor(`kubectl -n aiops-dev get servicemonitor` 可见)被 Prometheus 栈
      accepted:Prometheus `ServiceMonitors` 状态含本 SM,无 selector 不匹配告警。
- [ ] 端到端 metrics evidence 非空:dev-external smoke 的 `incident_evidence` metrics 路
      payload 含 `namespace=aiops-dev` 或 AIOps 服务 metric series(不再 `skipped`/空)。
- [ ] 既有测试不新增失败;`/metrics` 路由有最小自检或断言(非空 body、正确 content-type)。
- [ ] 不引入第三方依赖(若 design 选 stdlib 手写 exposition),Docker image copy scope 不扩大。

## Out of Scope

- 不做完整 RED 指标仪表盘 —— 一条可 scrape 的 metric 即满足 AC。
- 不补 middleware(emqx/redis/nacos…)ServiceMonitor。
- 不动 Grafana dashboard。
- 不改 dev-bundled payload-api mock(已废弃、dev-external 不带它)。
- 不为 metrics 加鉴权 —— ServiceMonitor 在集群内 pod→pod,与 `/healthz` 同信任面。
- parent 端到端四路全绿由 parent AC 兜;本 child 只对 metrics 路负责。