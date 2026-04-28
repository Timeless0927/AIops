# CLAUDE.md

## 项目概述

AIOps SRE Agent — 基于 hermes-agent fork 构建的 K8s 智能运维 Agent。当前阶段聚焦单集群值班闭环：告警进入后，agent 自动采集证据、输出调查结论与修复建议，在人工审批后执行低风险动作，并完成验证、审计与 case 沉淀。主动风险发现与预防建议保留为后续阶段能力，不作为当前主线承诺。

完整架构方案：`docs/hermes-sre-agent-architecture.md`

## 核心架构决策

- **底座**：hermes-agent fork（NousResearch，MIT），最小化核心修改，尽量用插件/扩展方式接入
- **单 Agent 架构**：不用多 Agent。单 agent + 并行工具调用，每个工具内部用 langextract（便宜模型）预处理大数据量
- **单 Agent 多用户**：飞书/钉钉私聊天然隔离 session，gateway 层身份绑定，工具层硬编码权限校验，跨 session 非阻塞审批
- **三级工具安全**：k8s_read（无审批）→ k8s_write（标准审批）→ k8s_exec（高级审批）
- **delete 二次分级**：delete pod/deployment 走标准审批，delete namespace/node/pv 走高级审批
- **非阻塞审批**：工具发审批请求后立即返回，审批通过后 callback 注入消息触发执行
- **langextract 预处理**：工具输出 >= 200 行时自动触发结构化提取（便宜模型），< 200 行直接返回强模型
- **Skill 动态闭环**：incident 处理完成 → 自动提取可复用步骤 → 生成 skill 草稿 → 专家审核 → 上线
- **数据合规**：langextract 用本地 Ollama 处理原始数据，仅结构化结果发云端强模型
- **不保留旧代码**：全新项目，不保留 k8s-aiops 的 React UI / LangGraph / FastAPI

## 技术栈

- Python 3.10+
- hermes-agent v0.10.0（fork）
- LLM：Anthropic Claude / OpenAI（强模型），Gemini Flash / Ollama（便宜模型）
- 观测栈：Kubernetes API / Prometheus / Loki / Alertmanager
- 消息通道：飞书（lark-oapi）/ 钉钉（dingtalk-stream）
- 持久化：SQLite（WAL 模式）— incidents / audit_log / operation_locks / approvals
- 结构化提取：Google langextract（Apache-2.0）

## 常用命令

```bash
# 安装依赖
pip install -r requirements.txt

# CLI 模式运行（开发调试）
python -m hermes --mode cli

# 飞书 gateway 模式
python -m hermes --mode gateway --platform feishu

# 运行测试
pytest tests/
pytest tests/test_k8s_guard.py  # 单个测试文件
```

## 关键约定

- 所有 UI 文本和注释用中文
- Commit message 用中文，简洁描述变更意图
- `.env` 不提交真实密钥
- 全链路 async/await，不引入阻塞调用
- 工具遵循 hermes tool 格式（参考 hermes 已有工具结构）
- 权限校验在工具 Python 代码中硬编码，不依赖 LLM prompt
- 新增模块优先通过 hermes 扩展机制接入（toolsets / skills / hooks），避免改核心代码

## 多 Agent 协作分工

### 角色分配

| Agent | Provider | 角色 | 职责 |
|-------|----------|------|------|
| agent3 | claude | 主管/架构师 | 任务拆分、方案设计、最终验收、协调调度，不直接编码 |
| agent1 | codex | 主力开发 A | 核心模块编码（k8s 工具集、安全护栏、guard） |
| agent2 | codex | 主力开发 B | 辅助模块编码（审批流、审计日志）+ 测试编写 |
| agent4 | gemini | 审查/探索 | 代码审查、方案探索、文档生成，不直接改代码 |

### 交叉审计链路

- agent1 写代码 → agent4 审查 → agent3 验收
- agent2 写代码 → agent4 审查 → agent3 验收
- agent4 出方案 → agent1/agent2 可行性校验 → agent3 决策

### 任务拆分原则

好的拆分（按模块隔离，不同 agent 写不同文件）：
- 前端 vs 后端
- 不同模块（如 k8s_read vs k8s_write）
- 实现 vs 测试（可并行）

避免的拆分：
- 同一个文件的不同部分（会冲突）
- 有强依赖的任务（必须串行）
- 需要频繁沟通的任务（协调成本高）

### 统一输出格式

所有 agent 编码完成后须提供：
- `changedFiles` — 修改的文件列表
- `diffSummary` — 变更摘要
- `testResults` — 测试结果
- `risks` — 风险点

## 实施阶段

Phase 0 → 环境搭建 + 跑通 hermes（含多用户三个假设验证）
Phase 1 → K8s 工具集（k8s_read/write/exec + guard + langextract + 安全护栏）
Phase 2 → SRE 诊断 Skill（triage/investigate/remediate/postmortem + 动态闭环）
Phase 3 → 告警 → 自动诊断 → 非阻塞审批修复
Phase 4 → 审计日志 + 权限管理 + incident 时间线 + 运维交接
Phase 5 → 语音交互
Phase 6 → 运营健壮性（通知防疲劳 / LLM 降级 / 成本监控 / 自监控 / 效果度量）
Phase 7 → Helm / ArgoCD 扩展（可选）
