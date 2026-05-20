# Repository Guidelines

## Project Structure & Module Organization
- `hooks/`: alert, approval, audit, identity, recovery, and voice hooks.
- `toolsets/`: AIOps tools and guards such as `k8s_read`, `k8s_write`, and `k8s_exec`.
- `runtime/`: gateway and overlay helpers.
- `skills/`: SRE skill drafts and runbooks.
- `tests/`: root pytest suite.
- `hermes-agent/`: vendored Hermes fork with its own [AGENTS.md](hermes-agent/AGENTS.md); `hermes-agent/web/` is the UI, and `deploy/k8s/` holds manifests.

## Build, Test, and Development Commands
- `pip install -r requirements.txt`: install the root editable stack.
- `python -m hermes --mode cli`: run the local CLI loop.
- `python -m hermes --mode gateway --platform feishu`: exercise the Feishu gateway path.
- `pytest tests/`: run the root test suite.
- `cd hermes-agent && uv pip install -e ".[all,dev]" && python -m pytest tests/ -q`: install and test the Hermes subtree.
- `cd hermes-agent/web && npm run dev`, `npm run build`, or `npm run lint`: start the UI, build assets, or run ESLint/TypeScript checks.
- `docker build -f Dockerfile.aiops -t aiops-agent:latest .` and `kubectl apply -f deploy/k8s`: package and deploy the AIOps image/manifests.

## Coding Style & Naming Conventions
- Use 4-space indentation in Python, `snake_case` for modules/functions, and `test_*.py` for tests.
- Keep guard, approval, and incident logic small and deterministic; prefer focused helpers over broad branching.
- Match the surrounding language in each area. Many SRE-facing Python files use Chinese docstrings/comments, while `hermes-agent/` follows Hermes' English style.
- For the web UI, follow `hermes-agent/web/eslint.config.js`; run `npm run lint` before merging UI changes.

## Testing Guidelines
- Add or update tests with every behavior change; approval, guard, webhook, and incident flows need focused regression tests.
- Use pytest naming conventions: files `test_*.py`, functions `test_*`.
- In `hermes-agent/`, `pytest` skips `integration` tests by default via `pyproject.toml`; run them explicitly when needed.

## Development Progress Tracking
- Multica issues are the source of truth for task status, blockers, acceptance results, remaining risks, assignees, and completion comments.
- `docs/development-progress.md` is a historical progress snapshot and long-term capability index. Use it to find existing code/test evidence, not to determine current task state.
- `docs/TODD.md` is a historical handoff snapshot. Do not maintain it as the current-work ledger.
- Before starting feature work, read the relevant Multica issue and use the historical progress table only to avoid rebuilding completed pieces.
- After any feature change, update the relevant Multica issue with status, code/test evidence, remaining work, and latest verification. Update repository docs only when long-term product, architecture, test, deployment, or evidence-index knowledge changed.
- Do not mark a feature `完成` unless implementation, tests, and the relevant acceptance path are complete.
- In the final response or issue comment, state which docs changed, what verification ran, and which issue carries the current acceptance conclusion.

## Knowledge Graph Usage
- `graphify-out/graph.json` is the persistent project knowledge graph for this repository, excluding the upstream `hermes-agent/` subtree.
- Before architecture analysis, module relationship analysis, feature planning, or impact assessment, check the graph first instead of rediscovering the repository through broad `grep`/`rg` sweeps.
- Use the graph to choose likely modules, communities, bridge nodes, and related documents; then use `rg` only for precise symbol lookup, current implementation verification, and line-level evidence.
- If the graph is missing or stale for the relevant area, rebuild it with `$graphify` or `$graphify --update` before relying on it for cross-file reasoning.

## 固定 Agent 角色
- 本项目使用三个固定角色。新的 Codex 窗口开始工作前，必须先读取本文件和对应角色文件。
- 默认面向用户入口角色：`dev-lead-agent`。
- 角色定义：
  - `product-domain-agent`：`.agents/roles/product-domain-agent.md`
  - `architect-agent`：`.agents/roles/architect-agent.md`
  - `dev-lead-agent`：`.agents/roles/dev-lead-agent.md`
  - `implementation-agent`：`.agents/roles/implementation-agent.md`
  - `test-agent`：`.agents/roles/test-agent.md`
  - `review-agent`：`.agents/roles/review-agent.md`
- 变更流程：`.agents/workflows/change-request.md`

## 文档语言规则
- 面向人阅读的项目文档、CR、TODD、PDD、BDD、DDD、SDD、实施计划和测试计划均以中文为主。
- 代码标识符、文件路径、命令、API 字段、错误信息、协议名和通用技术术语可保留英文。
- Mermaid 图中的节点名称优先使用中文；必须对应代码模块或外部系统名时可保留英文。

