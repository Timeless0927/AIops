# Pilot A01 验收 Runner

`scripts/run_pilot_acceptance.py` 从 immutable Pilot tarball 执行 A01 的 `P01-P03`、`I01-I05` 和 `S01-S06` gate，并生成 `.scratch` 规格定义的脱敏 evidence bundle。Runner 只调用公开 CLI、Kubernetes API、Gateway HTTP API 和真实浏览器，不写产品数据库、不 seed state，也不把 password、cookie、CSRF、provider credential、Notification recipient 或 raw model/log output保存到 evidence。

## 前提

- 使用此前没有安装过该 candidate 的 clean non-production Cluster；`aiops-system`、`aiops-verification` 和同名 cluster RBAC 必须不存在。
- 当前 kube context 必须稳定指向该 Cluster；默认 StorageClass 能动态供给 `32Gi` RWO，NodePort `30088` 空闲，CNI 实施 NetworkPolicy。
- 本机已有 `kubectl`、Python 3、Node/npm、Console dependencies、Playwright Chromium 和 OpenSSH `ssh-keygen`。
- 准备 Platform Operator/Platform Administrator 的 OpenSSH 签名 key，以及一个由人员通过 Console 正常创建的 ordinary SRE User。创建 User 是公开产品操作，不由 Runner 写库或 seed。
- Model 与 Notification provider 必须是真实可达配置；真实 secret 只在隐藏式交互 prompt 中输入，不得放在命令行、环境变量或配置文件。

Runner 和 Gateway HTTP client 默认显式直连；浏览器使用 `--no-proxy-server`。不要预设代理。只有先确认某个外部下载目标直连不可达后，才可在该独立下载命令上临时使用用户授权的代理；不得把代理写入项目或 acceptance 配置。Cluster 内 provider delivery 使用 Pod 自身的正常网络路径，不继承本机代理。

## 连续成功路径

以下示例中的 `<run>` 是 `init` 输出的目录。`work` 位于 evidence bundle 外，只保存可删除的 release 解压工作副本。

```bash
python3 scripts/run_pilot_acceptance.py init \
  --archive dist/aiops-pilot-v0.1.0.tar.gz \
  --output acceptance \
  --access-profile http_nodeport

python3 scripts/run_pilot_acceptance.py package \
  --acceptance <run> \
  --archive dist/aiops-pilot-v0.1.0.tar.gz \
  --checksums dist/SHA256SUMS \
  --work-dir .scratch/acceptance-work
```

Platform Operator 核实 non-production、capacity 和 CNI 后签署 `P03`：

```bash
python3 scripts/run_pilot_acceptance.py attest \
  --acceptance <run> --gate P03 \
  --actor operator@example.com --key ~/.ssh/aiops-acceptance

python3 scripts/run_pilot_acceptance.py install \
  --acceptance <run> \
  --archive dist/aiops-pilot-v0.1.0.tar.gz \
  --checksums dist/SHA256SUMS \
  --work-dir .scratch/acceptance-work
```

安装后，人员按 release README 从 Kubernetes Secret 读取 bootstrap password，在真实浏览器完成首次登录，并通过 Console 创建 ordinary SRE User；随后签署 `I05`。签名只保存公钥、SHA256 fingerprint 和 detached signature，不保存私钥或私钥路径。

```bash
python3 scripts/run_pilot_acceptance.py attest \
  --acceptance <run> --gate I05 \
  --actor admin@example.com --key ~/.ssh/aiops-acceptance

python3 scripts/run_pilot_acceptance.py web \
  --acceptance <run> --base-url http://<NodeIP>:30088

python3 scripts/run_pilot_acceptance.py setup \
  --acceptance <run> --base-url http://<NodeIP>:30088 \
  --archive dist/aiops-pilot-v0.1.0.tar.gz \
  --checksums dist/SHA256SUMS \
  --work-dir .scratch/acceptance-work
```

`setup` 会依次执行真实 Platform Status/role guard、Model invalid→verified、Notification dead-letter→sent/selected、Connector enrollment/read verification 和 Prometheus/Loki/Alertmanager/MCP gate。Notification 收件和 Connector one-time credential 边界发生后，CLI 会暂停并要求人员签署 `S04`/`S05`，然后才记录 gate passed。

全部 A01 gate 连续成功后，验证所有 OpenSSH signature 并生成最终 checksum：

```bash
python3 scripts/run_pilot_acceptance.py finalize --acceptance <run>
```

`https_ingress` profile 仍必须先通过 HTTP NodePort；初始化时声明该 profile，并在 `web` 增加 `--https-base-url https://... --https-ingress <namespace>/<name>`。若该 Ingress profile 还声明 HTTP→HTTPS redirect，同时传 `--https-require-redirect --https-http-url http://...`。Runner 会保留 Ingress UID/generation/host、TLS certificate SHA256/subject/issuer/expiry 和 redirect 结果；未声明 HTTPS 时，只有 `I04` 记录 `not_applicable`。

`I03` 的 event-stream handshake 使用 authenticated `/api/v1/platform/status/stream`，事件内容来自真实 Platform Status owner snapshot；因此 clean install 不需要预造 Incident，也不会为了验收 seed 产品状态。

## 失败规则

任何失败 attempt 都永久保留，后续 retry 不会把它改写成 passed，且该 acceptance run 的 `promotion_eligible` 保持 false。可以继续采集诊断，但不得 patch 产品数据库、手工改 terminal state、保存 raw provider output 或删除失败 artifact。修复 candidate 后必须使用新 immutable artifact、clean Cluster 和新 `acceptance_id` 从 `P01` 开始；环境瞬时问题若要形成 promotion evidence，也应重新建立一条 clean、连续成功路径。
