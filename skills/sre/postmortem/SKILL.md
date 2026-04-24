---
name: sre-postmortem
description: SRE 事后复盘。整理时间线、总结根因、提取可复用步骤，并生成 runbook 草稿。
version: 1.0.0
author: AIOps SRE Agent
license: MIT
metadata:
  hermes:
    tags: [sre, postmortem, incident, runbook, knowledge]
    related_skills: [sre-triage, sre-investigate, sre-remediate]
---

# SRE Postmortem

用于在事件处理完成后沉淀复盘材料和可复用操作知识。

## 输入

整合以下阶段的输出：

- triage
- investigate
- remediate

如果某一阶段缺失，明确记录缺口，不要伪造完整链路。

## 目标

完成以下事情：

1. 整理完整时间线
2. 总结根因与诱因
3. 提取可复用的诊断与修复步骤
4. 形成改进建议
5. 调用 `skill_extractor` 生成 runbook 草稿，并存入 `drafts/`

## 工作流

### 1. 整理时间线

按时间顺序整理：

- 告警触发时间
- 首次响应时间
- triage 结论产出时间
- investigate 关键发现时间
- 审批发起与通过时间
- 修复执行时间
- 恢复确认时间

如果缺少精确时间，用“相对顺序 + 证据来源”补齐。

### 2. 总结根因

把根因拆成三层：

- 直接原因
- 深层原因
- 触发条件 / 放大因素

同时区分：

- 检测为什么发生
- 处置为什么耗时
- 哪些地方原本可以更早发现或更快恢复

### 3. 提取可复用步骤

从整个处置过程中抽取稳定、可重复的步骤，例如：

- 先看哪些指标
- 再查哪些日志
- 需要哪些 `kubectl` 命令
- 哪些现象可作为根因判断信号
- 哪些修复动作需要审批

提取时避免绑定一次性细节，保留通用决策逻辑。

### 4. 形成改进建议

建议至少覆盖以下方向中的两项：

- 告警质量优化
- 可观测性补强
- 自动化修复机会
- 发布流程改进
- 容量 / 稳定性治理
- 审批流程优化

### 5. 生成 runbook 草稿

调用 `skill_extractor`，基于本次事件沉淀一个可复用 runbook 草稿。

要求：

- 草稿写入 `skills/sre/drafts/`
- 文件名使用事件主题或告警名生成 slug
- 明确标注为草稿，等待人工审核后再迁入 `runbooks/`

如果运行环境尚未提供真正的 `skill_extractor` 工具，也要输出一份结构化草稿内容，供后续落盘。

## 输出格式

### 事件时间线

- 按时间顺序列出关键节点

### 根因总结

- `direct_cause`
- `root_cause`
- `contributing_factors`

### 可复用步骤

- 列出诊断与修复流程模板

### 改进建议

- 列出可执行的后续动作

### Runbook 草稿

- `draft_target_path`
- `draft_summary`
- `review_needed`: true

## 约束

- 复盘不能变成流水账，必须提炼决策点
- 不能把没有证据支持的猜测写成事实
- runbook 草稿必须强调“待审核”，不要直接当正式规范