### product-domain-agent
- 允许使用的 skill 家族：`domain-driven-design-skills`。
- 负责 `docs/00-PDD.md`、`docs/01-BDD.md` 和 `docs/02-DDD.md`。
- 可以更新 `docs/CHANGE-REQUESTS.md` 与 `docs/TODD.md` 中的产品/领域部分。
- 禁止修改应用代码、部署、实施计划和测试计划。

### architect-agent
- 允许使用的 skill 家族：`gstack`。
- 负责 `docs/03-SDD.md` 和 `docs/adr/*.md`。
- 可以更新 `docs/CHANGE-REQUESTS.md` 与 `docs/TODD.md` 中的架构部分。
- 禁止直接实现代码、部署、合并，或改写产品/领域决策。

### dev-lead-agent
- 允许使用的 skill 家族：`Superpowers`。
- 启动后必须先读取 `using-superpowers`，由 Superpowers 自动选择合适的开发流程 skill。
- 负责 `docs/04-IMPLEMENTATION-PLAN.md`、`docs/05-TDD-TEST-PLAN.md`、`docs/CHANGE-REQUESTS.md` 和 `docs/TODD.md`。
- `docs/TODD.md` 仅作为历史交接快照维护；当前状态以 Multica issue 为准。
- 作为默认 CR 入口负责人。
- 禁止私自改变 PDD、BDD、DDD 或 SDD 决策。
- 禁止直接读取源码全文、修改应用代码或直接运行测试；这些工作必须分派给子 agent。
- Superpowers 的自动规划不得突破本项目角色边界；代码读取、代码修改、测试执行和 diff 审查必须通过子 agent 完成。

### 开发子 Agent
- `implementation-agent`：负责按明确文件范围实现代码，不负责最终验收。
- `test-agent`：负责写/改测试、运行测试和必要的用户流程验收，不负责实现业务代码。
- `review-agent`：负责独立审查 diff、风险和计划符合度，不负责实现或修复。
- 子 agent 必须返回摘要，不把大段源码、测试日志或原始 diff 塞回主对话。

## 变更控制
- 会改变行为的用户反馈，必须先记录或关联到 Multica issue，再修改代码；如果形成长期产品、架构、测试或部署决策，再同步到 `docs/CHANGE-REQUESTS.md` 或对应 PDD/BDD/DDD/SDD/TDD 文档。
- `dev-lead-agent` 先初筛 CR 对 PDD、BDD、DDD、SDD、TDD 和 TODD 的影响。
- 产品、行为或领域影响必须由 `product-domain-agent` 评审。
- 架构、API、数据、部署、安全或可观测性影响必须由 `architect-agent` 评审。
- 仅实现 bug 和测试缺口可由 `dev-lead-agent` 处理。
- CR 完成前，必须在对应 Multica issue 记录测试或验证、验收结论和剩余风险；仅当长期知识发生变化时更新受影响文档，不再要求同步 `docs/TODD.md` 或 `docs/development-progress.md` 作为事实源。
- `dev-lead-agent` 处理实现工作时，只能分派、收摘要、更新计划和状态；不能亲自读代码、改代码或跑测试。
- 实现不能自验收：`implementation-agent` 完成后，必须由 `test-agent` 验证，并由 `review-agent` 独立审查。

## 子 Agent 摘要格式
- 任务：
- 结果：
- 修改文件：
- 验证：
- 风险：
- 需要决策：

## 角色启动提示
- 产品/领域：`读取 AGENTS.md。你现在是 product-domain-agent。读取 .agents/roles/product-domain-agent.md。`
- 架构：`读取 AGENTS.md。你现在是 architect-agent。读取 .agents/roles/architect-agent.md。`
- 开发主管：`读取 AGENTS.md。你现在是 dev-lead-agent。读取 .agents/roles/dev-lead-agent.md。`

## Commit & Pull Request Guidelines
- Recent commits mostly use short conventional prefixes such as `feat:`, `fix:`, `docs:`, and `test:`. Keep the subject concise and imperative.
- PRs should explain the change, list verification commands, and link the relevant issue or plan.
- Include screenshots or short recordings for web/UI changes, and call out config or secret updates.

## Security & Configuration Tips
- Never commit real secrets. Keep placeholders in `.env` and `config.yaml`.
- Prefer `AIOPS_DATA_DIR` for runtime state and SQLite files when available.
- Follow the nearest `AGENTS.md` first; `hermes-agent/AGENTS.md` governs that subtree.
