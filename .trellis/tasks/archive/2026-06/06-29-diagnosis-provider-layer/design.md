# Design: Diagnosis provider layer + outgress logging

> Active task: `06-29-diagnosis-provider-layer`(激活后 `task.py start` 此 task)。
> 依赖:无。是 child 2(`06-29-llm-tooluse-rewrite`)的硬前置。

## Goal Recap

在 `hermes/` 新增 OpenAI-compatible provider 调用层 + 出境日志(ADR-0005 Issue D)+ `ScriptedProvider`
测试件。child 2 的 tool-use 编排通过它驱动 LLM。本 child 不触 `incident_diagnosis.py`。

## Key facts(读码确认)

- 现有 MCP adapter 全用 **stdlib `urllib`** + `asyncio.to_thread`(`hermes/service_main.py:357-517`),
  **不用 httpx**(`_post_json` 走 `request.urlopen`;`_adapter_timeout` 读 `AIOPS_HERMES_TOOL_TIMEOUT_SECONDS`,缺省 3s)。
  → provider 层复用同套,**不引 httpx/openai/litellm**(ladder rung 3:stdlib,与现有模式同体量取一致的)。
- provider env 接缝已在 deploy 侧备好,实为 **4 个**(`deploy/k8s/configmap.yaml:21-23`
  `AIOPS_MODEL_PROVIDER`/`AIOPS_MODEL_NAME`/`AIOPS_MODEL_BASE_URL`、`deploy/k8s/secret.example.yaml:14`
  `AIOPS_MODEL_API_KEY`)。**无** `AIOPS_MODEL_TIMEOUT_SECONDS`、**无** `AIOPS_AGENT_MAX_TURNS`。
  → timeout 复用现有 `AIOPS_HERMES_TOOL_TIMEOUT_SECONDS`(`hermes/service_main.py:_adapter_timeout`,缺省 3s);
  max_turns 不走 env、不在 provider 层收(循环计数归 child 2)。**deploy 侧零改动**。
- 出境日志规范(`.trellis/spec/hermes-agent/backend/logging-guidelines.md`):stdlib `logging` 作**防御性 fallback**
  合规——`loki_query.py`/`prometheus_query.py` 已用 `logger.warning` 做同类出境/慢查询提示。`logging.warning` 一行
  正是 Issue D "启动打印 base_url + 出境提示"的落点。**不用** `audit_log`(出站点不是 control-plane durable 事件)。
- error-handling 规范:provider 不可达用 `*Error(ValueError)` 带 `code`/`status`(式样见 `approval_service.ApprovalServiceError`)。
  但 provider 在 Hermes 进程内、不对外开 HTTP 路由,故**不映射成 HTTP status**——抛 `ProviderUnavailable(ValueError)`,
  由 child 2 编排捕获后调 `_derive_session_status` 降级(needs_human/partial)。

## Decision: HTTP 用 stdlib urllib,不引 httpx

`requirements-runtime.txt` 虽列 `httpx>=0.27`,但现有 4 个真 adapter 全用 stdlib urllib,无 httpx 实际用法。
provider 层跟现有模式一致(urllib + `asyncio.to_thread` + `_post_json` 范式),`# ponytail: 与现有 _http_tool_adapter 同 stdlib urllib,不引 httpx`。
代价:tool-use 循环手写约 40 行(ADR-0003 §决策4 "SDK 兜底" 与本项目不引 SDK 的张力,已接受)。

## Decision: 模块边界

新文件 `hermes/diagnosis_provider.py` —— 纯函数 + 配置 dataclass + `ScriptedProvider`,放在 Hermes 边界内
(与 `hermes/service_main.py` 同包,可被 child 2 从 `toolsets/incident_diagnosis.py` import)。

## Decision: chat_with_tools 接口

