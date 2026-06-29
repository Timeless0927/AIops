# Diagnosis provider layer + outgress logging

Parent: `06-29-adr0003-diagnosis-brain`。本 child 是 child 2(tool-use 重写)的硬前置。

## Goal

在 `hermes/` 新增一个 OpenAI-compatible 的 provider 调用层(纯 httpx,不引 SDK),提供四路证据 tool-use 循环驱动能力 +
provider 不可达降级 + 启动出境日志(ADR-0005 Issue D)。产出可注入的 `ScriptedProvider` 测试件供 child 2/3 复用。

## Requirements

- `hermes/diagnosis_provider.py`:纯函数 + httpx。`async def chat_with_tools(base_url, api_key, model, messages, tools, *, max_turns, timeout)`
  发起 `POST {base_url.rstrip("/")}/chat/completions`,解析 `tool_calls`/`finish_reason`,每步 tool result 回灌 messages,
  循环至无 tool_call 或达 max_turns。retry/timeout 用 httpx 原生(不引 openai/litellm)。
- env 读 `AIOPS_MODEL_BASE_URL`/`AIOPS_MODEL_API_KEY`/`AIOPS_MODEL_NAME`/`AIOPS_MODEL_PROVIDER`/`AIOPS_AGENT_MAX_TURNS`
  (configmap:21-24 + secret.example:14)。提供 cfg dataclass + `load_from_env()`。
- 出境日志:`load_from_env()` 时 `logging.warning("diagnosis provider outgress: base_url=%s ... model=%s — 确认在数据出境允许范围内")`
  一行,可见可审计。
- provider 不可达(连接异常/超时)抛 `ProviderUnavailable`,由 child 2 编排捕获走 `_derive_session_status` 降级。降级不崩溃。
- 测试件:`ScriptedProvider`(prompt→tool_call 回答脚本,返回 OpenAI-shape dict),不依赖真 model-service,
  供 child 2/3 注入。放在 `hermes/diagnosis_provider.py` 同模块或 `tests/` conftest。

## Constraints

- `requirements-runtime.txt` 只有 `httpx>=0.27`,**不新增依赖**。tool-use 循环手写(~40 行,厂商 SDK 不引入,
  与 ADR-0003 §决策 4/5 张力已接受)。
- 不建跨厂商抽象(LiteLLM 禁),单点配置接缝。
- 不触 `toolsets/incident_diagnosis.py`(child 2 的范围)。

## Acceptance Criteria

- [ ] `pytest tests/test_diagnosis_provider.py -q` 绿:覆盖 (a) 脚本 tool-use 跑通→final diagnosis;
      (b) httpx 超时→`ProviderUnavailable`;(c) `load_from_env()` 出境日志含 base_url。
- [ ] `grep -rn 'litellm\|openai' hermes/` 为空。
- [ ] `ScriptedProvider` 可被无关测试 import 注入(单测自验)。
- [ ] 不修改 `incident_diagnosis.py`;现有诊断测试 0 回归。

## Verification

`pytest tests/test_diagnosis_provider.py tests/test_incident_diagnosis.py tests/test_incident_evidence_collection.py -q`。
rollback:删 `hermes/diagnosis_provider*.py` + 对应测试即恢复,不触其它文件。