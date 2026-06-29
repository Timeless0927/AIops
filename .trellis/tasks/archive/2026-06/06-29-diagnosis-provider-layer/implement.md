# Implement: Diagnosis provider layer + outgress logging

> Active task: `06-29-diagnosis-provider-layer`(执行前先 `task.py start` 该 task)。
> 依赖:无。纯新增,不改现有文件。

## Step 0 — 读 child 1 PRD/确认 env

- [ ] 时确认 `AIOPS_MODEL_*` 在 configmap 的确切清单与 `AIOPS_MODEL_TIMEOUT_SECONDS` 是否存在
      (`grep -n AIOPS_MODEL deploy/k8s/configmap.yaml deploy/k8s/base/configmap.yaml`)。缺 timeout env 则沿用
      `AIOPS_HERMES_TOOL_TIMEOUT_SECONDS` 缺省逻辑(`hermes/service_main.py:_adapter_timeout`)。
- [ ] 确认 `_post_json`(`hermes/service_main.py:448`)+ `_adapter_timeout` 可作为 provider HTTP 调用范式参考
      (provider 在 `hermes/diagnosis_provider.py` 独立写一个最小 `_chat_post`,不直接 import service_main 避免循环)。

## Step 1 — `hermes/diagnosis_provider.py` 配置 + 出境日志

- [ ] `ProviderConfig` dataclass(base_url/api_key/model/extra_headers/max_turns/timeout_s/outgress_to_external)。
- [ ] `load_from_env()` 读 env(见 Step 0 清单),解析 base_url host 判 `outgress_to_external`
      (host 不以 `.svc.cluster.local`/`.local` 结尾 → True)。
- [ ] 模块级 `logger = logging.getLogger(__name__)`;`load_from_env()` 末尾
      `logger.warning("diagnosis provider outgress: base_url=%s model=%s %s", base_url_tail, model, hint)`
      (external 加 "— 数据出境,确认在允许范围内";internal 仅记 base_url 尾 + model)。
      **不**记 api_key 全文,只 `api_key[-4:]` 尾部。
- [ ] `ProviderUnavailable(ValueError)`:带 `code="provider_unavailable"`,chained `from exc`。

## Step 2 — `chat_with_tools`(单步推进)

- [ ] `async def chat_with_tools(cfg, messages, tools, *, max_turns=None) -> ProviderResult`:
      `POST {cfg.base_url.rstrip('/')}/chat/completions`,body 含 `model/messages/tools/tool_choice="auto"`
      + `cfg.extra_headers` 注入 `Authorization: Bearer <api_key>`。
      用 stdlib `urllib.request` + `asyncio.to_thread`(套 `_post_json` 范式;超时 `cfg.timeout_s`)。
- [ ] 解析响应:`assistant_message`(`content`/`tool_calls`/`role`)、`finish_reason`、`usage`
      (`prompt_tokens`/`completion_tokens`)。`tool_calls[].function.arguments` 是 JSON 字符串,parse 成 dict 回供。
- [ ] 异常路径:`urllib.error.URLError`/`TimeoutError`/JSON 解析失败 → `raise ProviderUnavailable(...) from exc`。
      **不**在 provider 层吞异常降级——降级是 child 2 编排的职责(用 `_derive_session_status`)。
- [ ] `max_turns` 仅作防呆上限校验(本层不做循环驱动,循环在 child 2);超限抛 `ProviderUnavailable(code="max_turns_exceeded")`?
      → 否:本层单步,不在本层校验 max_turns;留 child 2 循环里计数。本层 `max_turns` 参数保留为透传/未用占位。
      implement 时若确认无用干脆不收该参(避免无用参数)。**默认:不收 max_turns,循环计数归 child 2。**

## Step 3 — `ScriptedProvider` 测试件

- [ ] `ScriptedProvider`:构造收 `list[dict]`(每条 = 预期响应 dict 或可调 callable(messages)->dict),
      暴露 `async chat_with_tools(messages, tools, **kw) -> ProviderResult` 同真 provider 接口,
      逐条 pop 脚本返回。记录调用历史 `messages_history` 供断言 tool 回灌序列。
- [ ] 不打网络、不读 env,纯内存。

## Step 4 — `tests/test_diagnosis_provider.py`

按 `.trellis/spec/hermes-agent/backend/testing.md` 的 conftest async runner + 不依赖外部服务的模式。

- [ ] `test_chat_with_tools_single_turn_final`:ScriptedProvider 脚本 = 一轮直接 finish_reason="stop",
      断言 `ProviderResult.finish_reason=="stop"`、`assistant_message` 有 content。
- [ ] `test_chat_with_tools_returns_tool_call_for_reinject`:脚本 = 一轮返回 tool_call,断言
      `tool_calls[0].function.name`/`arguments`(已 parse 成 dict),供 child 2 回灌。
- [ ] `test_provider_unavailable_on_timeout`:monkeypatch urlopen 抛 `URLError`(timeout),
      断言 `chat_with_tools` raises `ProviderUnavailable` 且 `code=="provider_unavailable"`,
      `__cause__` 是 URLError(chained)。
- [ ] `test_load_from_env_outgress_log_external`:设 `AIOPS_MODEL_BASE_URL` 指向外部 host、
      `caplog` 捕获 logger.warning,断言含 base_url 尾、model、"出境"提示词、api_key 仅尾部 4 位、**不含** api_key 全文。
- [ ] `test_load_from_env_outgress_log_internal`:base_url 指 `model-service.default.svc.cluster.local`,
      断言日志含 base_url 尾 + model,无"出境"警示词(或措辞为 internal)。
- [ ] `test_scripted_provider_records_history`:两轮(tool_call→回灌→stop),断言 history 含两轮 messages 序列。

## Step 5 — 验证 + 回归

- [ ] `pytest tests/test_diagnosis_provider.py -q` 全绿。
- [ ] `grep -rn 'litellm\|openai' hermes/` 为空(确认未引 SDK)。
- [ ] `pytest tests/test_incident_diagnosis.py tests/test_incident_evidence_collection.py tests/test_hermes_diagnosis_service.py -q`
      0 回归(本 child 未改这些文件,确认 import 不冲突:`hermes/diagnosis_provider` 不 import `service_main`)。
- [ ] 全仓 `python3 -c "import hermes.diagnosis_provider"` import 自洽。

## Review gates / Rollback points

- Step 1-3 完成且 pytest 绿 → commit「feat(hermes): add OpenAI-compatible diagnosis provider layer + outgress logging (ADR-0003 child 1)」。
- 回滚:删 `hermes/diagnosis_provider.py` + `tests/test_diagnosis_provider.py`,无任何现有文件残留引用(本 child 被 consumer 只在 child 2 之后)。

## Notes

- urllib 单步推进:`# ponytail: 与现有 _http_tool_adapter 同 stdlib urllib,不引 httpx;高并发需切 httpx`。
- 出境日志:`# ponytail: 出境日志走 stdout→alloy→Loki 复用 Issue A 采集面,不双写 audit_log`。
- 不收无用 `max_turns` 参数(避免 ladder rung 1:无请求抽象的反面——无请求参数)。
- `chat_with_tools` 不内置 tool 派发循环:循环 glue 归 child 2,provider 层不知 adapter/evidence 落库,边界干净。