Type: design
Status: resolved

# 可信 Acceptance Contract 设计

本文件记录 `remediation-tickets.md` 进入 spec 前已确认的代码设计决策。本阶段只设计，不实现、不部署、不执行 live run。

## 已确认决策

### D01 Clean Acceptance Run 的中断边界

Clean Acceptance Run 以证据链连续且外部 effect 未重放为准，不要求 Acceptance Runner 进程连续存活。Acceptance Runner/PTY 中断后，仅当操作 identity 已在 effect 前 durable 记录，且公开产品事实能证明同一 gate 的 exactly-once terminal outcome 时，才可在同一 `acceptance_id` 恢复并补完原 gate；不得重放 mutation 或新增 attempt。任一条件不满足时，该 run 为 `no_promote`。

### D02 唯一 gate DAG owner

Acceptance Evidence Module 单独拥有完整 gate DAG，并公开 frontier、gate start 和 terminal result recording。各 Gate Module 只拥有本 gate 的动作与证据判断；Conductor 只读取 frontier 并分发；artifact phase mapping 只决定目录，不参与依赖判断。Product Module 不感知 acceptance gate，Gate Module、Conductor 和 CLI 均不得复制 predecessor 顺序。

### D03 Gate failure 与 diagnostic evidence

任一 mandatory gate 记录 `failed` 后默认立即 terminal 为 `no_promote`；同一 ledger 不增加 retry attempt。失败同时记录独立 Failure Attribution：`product_failure|tool_failure|environment_failure|inconclusive`。唯一例外是 D27 的纯 Evaluator Correction：原 failed fact 保留，但 correction 可在 eligibility evaluation/seal 前恢复 effective frontier。执行、取证、外部 effect、DAG/schema 或 Product 变化不属于该例外，继续排障时仍创建独立 diagnostic bundle，且修复后使用新 run。

### D04 只保留最终 finalization

删除 A01 等中途 finalize/checkpoint seal。每个 gate 的原子 ledger write 已提供恢复基础；中途只提供 read-only status/verify，用于重验已有 artifact hash 并显示当前 frontier。唯一 final stage 只能在 C03 后执行 D17 的 `evaluate -> decide -> seal`；P/I/S 完成仅表示 frontier 到达 V01，不使用 `accepted`、`finalized` 或同义措辞。

### D05 Gate durable execution intent

Acceptance Evidence Module 在 gate 开始时原子创建唯一 open gate 和 gate execution ID。任何可能产生外部 effect 的请求都必须在发送前绑定稳定 request/operation identity；中断恢复只按这些 identity 查询公开 terminal facts，不重发 effect。可证明 terminal outcome 时完成原 gate，无法证明时记录 `failed`。同一时刻只允许一个 open gate，且不得创建第二 attempt。该 execution journal 仅属于 Acceptance Runner，不进入产品数据库，也不成为第二套产品状态机。

### D06 Product projection，不建 acceptance endpoint

不新增聚合 `/acceptance-evidence` endpoint、acceptance-only 产品表或产品状态字段。各 Product Module 只通过现有 actor-scoped projection 公开自己拥有的稳定 ID、时间、revision、typed outcome 与关联 identity；Acceptance Runner 在本地组合跨 Module 证据。若某项必需事实只能通过私有 SQL 取得，应修正拥有该事实的 Module Interface，不为 Acceptance Runner 建私有后门。

### D07 Acceptance Runner Module seam

保留现有 P/I/S Gate Module，不做无关重构。新增 First Run Module（`V01-V07`，含位于 V04/V05 之间的 R05）、Recovery Module（`R01-R04/R06`）和 Rerun and Cleanup Module（`V08/C01-C03`）。Acceptance Evidence Module 独立拥有 DAG、journal、artifact、verification 与 finalization；Conductor 只装配并分发当前 frontier。不建立通用 `GateRunner` interface、factory 或插件层。各 Module 通过自己的公开 Interface 测试，远程产品/Kubernetes/浏览器/provider seam 复用已有 Adapter 与测试替身。

