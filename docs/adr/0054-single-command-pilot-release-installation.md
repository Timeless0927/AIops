# Pilot Release 使用单命令 Kustomize 安装

Status: accepted

Pilot Release 以 `aiops-pilot-vX.Y.Z.tar.gz` 和 `SHA256SUMS` 发布。解压目录是固定安装到 `aiops-system` 的唯一、自包含 Kustomize overlay；所有 Console 和 backend image 都公开可拉取并以 digest 固定。Platform Operator 的唯一安装入口是 `kubectl apply -k ./aiops-pilot-vX.Y.Z`，不需要源码、remote Git base、Helm、本地 image build 或独立 prepare/preflight 工具。

Kustomize 不能生成随机 Secret，因此 release 包含一次性 bootstrap Job，在 Cluster 内幂等创建 admin password、Alertmanager webhook token、Model encryption key、Notification encryption key 和独立 Change encryption key。完成 marker 使重复 apply 保留已有 Secret，并让 Secret/key loss 显式失败而不是生成会破坏 encrypted data 的替代 key。Model、Notification provider 和 Connector Enrollment 由首次登录后的 Web setup 管理，不阻塞控制面安装 readiness。

Pilot 固定使用 `aiops-console` NodePort `30088` 作为默认 HTTP 单一 browser origin，Console edge 反向代理 Gateway browser routes；Ingress/TLS 由 Platform Operator 按需配置。远程 Cluster Connector 不能使用该 HTTP NodePort，必须通过验证证书的 HTTPS `/connectors` route 主动连接同一 Gateway。Release 使用默认动态 StorageClass 上 6 个独立 RWO PVC，共 `32Gi`，且要求 CNI 实际执行 NetworkPolicy。它不依赖 Prometheus Operator CRD，也不设置 Kubernetes 版本门禁。

单命令优先意味着安装没有独立 preflight，也不是原子操作。API Server 直接报告权限、API validation 和 NodePort 冲突，workload status 报告 storage、image pull、bootstrap 和 probe failure；Operator 修复后重复 apply 同一 bundle。已进入 terminal `Failed` 的 bootstrap Job 不会被 apply 重跑，恢复时必须只删除该 Job 后再 apply，保留所有已生成 Secret。Pilot 当前不承诺自动 rollback、N-1 downgrade 或 data-preserving uninstall。
