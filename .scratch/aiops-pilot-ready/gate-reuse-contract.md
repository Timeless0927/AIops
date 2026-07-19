Type: design
Status: accepted

# Contract v4 Gate 级证据复用

## 目标

Acceptance Runner 工具缺陷不再自动导致所有已通过 gate 重跑。旧 ledger 继续永久
`sealed/no_promote`；新 ledger 只复用能够逐项证明身份、实现、effect 与 artifact 均未受
影响的 terminal gate，其余 gate 按 canonical frontier 正常执行。

## 三类 contract

### Product contract

- Product artifact、image、configuration 与公开 projection 不感知 gate reuse。
- 不新增 acceptance endpoint、数据库字段或私有状态读取。
- 历史 mutation 只能通过 durable operation identity 与公开对象/revision 事实核对。

### Acceptance contract

- 不 reopen、修改或迁移旧 ledger；复用发生在全新 ledger 的全新 gate attempt。
- 每次 `reuse` 只推进当前唯一 frontier，不产生产品、Kubernetes 或 provider effect。
- 新 attempt 复制并重验原 artifact bytes/hash，并追加 `reuse-<source-id>.json` provenance。
- provenance 绑定 source acceptance/seal、source gate execution/status/artifacts、replacement
  Product/Tool/Cluster/access identity、Continuation record 与全部相关 reconciliation。
- Gate 未显式列入 canonical reuse policy 时默认禁止复用。
- mutation 未核对、Unknown Outcome、不可逆副作用、环境污染、identity drift、artifact
  tamper、lost credential、freshness/HITL 要求或 source gate 非 terminal success 均 fail closed。

### Promotion contract

- Imported gate 与 live-executed gate 都必须在新 ledger 中形成唯一 terminal attempt。
- Eligibility 仍要求完整 canonical DAG、当前 ledger identities、artifact integrity、所需
  HITL attestations 以及最终 `evaluate -> decide -> seal`。
- 旧 ledger 的 eligibility/decision/attestation 不继承；需要当前 run 人工动作的 gate 不可
  复用。

## Canonical gate graph 与 reuse policy

Gate 顺序继续唯一由 `GATE_SEQUENCE` 拥有。复用不建立第二张 DAG，也不跳过 frontier。

初始 opt-in policy：

| Gate | Policy | 必须证明 |
| --- | --- | --- |
| P01 | immutable_product | Product SHA 与 package artifacts 不变 |
| I02 | deployment_identity | Product/manifest/image/config/Cluster identity 未变且 Continuation healthy |
| I03 | stable_access | Product、Console image、Cluster 与 access profile 未变 |
| I04 | stable_access | 同一 access profile；原 not_applicable 只可在 HTTP profile 复用 |
| S01 | reconciled_effects | Notification setup decision 的全部成功 mutation 已唯一核对 |
| S02 | stable_security_contract | Product/Console/Gateway identity 未变；原 gate 无成功 domain mutation |

P02、I01、I05、S03-S06、V/R/C 默认不可复用：新 Tool 自检、当前 deployment observation、
credential/account、provider/HITL/freshness、真实变更、恢复、rerun 与 cleanup 必须在新
ledger 中重新执行。后续 gate 只有在自身 contract 明确稳定输入、effect 与退出条件并有
定向测试后才可加入 opt-in policy。

## Current A100 replacement

- F102 与 unsigned `continuation-F102` 保留为 superseded diagnostic artifacts，不签名、不用于 init。
- 新 Diagnostic bundle 必须补全所有拟复用 effect gate 的 operation inventory；至少 S01
  `acceptance-s01-notification-skip` 必须通过公开 audit/revision 唯一核对。
- 新 replacement freeze 与 signed Continuation 冻结 exact reusable gate plan。
- 新 ledger 按 frontier 执行：reuse P01；execute P02/I01；reuse 合法 I02/I03/I04；execute
  I05 创建全新账号；reuse 合法 S01/S02；从 S03 开始执行剩余 gates。

## 失败边界

- 复用计划只能缩小重跑范围，不能令 failed source ledger eligible。
- Gate policy、evidence schema、canonical DAG、共享 ledger/promotion validator 变化时，未被
  新 freeze 明确证明兼容的 gate 默认重跑。
- 不允许按“曾经 passed”复用，不允许自动猜测 mutation 为零，不允许复用旧 HITL 签名。
