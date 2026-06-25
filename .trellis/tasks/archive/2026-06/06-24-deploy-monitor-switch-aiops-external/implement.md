# Implement: Deploy monitor stack and switch AIOps to external backends

## 验证命令(每步后跑)

- pods Ready: `kubectl -n loki get pods`
- overlay 渲染: `kubectl kustomize deploy/k8s/overlays/dev-external`
- AIOps rollout: `kubectl -n aiops-dev rollout status deploy/<name> --timeout=180s`

## 有序执行 checklist

### 阶段 0: 部署前置(无依赖)

- [ ] **0.1** 给 k8snode-1 打标签
  `kubectl label node k8snode-1 ops=test --overwrite`
  验证: `kubectl get node k8snode-1 --show-labels | grep ops=test`
  回滚: `kubectl label node k8snode-1 ops-`

- [ ] **0.2** 在 `loki` ns 建 harbor imagePullSecret
  ```bash
  kubectl create namespace loki   # 若不存在(helm --create-namespace 也会建)
  kubectl -n loki create secret docker-registry harbor-pull \
    --docker-server=harbor.5gfusion.com \
    --docker-username=admin --docker-password='fa56#SNks'
  ```
  验证: `kubectl -n loki get secret harbor-pull`
  注意: 凭据不写入仓库任何文件。

### 阶段 1: 部署监控栈(顺序: pent → loki → alloy-agg → alloy-agent)

- [ ] **1.1** helm install prometheus-stack(注入 CRD + operator)
  ```bash
  helm upgrade --install prometheus-stack /root/aiops/monitor/prometheus/kube-prometheus-stack-76.2.1.tgz \
    -n loki -f /root/aiops/monitor/prometheus/prometheus-lean-values.yaml \
    --set global.imagePullSecrets[0].name=harbor-pull --wait --timeout 10m
  ```
  验证: `helm -n loki list`;`kubectl -n loki get pods`;`kubectl get crd | grep monitoring.coreos`
  风险: 首次注入 CRD 不可逆;镜像拉取若 harbor 在该节点不可达会 imagePullBackOff → 看 pod events 确认 harbor 可达性。
  回滚: `helm -n loki uninstall prometheus-stack`(CRD 留下,本任务不删)。

- [ ] **1.2** helm install loki
  ```bash
  helm upgrade --install loki /root/aiops/monitor/loki/loki-6.49.0.tgz \
    -n loki -f /root/aiops/monitor/loki/loki-lean-values.yml \
    --set global.imagePullSecrets[0].name=harbor-pull --wait --timeout 10m
  ```
  验证: `kubectl -n loki get pods -l app.kubernetes.io/name=loki`;记下 Loki Service 名:
  `kubectl -n loki get svc | grep -I loki`(回填 overlay 用)。

- [ ] **1.3** helm install alloy-aggregator
  ```bash
  helm upgrade --install alloy-aggregator /root/aiops/monitor/alloy/alloy-1.5.0.tgz \
    -n loki -f /root/aiops/monitor/alloy/alloy-aggregator-values.yaml \
    --set global.imagePullSecrets[0].name=harbor-pull --wait --timeout 10m
  ```

- [ ] **1.4** helm install alloy-agent(daemonset,两节点)
  ```bash
  helm upgrade --install alloy-agent /root/aiops/monitor/alloy/alloy-1.5.0.tgz \
    -n loki -f /root/aiops/monitor/alloy/alloy-agent-values.yaml \
    --set global.imagePullSecrets[0].name=harbor-pull --wait --timeout 10m
  ```
  验证: `kubectl -n loki get ds` 两个节点都 Running。

- [ ] **1.5** 等待数据累积(3-5min),验证后端可达 aiops-dev
  ```bash
  kubectl -n loki exec deploy/prometheus-stack-kube-prom-prometheus -- \
    wget -qO- 'localhost:9090/api/v1/query?query=up{namespace="aiops-dev"}'
  kubectl -n loki exec deploy/loki -- \
    wget -qO- 'localhost:3100/loki/api/v1/query_range?query={namespace="aiops-dev"}&limit=5'
  ```
  验收: Prometheus 或 Loki 至少一方对 `aiops-dev` 有非空结果(日志侧 alloy 自动抓 pod stdout,
  通常先有数据)。若全空,查 alloy-agent pod 日志确认采集是否运行。

### 阶段 2: 改 dev-external overlay

- [ ] **2.1** 确认 Loki Service 名(`kubectl -n loki get svc`),用实际名定 LOKI_URL。
  Prometheus 确定为 `prometheus-stack-kube-prom-prometheus`(kube-prometheus-stack 默认)。

- [ ] **2.2** 编辑 `deploy/k8s/overlays/dev-external/kustomization.yaml` 的 ConfigMap patch:
  - `PROMETHEUS_URL` → `http://prometheus-stack-kube-prom-prometheus.loki.svc.cluster.local:9090`
  - `LOKI_URL` → `http://<loki-svc>.loki.svc.cluster.local:3100`(用 2.1 实际名)
  - `AIOPS_NAMESPACE_SCOPE` → `aiops-dev`
  保留 SECURITY 注释行。

