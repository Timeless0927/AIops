Type: grilling
Status: resolved
Blocked by: 01 (Establish Current Pilot Baseline), 09 (Define Generic Kubernetes Change Contract)

# Define Kustomize Release Installation Contract

## Question

What is the exact installation and release contract for a new Platform Operator to deploy the Pilot-ready Release from immutable OCI images and one Kustomize release overlay, including required Kubernetes inputs, generated credentials, storage and ingress prerequisites, preflight checks, readiness output, and rollback expectations?

The result must keep model and notification provider configuration out of the deployment happy path and must not require a source checkout or local image build.

## Comments

- Confirmed: distribute each version as `aiops-pilot-vX.Y.Z.tar.gz` plus `SHA256SUMS`. After verification and extraction, the canonical install entry is `kubectl apply -k ./aiops-pilot-vX.Y.Z`. The bundle contains one self-contained top-level overlay with Console and every backend image pinned by digest; Git checkout, remote Git bases, and an OCI manifest client are not install requirements.
- Confirmed: the installed product supports governed mutation across the entire Cluster, including system namespaces. The resource scope includes Deployments, StatefulSets, DaemonSets, and Jobs; the verification namespace is an acceptance fixture, not the product authorization boundary. Every mutation still requires the existing Evidence Gate, Approval Authority, explicit Approval, Execution Grant, and Connector Command chain.
- Confirmed: use a generic Kubernetes Change contract rather than enumerating one product action per operation. Approval freezes the exact structured create, patch, or delete diff; arbitrary shell, free-form kubectl, and approval of vague intent remain forbidden. The exact contract and ClusterRole surface are now prerequisites to this installation contract.

## Answer

Pilot Release Bundle 是面向新 Platform Operator 的独立二进制发布物，不是源码归档。每个版本发布：

```text
aiops-pilot-vX.Y.Z.tar.gz
SHA256SUMS
```

校验并解压后，目录 `aiops-pilot-vX.Y.Z/` 本身是唯一顶层 Kustomize overlay，包含本地 `kustomization.yaml`、安装说明和全部本地 manifest。它不得引用 remote Git base，不要求 Git checkout、Helm、OCI manifest client、Docker 或本地 image build。Console、Gateway、Diagnosis、Connector、MCP、Notification Engine、bundled Prometheus/Loki、bootstrap Job 及其他 Pod 使用的每个 OCI image 都必须以完整 `@sha256:` digest 固定，并允许 Cluster node 匿名拉取。

### 唯一安装入口

Release 固定安装到 `aiops-system`，Pilot 不支持修改 namespace。安装命令只有：

```bash
kubectl apply -k ./aiops-pilot-vX.Y.Z
```

bootstrap Job 成功后，重复执行同一命令可以收敛 manifest 且不会轮换 Secret；生成 Secret 是持久化安装状态，不属于 apply 可重建的 manifest。Kustomize render、当前 kube context 的写权限、内置 API validation 和固定 NodePort 冲突由 `kubectl apply` 与 API Server 直接报告；不再要求独立 `prepare` 或 `preflight` 命令，也不做 Kubernetes 版本门禁。该选择不承诺原子安装：API Server 拒绝某个对象时，之前接受的对象可能已经存在。

Platform Operator 必须预先提供以下真实 Kubernetes 条件：

- 当前 kube context 指向目标非生产 Cluster，并允许创建 `aiops-system`、namespaced workload/config/storage/RBAC、ClusterRole 和 ClusterRoleBinding。
- Cluster 有一个默认、可动态供给 `ReadWriteOnce` PVC 的 StorageClass，并有至少 `32Gi` 可用容量。
- `30088` 未被其他 NodePort Service 占用，且 Operator 至少知道一个从浏览器可达的 Node IP。
- Cluster node 可以访问 Release 使用的公开 OCI registry。
- CNI 实际执行 `networking.k8s.io/v1` NetworkPolicy；只接受对象但不实施隔离不满足该前提。

Pilot Bundle 不依赖 Prometheus Operator，不要求 `ServiceMonitor`、`PrometheusRule` 或 `AlertmanagerConfig` CRD。对应的外部 Operator 集成不是 canonical overlay 的安装前提。

### Bootstrap credentials

一个最小权限、一次性的 bootstrap Job 在 Cluster 内使用 cryptographic RNG 创建以下不存在的 Secret：

| Secret | 内容 |
| --- | --- |
| `aiops-runtime-secret` | `AIOPS_BOOTSTRAP_ADMIN_PASSWORD`、`AIOPS_ALERTMANAGER_WEBHOOK_TOKEN` |
| `aiops-notification-encryption` | Notification Destination 的独立 32-byte encryption key |
| `aiops-change-encryption` | Gateway/Connector Secure Input journal 的独立 32-byte encryption key |

