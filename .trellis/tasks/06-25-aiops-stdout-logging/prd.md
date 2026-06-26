# AIOps services emit stdout lifecycle logs

## Background / Problem

ADR-0005 Issue A `dev-external` smoke 中,6 个 AIOps 核心 pod 的 `kubectl logs` 全空。
Loki evidence 经 alloy 抓 pod stdout 采集,采不到不存在的 stdout → Loki `namespace=aiops-dev`
只有冒烟 pod `aiops-alertmanager-smoke` → logs evidence `line_count:0`。详见
`.trellis/spec/deploy/dev-external-observability-contract.md` §5 Bad case 1 与
`.trellis/spec/hermes-agent/backend/logging-guidelines.md`「stdout is the Loki collection surface」段。

## Root Cause(已读码确认)

所有 6 个服务(gateway/connector/hermes/mcp-prometheus/mcp-loki/mcp-topology)的 HTTP handler
都继承 `apps/service_http.py:33 JsonHandler(BaseHTTPRequestHandler)`。`JsonHandler.log_message`
(`apps/service_http.py:38-39`)是一行空 `return`,把 `BaseHTTPRequestHandler` 自带的
access-log(默认写 stderr,每请求一行 `"<addr> - - <ts> <method> <path> <status>"`)
完全吞掉。除此之外全服务无任何 `print` / `sys.stdout.write` / 配置过的 `logging` handler。
结论:**一句话根因 = access-log 被禁,且无任何 stdout 兜底**。

6 个服务的 Handler 全部走这一个基类,无任何子类再覆盖 `log_message`:

- `apps/aiops_k8s_gateway/main.py:205 GatewayHandler(JsonHandler)`
- `hermes/service_main.py:27 HermesServiceHandler(JsonHandler)`
- `apps/cluster_connector/main.py:82 ConnectorHandler(JsonHandler)`
- `apps/observability_http.py:50 ObservabilityHandler(JsonHandler)`(mcp-prometheus/loki/topology 共用)

**因此修复点是单点:** 改 `JsonHandler.log_message` 一处,6 个服务同时获得每请求一行 stdout 日志。
不必逐服务改。

## Requirements

1. 每个能被 alloy 抓到的请求,每个 AIOps 服务至少产生一行**写到 stdout** 的日志,覆盖
   request/diagnosis lifecycle。最小可验收面 = 每个被请求过的 HTTP handler 落一行访问日志。
   生命周期更深的事件(incident 创建、Hermes adapter 调用、observation status、session end)
   可选加,不阻塞 PRD AC;AC 只要求「不为空且可被 Loki 查到」。
2. 不破坏 `audit_log`(SQLite WAL durable)与 `incident_events`(per-incident timeline)两路
   durable channel —— stdout 是 alloy/Loki 采集面,**与 durable channel 并存不替代**
   (`logging-guidelines.md` 已写)。
3. 不引入新依赖。`logging` stdlib 即可,不引第三方结构化日志库。
4. 不泄露敏感值:不把 Authorization、`AIOPS_GATEWAY_WRITEBACK_SECRET`、session token、LDAP 密码
   写进 stdout(与 `logging-guidelines.md` Common mistakes 一致)。
5. 保持「access log 默认禁」的设计意图不回退 —— 不能简单删掉 `log_message` 覆盖让
   `BaseHTTPRequestHandler` 回到默认 stderr 行;改成受控的 stdout 访问日志,而非放开噪音。

## Implementation Approach(用于规划,不是验收)

- 在 `apps/service_http.py` 单文件改动:让 `JsonHandler.log_message` 写一行受控访问日志到
  stdout(用 stdlib `logging` 配一个 StreamHandler(stdout),或直接 `sys.stdout.write` + flush;
  handler 每次 request 由 stdlib 自动调用)。行内含 service 名、method、path、status、
  request_id(若 handler 上有)。不含 body、不含敏感头。
- stdout 而非 stderr:spec 显式要求 stdout 是 collection surface。alloy 实际抓 stdout+stderr,
  但为对齐 spec 与最小歧义,显式 `logging.StreamHandler(sys.stdout)`。
- 复用 `_request_id` 已有提取逻辑(gateway `main.py:52`,各服务各自有透传);若基类拿不到
  request_id,日志行退化为不带 request_id,不阻塞 AC。

## Acceptance Criteria

- [ ] `apps/service_http.py` 中 `JsonHandler.log_message` 不再静默 `return`,改为每请求写一行
      stdout 访问日志;`grep -n "def log_message" apps/service_http.py` 显示实现非空。
- [ ] 单元/自检:`ThreadingHTTPServer` + `JsonHandler` 子类发起一次 `/healthz` 请求后,进程
      stdout 至少一行且不含 `replace-me`/secret 占位串(最小 `__main__` 自检或一条
      `test_*` 断言抓到日志行)。
- [ ] 集群验证(部署 dev-external 后):对 6 个 `deploy/aiops-<svc>` 任取一个,
      `kubectl -n aiops-dev logs <pod>` 在打过一次请求后**非空**(README `aiops-health-smoke`
      触发后即可)。
- [ ] Loki 采集验证:`kubectl -n loki` 对 Loki 发起 `query_range {namespace="aiops-dev"}` 类
      查询,结果含至少一个 AIOps 服务 pod 的日志行(不仅 smoke pod)。
- [ ] 既不回退 `audit_log`/`incident_events` durable 写入,也不在 stdout 复述敏感值;现有
      `tests/` 不新增失败。

## Out of Scope

- 不加 `/metrics` 端点(那是 `06-25-aiops-dev-servicemonitor` child)。
- 不改 Loki/alloy 部署侧采集配置。
- 不为每条 lifecycle 单独设计结构化 schema —— AC 只要「非空可被 Loki 抓到」,不追求花式字段。
- 不动 Console/Feishu 下游。