---
name: sre-remediate
description: SRE 修复执行。基于 investigate 结论生成修复计划，发起审批，执行修复并验证结果。
version: 1.0.0
author: AIOps SRE Agent
license: MIT
metadata:
  hermes:
    tags: [sre, remediate, recovery, kubernetes, approval]
    related_skills: [sre-triage, sre-investigate, sre-postmortem]
---

# SRE Remediate

用于把诊断结论转成可审批、可执行、可验证的修复动作。

## 输入

接收 `investigate` 的输出，至少应包含：

- 根因结论
- 关键证据
- 修复建议
- 受影响对象和命名空间

## 目标

完成以下事情：

1. 生成明确的修复计划
2. 优先做 dry-run 或等价预检
3. 通过 `k8s_write` 或 `k8s_exec` 发起修复
4. 修复后再次检查状态和指标
5. 输出修复报告

## 工作流

### 1. 生成修复计划

把建议转换成具体命令，不要停留在“建议重启服务”这种描述层面。

修复计划至少包含：

- 修复目标
- 具体 kubectl 命令列表
- 每条命令的目的
- 风险说明
- 回滚思路

如果存在多种修复路线，先给低风险方案，再给激进方案。

### 2. dry-run / 预检

如果命令支持 dry-run，优先验证：

- `kubectl apply --dry-run=client|server`
- `kubectl diff`
- `kubectl auth can-i`

如果命令本身不支持 dry-run，要做等价预检，例如：

- 再次读取当前 Deployment / Pod / HPA / ConfigMap 状态
- 确认目标资源存在
- 确认期望副本数、镜像、配置项

### 3. 选择执行工具

根据命令性质选择：

- 写操作：`k8s_write`
- 高危操作：`k8s_exec`

严格遵守工具审批语义：

- `k8s_write` 返回标准审批请求
- `k8s_exec` 返回高级审批请求

如果工具返回需要审批，不要假装已经执行，必须明确写出“等待审批”。

### 4. 执行后验证

审批通过并执行后，必须再次取证验证。

至少做以下检查：

- `k8s_read` 检查 Pod / Deployment / ReplicaSet 状态
- `prometheus_query` 检查关键指标是否恢复
- 必要时查询最新 events，确认没有新的异常

验证目标：

- 故障现象是否消失
- 目标副本是否恢复
- 错误率和延迟是否回落
- 是否引入新的副作用

## 输出格式

### 修复计划

- `plan_summary`
- `commands`
- `risk_assessment`
- `rollback_plan`

### 审批状态

- `approval_required`
- `approval_level`
- `pending_or_executed`

### 验证结果

- `post_check_k8s`
- `post_check_metrics`
- `residual_risk`

### 修复报告

- 说明是否已修复
- 说明是否需要继续观察
- 说明是否应进入 `postmortem`

## 约束

- 不要跳过审批链路
- 不要执行与根因无关的广谱高风险命令
- 如果验证失败，要明确回滚或继续排查建议

