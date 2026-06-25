# Deploy monitor stack and switch AIOps to external backends

## Goal

把 `monitor/` 下的观测栈部署到集群,让 AIOps 能从真实 Prometheus/Loki 后端采集证据,进而
在线沉淀 ADR-0003 要的故障回放评测集(ADR-0005 Issue A/B/C 已落地进代码,缺的只是真实后端
和把 AIOps 切到 external profile)。

## Confirmed Facts (代码/集群已查明)

- 集群: `kubernetes-admin@cluster.local`,2 节点。`k8smaster-1`(control-plane+worker,本机宿主)
  + `k8snode-1`(worker,当前无 `ops` 标签、无 taint,装监控栈目标节点)。
- 节点资源: k8snode-1 allocatable 7.6cpu/14.4Gi,当前用 ~9%/4%。local-path 为 default StorageClass。
- 镜像仓库 `harbor.5gfusion.com`,凭据 admin / `fa56#SNks`。本机 curl harbor 不通(000),但集群
  节点能拉(ks-post-delete pod 用 harbor 镜像在跑)。需在目标 namespace 建 imagePullSecret。
- 集群当前无任何 prometheus/monitoring CRD → kube-prometheus-stack 首次安装,会注入一批 CRD
  (ServiceMonitor/PodMonitor/Prometheus/Alertmanager…),全局且不可逆。
- AIOps 已部署在 `aiops-dev`(15d): gateway/connector/hermes/mcp-prometheus/mcp-loki/mcp-topology
  + bundled 假后端 `aiops-dev-prometheus`/`aiops-dev-loki` + `payment-api`。`aiops` ns 另有一个
  pending trigger pod 和一个反复重启(12894 restarts)的 pod,本任务不处理(另行提示)。
- helm v3.9.0,kubectl v1.26.5。无 helm repo(用本地 `.tgz` chart,不依赖网络)。
- monitor 包结构(均在 `/root/aiops/monitor/`):
  - `prometheus/kube-prometheus-stack-76.2.1.tgz` + `prometheus-lean-values.yaml`
  - `loki/loki-6.49.0.tgz` + `loki-lean-values.yml`(SingleBinary,filesystem 存储)
  - `alloy/alloy-1.5.0.tgz` + `alloy-agent-values.yaml`(daemonset)+ `alloy-aggregator-values.yaml`(1 副本)
  - `pyroscope/`、`tempo/`(本任务不装)
  - `ServiceMonitor.yml`(middleware 的 emqx/redis/nacos/kafka/influx/pxc,本任务不补)+ `id.yaml`(grafana dashboard id)
- **关键: 三个 value 文件里所有内部 service URL 硬编码 `*.loki.svc.cluster.local`** → 部署 namespace
  定为 `loki` 以零改动适应 author 的配置。
  - Prometheus service 名将是 `prometheus-stack-kube-prom-prometheus.loki.svc:9090`(release name `prometheus-stack`)
  - Loki service 名 `loki.loki.svc:3100`
  - 合金 aggregator: `alloy-aggregator.loki.svc`,agent remote_write → aggregator → {prom,loki}
- kube-prometheus stack values: node-exporter 关闭(由 alloy 采 host metrics),grafana 开启,
  alertmanager 开启,全部组件 nodeSelector `ops:test` + tolerations `ops=test:NoSchedule`。
- alloy-agent daemonset tolerations 含 master/control-plane → 两个节点都跑(采全节点日志)。
  traces/pyroscope pipeline 内嵌,但无 app 向其 push OTLP/profile 时为 dormant,不产生错误,
  本任务不剥离,保持 author 配置零改动。
- dev-external overlay(`deploy/k8s/overlays/dev-external/kustomization.yaml`)当前指向占位:
  `PROMETHEUS_URL=prometheus.monitoring.svc:9090`、`LOKI_URL=loki.monitoring.svc:3100`、
  `AIOPS_NAMESPACE_SCOPE=default,prod`。需改成真实 `loki` ns service 名 + `aiops-dev` scope。
