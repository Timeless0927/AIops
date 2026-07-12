Type: grilling
Status: resolved
Blocked by: 04 (Define Bundled Observability And Alert Path), 05 (Define Model And Notification Readiness), 09 (Define Generic Kubernetes Change Contract)

# Define Controlled Verification Scenario

## Question

What exact controlled verification scenario lets a User create and recover one real Incident from a sample workload, collect real metric/log/Kubernetes evidence, receive an evidence-grounded restart recommendation, explicitly approve a real isolated restart, observe its outcome, resolve the Incident, publish its Report, and receive a real notification?

Resolve sample workload ownership, failure trigger and recovery, namespace and RBAC isolation, idempotency, cleanup, rerun behavior, safety limits, retained history, and the evidence that proves every production boundary was used.

## Answer

Pilot 使用 Release Bundle 内 version-matched、默认不安装的 `verification/` Kustomize fixture，完成两轮真实 Alert-to-Report 演练。Fixture 不写 Gateway API/SQLite、不 seed product state、不调用 fake backend；唯一故障是一个隔离 workload 的进程内 latched readiness failure，唯一恢复是 User 审批后由 Connector 执行的真实 Deployment rollout。

### Fixture ownership and shape

Platform Operator 显式安装和最终删除 fixture；SRE 从 Incident 出现后接管 Console 内 Investigation、Change Request、Approval 和 Report。Release 包含两个入口：

```bash
kubectl apply -k verification/base
kubectl apply -k verification/run
```

`verification/base` 创建固定 `aiops-verification` namespace、ResourceQuota、LimitRange、restricted Pod Security labels、NetworkPolicy、无 token 的 ServiceAccount、单副本 `verification-api` Deployment 与 ClusterIP Service。它不随 canonical install 默认部署，避免在 Connector、Model、Notification 或 Resource Catalog 尚未 ready 时自动制造故障。

`verification-api` 使用 release-pinned immutable digest，提供：

```text
GET  :8080/livez    process liveness，fault 时仍为 200
GET  :8080/readyz   readiness，fault 时为 503
GET  :9090/metrics  real Prometheus metric
POST :8081/trigger  internal one-shot fault trigger
```

App 不访问 Kubernetes API、无 privileged capability/hostPath/hostNetwork，不持久化产品状态。App 与 trigger Job 的单 Pod request/limit 固定为 `50m/64Mi` 与 `200m/128Mi`。Namespace ResourceQuota 固定为最多 5 Pods、1 Deployment、1 Job，合计 request `500m/512Mi`、limit `1 CPU/1Gi`，允许 rollout 临时 surge 但限制 blast radius。

`verification/run` 只创建一个 trigger Job，复用同一 verification image 的 `trigger` command。Job 从 Pod label `batch.kubernetes.io/controller-uid` 取得 Kubernetes 生成的 `run_id`，等待 app ready 后 POST `/trigger`；Pod retry 使用同一 controller UID，因此 request 幂等。相同 `run_id` 重放返回原成功，不同 `run_id` 在 fault active 时返回 conflict。删除 run overlay 后再次 apply 会创建新 Job UID，不需要人工输入 ID：

```bash
kubectl delete -k verification/run --ignore-not-found
kubectl apply -k verification/run
```

NetworkPolicy 以 port 区分能力：只允许 matching trigger Job 访问 app `8081`、bundled Prometheus 访问 app `9090`，并只允许 trigger Job egress 到 app 与 DNS；App 禁止 egress。Kubelet 直接执行 `8080` probes，Alloy 通过已授权 Kubernetes log API 读取 stdout/stderr，不需要 fixture ingress。

### Fault and real signals

第一次有效 trigger 把当前进程内状态锁定为 `fault_active`，记录 `run_id`，输出一条 structured fault log，使 `/readyz` 失败但不影响 `/livez`，从而阻止 Kubernetes 自动重启。App 暴露：

