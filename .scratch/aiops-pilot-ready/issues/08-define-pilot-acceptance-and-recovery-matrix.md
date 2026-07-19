Type: grilling
Status: resolved
Blocked by: 02 (Define Kustomize Release Installation Contract), 06 (Define Controlled Verification Scenario), 07 (Prototype Setup And Platform Status)

# Define Pilot Acceptance And Recovery Matrix

## Question

What finite release-gate matrix proves the Pilot-ready Release can be installed and used end to end by a new operator, including clean install, optional HTTP or HTTPS ingress, first login, setup and skip behavior, the controlled verification scenario, real provider paths, authorization, restart and rolling-update recovery, failure diagnostics, cleanup, and a second rerun?

The matrix must name the exact automated or HITL check for each promise, the artifact retained as evidence, and which failures block Pilot promotion. It must not substitute fake-backed unit tests for real deployed acceptance.

## Answer

Pilot promotion 使用一套有限的 release gate：先通过仓库 contract/static checks，再由一名此前没有部署过该版本的 Platform Operator，在一个清洁的真实非生产 Cluster 上从发布 tarball 连续完成安装、Web setup、故障恢复、两轮 controlled scenario 和 cleanup。fake-backed test 只能阻止坏 candidate 进入 live run，不能替代任何 live gate。

### Run contract

每次验收生成一个本地 `acceptance_id=<release>-<UTC timestamp>`，仅用于组织证据文件，不写产品数据库，也不建立第二套 acceptance state machine。所有命令记录 exit code、开始/结束时间、release digest、kube context 和脱敏输出。任何 secret、cookie、Authorization header、model response、Notification recipient、unredacted object 或 raw reasoning 都不得进入证据包。

Candidate 可以在失败后继续采集诊断，但不得覆盖失败 attempt。Pilot promotion 需要同一 immutable candidate 完成一条连续成功路径；局部 retry 只证明恢复，不把早先失败改写为成功。修复产品或 release artifact 后必须 cleanup/redeploy，并使用新 candidate identity 重新从 gate `P01` 开始。仅修复 Acceptance Runner 时，若 Deployment Continuation Epoch 证明 exact Product/Cluster identity 未变、全部 effect 唯一核对且环境未污染，可由新 ledger 从 `P01` 开始并在 `I01` adopt existing deployment；旧 gate 结果不继承。人为看到消息、浏览器 first login、exact diff 审批和 Report 内容必须由 HITL attestation 证明，其余尽量自动化。

`S04` 的真实回执 attestation 同时验证本次 acceptance 使用的 exact Destination revision。`V07`/`V08` 继续使用该 revision 时，以可关联的 terminal `sent` Delivery 作为送达证据，不重复要求人员确认；Destination revision 变化后必须先取得新的真实回执 attestation。

证据包固定结构：

```text
acceptance/<acceptance_id>/
  manifest.json                 # release/cluster/access profile、gate result 与 artifact index
  00-package/
  01-install/
  02-setup/
  03-recovery/
  04-run-1/
  05-run-2/
  06-cleanup/
  human-attestation.yaml        # actor、time、gate ID、结论，不含 secret
  SHA256SUMS                    # 对全部 evidence artifact 校验
```

`manifest.json` 对每个 gate 只记录 `passed|failed|not_applicable`、attempt、时间和 artifact path。只有显式标为 conditional 的 gate 可为 `not_applicable`。

### Package and installation gates

