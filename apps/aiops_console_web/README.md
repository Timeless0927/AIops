# AIOps Console Web

Console Web workspace 独立安装、测试和构建：

```bash
npm ci
npm run dev
npm run build
```

本地开发时，Vite 将相对路径 `/api` 与 `/auth` 代理到 `http://127.0.0.1:18080`。

生产 image 由 `.github/workflows/console-image.yml` 从本 workspace 的 lockfile 构建，以 OCI revision label 保存完整 monorepo commit SHA，并只按 digest 发布。`promote-console` workflow 校验 digest 的 revision label，在受保护的 `production` environment 中显式更新 `deploy/k8s/console`；main build 不会自动部署。

`deploy/k8s/console` 的 edge Ingress 构成浏览器的单一 HTTPS origin：`/` 和 static assets 进入 `aiops-console`，`/api/v1/*` 与 `/auth/*` 直接进入 `aiops-gateway`。Gateway route 禁用 buffering 并允许 long-lived SSE；edge 传递的 HTTPS scheme 保留 first-party secure Cookie 与 CSRF 语义。生产环境需提供 `aiops-console-tls` Secret，并由 ingress-nginx 终止 TLS；Gateway Service 不应单独暴露给浏览器。

初始 Kustomize digest 为全零的不可部署哨兵值，只能由人工 promotion workflow 替换为 registry 中验证过的 digest。生产环境需要配置 `PRODUCTION_KUBECONFIG_B64`、Aliyun registry credentials 和 `production` environment 审批规则。