```text
aiops_verification_fault_active{service="verification-api",run_id="<job-controller-uid>"} 1
```

`run_id` label 仅允许本验收固定两轮，cleanup 后 fixture 消失；该严格 ceiling 防止把 run identity 扩散成长期 metric cardinality。Deployment pod template 的 `aiops.dev/verification-run-id` annotation 通过 Downward API 传给新 Pod；rollout 后 app 对同一 run 输出 recovery log 和 metric `0`。

04 定义的 Prometheus rule 只匹配 exact namespace、Deployment 和 metric，生成包含 `cluster`、`namespace`、`deployment`、`service`、`severity`、`run_id` 和 `aiops_route="gateway"` 的 alert。Prometheus 实际 evaluate 后交给 Alertmanager；Alertmanager 以 bearer-authenticated `send_resolved` webhook 调 Gateway。人工 POST webhook、synthetic log push、constant metric 或 label-only Pod 不算触发。

### Product prerequisites

第一次 run 前必须通过公开产品边界完成：

1. Pilot installation ready，bundled observability targets/rules ready。
2. Connector Enrollment、Cluster registration/read verification ready；其 exact `cluster_id` 与 Prometheus external label 相同。
3. exact Model Provider revision ready，selected Notification Destination ready。
4. Platform Administrator 在 Console 创建 verification Team/Service，把 Connector 真实发现的 Deployment Discovery Candidate 提升为 Deployment Target，并确认 non-production Resource Binding。Manifest、Job 和脚本不得创建 catalog state。
5. User 获得只覆盖 `non-production + aiops-verification namespace` 的 Approval Authority；不要求 Cluster Change Authority，也不从 Platform Administrator 身份推导权限。

### Evidence Gate and diagnosis

Alert Signal 创建或 reopen Incident 后，Diagnosis 必须使用 verified Model revision 并经真实 MCP/Connector path 收集同一 scope、2 分钟 freshness 内的四项 evidence：

- Prometheus/Alertmanager 产生的真实 Alert Signal；
- Prometheus `fault_active=1` 与 kube-state-metrics Deployment unavailable series；
- Loki 中同一 `run_id` 的 fault activation log；
- Connector live read 的 exact Deployment/Pod UID、resourceVersion 与 readiness failure。

四项缺一只能形成 `partial/needs_input`，不得产生 approvable restart；Human Input 不能补足 Evidence Gate。Topology 可以提供补充关系，但不作为本场景 gate。Diagnosis 必须生成 evidence-grounded Recommended Action，不能使用 keyword fallback、fixture truth 或预写 root cause。

### Governed restart

Recommended Action 不能直接 Approval。User 显式从 Recommendation 创建 Change Request；模型根据 evidence 生成一个 exact RFC 6902 Kubernetes Change，把 Deployment pod template annotation `aiops.dev/verification-run-id` add/replace 为当前 `run_id`。Gateway 通过 Connector 读取 live target、冻结 UID/resourceVersion/old value、执行 API Server dry-run，并向 User 展示 exact diff。

普通 Deployment annotation 不是 Sensitive Change，因此 namespace-scoped Authority 可以 self-approve；Gateway 必须在 plan、diff、Approval、Execution Grant 和 dispatch 全程证明 scope 未离开 `aiops-verification`。User 仍需 5 分钟 fresh auth、reason 和 exact target confirmation。

Rollout 无法恢复旧 Pod identity，所以 Change 明确声明 `rollback: unavailable`，不伪造 inverse patch。Approval 后 Gateway 签发 60 秒 single-use Execution Grant，Connector 只执行一次 frozen patch；drift 产生 Stale Change，response loss 进入 Unknown Outcome，均禁止自动 retry。

Frozen execution post-check 在 5 分钟内只要求：

- pod template annotation exact 等于 `run_id`；
- Deployment observedGeneration、updated/available replica 与 readiness 全部收敛；
- Prometheus 连续两个 15 秒 evaluation 返回同一 run 的 `fault_active=0`。