| ID | Check | Retained evidence | Promotion effect |
| --- | --- | --- | --- |
| `P01` | 自动校验发布的 `SHA256SUMS`，从 tarball 解压；扫描 top-level Kustomize overlay 无 remote base，所有 Pod image 均为完整 `@sha256:`，没有全零 digest、mutable tag、Helm/Operator CRD 或 synthetic observability backend。 | checksum output、artifact inventory、rendered manifest、image list | 任一失败阻塞。 |
| `P02` | 自动运行仓库定向 contract/static gates：Kubernetes packaging、OpenAPI producer/consumer、Gateway/Diagnosis/Connector/Notification shared contract、Console tests/build。输出必须同时标记 fake-backed tests 不是 live evidence。 | test selectors、版本、JUnit/exit output、build sizes | 任一失败阻塞；全量测试留给 CI。 |
| `P03` | 自动记录 clean baseline：无 `aiops-system`/`aiops-verification` namespace、无同名 cluster RBAC，默认 StorageClass 可动态供给 RWO、至少 `32Gi` 可用、NodePort `30088` 空闲、CNI 实施 NetworkPolicy、node 可拉取全部 digest。 | context/cluster identity、redacted preflight snapshot、image pull result、operator attestation for CNI/capacity | 任一前提不满足时不开始安装，也不归类为产品失败；在声明合格的 acceptance Cluster 上仍不满足则阻塞。 |
| `I01` | `clean_install` 时 Operator 只执行一次 `kubectl apply -k ./aiops-pilot-vX.Y.Z`；`adopt_existing` 时不执行 apply，只读核对 exact Deployment Continuation Epoch、Product/Cluster identity、manifest/config/image、NodePort owner、bootstrap/PVC/workload health。两种模式都产生本 ledger 的新 I01 evidence。 | mode、epoch hash（若采用）、apply 或 zero-mutation proof、object UID/image digest、Job/PVC/Deployment conditions、events | 任一失败阻塞；adoption 不得继承旧 gate，也不得把 drifted/unknown deployment 修成通过。 |
| `I02` | 自动核对四个 bootstrap Secret 只含必需 key，completion marker immutable；记录 hash/UID 而非值。再次 apply 同一 bundle 后 UID 与 value hash 不变，workload 收敛。 | redacted key inventory、UID/hash before/after、second apply diff | secret 被轮换、丢失或 reapply 不收敛均阻塞。 |
| `I03` | 自动读取 Console `/healthz`，浏览器通过 `http://<NodeIP>:30088` 加载同源静态资源、`/auth`、`/api/v1` 和 event stream；浏览器不得直连内部 Service。 | curl transcript、browser network summary、same-origin assertion | canonical HTTP NodePort 失败阻塞。 |
| `I04` | 条件 gate：当本次声明 `access_profile=https_ingress` 时，由 Operator 提供 Ingress/TLS，浏览器验证有效证书、HTTP->HTTPS policy（若声明）、同源 routes 和 event stream。 | Ingress/TLS identity、browser network summary | 仅在选择/发布该 HTTPS profile 时阻塞；第三方 Ingress/TLS 未参与时记 `not_applicable`，不降低 mandatory HTTP gate。 |
| `I05` | HITL 使用 bootstrap password 首次登录；自动确认匿名、错误密码和无 CSRF mutation 被拒绝，普通 User 与 Platform Administrator 权限不同。Bootstrap password 的读取与后续管理服从 02，不在本票增加强制改密 contract。 | redacted auth audit、HTTP status matrix、HITL login attestation | 登录、session/CSRF 或 role boundary 失败阻塞。 |

### Setup and dependency gates

| ID | Check | Retained evidence | Promotion effect |
| --- | --- | --- | --- |
| `S01` | 首次登录后 Platform Status 显示四项独立 capability；缺配置为 `not_ready`，不存在 `all_ready/setup_complete`。管理员跳过 Notification 后必须显示 `skipped` 且不计 ready，Incident workspace 仍可进入；退出再登录、从常规导航返回后状态一致。 | Platform Status JSON、desktop/390px screenshots、Gateway setup audit | false ready、强制 redirect、状态不可恢复或窄屏溢出均阻塞。 |
| `S02` | 普通 User 读取安全 capability summary，但不能读取 endpoint/recipient/secret metadata，也看不到或不能调用 configure/test/skip。Platform Administrator 的所有 mutation/test 要求 CSRF、reason、5m fresh auth、`expected_revision` 和 `request_id`。 | role/API negative matrix、redacted response fixtures、audit correlation | 任一越权或 secret exposure 阻塞。 |
| `S03` | 管理员先保存一版确定会被拒绝的 Model credential，真实 test 必须以 bounded `authentication_failed` 失败且无 keyword fallback；再保存真实配置并在 live scenario 前 15m 内完成两轮 tool-use/nonce probe，exact revision 投影 `ready`。 | verification IDs/revisions、safe reason/latency、Platform Status、admin audit、provider billing/request identity when available | false success、fallback、旧 revision ready 或最终未 ready 阻塞。预期失败本身是 pass。 |
| `S04` | 管理员创建无效 Notification configuration，真实 test Delivery bounded retry 后 `dead_letter`；修复后创建新 revision，真实 test Delivery 在 15m 窗口内 `sent`，管理员实际看到测试消息，并显式选择 Pilot catch-all Route。 | Delivery/attempt IDs、安全 reason、Route revision、Platform Status、HITL receipt attestation | false sent、未实际送达、未 selected 或最终未 ready 阻塞。预期 dead-letter 是 pass。 |
| `S05` | 管理员创建 exact `connector_id/cluster_id` Enrollment，plaintext credential 只显示一次；Operator 写入 Connector Secret 并 rollout。Registration/heartbeat 后仍需 bounded read verification，确认 cluster identity、discovery 和 permission summary才可 ready。 | Enrollment/request IDs、one-time-display attestation、heartbeat/read Command result、Platform Status | credential 可重读、identity 冲突未拒绝、只靠 heartbeat ready 或 read verification 失败均阻塞。 |
| `S06` | 自动检查 Prometheus 的 AIOps/kube-state-metrics targets、loaded rules 与真实 AIOps workload series，Loki 中 Alloy-tailed AIOps Pod stdout，Alertmanager route，以及两个 MCP Interface 的 guarded query。Fixture-specific series/run ID 留给 `V02`。禁止 synthetic push、constant response、manual webhook 或 backend 200 代替。 | target/rule/query snapshots、Loki stream ref、MCP envelopes、image/config digest | 任一真实观测链路未 ready 阻塞。 |

