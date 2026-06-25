# Design: Deploy monitor stack and switch AIOps to external backends

## 架构与边界

本任务不动 AIOps 任何进程边界或契约,只改两件事的部署侧:

1. 在 `loki` namespace 新起一套观测栈(Prometheus/Loki/Alloy),作为 AIOps 的真实后端。
2. 把 AIOps 的 dev 配置从 `dev-bundled`(自带假后端)切到 `dev-external`(指真实后端)。

AIOps 代码侧 `_collect_evidence`(ADR-0005 Issue A)、`/api/case-profile`(Issue B)、
dev-external overlay 注释(Issue C)已就绪,本任务只做部署 + 验证,不改 `toolsets/`、`apps/`、`hermes/` 代码。

```
aiops-dev namespace:                      loki namespace (new):
  aiops-gateway ── PROMETHEUS_URL ──►     prometheus-stack-kube-prom-prometheus:9090
  aiops-mcp-prometheus ─┘                   ▲ remote_write
                                            │
  aiops-gateway ── LOKI_URL ──►          loki:3100
  aiops-mcp-loki ─┘                          ▲ push
                                             │
  aiops-dev 内工作负载 ◄── 采集 ── alloy-agent(ds,两节点)► alloy-aggregator:1 ► {prom,loki}
```

## 监控栈部署细节

### Namespace: `loki`

选定理由: 三个 value 文件内部 service URL 全硬编码 `*.loki.svc.cluster.local`,装 `loki` ns 零改动。
若装 `monitoring` 则要跨 3 个文件改约 8 处 service 引用,易错且与 upstream value 分叉。idle 的
`monitoring` ns 不动。

### 镜像拉取: imagePullSecret

harbor 凭据 admin / `fa56#SNks`。在 `loki` ns 建 secret:

```bash
kubectl -n loki create secret docker-registry harbor-pull \
  --docker-server=harbor.5gfusion.com \
  --docker-username=admin \
  --docker-password='fa56#SNks'
```

三个 chart 通过 `--set global.imagePullSecrets[0].name=harbor-pull` 或各 chart 的 imagePullSecrets
字段挂载。三个 chart 都用 `image.registry=harbor.5gfusion.com`,release 时该 secret 必须就绪。

### 节点标签: `k8snode-1` ops=test

```bash
kubectl label node k8snode-1 ops=test --overwrite
```

监控栈所有组件 nodeSelector `ops:test` + tolerations `ops=test:NoSchedule` → 全部落 `k8snode-1`。
alloy-agent 是 daemonset,其 tolerations 还含 `node-role.kubernetes.io/control-plane` → master
也会跑一个,采 master 节点日志(符合 author 意图:采全节点日志),不与 `ops=test` 相关。
本任务**不给任何节点加 `ops=test:NoSchedule` taint**——只用 nodeSelector 把监控栈钉到 k8snode-1,
不加 taint 是为了不影响其他 workload 调度(集群只有 2 节点,加 taint 风险大)。

### Chart 安装顺序与命令

依赖: alloy-aggregator 的 remote_write/loki.push 指向 prometheus/loki service,但 alloy 启动时
若后端没就绪会重试(不会 crashloop 致命)。Prometheus 是接收方远程写端点,需先于 alloy 就位。
顺序: **prometheus-stack → loki → alloy-aggregator → alloy-agent**。

```bash
# 1. prometheus-stack (含 CRD + operator + prometheus + alertmanager + grafana + ksm)
helm upgrade --install prometheus-stack /root/aiops/monitor/prometheus/kube-prometheus-stack-76.2.1.tgz \
  -n loki --create-namespace \
  -f /root/aiops/monitor/prometheus/prometheus-lean-values.yaml \
  --set global.imagePullSecrets[0].name=harbor-pull

# 2. loki
helm upgrade --install loki /root/aiops/monitor/loki/loki-6.49.0.tgz \
  -n loki \
  -f /root/aiops/monitor/loki/loki-lean-values.yml \
  --set global.imagePullSecrets[0].name=harbor-pull

# 3. alloy-aggregator
helm upgrade --install alloy-aggregator /root/aiops/monitor/alloy/alloy-1.5.0.tgz \
  -n loki \
  -f /root/aiops/monitor/alloy/alloy-aggregator-values.yaml \
  --set global.imagePullSecrets[0].name=harbor-pull

# 4. alloy-agent
helm upgrade --install alloy-agent /root/aiops/monitor/alloy/alloy-1.5.0.tgz \
  -n loki \
  -f /root/aiops/monitor/alloy/alloy-agent-values.yaml \
  --set global.imagePullSecrets[0].name=harbor-pull
```

