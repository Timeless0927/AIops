# Pilot Clean Acceptance Runner

`scripts/run_pilot_acceptance.py` 先在 Clean Acceptance ledger 外执行可重复的 Environment Qualification，再以 format v3 ledger 单 gate 推进 Deployment Qualification 与 Product Acceptance。Runner 不写产品数据库、不 seed state，也不把 password、cookie、provider credential 或 raw provider output 写入 evidence。

## 四个资格边界

1. Fxx Candidate Freeze 固定 Pilot Release Bundle、Acceptance Tool、contract 和 admission evidence。
2. Qxx Environment Qualification 检查 exact Cluster 的 clean allowlist、节点、NodePort、默认 StorageClass/32Gi PVC、NetworkPolicy probe 和每节点 exact image pull，并证明临时 namespace 已清理。
3. I01-I05/S01-S06 是 Deployment Qualification；I01 是第一个 live deployment gate。
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
- I/S failure：当前 Clean Acceptance ledger 立即 ineligible；若 immutable inputs 未变，可复用同一 freeze，但必须使用 fresh qualification 和全新 ledger。
- V/R/C failure：当前 ledger 立即 ineligible；新的 promotion evidence 必须从全新 ledger 的 P01 开始，不能复用旧 ledger 的 passed gate。
- 任一 ledger 都不得补写、重试 gate、重放 mutation、patch 产品数据库或把 diagnostic evidence 合并为 promotion evidence。
