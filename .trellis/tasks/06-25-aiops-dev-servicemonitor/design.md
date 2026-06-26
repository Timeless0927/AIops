# Design: aiops-dev ServiceMonitor for metrics evidence

## Goal Recap

让 AIOps 6 个真实服务被 `loki` ns 的 kube-prometheus-stack Prometheus scrape 到,使
`up{namespace="aiops-dev"}` 非空、Issue A metrics evidence 路有数据。三步联动:
代码暴露 `/metrics` → 部署开 metrics 端口/Service → ServiceMonitor 接入。

## Key facts(读码确认)

- `prometheus_client` **不在 requirements.txt**;6 个服务 entry 都不 import 它。
- 6 服务的 HTTP handler 都继承 `apps/service_http.py:33 JsonHandler`。
- `JsonHandler` 子类各自实现 `do_GET`(纯函数覆盖,不调 `super().do_GET()`):
  - `apps/aiops_k8s_gateway/main.py:205` GatewayHandler.do_GET — 先 urlparse,再分路由。
  - `hermes/service_main.py:27` HermesServiceHandler.do_GET。
  - `apps/cluster_connector/main.py:82` ConnectorHandler.do_GET — 直接 `self.path ==`。
  - `apps/observability_http.py:50` ObservabilityHandler.do_GET — mcp-prometheus/loki/topology 共用。
- Deployment/Service 现「单 http 端口」(`deploy/k8s/base/{deployment,service}.yaml`)。
- 全仓仅废弃 bundled mock 带 `prometheus.io/scrape` annotation。
- ServiceMonitor CRD 由 kube-prometheus-stack 提供;其 Prometheus 的 ServiceMonitor selector
  label 需集群实地确认(默认 release-based)。alloy/Prometheus 在 `loki` ns。

## Decision: exposition 用标准库手写,不引 prometheus_client

理由(ponytail 第 5/6 级):

1. `prometheus_client` 不在依赖,引入需改 `requirements.txt` + 各 split image Docker copy scope,
   扩面大、收益小(AC 只要「一 metric、空依赖、可 scrape」)。
2. 废弃 bundled mock 已验证手写 exposition 充分(`observability-bundled.yaml:329` inline)。
3. AC 不要求 RED 全套仪表 —— 一行 `aiops_service_up{service="X"} 1` 即让 `up` 路有数。

降到 exposition helper:在 `apps/service_http.py` 加共享函数 `metrics_body(service: str) -> bytes`,
返回 `text/plain; version=0.0.4` 的最小 exposition:

```
# HELP aiops_service_up AIOps service liveness metric for Prometheus scrape
# TYPE aiops_service_up gauge
aiops_service_up{service="<svc>"} 1
```

并在 `JsonHandler` 加两个共享 helper:`_is_metrics(self) -> bool`(查 `self.path`/`urlparse().path`
== `/metrics`)与 `write_metrics(self, service: str)`,这样 6 个服务不重复实现展览/响应头/字节编码。

## Decision: /metrics 路由怎么落进 6 个 do_GET

子类 `do_GET` 是纯覆盖、不 super。两条路线:

A. **共享 helper + 每个 do_GET 早返回(推荐)**:在 4 个含 `do_GET` 的文件(gateway/hermes/
   connector/mcp-observability,后者覆盖 3 个 mcp)各加一行 `if self._is_metrics():
   self.write_metrics(APP_NAME); return`,放各自 `do_GET` 顶部。改动面 = 4 文件各 1-2 行 +
   `apps/service_http.py` 增加 ~15 行 helper。无新依赖。
B. 干脆让 `JsonHandler` 不被覆盖的 `do_GET` 兜底 —— 不可行,所有子类都覆盖了。

选 A:贴现有模式(每个 do_GET 手写路由),diff 小、不引入 super 链。

`APP_NAME` 各服务已各自定义(gateway `APP_NAME`,connector `APP_NAME`,observability_http 用
`service_name` 闭包变量,trle hermes 自查)—— implement 时确认每处 service label 来源。

## Decision: 端口策略 —— 共用现有 http 端口

Deployment/Service 不新增 metrics 容器端口。ServiceMonitor 直接指向现有 `http` 端口
(8080-8085),`/metrics` 与 `/healthz`、业务路由共用。理由:最小改动、不增 Service 端口声明、
ServiceMonitor 一个 `port: http` 即可。若后续要隔离 metrics 流量,再拆端口(留作后续)。

代价:prometheus 抓 http 端口意味着 `/metrics` 与业务面同 port —— 信任面与 `/healthz` 一致,
服务内网 pod→pod,K8s RBAC 在 ServiceMonitor picker 层。

## Decision: ServiceMonitor 放哪 + selector

- 资源放 `deploy/k8s/base/`:6 个服务都 base Deployment,ServiceMonitor 选中它们,所有 overlay
  自动获得。dev-bundled 多出 bundled mock 不受影响(它有自己的 annotation,SM 不必管它)。
- selector:labelSelector match `app.kubernetes.io/part-of: aiops-sre-agent` + 命中 6 个
  服务名。但 Prometheus 栈的 **SM selector label**(由 kube-prometheus-stack Prometheus
  `serviceMonitorSelector` 决定)必须集群实地确认 —— 这是 implement 第一步硬验证项:查
  `kubectl -n loki get prometheus -o yaml` 的 `serviceMonitorSelector`,把该 label 写进 SM
  `metadata.labels`。常见默认 `release: prometheus-stack` 之类,不可假设。
- namespaceSelector:`aiops-dev`(dev-external 的 ns;base Deployment `namespace: aiops` 会被
  overlay 改成 `aiops-dev`,见 `overlays/dev-external/kustomization.yaml:3`)。

## Tradeoffs

- 手写 exposition 不能自动累计 counter(进程内状态),AC 只要 liveness metric,不在意精度。
  `ponytail: 不带 counter 累加,后续要 RED 指标再引 prometheus_client`。
- 共用 http 端口让 metrics 与业务同面:可接受,信任面等同 `/healthz`。
- base 放 ServiceMonitor 会影响所有 overlay 含 dev-bundled —— dev-bundled 的 bundled
  Prometheus 是 inline 没 `serviceMonitorSelector`,SM 不会被它 pick,无害;dev-disabled 更无害。
  implement 时确认 bundled 不报错。

## Rollout / Rollback shape

- 代码:加 helper + 各 do_GET 一行回退(删 AI 单 line 即回)。
- 部署:`apply -k dev-external` 后 ServiceMonitor 落地;回滚删 SM 即可(Prometheus 自动摘
  target,`up` 回空)。
- 无 schema/durable 变更,无数据迁移。

## Open items(implement 时关闭)

1. 集群实测 Prometheus `serviceMonitorSelector`(必须在加 SM 前确认,否则 SM 不被 pick)。
2. ServiceMonitor 是否需 `namespaceSelector` 含 `aiops-dev`;若 SM 自己在 `aiops-dev` ns,
   Prometheus 跨 ns pick 需 Prometheus 配置允许(implement 确认,可能需 Prometheus 的
   `serviceMonitorNamespaceSelector`)。
3. hermes 服务 `APP_NAME`/service label 来源确认。