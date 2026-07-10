# AIOPS Web

Console Web frontend for AIOps.

```bash
npm ci
npm run dev
npm run build
```

Gateway lives in the same monorepo under `apps/aiops_k8s_gateway`. During local development, Vite proxies `/api` and `/auth` to `http://127.0.0.1:18080`.