- [ ] **2.3** 验证渲染
  `kubectl kustomize deploy/k8s/overlays/dev-external | grep -E "PROMETHEUS_URL|LOKI_URL|NAMESPACE_SCOPE"`
  三个值出现且正确。

### 阶段 3: AIOps 切到 external(核心服务平滑过渡)

- [ ] **3.1** apply external(核心 Deployment upsert,新 ConfigMap 生成)
  `kubectl apply -k deploy/k8s/overlays/dev-external`

- [ ] **3.2** 显式重启核心让 ConfigMap 生效
  ```bash
  kubectl -n aiops-dev rollout restart deploy/aiops-mcp-prometheus deploy/aiops-mcp-loki \
    deploy/aiops-hermes deploy/aiops-gateway deploy/aiops-connector
  kubectl -n aiops-dev rollout status deploy/aiops-gateway --timeout=180s
  kubectl -n aiops-dev rollout status deploy/aiops-connector --timeout=180s
  kubectl -n aiops-dev rollout status deploy/aiops-hermes --timeout=180s
  ```

- [ ] **3.3** 删 bundled-only 资源(dev-external 不含它们)
  ```bash
  kubectl -n aiops-dev delete deploy aiops-dev-prometheus aiops-dev-loki payment-api
  kubectl -n aiops-dev delete job aiops-loki-synthetic-log 2>/dev/null || true
  ```
  验证: `kubectl -n aiops-dev get deploy` 只剩 AIOps 核心 + mcp;`kubectl -n aiops-dev get pods` 无 bundled 遗留。

- [ ] **3.4** 健全性 smoke(健康 + 注册)
  ```bash
  kubectl -n aiops-dev run aiops-health-smoke --rm -i --restart=Never \
    --image=registry.cn-hangzhou.aliyuncs.com/timelessmao/aiops-mcp-loki:latest \
    --command -- python3 -c "import urllib.request; print(urllib.request.urlopen('http://aiops-gateway:8080/healthz', timeout=5).read().decode())"
  ```

### 阶段 4: 证据采集链路验收(ADR-0005 Issue A 锚点)

- [ ] **4.1** 确认 mbedtls 数据库路径(从 `incident_store` 代码查 `$AIOPS_DATA_DIR`/aiops.db 或类似),
  准备查 `incident_evidence` 的方式。

- [ ] **4.2** 从集群内 post 构造告警(namespace=aiops-dev)
  ```bash
  kubectl -n aiops-dev run aiops-alertmanager-smoke --rm -i --restart=Never \
    --image=registry.cn-hangzhou.aliyuncs.com/timelessmao/aiops-mcp-loki:latest \
    --command -- python3 -c "import json, urllib.request; payload={'alerts':[{'status':'firing','labels':{'alertname':'PodCrashLooping','severity':'critical','namespace':'aiops-dev','cluster':'dev-external'},'annotations':{'description':'pod restart count increasing'}}]}; req=urllib.request.Request('http://aiops-gateway:8080/webhooks/alertmanager', data=json.dumps(payload).encode(), headers={'Content-Type':'application/json'}, method='POST'); print(urllib.request.urlopen(req, timeout=20).read().decode())"
  ```
  预期: 返回 incident_id/session 起手;Hermes 异步跑诊断(~几十秒)。

- [ ] **4.3** 等 Hermes 处理完(60s-2min),查 `incident_evidence`
  ```bash
  kubectl -n aiops-dev exec deploy/aiops-hermes -- python3 -c \
    "<查 incident_evidence 的 sqlite 查询>"
  ```
  验收: 四路(metrics/logs/k8s_read/topology)evidence 记录存在,`aiops-dev` 相关 payload 非空,
  脱敏生效(payload 内无明文 secret)。

### 阶段 5: 截止

- [ ] **5.1** 更新 spec/docs: 在 `deploy/k8s/README.md` 的 dev-external 说明补"本机部署用了真实
  loki ns 后端"事实(若改动 overlay 已可从 commit 看出,README 视情况)。ADR-0005 Issue A 验收锚点写入注记。

- [ ] **5.2** git 提交: `dev-external/kustomization.yaml` 改动(PROM/LOKI/NAMESPACE_SCOPE 三个值)。
  只提交 overlay 注释 + 值变更,不提交任何含 harbor 凭据的文件。

## 风险点 / 回滚锚

| 步骤 | 风险 | 回滚 |
|---|---|---|
| 1.1 | CRD 注入不可逆;harbor 不可达 imagePullBackOff | uninstall chart;CRD 留下无害 |
| 1.x | Pod 不 Ready | `kubectl describe pod` 看 events,多为 harbor 达性/资源 |
| 3.3 | 误删核心(命令明确只删 bundled-only,核对名) | 改回 apply dev-bundled |
| 4.x | evidence 空(后端虽有数据但 query 不匹配) | 查 mcp-prometheus/loki pod 日志看 query 结果 |

## 执行前需用户确认的最后两点(执行时)

- Loki 实际 Service 名(1.2 装完才能确定 → 进 2.1 回填 overlay)。
- `incident_evidence` 的 DB 查询语句(4.1 从 incident_store 代码确定)。