> 注: 同一个 alloy chart 用两套 values 部署两次(release 名 alloy-aggregator / alloy-agent),
> 两套 values 里 `service`/`controller` 形态不同,各自独立不冲突。这是 author 的设计。

### 预期 service 名(用于 AIOps overlay)

- Prometheus: `prometheus-stack-kube-prom-prometheus.loki.svc.cluster.local:9090`
  (kube-prometheus-stack chart 默认 service 名 `<release>-kube-prom-prometheus`)
- Loki: `loki-headless` 或 `loki`。SingleBinary 模式 chart 通常暴露 `loki` service on 3100。
  装后用 `kubectl -n loki get svc` 确认实际 Service 名再回填 overlay(避免猜错)。
- Alloy 远程写集中点: `alloy-aggregator.loki.svc` (agent → aggregator → {prom,loki})。AIOps 不直连 alloy。

## AIOps 切换: bundled → external

### overlay 改动(`deploy/k8s/overlays/dev-external/kustomization.yaml`)

只改 ConfigMap patch 的三个值:

| 字段 | 旧(占位) | 新 |
|---|---|---|
| `PROMETHEUS_URL` | `http://prometheus.monitoring.svc.cluster.local:9090` | `http://prometheus-stack-kube-prom-prometheus.loki.svc.cluster.local:9090` |
| `LOKI_URL` | `http://loki.monitoring.svc.cluster.local:3100` | `http://loki.loki.svc.cluster.local:3100`(装后核实 Service 名) |
| `AIOPS_NAMESPACE_SCOPE` | `default,prod` | `aiops-dev` |

`AIOPS_CONNECTOR_URL`/`AIOPS_GATEWAY_URL`/`AIOPS_CLUSTER_ID` 已在 overlay 内,保持不变。

### 切换流程(保留 AIOps 核心,删 bundled-only)

dev-bundled 与 dev-external 共用 `aiops-dev` ns,kustomize apply 只 upsert 不删旧资源。
bundled-only(假后端 + payment-api + synthetic log job)须显式删,避免和 external 共存产生混乱。

```bash
# 1. 先 apply external(新 ConfigMap 生效,核心 Deployment 滚动)
kubectl apply -k deploy/k8s/overlays/dev-external

# 2. 再删 bundled-only 资源(dev-external 不含它们)
kubectl -n aiops-dev delete deploy aiops-dev-prometheus aiops-dev-loki payment-api
kubectl -n aiops-dev delete job aiops-loki-synthetic-log   # 若存在
```

顺序原因: 先 apply external 让核心服务(AIOps 核心在两个 overlay 里同名同构)平滑滚动到新 ConfigMap,
再删 bundled-only 假后端。反过来(先删bundled 再 apply external)会造成窗口期核心服务短暂指向已删后端。

### 关键风险: rolling 时 ConfigMap 生效时机

AIOps 核心 Deployment 是 `apply` 更新 ConfigMap 后需 Pod 重启才读到新 env。base Deployment 若未设
`rollme`/checksum 注解,ConfigMap 改动不会自动滚动。执行时需显式:

```bash
kubectl -n aiops-dev rollout restart deploy/aiops-mcp-prometheus deploy/aiops-mcp-loki \
  deploy/aiops-hermes deploy/aiops-gateway deploy/aiops-connector
```

## 验证与数据流

### 1. 后端可达(独立验证,不动 AIOps)

```bash
# Prometheus 查 aiops-dev 指标
kubectl -n loki exec deploy/prometheus-stack-kube-prom-prometheus -- \
  wget -qO- 'localhost:9090/api/v1/query?query=up{namespace="aiops-dev"}'

# Loki 查 aiops-dev 日志
kubectl -n loki exec deploy/loki -- \
  wget -qO- 'localhost:3100/loki/api/v1/query_range?query={namespace="aiops-dev"}&limit=5'
```