- dev-external 不含 `payment-api`(仅 bundled/rc profile 含)。
- AIOps 唯一诊断目标 namespace = `aiops-dev`(用户确认;项目开发测试全在此 ns)。

## Requirements

- 在 `loki` namespace 部署 kube-prometheus-stack(prometheus+alertmanager+grafana+operator+ksm)、
  loki(singleBinary)、alloy-agent + alloy-aggregator,镜像从 harbor 拉(imagePullSecret)。
- 给 `k8snode-1` 打 `ops=test` 标签,使 nodeSelector/tolerations 生效。
- 验证 Prometheus 能查到 `aiops-dev` 内工作负载指标,Loki 能查到 `aiops-dev` 内 pod 日志。
- 改 `dev-external` overlay: `PROMETHEUS_URL`/`LOKI_URL` 指向 `loki` ns 真实 service,
  `AIOPS_NAMESPACE_SCOPE=aiops-dev`。
- 把 AIOps 从 `dev-bundled` 切到 `dev-external`(delete bundled → apply external),保持
  `aiops-dev` ns 内 AIOps 核心服务不丢。
- 验证切到 external 后,告警 → Gateway → Hermes 链路四路 evidence 落 `incident_evidence`、
  脱敏生效(ADR-0005 Issue A 验收锚点)。
- harbor 凭据只用于部署阶段生成 imagePullSecret,不写入仓库 artifact/docs。

## Acceptance Criteria

- [ ] `loki` namespace 内 prometheus/loki/alloy pod 全 Ready;k8snode-1 有 `ops=test` 标签。
- [ ] `kubectl -n loki exec` 进 prometheus pod 查询 `up{namespace="aiops-dev"}` 有非空结果。
- [ ] Loki 能查到 `aiops-dev` 内某 pod 的日志流(`{namespace="aiops-dev"}`)。
- [ ] `dev-external` overlay 的 `PROMETHEUS_URL`/`LOKI_URL`/`AIOPS_NAMESPACE_SCOPE` 已改对,
      `kubectl kustomize` 渲染可见配置值。
- [ ] AIOps 切到 external 后,`aiops-gateway`/`aiops-connector`/`aiops-hermes`/mcp 三件 rollout 正常,
      bundled-only 资源(`aiops-dev-prometheus`/`aiops-dev-loki`/`payment-api`/synthetic log job)已删。
- [ ] 从集群内 `POST /webhooks/alertmanager`(README 文档化 smoke 命令)触发一次构造告警后,
      `incident_evidence` 表出现四路 evidence 记录,`aiops-dev` 相关证据非空,脱敏字段生效。

## Out of Scope

- 不装 pyroscope / tempo(无 traces/profiles 后端;alloy 内对应 pipeline dormant)。
- 不补 `ServiceMonitor.yml` 里 middleware 的 emqx/redis/nacos/kafka/influx/pxc ServiceMonitor。
- 不处理 `aiops` namespace 的 pending/反复重启 pod(另行提示)。
- 不写 LLM 诊断大脑(ADR-0003,后续任务)。
- 不做 Helm chart for AIOps(native YAML 不变)。

## Resolved Decisions

- **诊断目标 namespace**: `aiops-dev`(项目开发测试全在此 namespace)。
- **monitor 安装 namespace**: `loki`(三个 value 文件内部 service URL 全硬编码 `*.loki.svc.cluster.local`,
  零改动适配 author 意图;idle 的 `monitoring` ns 不动)。
- **监控栈范围**: 最小集 → kube-prometheus-stack + loki + alloy(agent+aggregator)。不装 pyroscope/tempo。
- **承载节点**: `k8snode-1`,打 `ops=test` 标签;监控栈组件 tolerations `ops=test:NoSchedule`。
- **告警触发方式(本任务)**: **B 手工触发**。本任务不接真实 Alertmanager 路由,验收用 README 文档化的
  从集群内 `POST /webhooks/alertmanager` 构造告警证明证据采集链路通。真实 Alertmanager 路由 + webhook
  HMAC secret 配置作为后续独立任务。Gateway 对 unsigned webhook 容忍(HMAC optional),手工 post 不需配 secret。

## Out of Scope