### Recovery and negative gates

`R05` 作为 authorization negative gate 在 `V04` 和 `V05` 之间执行。其余恢复测试在 `V07` 第一轮成功、删除 `verification/run` Job 后执行；`verification/base`、同一 Incident 和治理历史仍保留。开始前不得有 active mutation、Unknown Outcome 或 unfinished rollback。Operator mutation 只作用于 AIOps workload 或 `aiops-verification` fixture，并完整留痕。全部恢复 gate 通过后才进入 `V08` 第二轮，借第二轮证明恢复后的完整系统仍可工作。

| ID | Check | Retained evidence | Promotion effect |
| --- | --- | --- | --- |
| `R01` | 逐个删除 Gateway、Diagnosis、Connector、Notification、Prometheus、Loki 的当前 Pod，每次等待 owner Available/Ready 后再继续；登录、configuration revision、Incident/governance state、command journal、Delivery、metrics/logs 在其 retention 边界内保持。 | before/after object UID、PVC UID、owner status/data hash、rollout events | crash restart 丢 durable state、并行测试互相掩盖或无法恢复均阻塞。 |
| `R02` | 对 Release 中每个 Deployment 与 Alloy DaemonSet 逐个执行普通 pod-template annotation rollout，等待 Kubernetes rollout 收敛；随后 reapply 同一 bundle。所有 image 仍是 candidate digest，Platform Status 恢复且无 credential rotation。 | rollout status、ReplicaSet/Pod UID、digest inventory、reapply diff | rolling update 不收敛、漂移 digest、丢 secret/state 或 reapply 失败均阻塞；这不声明跨版本 upgrade。 |
| `R03` | 将 Connector 缩为 0，在无 active command 时确认 availability `unavailable`、新 live evidence/dry-run/grant/dispatch 被拒绝；reapply candidate manifest 恢复 replica 后 heartbeat + read verification 恢复。 | Platform Status before/during/after、API denial、Deployment events | offline 时仍 dispatch、恢复依赖数据库 patch 或状态误报均阻塞。 |
| `R04` | 将 Loki 缩为 0，确认日志 capability 降级、MCP Loki query 返回 bounded unavailable error 且不能产生 verified log Evidence；reapply candidate manifest 恢复后，第一轮 retained log 与新 probe log 均能真实查询。不得注入 fake log 补过。 | Platform Status、MCP error envelope、Evidence rejection、recovery query | owner 失败被显示健康、unavailable response 被包装为 Evidence 或不能恢复均阻塞。 |
| `R05` | 使用第二个无 Approval Authority 的普通 User 读取/审批 `V04` verification Change，必须在 diff access 与 Approval 处拒绝，因此不得产生 Execution Grant；`V05` 仍由已具有 `non-production + aiops-verification namespace` Authority 的验收 User 完成。 | 403/error code、authorized/unauthorized actor、authority revision、audit events、grant inventory | 越权读取 exact diff、审批或产生 grant 均为安全阻塞。 |
| `R06` | User 请求给 verification Deployment 的 object metadata 添加唯一 acceptance annotation，完成真实 discovery、dry-run 和 Approval；dispatch 前 Operator 用 Kubernetes API 写入不同 annotation 值以改变 resourceVersion，但不改 pod template。Connector 必须返回 `stale` 且不执行 approved patch，Gateway 不自动 rebase/retry。该故障不产生 Unknown Outcome；其余 grant/idempotency/cancellation/Unknown Outcome contract 由 `P02` 定向测试覆盖。 | plan/diff/Approval hash、before/after UID/resourceVersion、Operator patch、Connector stale result、event timeline | stale 后仍 mutation、自动 retry 或把 Operator effect 归为 AIOps success 均阻塞。 |

### Controlled scenario gates

`V01-V08` 严格使用 06 的 version-matched `verification/base` 与 `verification/run`。每轮以 Kubernetes Job controller UID 作为 `run_id`，共享 correlation key；禁止 seeded state、direct SQLite、手工 webhook、fake provider、人工改 terminal status 或 out-of-band workload restart。