```python
@dataclass
class ProviderConfig:
    base_url: str
    api_key: str
    model: str
    extra_headers: dict[str, str]   # 预留 Authorization 注入位
    max_turns: int
    timeout_s: float
    outgress_to_external: bool      # base_url 解析后是否非 internal

def load_from_env() -> ProviderConfig:   # 读 AIOPS_MODEL_BASE_URL/API_KEY/NAME/PROVIDER(4 个)
                                          # timeout 复用 AIOPS_HERMES_TOOL_TIMEOUT_SECONDS（缺省 3s）
                                          # 解析 base_url host 是否 cluster.local/内网 → outgress_to_external
                                          # logger.warning 出境提示（无论内外网均打 base_url 尾部 + model，出境额外提示）

async def chat_with_tools(cfg, messages, tools, *, max_turns=None) -> ProviderResult:
    # 循环:POST {base_url}/chat/completions 带 tools,解析 tool_calls/finish_reason,
    # 调用方回灌 tool result(返回新的 messages 列表),至无 tool_call 或达 max_turns。
    # 连接异常/超时 → raise ProviderUnavailable(code="provider_unavailable") from exc。
```

`chat_with_tools` 设计为**单步推进**而非内置循环驱动 adapter(child 2 决定 tool 实际派发)——
返回 `ProviderResult`(assistant_message, tool_calls, finish_reason, usage);child 2 收到 tool_calls
后自己调 adapter + `_collect_evidence`,再回灌 messages 调下一轮 `chat_with_tools`。
理由:provider 层不知 adapter / evidence 落库,把循环切在 child 2 边界,职责干净。

## Decision: ScriptedProvider 测试件

`ScriptedProvider`:接受 `list[dict]` 脚本(每轮预期 messages → 返回 assistant+tool_calls/finish),
返回 OpenAI-shape dict,不打网络。放 `hermes/diagnosis_provider.py` 同模块(供 child 2/3 import),
不进 `tests/conftest.py`(避免 import 污染;它是有 API 的可复用测试件而非 fixture)。

## Tradeoffs

- urllib 比 httpx 缺连接池/HTTP2/原生 async——但 provider 每会话几次调用,非热路径;与现有 adapter 一致性优先。
  `# ponytail: urllib 无连接池,provider 每会话调用次数低,高并发需切 httpx`。
- `chat_with_tools` 单步推进把 tool-use 循环逻辑拆给 child 2:边界清晰但 child 2 要写循环 glue,~20 行。
- 出境日志只 `logging.warning`:非 durable。Issue D 要求"可见可审计",stdout→alloy→Loki 已能被采集
  (Issue A 建立),审计诉求由 alloy 落 Loki 满足;不在 audit_log 重复落。
  `# ponytail: 出境日志走 stdout→alloy→Loki 复用 Issue A 采集面,不双写 audit_log`。

## Open items

- `AIOPS_MODEL_TIMEOUT_SECONDS` env 是否已在 configmap:**未在 :21-24 见,需 implement 时确认**;无则沿用
  `AIOPS_HERMES_TOOL_TIMEOUT_SECONDS` 缺省逻辑或新增 env(后者需 configmap 侧加,但 deploy 侧改动应避免 → 复用缺省)。
- OpenAI-compatible `tool_calls` 解析的兼容字段名(`tool_calls`/`function`/`arguments` JSON 字符串):implement 时按
  OpenAI spec 对齐,`ScriptedProvider` 模拟同 shape。
- `outgress_to_external` 判据 implement 时定:host 不以 `.svc.cluster.local`/`.local` 结尾视为 external;
  内部 `model-service.default.svc.cluster.local` 视为 internal。出境提示两者都打 base_url 尾 + model,external 额外打提示词。

## Rollout / Rollback shape

- 新增 `hermes/diagnosis_provider.py` + `tests/test_diagnosis_provider.py`。零 schema、零 deploy、零现有文件改动。
- rollback:删上述两文件即恢复,不动其它。
- 不激活 child 2 前,本 child 产出无运行时调用方(纯库 + 测试),独立可验证。