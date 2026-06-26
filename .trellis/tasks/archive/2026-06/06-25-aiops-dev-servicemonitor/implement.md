# Implement: aiops-dev ServiceMonitor for metrics evidence

> 执行顺序按 dependency。验证命令可跑即跑;集群命令需 dev-external 实地环境。
> Active task: `06-25-aiops-dev-servicemonitor`(执行前先 `task.py start` 该 task)。

## Step 0 — 集群前置确认(硬门,不通过则 SM 必不被 pick)

- [ ] `kubectl -n loki get prometheus -o yaml | grep -A8 serviceMonitorSelector` — 记下
      ServiceMonitor 必须带的 label(常见 `release=<release>` 或空 match-all)。
- [ ] `kubectl -n loki get prometheus -o yaml | grep -A4 serviceMonitorNamespaceSelector` —
      确认 Prometheus 是否跨 ns pick SM;若仅限本 ns,SM 要放 `loki` ns 且 selector 打 aiops-dev。
- [ ] 记下 dev-external 现网 ns = `aiops-dev`(overlay `namespace: aiops-dev`)。
- 上述命令若集群不可达,记录「需 dev-external 实地补」并阻塞 Step 4 以后的验收,代码改动仍可做。

## Step 1 — 共享 `/metrics` exposition helper(单文件)

- [ ] `apps/service_http.py`:加 `metrics_body(service: str) -> bytes`(返回最小 exposition,
      `text/plain` 文本,gauge `aiops_service_up{service="..."} 1`)。
- [ ] `JsonHandler` 加 `def _is_metrics(self) -> bool`(用 `urlparse(self.path).path == "/metrics"`,
      规避各服务 `self.path` 形态不一)与 `def write_metrics(self, service: str)`(发 200、
      `Content-Type: text/plain; version=0.0.4`、写 `metrics_body` 字节)。
- [ ] 自检:`python3 -m apps.service_http` 或一条 `test_*`(ThreadingHTTPServer 起 handler、
      GET `/metrics`、断言 200 + body 含 `aiops_service_up` + `service` label)。

## Step 2 — 4 个 do_GET 入口挂 `/metrics`

- [ ] `apps/aiops_k8s_gateway/main.py` `GatewayHandler.do_GET` 顶部:
      `if self._is_metrics(): self.write_metrics(APP_NAME); return`(确认 `APP_NAME` 已定义)。
- [ ] `hermes/service_main.py` `HermesServiceHandler.do_GET` 同上(确认 hermes 的 service 名常量)。
- [ ] `apps/cluster_connector/main.py` `ConnectorHandler.do_GET` 同上(`APP_NAME`)。
- [ ] `apps/observability_http.py` `ObservabilityHandler.do_GET`:`service_name` 闭包变量已可用,
      `if self._is_metrics(): self.write_metrics(service_name); return` —— 一处覆盖 mcp-prometheus/
      loki/topology 三个服务。
- [ ] 回归:`pytest tests/` 不新增失败;现有 health/ready/connectivity/webhook 路由不受影响。

## Step 3 — Service / Deployment 端口(共用 http,不增端口)

- [ ] 确认 `deploy/k8s/base/service.yaml` 6 个 Service 的 `http` port 已就位,ServiceMonitor
      直接 `targetPort`/`port: http` 即可,不改 service/deployment。若 Step 4 SM 指定 port
      name `http` 成功则本步零改动。
- [ ] (可选,若共用 http 不可达)在 base deployment/service 加独立 `metrics` 端口。
      倾向:不增加,保持共用。

## Step 4 — ServiceMonitor 资源

- [ ] 新增 `deploy/k8s/base/servicemonitor.yaml`:`apiVersion: monitoring.coreos.com/v1`,
      `kind: ServiceMonitor`,label 带 Step 0 探到的 selector label;`spec.namespaceSelector`
      含 `aiops-dev`;`spec.selector.matchLabels` 命中 6 服务(`app.kubernetes.io/part-of:
      aiops-sre-agent` + 或枚举 6 个 name);`spec.endpoints` 一个,`port: http`,`path: /metrics`,
      `interval: 30s`。
- [ ] `deploy/k8s/base/kustomization.yaml` `resources:` 追加 `servicemonitor.yaml`。
- [ ] `kubectl kustomize deploy/k8s/overlays/dev-external` 含 rendered ServiceMonitor。

## Step 5 — 部署 + 端到端验收(需 dev-external 实地)

- [ ] 重新构建并 push 6 个 split image(`Dockerfile.aiops` 各 target),或用 candidate-<branch>。
- [ ] `kubectl apply -k deploy/k8s/overlays/dev-external`(先确认 ConfigMap rollout 已就绪)。
- [ ] `kubectl -n aiops-dev rollout restart deploy/aiops-{gateway,connector,hermes,mcp-prometheus,
      mcp-loki,mcp-topology}`;`rollout status` 6 个全 Ready。
- [ ] 抓 metrics:`kubectl -n aiops-dev run <probe> ... -- python3 -c "import urllib.request;
      print(urllib.request.urlopen('http://aiops-gateway:8080/metrics', timeout=5).read().decode())"`
      每个 svc 抽样 200 + 含 `aiops_service_up`。
- [ ] Prometheus 接收:`kubectl -n loki exec deploy/prometheus-stack-kube-prom-prometheus -- \
      wget -qO- 'http://localhost:9090/api/v1/query?query=up%7Bnamespace%3D%22aiops-dev%22%7D'`
      含 6 个 AIOps target 且 `value` 为 `1`(或至少 `1` 而 health 正常)。
- [ ] SM 被接受:`kubectl -n aiops-dev get servicemonitor`;Prometheus targets 页/SDB
      `--service.openshift.io` 等价确认无 selector mismatch。

## Step 6 — 联动 parent 端到端 metrics evidence

- [ ] 跑 README alertmanager smoke,验 `incident_evidence` metrics 路 payload 非空、
      `namespace=aiops-dev` 或含 `aiops_service_up` series(parent AC 项)。
- [ ] 若 session 仍 failed 但 metrics 路非空 → 与 stdout child + diagnosis child 合查;
      本 child 仅对 metrics 路负责。

## Review gates / Rollback points

- Step 1-2 完成且 pytest 绿 → 提交一个 commit「feat: expose /metrics on AIOps services」。
- Step 4 完成 → 可单独 commit「deploy: add ServiceMonitor for aiops-dev」。
- 回滚:删 servicemonitor.yaml + 各 do_GET `_is_metrics` 行 + helper,无需数据迁移。

## Notes

- `prometheus_client` 不引;`metrics_body` 手写最小 exposition,见 design.md rationale。
- `/metrics` 不走 `audit_log`(它在 AIOps 自身被 scrape,非 control-plane durably event)。
- ServiceMonitor selector 实地确认是硬门,绝不照搬默认值。