| ID | Check | Retained evidence | Promotion effect |
| --- | --- | --- | --- |
| `V01` | Operator apply `verification/base`，管理员通过公开 Console 创建 Team/Service、提升真实 Discovery Candidate 为 Deployment Target 并确认 Binding；apply `verification/run` 后 Job 2m 内成功。 | fixture digest/object UID、catalog/binding revision、Job controller UID/log | 任一失败阻塞。 |
| `V02` | 2m 内真实 metric/log 可查，3m 内 Prometheus rule -> Alertmanager -> bearer webhook 创建 Alert Signal 和 Incident；fingerprint/labels/run ID 一致。 | target/series/log/rule/Alertmanager refs、webhook request ID、Alert/Incident ID | 超时、manual payload 或 label-only alert 阻塞。 |
| `V03` | 10m 内 Diagnosis 冻结 verified Model revision，并经 MCP/Connector 取得 Alert、Prometheus、Loki、live Kubernetes 四项 fresh evidence；模型产生 evidence-grounded restart Recommendation。 | Investigation/model revision、Evidence refs/status、Recommendation revision | 缺任一 Evidence、keyword fallback、Human Input 补 gate 或预写结论均阻塞。 |
| `V04` | User 从 Recommendation 显式创建 Change Request；模型生成 exact RFC 6902 annotation patch。Gateway 校验 scope/precondition、API Server dry-run，并展示 exact diff、`rollback: unavailable` 和 post-check。 | Change/Plan revision/hash、live UID/resourceVersion、dry-run object diff | vague approval、非 dry-run diff、scope 扩展或伪造 rollback 阻塞。 |
| `V05` | 获授权 User 以 5m fresh auth、reason、exact target confirmation 审批；Gateway 发 60s single-use grant，Connector 执行一次，5m 内 rollout 与 frozen post-check 成功。 | Authority/Approval/Grant、Command journal/result、Kubernetes audit/object UID、post-check | 无审批 mutation、重复执行、drift 隐藏、自动 retry 或超时阻塞。 |
| `V06` | 新 Pod 写 recovery log/metric 0，Prometheus 连续两次满足、Alertmanager 同 fingerprint resolved webhook 到达；5m stabilization 后 Incident resolved。 | recovery metric/log、resolved webhook、Recovery Observation、Incident events | 人工 resolve、缺真实 recovery 或时序超限阻塞。 |
| `V07` | User 完成并发布 Report version 1；`incident.resolved` 通过 transaction outbox、Notification Engine、`S04` 已验证的 exact Destination revision 达到 `sent`。 | immutable Report/hash、outbox/request/Delivery/provider identity、Destination revision 与 `S04` attestation 关联 | 缺内容、可变 published Report、通知假成功或 revision 未经回执验证均阻塞。 |
| `V08` | `R01-R04/R06` 完成后重新 apply 已删除的 `verification/run`，新 controller UID 在 24h window 内 reopen 同一 Incident、创建新 Investigation；完整重复 `V02-V07` 并发布 immutable Report version 2，旧版不变且第二条 resolved Delivery 在同一已验证 Destination revision 达到 `sent`。 | second run index、same Incident/new Investigation IDs、两版 hash、second Delivery 与已验证 Destination revision | 创建错误 Incident、复用旧 Evidence/Approval/grant、覆盖 v1、revision 未经回执验证或第二轮未送达均阻塞。 |

### Cleanup and final decision

| ID | Check | Retained evidence | Promotion effect |
| --- | --- | --- | --- |
| `C01` | Operator 依次 delete `verification/run` 和 `verification/base`；namespace/fixture workload 消失，不能误删 `aiops-system`。 | delete output、namespace/resource snapshot | fixture 残留或产品资源被删阻塞。 |
| `C02` | 自动确认 Gateway/Diagnosis/Notification governance history 仍可通过公开 API读取：两轮 Incident/Investigation/Evidence、Change/Approval/Grant/Command/outcome、Recovery、Report 与 Delivery；Resource Catalog 把已删除 target 显示 unavailable。 | public API evidence index、redacted hashes | cleanup 删除治理历史、需要 SQLite 才能读取或 target 假在线均阻塞。 |
| `C03` | 自动生成 artifact `SHA256SUMS`，HITL 检查 `manifest.json` 无缺 gate/secret，Platform Operator、Platform Administrator 和 SRE 分别签署其实际动作。 | final manifest/checksums/attestation | 缺必需 gate、artifact 不可关联、secret 泄露或角色未确认均阻塞。 |

最终 promotion 规则是：`P01-P03`、`I01-I03/I05`、`S01-S06`、`R01-R06`、`V01-V08`、`C01-C03` 全部 `passed`；`I04` 只能在未声明 HTTPS profile 时为 `not_applicable`。外部 provider 的瞬时失败允许在同一 bounded operation 内按产品 contract retry，但超出 deadline 就是该 run 失败，不能由验收脚本隐藏重试。发布负责人只依据 signed `manifest.json`、证据 hash 和人工 attestation 做 promote/no-promote，不依据演示观感或单元测试通过数。
