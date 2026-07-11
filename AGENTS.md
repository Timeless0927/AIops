# AGENTS.md

@/root/.codex/RTK.md

## 事实源

- 领域术语以 `CONTEXT.md` 为准。
- 当前架构、运行边界和部署状态以 `docs/README.md` 指向的文档为准，不在本文件复制易过期的实现清单。
- 当前任务规格和票据以 `.scratch/` 下对应的 active Markdown 为准。
- Legacy Console 只存在于 Git 历史。不得恢复、迁移或包装旧组件、样式、路由、API wrapper 和状态流。

## 模块设计

- 所有代码必须归属最小内聚、可独立测试的 Module。“最小”指单一职责、窄 Interface 和清晰依赖，不指把实现拆成大量碎文件。
- 进程边界不等于 Module 边界。Gateway、Diagnosis、Connector、MCP、Notification Engine 和 Console 是运行进程；每个进程内部仍须按可独立变化和测试的领域能力划分 Module，不得把整个进程当成一个 Module。
- Module 只暴露调用方完成业务所需的最小 Interface；实现细节保持模块内聚，其他模块不得导入其内部文件。
- 领域决策与网络、数据库、文件系统、时钟、随机数、进程启动和 UI framework 分离。外部能力只在真实 Seam 处通过 Adapter 接入。
- 会产生 I/O、非确定性或共享状态的依赖由装配层传入；纯函数、标准库和 Module 内部实现不得仅为“依赖注入”再包装一层。结果通过返回值或明确事件输出，领域逻辑不得创建外部客户端或依赖隐式全局状态。
- 不为单一实现额外创建代码层面的 interface、factory、provider 或插件层。只有存在第二个真实 Adapter 或明确替换需求时才建立 Seam。
- 禁止只转发参数的浅 Module、跨 Module 的循环依赖和为未来需求预留的扩展点。
- 共享代码必须承载真实共享约束。只有一个调用方的 helper 留在所属 Module，不进入公共目录。
- 修改应落在拥有该行为的 Module。若同一修复需要散落到多个调用方，先修正共同 Interface 或依赖方向。

## 模块体量门禁

- 行数只是职责混杂的风险信号，不是拆分目标。不得为了满足行数制造只转发参数的文件、空壳分层或一个函数一个文件。
- 手写生产源码或测试文件达到 500 行后，修改前必须先确认并在任务记录中说明所属 Module、公开 Interface 和定向测试 selector；若文件仍只有一个内聚职责，可以保持不拆。
- 手写生产源码或测试文件在任务开始时已超过 800 行，任务完成时总行数不得高于开始时；新文件不得超过 800 行。生成类型、OpenAPI/contract artifact、lockfile、固定 fixture/snapshot、vendored code 和数据文件不计入该门禁。
- 超过 800 行的文件不得承载新业务能力。普通任务必须把本任务涉及的完整能力迁入所属 Module；生产故障、安全修复或数据损坏风险可以先做最小修复，但必须记录例外原因和后续 owner，不得借例外扩展功能。
- `main.py`、`__main__.py` 和同类进程入口只负责参数读取、依赖装配、路由/命令分发与进程启动；不得新增领域决策、SQL、外部调用编排或业务状态转换。
- Store/Repository 按领域能力归属，不得把同一进程的 Session、User、Resource Catalog、Incident、Investigation、Command、Approval、Report 等状态持续堆入一个总 Store 类。共享数据库只共享连接、migration 和 transaction 约束，不共享无边界的业务 Interface。
- 触碰超大旧文件时只迁移当前任务需要的完整能力，不顺手拆无关区域。迁移必须保持原公开 Interface 行为不变，并通过该能力的定向测试。
- 迁移是搬家，不是复制：新实现可用的同一变更中必须删除旧实现。除规格明确要求且写明退出条件的滚动兼容外，不保留 wrapper、双写、双读或两套 owner。
- 同一票据需要迁移和新增行为时，先以独立提交完成行为不变的迁移及验证，再以独立提交实现新行为，保证可审查、可回滚且不会把重构与行为变化混在一起。

## V1 增量迁移门禁