alloy-agent 采到 `aiops-dev` 内 pod 日志/指标需几分钟累积。`aiops-dev` 工作负载需暴露 metrics 或
被 alloy 的 `discovery.kubernetes` 抓取(日志侧 alloy 用 pod 发现抓 stdout,无需 app 改造)。
指标侧若 `aiops-dev` 内 pod 无 metrics endpoint,`up{namespace="aiops-dev"}` 可能为空——这是正常
(只有日志的故障仍能在日志侧沉淀证据)。验收以"Prometheus/Loki 各能查到 aiops-dev 非空数据"为准,
不强求 metrics 全覆盖。

### 2. AIOps external 切换后健全性

```bash
kubectl -n aiops-dev rollout status deploy/aiops-gateway --timeout=180s
kubectl -n aiops-dev rollout status deploy/aiops-connector --timeout=180s
kubectl -n aiops-dev rollout status deploy/aiops-hermes --timeout=180s
# mcp
kubectl -n aiops-dev run aiops-health-smoke --rm -i --restart=Never \
  --image=registry.cn-hangzhou.aliyuncs.com/timelessmao/aiops-mcp-loki:latest \
  --command -- python3 -c "import urllib.request; print(urllib.request.urlopen('http://aiops-gateway:8080/healthz', timeout=5).read().decode())"
```

### 3. 证据采集链路(ADR-0005 Issue A 锚点)

从集群内 post 构造告警(README 文档化 smoke),payload 里 `namespace=aiops-dev`:

```bash
kubectl -n aiops-dev run aiops-alertmanager-smoke --rm -i --restart=Never \
  --image=registry.cn-hangzhou.aliyuncs.com/timelessmao/aiops-mcp-loki:latest \
  --command -- python3 -c "import json, urllib.request; payload={'alerts':[{'status':'firing','labels':{'alertname':'PodCrashLooping','severity':'critical','namespace':'aiops-dev','cluster':'dev-external'},'annotations':{'description':'pod restart count increasing'}}]}; req=urllib.request.Request('http://aiops-gateway:8080/webhooks/alertmanager', data=json.dumps(payload).encode(), headers={'Content-Type':'application/json'}, method='POST'); print(urllib.request.urlopen(req, timeout=15).read().decode())"
```

然后查 `incident_evidence` 表确认四路 observation 落库 + 脱敏。Hermes 数据库在 PVC `aiops-hermes-data`
内,查法:
```bash
kubectl -n aiops-dev exec deploy/aiops-hermes -- python3 -c \
 "import sqlite3,os; c=sqlite3.connect(os.path.expandvars('$AIOPS_DATA_DIR')+'/aiops.db'); ..."
```
(具体 DB 路径用 `incident_store` 代码确认,执行时再定。)

## 兼容性与回滚

### 回滚到 bundled

```bash
kubectl delete -k deploy/k8s/overlays/dev-external
kubectl apply -k deploy/k8s/overlays/dev-bundled
# 重启核心
kubectl -n aiops-dev rollout restart deploy/aiops-mcp-prometheus deploy/aiops-mcp-loki deploy/aiops-hermes
```

监控栈可独立保留(loki ns),不影响 bundled AIOps。

### 不可逆项

- kube-prometheus-stack 注入的 CRD(`serviceMonitors`/`podMonitors`/`prometheuses`/`alertmanagers` 等)
  是集群全局 CRD,卸 chart 默认不删 CRD。回滚 AIOps 不卸监控栈时这些 CRD 留下,对其他 workload 无害。
  完全卸载需 `kubectl delete crd -l app.kubernetes.io/name=prometheus-operator` 等(本任务不做)。

## 不改之物

- AIOps 代码(`apps/`、`hermes/`、`toolsets/`、`aiops/`): 零改动。
- monitor helm values: 零改动(因选 `loki` ns 适配硬编码 URL)。
- `deploy/k8s/base/`: 零改动。只改 `dev-external` overlay 的 ConfigMap patch 三个值。