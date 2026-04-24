---
name: sre-voice-triage
description: 面向语音交互的 SRE 快速诊断流程。适合用户通过语音询问集群状态、告警原因和下一步处理建议。
version: 1.0.0
author: AIOps SRE Agent
license: MIT
metadata:
  hermes:
    tags: [sre, voice, triage, kubernetes, speech]
    related_skills: [sre-triage, sre-investigate]
---

# 语音快速诊断

用于处理用户通过语音发起的集群状态询问、故障确认和快速初筛请求。

## 触发条件

- 用户通过语音询问“现在集群怎么样”
- 用户通过语音询问“哪个服务挂了”“哪个 pod 有问题”
- 用户通过语音询问某条告警的原因、影响和下一步动作
- 平台已经将语音转写成文本，需要输出更适合 TTS 的简短回复

## 目标

完成以下事情：

1. 快速识别受影响的 namespace、服务、Pod 或节点
2. 调用只读工具获取最关键的 K8s 状态、事件和日志证据
3. 输出语音友好的结论，而不是大段原始命令结果
4. 给出明确下一步建议，并在结尾追加 `[[audio_as_voice]]`

## 诊断步骤

### 1. 规范化用户问题

先从语音转写文本中提取这些关键信息：

- 目标对象：服务名、Deployment、Pod、Node、namespace
- 症状：重启、不可用、响应慢、错误率高、证书过期、存储满
- 时间范围：刚刚、最近几分钟、今天等

如果字段不完整，不要中断，先基于已有信息做最小可行排查。

### 2. 获取 K8s 基础状态

优先使用 `k8s_read` 获取最关键状态。

推荐查询：

```text
k8s_read(command="kubectl get pods -n <namespace> -o wide")
k8s_read(command="kubectl describe pod <pod_name> -n <namespace>")
k8s_read(command="kubectl get deploy -n <namespace>")
k8s_read(command="kubectl get events -n <namespace> --sort-by=.lastTimestamp")
```

如果问题与容器重启、探针失败或配置异常有关，再补充：

```text
k8s_read(command="kubectl logs <pod_name> -n <namespace> --previous")
k8s_read(command="kubectl logs <pod_name> -n <namespace> -c <container_name> --tail=100")
```

### 3. 获取关键指标

使用 `prometheus_query` 获取与问题直接相关的指标，只保留对结论有帮助的内容。

优先关注：

- CPU / 内存压力
- 重启次数
- 错误率
- 延迟
- 请求量

如果只是快速确认健康状态，可优先查询最少的一到两个指标，不要堆叠太多指标细节。

### 4. 语音化表达结果

语音回复必须遵循以下规则：

- 省略原始命令输出、YAML、表格、长日志
- 优先输出结论，例如“发现 pod `api-7d8c` 在反复重启，原因更像是内存不足”
- 用口语化中文表达，避免机械罗列字段名
- 如果证据还不充分，要明确说“目前更像是……，还需要继续查日志或事件”
- 如果结果较长，调用 `sre_voice_summary` 进一步压缩

推荐调用：

```text
sre_voice_summary(text="<完整诊断结果>", max_sentences=5)
```

### 5. 给出明确下一步建议

回复结尾必须给出一个明确动作建议，例如：

- “需要我继续帮你查日志吗？”
- “需要我帮你检查是不是探针配置错误吗？”
- “需要我帮你生成扩容建议吗？”
- “需要我继续进入 investigate 流程吗？”

最后一行固定追加：

```text
[[audio_as_voice]]
```

## 输出格式

最终回复使用以下结构：

1. 一句话结论
2. 两到三句口语化证据摘要
3. 一句明确下一步建议
4. 最后一行加 `[[audio_as_voice]]`

示例：

```text
我刚看了一下，default 命名空间里的 api 服务确实有异常。
发现 pod api-7d8c 在反复重启，上一轮日志里有明显的内存不足报错，指标上内存使用也接近上限。现在更像是资源限制偏小，不像是节点故障。
需要我帮你继续看要不要扩容，还是先检查探针配置？
[[audio_as_voice]]
```

## 约束

- 不要直接输出大段原始命令结果给语音用户
- 不要执行写操作、高危命令或审批动作
- 不要使用过于书面化、表格化的表达
- 如果信息不够，先给当前最可能结论，再说明还缺什么证据