- 当前 V1 工作以 `.scratch/aiops-v1-model/tickets.md` 的依赖图推进。不得为了“先整理干净”暂停主线或全量拆分 Gateway；每张票只治理自己触碰的能力。
- T24 验收前，未版本化 `/api/*`、legacy Agent Run/Diagnosis Session/panel contract 与 Gateway static serving 保持冻结。除安全和正确性修复外不得扩展，也不得投入只为美化这些待删除路径的拆分工作。
- 新 `/api/v1` 行为必须进入领域能力拥有的 Module，HTTP Adapter 只负责输入验证、鉴权调用、调用 Module Interface 和序列化结果。不得继续把业务 handler 堆入 `apps/aiops_k8s_gateway/main.py`。
- `GatewayV1Store` 现有 Interface 视为待收敛的过渡实现，不得继续充当新领域能力的命名空间。后续 Resource Catalog、Incident、Investigation、Connector Command、Approval/Execution 和 Report 状态分别由所属 Module 管理，并在需要原子性时共享 Gateway-owned transaction。
- Notification Engine 相关票据必须落入独立进程及其 `notification.db` owner；现有 Gateway `notification_center.py` 作为待替换路径冻结。确有可复用实现时移动代码并删除旧实现，不复制或包装出长期双路径。
- T24 负责删除已被 V1 接受路径替代的 legacy contract 和实现。在此之前只建立后续票据会真实复用的 Seam，不拆分注定由 T24 整体删除的代码。

## 测试策略

- 每个 Module 的测试必须能按测试文件、目录或测试工具原生 selector 独立运行；新增测试基础设施必须保留这种能力。
- 日常修改只运行受影响 Module 的测试，再运行其直接 Interface/contract 消费方测试。禁止把全量测试作为局部修改的默认验证方式。
- 只有共享 contract、公共基础设施、数据库 migration、跨 Module 工作流发生变化，或进入 CI / 发布验收时，才扩大测试范围。
- 若无法指出某段代码对应的定向测试，或一个普通局部修改只能靠全量测试验证，视为 Module 或测试边界缺陷；先修正边界。
- Module 测试通过公开 Interface 验证行为，不依赖私有调用顺序。除 Adapter 集成测试外，不启动完整应用、不访问真实网络或共享外部服务。
- 时间、随机、环境和外部响应必须可控，使 Module 测试可重复、可并行且不依赖执行顺序。
- 测试数量按风险决定：一个行为保留覆盖正常路径和关键失败路径的最小测试集合，不为覆盖率数字复制案例。
- 提交前的验证顺序：受影响测试、直接 contract 测试、受影响 workspace 的静态检查。全量测试留给 CI 或明确的发布检查。

## 前端约束

- Console 使用 shadcn/ui 作为唯一通用组件系统；先组合已有 primitive，不引入第二套 UI library，不复制 legacy CSS。
- 前端按领域能力组织 Module；数据访问、领域状态转换或视图逻辑达到可独立变化和测试的程度时才分离，禁止为了分层制造只转发参数的文件。
- Server state、URL state 和 transient UI state 必须分清所有权，不建立浏览器端的第二套后端状态机。
- 保留语义化 HTML、键盘操作、可见焦点、可访问名称和移动端无溢出等基本可访问性要求。

## 系统与安全

- 除非当前任务明确包含并通过 ADR 变更架构，否则保持 Gateway、Diagnosis、Connector、MCP 和 Console 的既有进程边界；跨边界只使用已声明 contract，不导入对方内部实现。
- 浏览器只访问 Gateway 暴露的认证、API 和 event stream，不直连 Diagnosis、Connector、MCP、观测后端或通知渠道。
- 所有外部输入在信任边界验证。鉴权、审批、审计、幂等和 mutation guard 不得为了减少代码或测试而省略。
- 会改变 Kubernetes 状态的能力必须经过 Gateway-owned authorization 和显式审批链路；Human Input、通知和模型输出都不构成执行授权。
- Contract 变更必须同步更新生产方、直接消费方和对应 contract 测试。需要滚动升级时，兼容路径必须显式、限时且有测试，不保留无退出条件的隐式兼容分支。

## 变更纪律

- 优先删除、复用标准库和现有依赖；不增加未被当前需求使用的抽象、配置、依赖或无明确迁移期限的兼容层。
- 一次变更只处理一个明确目标，不夹带无关重构。发现相邻问题时单独记录。
- 领域术语变化同步更新 `CONTEXT.md`；难以逆转且存在真实权衡的架构决策才写 ADR。
- 面向人的项目文档以中文为主；代码标识符、路径、命令、API 字段和错误码保留英文。