### D08 Conductor 单 gate 推进

Conductor 每次调用最多执行一个 gate。`status` 只读验证 ledger 并显示 frontier/terminal 状态；`advance` 只执行唯一 current frontier；`resume` 只 reconciliation 已存在的 open gate，不开始下一 gate；`evaluate/decide/seal` 仅在 C03 passed 后按 D17 顺序可用。不提供长时间 `run-all`。Clean Acceptance Run 由同一 acceptance identity 和连续 ledger 定义，不要求单进程或单命令。

### D09 Acceptance identity 冻结 Acceptance Runner

F10 同时生成 checksummed Pilot Release Bundle 与 checksummed acceptance-tool artifact。Clean Acceptance Run 初始化时冻结 Product、effect/collection tool、gate DAG/contract revision、Cluster identity 和 access profile。Product、effect/collection code、DAG 或 evidence schema 变化使原 run `no_promote`。只改变无 I/O evaluator/assertion 的修正可按 D27 追加 old/new evaluator identity 后继续；它不改变 Product、既有 artifact 或 external operation identity。

### D10 Acceptance Runner 可使用 integration plaintext

Acceptance Runner 可以通过 Credential Source Interface 在内存中使用 Model/Notification secret，并以无头浏览器填写真实 Console 表单；不得绕过 UI 直调配置 mutation。A10 前由 Operator 将 secret 一次性放入 evidence/workspace 外的 mode `0600` 输入源，`init` 载入 run-scoped tmpfs secret store 后由 Operator移走或删除原文件。Secret 不得进入命令参数、PTY、环境变量、普通配置、日志、截图、trace、audit 或 evidence。Invalid credential 在内存中从真实值派生且不保存。Secret scan 验证结构化 non-disclosure，不读取 secret 本身做字符串搜索。

### D11 新 evidence format 不兼容旧 ledger

现有 format v1 与 Contract v3/format v3 evidence bundle 保持原样，作为 immutable diagnostic/no-promote 历史。当前 ledger 继续使用 format v4 与 `pilot-clean-acceptance-v4`，因 canonical DAG 与 ledger schema 未变；合并授权使用 Deployment Continuation format v2。旧 Continuation/Gate Reuse bundle fail closed，不补字段、不转换、不双读。

### D12 时间所有权

Acceptance Runner 的 monotonic clock 负责验收等待 deadline；Product owner 持久化的 UTC timestamp 负责证明领域事件顺序，不跨进程 wall clock 计算验收耗时。有 deadline 的 open gate 若在进程中断后无法证明于原期限内完成，则该 gate `failed`，不得重新开始计时。P03 在 A10 开始前检查 node/control-plane clock skew，超出允许范围时不开始 run。

### D13 Eligibility 与 Promotion Authority 分离

Finalizer 只输出确定性的 `eligible|ineligible` 及 bounded reasons。`eligible` 要求完整 DAG、artifact hash、attestation 与 acceptance identity 全部通过；发布负责人随后签署独立 `promote|no_promote` decision。`ineligible` 只允许 `no_promote`，`eligible` 也不触发自动发布、部署或外部 promotion。最终 checksum 覆盖 eligibility result 和签名后的 promotion decision。

### D14 Console 拥有 User/HITL mutation

产品 User mutation 必须经过真实 Console UI，不允许 Acceptance Runner 绕过 UI 直调业务 mutation。Acceptance Runner 可在独立 role browser context 中自动导航、填写和普通提交；S04 receipt、S05 one-time credential handling、V04 exact diff、V05 Approval/execution、V07/V08 Report publication、C03 manifest review 与最终 Promotion Decision 必须暂停，输出 bounded/no-secret screenshot 与结构化摘要，并取得 User 确认/签署后才能继续最终动作。浏览器自动化不能生成 HITL attestation或自行批准。

