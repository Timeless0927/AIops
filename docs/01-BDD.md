# BDD：行为场景

## 参与者

- SRE
- Alertmanager
- 飞书
- Kubernetes
- AIOps Agent

## 功能：告警接入

### 场景：新告警创建 Incident

```gherkin
Given Alertmanager 发送一条新的 Kubernetes 告警
And 系统中不存在相同 fingerprint 的未关闭 Incident
When AIOps 接收到该告警
Then AIOps 创建一个新的 Incident
And Incident 状态为 open
And AIOps 在飞书中发送一条 Thread 消息
```

### 场景：重复告警合并到已有 Incident

```gherkin
Given 系统中已存在相同 fingerprint 的未关闭 Incident
When AIOps 再次收到匹配的告警
Then AIOps 不创建重复 Incident
And AIOps 将该告警追加到已有 Incident 的事件历史
```

## 功能：诊断摘要

### 场景：Incident 生成诊断摘要

```gherkin
Given 一个未关闭的 Incident 已具备足够告警上下文
When AIOps 执行诊断
Then AIOps 记录诊断摘要
And AIOps 将摘要发送到对应飞书 Thread
```

## 功能：人工审批

### 场景：需要审批的修复建议创建飞书原生审批

```gherkin
Given 某个 Incident 存在需要审批的修复建议
When AIOps 创建本地 approval
And AIOps 成功创建飞书原生审批实例
Then 本地 approval 状态为 external_pending
And 本地 approval 记录 external_provider、external_instance_code、external_uuid 和 external_status
And 飞书审批中心成为 SRE 的主审批入口
And incident thread 展示审批链接、风险摘要和操作摘要
```

### 场景：飞书原生审批创建失败时不得执行

```gherkin
Given 某个 Incident 存在需要审批的修复建议
When AIOps 创建本地 approval
And AIOps 创建飞书原生审批实例失败
Then 本地 approval 状态为 approval_create_failed
And AIOps 不执行该修复动作
And incident thread 明确提示审批创建失败或需要人工处理
```

### 场景：飞书批准只同步本地状态，不直接执行

```gherkin
Given 本地 approval 状态为 external_pending
And 飞书审批实例的 uuid 或 instance_code 能匹配该 approval
When AIOps 收到飞书 APPROVED 审批事件
Then AIOps 将本地 approval 幂等同步为 approved
And 飞书事件处理过程不直接执行修复命令
And 只有 execution worker 读取到本地 approved 后才可以继续执行
And AIOps 仍是修复动作的执行权威
```

### 场景：飞书拒绝、取消或过期后不得执行

```gherkin
Given 本地 approval 状态为 external_pending
When AIOps 收到飞书 REJECTED 或 CANCELED 审批事件
Then AIOps 分别将本地 approval 同步为 denied 或 canceled
And AIOps 不执行该修复动作
```

```gherkin
Given 本地 approval 状态为 external_pending
When AIOps 判定审批超过有效期或从补偿查询得到过期结果
Then AIOps 将本地 approval 同步为 expired
And AIOps 不执行该修复动作
```

### 场景：重复、未知或延迟事件不得回滚终态

```gherkin
Given 本地 approval 已处于 executed、failed、denied、canceled 或 expired
When AIOps 收到重复、延迟或未知 uuid / instance_code 的飞书审批事件
Then AIOps 不改变该 approval 的终态
And AIOps 只记录审计或异常观察结果
And AIOps 不重复触发执行
```

### 场景：webhook 遗失时通过 polling 补偿同步

```gherkin
Given 本地 approval 状态为 external_pending
And 对应飞书审批实例已经产生终态
And AIOps 没有收到飞书审批事件 webhook
When polling 补偿流程查询该飞书审批实例
Then AIOps 按外部终态幂等同步本地 approval
And approved 只表示后续 execution worker 可评估执行
And denied、canceled 或 expired 均不得执行
```

### 场景：自定义审批卡片不再作为主审批事实

```gherkin
Given 飞书原生审批能力已启用
When AIOps 在 incident thread 中展示自定义审批卡片或通知
Then 该卡片只作为通知展示或回退入口
And AIOps 不把卡片按钮点击视为主审批事实
And AIOps 以飞书原生审批结果同步后的本地 approval 状态作为执行前置条件
```

## 待确认行为问题

- 飞书原生审批过期状态的外部字段名称需由 architect-agent 在 SDD 中确认；产品语义已固定为本地 `expired` 终态。
