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

Gate `failed` 与 Failure Attribution 分开。Generic failure 先记 `inconclusive`；source ledger 仍按 `evaluate -> no_promote -> seal` 永久终止。不得因已核对的 tool/environment input failure 自动删除 `aiops-system`。

只有 Product SHA、image/config、Cluster identity 未变，每个已发 mutation 都由 `request_id + object identity + revision/time fact` 唯一核对，且没有 Unknown Outcome、不可逆未知副作用或环境污染时，才可生成 `retain_existing` epoch。Reconciliation JSON 是 bounded public facts，不含 secret：

```bash
python3 scripts/run_pilot_acceptance.py diagnostic create \
  --acceptance <sealed-no-promote-run> \
  --output diagnostics --diagnostic-id <diagnostic-id>

# 将已脱敏的 JSON public facts 写入 bundle 后，追加一次不可覆盖的结论并绑定证据 hash
# 所有诊断 artifact 都必须通过 --evidence 引用；结论写入后不得再增删文件
python3 scripts/run_pilot_acceptance.py diagnostic conclude \
  --diagnostic diagnostics/<diagnostic-id> \
  --failure-attribution tool_failure \
  --note "已由诊断证据确认可安全保留部署" \
  --evidence diagnostics/<diagnostic-id>/<redacted-proof-file> \
  --operation-accounting-complete \
  --recovered-operation-id <request-id-missed-by-old-runner>

python3 scripts/run_pilot_acceptance.py continuation create \
  --source-acceptance <sealed-no-promote-run> \
  --diagnostic diagnostics/<diagnostic-id> \
  --freeze dist/<replacement-freeze> \
  --reconciliations /absolute/path/reconciliations.json \
  --gate-reuse-plan /absolute/path/gate-reuse-plan.json \
  --output continuations

python3 scripts/run_pilot_acceptance.py continuation inspect \
  --continuation continuations/<epoch-id>

python3 scripts/run_pilot_acceptance.py continuation attest \
  --continuation continuations/<epoch-id> \
  --actor operator@example.com \
  --note "已核对 unchanged deployment、全部 mutation facts 与 exact reusable gates" \
  --key ~/.ssh/aiops-acceptance
```

`--failure-attribution` 可为诊断确认后的 `tool_failure` 或
`environment_failure`。仅当旧工具漏记 effect identity 时才重复传入
`--recovered-operation-id`；source ledger 已绑定的 identity 不得重复声明。
只有确认所有历史 mutation 均已列举完毕时才能传
`--operation-accounting-complete`。Continuation 创建会从 replacement freeze 重验并
解包 exact Product archive，在 owner Module 内重新读取当前 Cluster identity、执行
`kubectl diff -k`、核对 source P01 manifest/image baseline，并读取 Deployment、
DaemonSet、PVC、ConfigMap、Bootstrap 与 NodePort 健康事实；这些事实不能由命令行 JSON
自报，任一漂移都会拒绝保留部署。
`kubectl diff -k` 会保留真实 exit code 与输出 SHA256；exit `0` 表示 exact no-diff，
或 exit `1` 只能包含 API Server dry-run 预测的 `metadata.generation: N -> N+1`。
任意真实 spec、image 或 annotation 差异仍拒绝 Continuation，不能伪装为 zero diff。
如果 source ledger 与 recovered inventory 都没有 mutation identity，则不能把空数组
解释为“零 mutation”，当前 contract 按状态不可证明拒绝复用部署。

新 ledger 使用 `--deployment-continuation` 代替 `--environment-qualification`。它仍从
P01 开始；可导入 gate 已在同一份 signed Continuation 中逐项冻结，未列出的 gate
继续正常执行。签名同时授权 `retain_existing` 与 exact reusable gates，不再创建第二份 Gate Reuse Epoch。

## Gate 级证据复用

Gate Reuse plan 是 JSON 数组。每项只能引用 canonical opt-in policy 中的 source gate，并
精确列出该 gate 自己绑定的 mutation operation ID；不能把其他 gate 的 reconciliation
借给它。当前只有 S01 `reconciled_effects` 允许非空 operation inventory，其余允许项必须
是无 mutation 的稳定事实：

```json
[
  {"gate_id":"P01","operation_ids":[]},
  {"gate_id":"I02","operation_ids":[]},
  {"gate_id":"I03","operation_ids":[]},
  {"gate_id":"I04","operation_ids":[]},
  {"gate_id":"S01","operation_ids":["acceptance-s01-notification-skip"]},
  {"gate_id":"S02","operation_ids":[]}
]
```

初始 allowlist 是 P01、I02、I03、I04、S01、S02。P02、I01、I05、S03-S06、V/R/C
默认重跑；测试账号和所有当前 run HITL 必须重新创建/签署。`continuation create`
会在任何新 ledger 或外部 effect 之前，同时重验 sealed source、Product/Tool/Cluster/access
identity、source gate terminal artifact 与完整 mutation inventory，并把结果写入待签的
Continuation record。空 JSON plan `[]` 表示只保留部署、不导入任何 gate。

签名不会 reopen source ledger。新 ledger 创建后，只有当前 frontier 正好在 signed plan 中
时才能执行一次 `apply`；命令复制并重验 source artifact、追加
`reuse-<source-acceptance-id>.json` provenance，然后形成新 ledger 自己的唯一 terminal
attempt，全程不调用 Product、Kubernetes 或 provider Adapter：

```bash
python3 scripts/run_pilot_acceptance.py gate-reuse apply \
  --deployment-continuation continuations/<epoch-id> \
  --source-acceptance <sealed-no-promote-run> \
  --acceptance <new-run>
```

若当前 frontier 未授权，就使用普通 `advance`。例如可以 reuse P01，execute P02/I01，
reuse I02-I04，execute I05 创建全新账号，再 reuse S01/S02，之后从 S03 正常执行。任何
source/target identity drift、artifact tamper、operation 遗漏或多列、坏签名、过期 Continuation
都会 fail closed；旧 eligibility、promotion decision、账号 secret 和 HITL 不继承。

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

需要人员检查的 gate 使用 `attest` 签署 exact bounded evidence。

S04 明确使用三步 HITL，不在发送后的同一进程读取终端输入：

```bash
python3 scripts/run_pilot_acceptance.py advance --acceptance <run> --config <config> --credential-store <store>
python3 scripts/run_pilot_acceptance.py attest --acceptance <run> --gate S04 --role platform_administrator --actor <actor> --key <key>
python3 scripts/run_pilot_acceptance.py resume --acceptance <run> --config <config> --credential-store <store>
```

第一步只发送一次真实 test Delivery、持久化 `receipt-review.json` 并保持 S04 open；第三步不得重发该 Delivery，只在 exact receipt attestation 验证通过后启用 Destination 和选择 Pilot Route。

C03 后依次执行：

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
- V/R/C failure：当前 ledger 立即 ineligible；新的 promotion evidence 必须使用全新 ledger，并从 P01 frontier 开始。只有 canonical policy 明确允许且 signed Continuation 逐项授权的旧 gate 可以导入，V/R/C 本身默认全部重跑。
- 仅 diagnosed `tool_failure` 或未污染且可归责的 `environment_failure`，并且 exact deployment/effects 可核对时，允许 replacement freeze + 一份 signed Deployment Continuation Epoch + new ledger adoption；Product Failure、identity drift、unprovable/Unknown Outcome、不可逆未知副作用或污染均要求 cleanup/redeploy。
- 任一 ledger 都不得补写、重试 gate、重放 mutation、patch 产品数据库或把 diagnostic evidence 合并为 promotion evidence。