S04 的 Provider test effect 与 receipt HITL 必须跨命令分离：`advance` 在 effect 前绑定 operation identity，持久化 terminal Delivery、attempt/provider identity 与 `receipt-review.json` 后保持同一 gate open；独立 `attest` 绑定 exact receipt SHA；`resume` 重验该 Delivery/revision/签名后才启用 Destination 和选择 Route。等待签名不是 gate failure，不持久化新的 paused 状态；缺签名时不得 mutation，进程恢复不得重发 test Delivery。

### D15 Console action 的唯一公开 correlation

Console gate 的 `advance` 记录 expected actor、target、最早时间和预期领域 outcome。Browser Adapter 优先捕获 Console request/response identity并立即绑定 request/object/revision；进程中断时，`resume` 才通过公开 audit/projection 查找时间窗内唯一匹配的 product operation。零个或多个匹配都使 gate `failed`。不允许 User 手工挑选记录，也不向产品字段注入 acceptance token或增加 acceptance-only correlation endpoint。

### D16 Operator recovery 只允许冻结的结构化动作

Recovery Gate Module 只提供 acceptance matrix 已冻结的 exact Kubernetes operation：删除当前 Pod、对 candidate workload 做固定 annotation rollout、将 exact Connector/Loki scale-to-zero 后 reapply 同一 candidate，以及 R06 对 exact verification object 制造固定 metadata drift。每次先展示 exact target/effect并取得 Platform Operator 确认，再写 durable intent、执行一次和记录 Kubernetes identity/result。不接受自由 shell/kubectl 参数；额外命令只可进入独立 diagnostic bundle。

### D17 最终 evaluate、decide、seal

C03 后先由 `evaluate` 重验完整 DAG、acceptance identity、artifact 与 attestation，并写入 `eligible|ineligible`；再由发布负责人签署 `promote|no_promote`，其中 `ineligible` 不接受 `promote`；最后 `seal` 验证签名和一致性并生成最终 `SHA256SUMS`。Checksum 不包含自身，但覆盖 manifest、eligibility result、human attestations 和 promotion decision。Seal 后 ledger 永久只读，拒绝补证据、重签或其他写入。

### D18 F10 完整 DAG contract simulation

F10 candidate freeze 前必须使用现有测试替身/in-memory Adapter 驱动 P01-C03 完整 frontier，覆盖正常路径、每个 gate failure、open gate 中断恢复、duplicate effect 拒绝、artifact tamper、错误签名和旧 format 拒绝。Simulation 不访问真实 Cluster、Model Provider 或 Notification Destination，结果只进入测试/review，不进入 A10 evidence，也不替代 live gate。

### D19 Recovery gate 固定线性 frontier

Recovery frontier 固定为 `V07 -> R01 -> R02 -> R03 -> R04 -> R06 -> V08`，每次只引入一个 Cluster 故障，并要求前一 owner 完全恢复后才开始下一 gate，以保证 state/availability/audit 变化可唯一归因。R05 仍独立位于 `V04 -> R05 -> V05`。不支持 recovery gate 并行，也不增加并发锁或协调状态。

### D20 Acceptance Runner 可使用 Console credential

Acceptance Runner 可以通过窄 Credential Source Interface 取得动态 Console credential，并在内存中驱动无头浏览器登录；User 不需要在每个 gate 重复输入密码。Password、session 和 cookie 不得进入 CLI 参数、PTY、日志、evidence、product audit 或普通配置文件。普通 User、Platform Administrator 与无 Approval Authority User 继续使用独立 browser context。Credential 按 D24 跨 gate 保存。

### D21 A10 前不运行 automated Cluster rehearsal

F10 保持离线且 cluster-agnostic。Platform Operator 可在 A10 前完成普通基础设施准备和旧资源清理，但不得运行 acceptance tooling；A10/P03 是对 exact Cluster 的唯一自动 baseline check。P03 不满足时 run 立即 `ineligible/no_promote`，即使属于环境问题也不在当前 remediation graph 中换 Cluster 或执行第二次 Clean Acceptance Run。