Job 对已存在的 Secret 只校验必需 key，不覆盖、不轮换；全部 Secret 完成后写入 immutable ConfigMap `aiops-bootstrap-state` 作为 completion marker。marker 存在时，bootstrap Job 的任何后续运行发现 Secret 或必需 key 缺失都报告 `bootstrap_secret_lost`，不得生成替代 encryption key。因此重复 apply 不改变密码、webhook token 或 encrypted data key，也不掩盖 key loss。生成值只保存在 Kubernetes Secret，不写入 release 目录。忘记 admin password 时，持有 namespace Secret read 权限的 Operator 可以重新读取；安装契约不增加另一个密码恢复系统。

Model credential、model endpoint、Notification Destination、Notification provider credential 和 Connector Enrollment 都不属于部署 Secret，也不阻塞安装。它们由登录后的 Platform Administrator 在 Web setup/Platform Status 中配置和验证。

### Storage and access

所有 stateful process 保持 single replica 并使用独立 `ReadWriteOnce` PVC：

| Owner | PVC request |
| --- | ---: |
| Gateway | `5Gi` |
| Diagnosis | `5Gi` |
| Connector command journal | `1Gi` |
| Notification Engine | `1Gi` |
| Prometheus | `10Gi` |
| Loki | `10Gi` |

PVC 不设置 `storageClassName`，直接使用 Cluster 默认 StorageClass；Pilot 安装不提供容量或 StorageClass 参数。

默认浏览器入口是 `aiops-console` Service 的固定 NodePort `30088`。Console edge container 在同一 origin 下提供静态资源，并把 `/auth`、`/api/v1` 和 event stream 反向代理到 ClusterIP Gateway；Gateway、Diagnosis、Connector、MCP、Notification Engine 和 observability backend 不直接暴露给浏览器。默认 NodePort 使用 HTTP，只适用于受控的非生产网络。

Ingress 不是安装前提，canonical overlay 不创建 Ingress。Platform Operator 可以自行增加 Ingress、TLS 或其他入口，但必须把同一 origin 指向 `aiops-console` Service；不得让浏览器直连内部进程。

### Mutation authority

Bundle 安装 ADR-0053 定义的专用 `aiops-change-executor` ClusterRole/Binding，只绑定 Connector，并默认启用 Connector mutation execution。它覆盖整个 Cluster，包括 system namespaces、cluster-scoped resources 和 CRD；验证 namespace 只是 acceptance fixture，不是授权边界。

ClusterRole 不构成产品内 execution authority。每个 Kubernetes Change 仍必须通过 Evidence Gate、真实 Approval Authority、exact diff Approval、单次 Execution Grant 和 Connector Command。NetworkPolicy、projected token、structured API-only execution、precondition/hash validation 和 audit 仍是强制缓解措施。

### Readiness output

安装后允许使用只读命令等待和读取结果；这些命令不是额外安装入口：

```bash
kubectl wait -n aiops-system --for=condition=complete job/aiops-bootstrap --timeout=2m
kubectl wait -n aiops-system --for=jsonpath='{.status.phase}'=Bound pvc --all --timeout=10m
kubectl wait -n aiops-system --for=condition=Available deployment --all --timeout=10m
kubectl get secret -n aiops-system aiops-runtime-secret -o jsonpath='{.data.AIOPS_BOOTSTRAP_ADMIN_PASSWORD}' | base64 -d
kubectl get nodes -o wide
: "${NODE_IP:?set NODE_IP to a browser-reachable address listed above}"
curl --fail --show-error "http://${NODE_IP}:30088/healthz"
```

Installation Ready 表示 bootstrap Job 完成、6 个 PVC 均 Bound、Release Deployment 均 Available，且 `http://<reachable-node-ip>:30088/healthz` 返回成功。失败诊断固定从以下只读输出开始：

```bash
kubectl get pods,pvc,job -n aiops-system
kubectl describe pods,pvc,job -n aiops-system
```

`Pending` PVC、NodePort admission error、bootstrap Job failure、`ImagePullBackOff` 和 probe failure 必须保持可见，不得被报告为 ready。

Installation Ready 不等于 Operational Ready。Connector Enrollment、model、observability query 和 Notification Destination 可以保持 `unconfigured` 或 `unverified`，由登录后的 Platform Status 分别展示；它们不允许伪造默认值，但不阻塞首次登录。

### Failure and rollback

Pilot 当前只定义全新安装和同版本 reapply。普通 API admission、PVC、image pull 或 probe failure 修复后，Operator 重新 apply 同一 bundle 或等待现有 workload 收敛。Kubernetes 不会重跑已进入 terminal `Failed` 的同名 Job；若 `aiops-bootstrap` 已 Failed，唯一例外恢复步骤是先执行 `kubectl delete job -n aiops-system aiops-bootstrap`，再重新 apply，且不得删除任何已生成 Secret。completion marker 之后的 Secret loss 不通过 reapply 修复，必须保持显式失败。当前契约不提供自动 rollback，不保证 N-1 downgrade，也不定义保留数据的 uninstall；upgrade、downgrade、backup、key recovery 和 data-preserving removal 在出现真实需求时另行设计。