Loki recovery log、Alertmanager resolved webhook 和 Incident stabilization 是后续 scenario evidence，不决定 Connector execution success，避免 log delivery lag 把真实成功 rollout 误判为 mutation failure。

### Recovery, report and notification

Post-check 后，Alloy/Loki 必须出现新 Pod 对同一 `run_id` 的 recovery log；Prometheus rule 必须 resolve，Alertmanager 必须发送相同 alert fingerprint 的 resolved webhook，Gateway 形成 Recovery Observation。全局 5 分钟 stabilization 完成且 Investigation terminal 后 Incident 才能 resolved。

User 通过 Gateway/Console 创建 Report draft，填写 impact、root-cause explanation、resolution summary 和 follow-up，再显式发布 immutable version。场景必需的业务通知固定为 `incident.resolved`：Gateway transaction outbox、Notification Engine Route、selected Destination、Provider Delivery 必须达到 `sent`，且验收人员实际看到消息。其他 Incident/Approval/execution 通知不增加 gate；当前不新增 `report.published` event。

### Deadlines and invalidation

| Stage | Deadline |
| --- | ---: |
| Trigger Job terminal | `2m` |
| Metric/log queryable | `2m` |
| Alert Signal and Incident visible | `3m` |
| Diagnosis terminal | `10m` |
| dry-run Approval | 09 固定的 `10m` window |
| rollout and frozen post-check | `5m` |
| resolved webhook | `2m` |
| Incident stabilization | `5m` |
| resolved Notification Delivery | `10m`，内部仍遵循 3 attempts 与 `Retry-After <= 300s` |

任一自动阶段超时、owner 非 ready、缺 evidence、out-of-band Pod delete/restart、人工修改 metric/log/Alert payload、直接数据库/API state patch、未经批准的 mutation 或 Notification 未实际送达都会使该 run 失败。系统不得通过隐藏 retry、关键词 diagnosis、手工改 terminal state 或自动恢复补过。Fixture fault 不设 TTL；中止时 Operator 删除 fixture，且该 run 明确失败。

### Rerun and cleanup

第一轮成功后只删除 `verification/run` Job，保留 base Deployment 和 binding。第二次 apply 产生新 `run_id` 和 Alert Signal fingerprint，但保留相同 `cluster + namespace + workload + alertname` correlation key，因此在 24 小时 reopen window 内 reopen 原 Incident、创建新 Investigation。再次 recovery 后发布新的 immutable Report version，旧 publication 不变；第二轮同样要求新的 `incident.resolved` Delivery sent。

第二轮完成后执行：

```bash
kubectl delete -k verification/run --ignore-not-found
kubectl delete -k verification/base --ignore-not-found
```

Gateway/Diagnosis/Notification 保留 Incident、两轮 Investigation/Evidence、Recommended Action、Change Plan/dry-run diff、Approval/Grant、Connector Command/outcome、Recovery Observation、Delivery 和两版 Report history。Resource Catalog 保留 Service/Binding 并把已删除 Deployment Target 显示 unavailable；以后重新部署产生新 UID 时必须重新确认 binding。Prometheus/Loki raw data 按 04 的 7 天 retention 到期，不复制进 governance database。

### Acceptance evidence index

每轮以 `run_id` 串联并保留一个不含 secret/raw reasoning 的 evidence index：Release digest、fixture object UID、Platform Status revisions、Prometheus target/rule/series reference、Loki evidence reference、Alert fingerprint、Incident/Investigation ID、Model revision、Recommended Action/Change Plan revision 与 hash、dry-run diff、Authority/Approval/Execution Grant、Connector Command/terminal result、post-check、Recovery Observation、Report version、Notification Delivery/message identity。08 再把这些字段固化为 release-gate matrix；本票不建立第二套 run database 或 acceptance state machine。