### D22 P02 验证冻结的 F10 admission evidence

F10 执行全部受影响 Module、直接 consumer、静态检查、完整 DAG simulation 和 fixed-point code review，并把测试清单、版本、结果、review fixed point 与 hash 写入 acceptance-tool artifact。A10/P02 只验证该冻结报告与 acceptance-tool SHA、release contract revision 完全匹配，并运行最小 tool self-check；不在 live window 重跑仓库测试。P02 继续明确 fake-backed tests 只是 candidate admission，不是 live evidence。

### D23 Run status 完全派生

不持久化独立 `run_status` 状态机。存在 open gate 时派生为 `active/open`；无 open gate 且仍有 frontier 时为 `active/ready`；任一 mandatory gate failed 后为 `ineligible`；C03 后 evaluate 成功为 `eligible`；promotion decision 与最终 checksum 存在后为 `sealed`。只持久化 gate intent、terminal result、eligibility result、promotion decision 和 seal。Operator 主动停止时明确把当前 gate记为 failed，不增加 paused/resuming/aborted 状态。

### D24 Console credential 的跨 gate persistence

Bootstrap admin password 在需要时从 exact Kubernetes bootstrap Secret 重新读取，不复制保存。Acceptance Runner 创建的普通 User、SRE 和无 Approval Authority User password 由 CSPRNG 生成一次，只保存在工作机上 evidence/workspace 之外的 run-scoped tmpfs secret store；目录 mode `0700`、文件 mode `0600`。每个 gate 使用新 browser context 重新登录，不持久化 cookie/profile。Run failed 或 seal 后删除 secret store；tmpfs 丢失且 credential 不可重新取得时 gate/run failed，不通过 reset password 继续。

### D25 Failure Attribution 与 Deployment Disposition 分离

Gate result 继续只表示 `passed|failed|not_applicable`，其中 mandatory `failed` 永远使当前 ledger ineligible。Failure Attribution 单独表示 `product_failure|tool_failure|environment_failure|inconclusive`；无法在 terminal 时可靠判断就记录 `inconclusive`，不得猜测为产品失败。Deployment Disposition 只在 diagnostic 后派生：仅当 Product artifact、image、deployment configuration 和 Cluster identity 未变，diagnostic 将失败可靠归因为 `tool_failure|environment_failure`，所有已发 mutation 均由 durable operation identity 与唯一公开对象/revision 事实核对，且不存在 Unknown Outcome、不可逆副作用或环境污染时，才允许 `retain_existing`。若旧 Acceptance Runner 的缺陷使 operation identity 未进入 source ledger，Diagnostic Evidence Bundle 必须显式列出全部 recovered operation identity，并由公开 audit/projection 与签名后的 continuation reconciliation 证明完整性；不得把漏记当作“零 mutation”。`product_failure`、仍为 `inconclusive`、身份漂移、事实不可核对或环境污染均为 `rebuild_required`。Acceptance Runner 不提供“失败即 uninstall”的自动 cleanup。

### D26 Deployment Continuation Epoch 与 I01 adoption

允许保留部署时，修复后的 Acceptance Runner 必须先重新 freeze，并创建由 Platform Operator 签署的 checksummed Deployment Continuation Epoch。Epoch 以一次签名绑定 source sealed no-promote ledger、failed gate、Diagnostic Evidence Bundle、旧/新 tool SHA、未变 Product SHA、Cluster/access identity、全部 mutation reconciliation、derived Deployment Disposition 与 exact reusable-gate plan；不再创建独立 Gate Reuse attestation。新 ledger 以该 epoch 代替 clean-install Environment Qualification，仍从 P01/P02 开始；复用只在 signed plan 命中 current frontier 时创建新 ledger 的唯一 terminal attempt，不重放 effect。I01 adoption、fresh account、当前 HITL 和 Promotion Decision 必须实时重做。

