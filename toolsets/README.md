# 自定义工具集目录

当前目录用于放置 AIOps SRE 自定义工具模块。

- 保持对 Hermes 核心零侵入
- 新工具按 Hermes registry 规范注册
- 后续计划补充 `k8s_read`、`k8s_write`、`k8s_exec`

## V1 目录边界

`toolsets/` 是 V1 的稳定 facade 和旧入口兼容层。当前已有工具继续保留在这里，避免一次性迁移影响 Hermes 加载；新增模块按职责先放在以下边界内：

- `query_guard.py`：共享查询契约，包括时间窗、limit、服务地址解析和负向校验。
- `audit_log.py`：共享审计 helper 和 Hermes tool registry 入口。
- `prometheus_query.py`、`loki_query.py`：MCP facade，负责调用对应后端并返回统一 success/error payload。
- `k8s_read.py`、`k8s_write.py`、`k8s_exec.py`、`k8s_guard.py`：K8s gateway facade，保留审批、RBAC 和命令护栏。
- remediation / approval / notification 模块：平台 connector 和运行时编排层，继续复用共享审计与 guard。

`runtime/` 承载进程入口和镜像 smoke，例如 `runtime.hermes_gateway` 和 `runtime.image_smoke`。K8s manifests、entrypoint 和镜像构建逻辑保留在 `deploy/`、`Dockerfile.aiops` 和 `.github/workflows/`。

## Packaging 兼容策略

`toolsets` 现在是显式 Python package，镜像内标准导入必须优先解析到仓库内 `/app/toolsets/__init__.py`，避免被 site-packages 中可能存在的同名 `toolsets.py` 抢占。面向 Hermes 旧加载器的直接模块导入仍保持兼容；跨 facade 依赖优先使用相对导入，并保留直接导入 fallback。

平台门禁应至少覆盖：

```bash
python3 -m runtime.image_smoke
```

该 smoke 会验证 `toolsets.loki_query`、`toolsets.query_guard`、`toolsets.audit_log` 标准导入，以及 Loki facade 的 fake backend success、backend unavailable 和 contract negative path。正式候选镜像仍由 GitHub Actions 构建并输出 digest，本地镜像只用于语法和 smoke 预检。
