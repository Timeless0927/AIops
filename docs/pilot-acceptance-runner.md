# Pilot Clean Acceptance Runner

`scripts/run_pilot_acceptance.py` 先在 Clean Acceptance ledger 外取得 Environment Qualification 或 Deployment Continuation Epoch，再以 format v4 ledger 单 gate 推进 Deployment Qualification 与 Product Acceptance。Runner 不写产品数据库、不 seed state，也不把 password、cookie、provider credential 或 raw provider output 写入 evidence。

## 四个资格边界

1. Fxx Candidate Freeze 固定 Pilot Release Bundle、Acceptance Tool、contract 和 admission evidence。
2. Clean install 使用 Qxx Environment Qualification 检查 exact Cluster 的 clean allowlist、节点、NodePort、默认 StorageClass/32Gi PVC、NetworkPolicy probe 和每节点 exact image pull，并证明临时 namespace 已清理。仅 Acceptance Runner failure 且现有部署可完整核对时，改用 signed Deployment Continuation Epoch。
3. I01-I05/S01-S06 是 Deployment Qualification；I01 是第一个 live deployment gate，执行 `clean_install` 或 read-only `adopt_existing`。
4. V/R/C 是 Product Acceptance；最终只允许 `evaluate -> decide -> seal`。

Qxx 失败只生成 immutable `environment_not_ready` record，不创建 acceptance ledger。环境修复后使用新 qualification ID 重跑；product/tool/contract/artifact 未变化时不需要重新 freeze。

## Environment Qualification

以下示例假设 F50 输出目录是 `dist/f50-v0.1.0`：

```bash
python3 scripts/run_pilot_acceptance.py qualification create \
  --freeze dist/f50-v0.1.0 \
  --output qualifications \
  --access-profile http_nodeport

python3 scripts/run_pilot_acceptance.py qualification inspect \
  --qualification qualifications/<qualification-id>

python3 scripts/run_pilot_acceptance.py qualification attest \
  --qualification qualifications/<qualification-id> \
  --actor operator@example.com \
  --note "已核对 non-production、容量、CNI、节点和 exact image pulls" \
  --key ~/.ssh/aiops-acceptance
```

每次 qualification 使用独立 temporary namespace。若进程在 effect 后中断，只允许执行 cleanup reconciliation，不会重放 apply：

```bash
python3 scripts/run_pilot_acceptance.py qualification resume \
  --qualification qualifications/<qualification-id>
```

只有 `passed`、未过期、签名有效、checksum 完整、cleanup 已证明，且 freeze/product/tool/contract/Cluster/access identity 全部一致的 record 才能创建 ledger。

## Acceptance Runner failure 后保留部署

Gate `failed` 与 Failure Attribution 分开。Generic failure 先记 `inconclusive`；source ledger 仍按 `evaluate -> no_promote -> seal` 永久终止。不得因 tool failure 自动删除 `aiops-system`。

只有 Product SHA、image/config、Cluster identity 未变，每个已发 mutation 都由 `request_id + object identity + revision/time fact` 唯一核对，且没有 Unknown Outcome、不可逆未知副作用或环境污染时，才可生成 `retain_existing` epoch。Reconciliation JSON 是 bounded public facts，不含 secret：

```bash
python3 scripts/run_pilot_acceptance.py diagnostic create \
  --acceptance <sealed-no-promote-run> \
  --output diagnostics --diagnostic-id <diagnostic-id>

# 将已脱敏的诊断证据写入 bundle 后，追加一次不可覆盖的结论并绑定证据 hash
# 所有诊断 artifact 都必须通过 --evidence 引用；结论写入后不得再增删文件
python3 scripts/run_pilot_acceptance.py diagnostic conclude \
  --diagnostic diagnostics/<diagnostic-id> \
  --failure-attribution tool_failure \
  --note "已由诊断证据确认 Acceptance Runner 缺陷" \
  --evidence diagnostics/<diagnostic-id>/<redacted-proof-file>

python3 scripts/run_pilot_acceptance.py continuation create \
  --source-acceptance <sealed-no-promote-run> \
  --diagnostic diagnostics/<diagnostic-id> \
  --freeze dist/<replacement-freeze> \
  --reconciliations /absolute/path/reconciliations.json \
  --output continuations

python3 scripts/run_pilot_acceptance.py continuation inspect \
  --continuation continuations/<epoch-id>

python3 scripts/run_pilot_acceptance.py continuation attest \
  --continuation continuations/<epoch-id> \
  --actor operator@example.com \
  --note "已核对 unchanged deployment 与全部公开 mutation facts" \
  --key ~/.ssh/aiops-acceptance
```

新 ledger 使用 `--deployment-continuation` 代替 `--environment-qualification`。它仍从 P01/P02 开始；I01 只运行 `kubectl diff -k` 与健康/身份读取，证明 zero apply；I02 和后续所有 gate 均重新执行，测试账号必须全新。

## 创建并推进 Clean Acceptance

```bash
python3 scripts/run_pilot_acceptance.py init \
  --archive dist/f50-v0.1.0/product/aiops-pilot-v0.1.0.tar.gz \
  --acceptance-tool dist/f50-v0.1.0/acceptance-tool-v1.tar.gz \
  --environment-qualification qualifications/<qualification-id> \
  --output acceptance \
  --access-profile http_nodeport

python3 scripts/run_pilot_acceptance.py status --acceptance <run>

python3 scripts/run_pilot_acceptance.py advance \
  --acceptance <run> --config /absolute/path/acceptance-config.json \
  --credential-store /dev/shm/<run-credentials>
```

采用现有部署时，`init` 的唯一区别是：

```bash
python3 scripts/run_pilot_acceptance.py init \
  --archive dist/<replacement-freeze>/product/aiops-pilot-v0.1.0.tar.gz \
  --acceptance-tool dist/<replacement-freeze>/acceptance-tool-v1.tar.gz \
  --deployment-continuation continuations/<epoch-id> \
  --output acceptance --access-profile http_nodeport
```

每次 `advance` 最多推进当前唯一 frontier。`resume` 只 reconcile 已存在的 open gate，不开始下一 gate。P01/P02 只验证本地 artifact/admission identity；通过后 frontier 直接进入 I01。

需要人员检查的 gate 使用 `attest` 签署 exact bounded evidence。C03 后依次执行：

```bash
python3 scripts/run_pilot_acceptance.py evaluate --acceptance <run>
python3 scripts/run_pilot_acceptance.py decide \
  --acceptance <run> --decision promote --actor release-owner@example.com \
  --note "完整证据已复核" --key ~/.ssh/aiops-acceptance
python3 scripts/run_pilot_acceptance.py seal --acceptance <run>
```

## 失败规则

- Qxx failure：保留 `environment_not_ready` record，修环境后以新 qualification ID 重跑；不产生 Axx/no-promote ledger。
- I/S failure：当前 Clean Acceptance ledger 立即 ineligible，并记录独立 Failure Attribution；`failed` 本身不触发产品 cleanup。
- V/R/C failure：当前 ledger 立即 ineligible；新的 promotion evidence 必须从全新 ledger 的 P01 开始，不能复用旧 ledger 的 passed gate。
- 仅 diagnosed `tool_failure` 且 exact deployment/effects 可核对时允许 replacement freeze + signed Deployment Continuation Epoch + new ledger adoption；Product Failure、identity drift、unprovable/Unknown Outcome、不可逆未知副作用或污染均要求 cleanup/redeploy。
- 任一 ledger 都不得补写、重试 gate、重放 mutation、patch 产品数据库或把 diagnostic evidence 合并为 promotion evidence。