### D27 纯 Evaluator Correction

纯 evaluator/assertion 修正不重新部署 Product，也不重放、重取证或补造 live fact。Runner 必须在执行 assertion 前把 normalized source artifact durable 写入 ledger；correction 只能读取这些已索引且 hash 验证通过的 artifact，禁止网络、浏览器、Kubernetes、provider、时钟等待或 mutation。Correction append-only 记录 gate、原 failed execution、source artifact hashes、旧/新 evaluator SHA、原因、离线回归结果和 corrected_at；原 failed attempt 不删除、不改写，最终报告同时列出两版 evaluator identity。

只有 eligibility 尚未 evaluate、ledger 未 seal、failed gate 没有 Unknown Outcome，且 diagnostic 明确证明缺陷只在 evaluator 时，correction 才可恢复 effective frontier。缺 source artifact、需要新事实、执行/取证代码变化、external effect 不可核对、DAG/schema 变化或 Product 变化全部 fail closed，使用 D25/D26 的新 run。

若旧 run 已 seal，永久只读边界不变。工具可创建 checksummed evaluator-correction successor，自动绑定 source seal、Diagnostic Evidence Bundle、未变 Product/Cluster/access identity和 exact passed predecessor artifacts；这是审计容器迁移，不是 Deployment Disposition 或授权，因此不要求 Continuation/Gate Reuse 签名。Successor 只 fresh 执行无法从 source artifact 纯复算的 gate；已证明的 Product effect 不重放。Credential 已按旧 run 终态策略删除时，后续角色登录必须使用新的 scoped test account 或显式 credential recovery，不能伪造或恢复 plaintext。

### D28 Model Provider 输入只导入 scalar key

`credential-store import --name model-api-key` 接受 evidence/workspace 外的 mode `0600`
非 JSON-object-shaped raw key，
也接受 exact `api_key/endpoint/endpoint_scope/model/timeout_seconds` Model Provider JSON object。
后一种输入只把非空 scalar `api_key` 写入 run-scoped tmpfs store；完整 JSON、公开配置字段和
secret plaintext 都不得作为 API key 发送。Malformed/object-field mismatch 在写入前 fail closed，
Notification config 与其他 raw secret 的既有导入语义不变。该解析属于 Acceptance Tool 的输入
边界；它不改变 Product contract，也不允许失败 gate 在同一 ledger 重试。

## 已有 contract 继续生效

- 只有 `I04` 可在 `http_nodeport` profile 下为 `not_applicable`；其他 mandatory gate 必须 passed。
- Evidence 只保存 bounded、normalized、redacted facts 与 hash，不保存 secret、cookie、Authorization header、raw model response、Notification recipient、unredacted object 或 raw reasoning。
- External provider 的 bounded retry 由 Product Module 在单次 operation deadline 内拥有，不产生新的 gate attempt；超出 deadline 即 gate failed。
- 同一自然人可承担多个 Pilot 角色，但必须按其实际动作分别以 Platform Operator、Platform Administrator、SRE 或 release owner role 签署。
- 现有工作树修改全部保留；后续实现先按新 spec 审计复用，不 reset、不把当前 WIP 自动视为完成。

## 已观察到的实现约束

- 当前 gate 顺序分散在 A01 sequence、V01-V07 sequence 与脚本调用中，需要由 D02 的单一 DAG 替换。
- 当前 A01 finalize 可在缺少 R/V/C 时完成，需要按 D04/D17 删除中途 finalization 语义。
- 当前全局 admin audit 是管理员可见的最近 100 条记录，不能单独承担所有 HITL correlation；各 owning projection 必须公开稳定 operation identity，browser Adapter 可捕获 Console response identity，audit 用于恢复核验。
- 当前 R01-R06、V08、C01-C03 缺少完整 Acceptance Runner Module，因此 F10 的全 DAG simulation 是 A10 前硬门禁。
