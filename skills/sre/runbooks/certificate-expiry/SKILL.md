# 证书过期处理流程

## 场景描述

当集群中的 TLS 证书接近过期或已经过期时，可能引发入口流量失败、服务间握手异常、Webhook 不可用或控制面组件报错。处理时需要先确认具体证书对象、签发链路和自动续期机制是否正常。

## 触发条件

- 告警提示证书剩余有效期低于阈值
- 业务日志出现 TLS 握手失败、证书验证失败
- `cert-manager` 相关告警提示续期失败

## 诊断步骤

1. 查看证书资源和到期时间，确认受影响范围。

```bash
kubectl get certificate -A
kubectl describe certificate <certificate_name> -n <namespace>
kubectl get secret <tls_secret_name> -n <namespace> -o yaml
```

2. 检查 `cert-manager` 组件状态与日志。

```bash
kubectl get pods -n cert-manager
kubectl logs -n cert-manager deploy/cert-manager --tail=200
kubectl logs -n cert-manager deploy/cert-manager-webhook --tail=200
```

3. 检查 `Issuer` 或 `ClusterIssuer` 是否健康，确认签发链路未中断。

```bash
kubectl get issuer,clusterissuer -A
kubectl describe issuer <issuer_name> -n <namespace>
kubectl describe clusterissuer <clusterissuer_name>
```

4. 检查 ACME、DNS、Webhook 或凭据配置问题。

```bash
kubectl get challenges,orders -A
kubectl describe challenge <challenge_name> -n <namespace>
kubectl get secret -n <namespace>
```

```text
prometheus_query(query='certmanager_certificate_expiration_timestamp_seconds')
prometheus_query(query='certmanager_http_acme_client_request_count')
```

## 常见根因

- `cert-manager` 控制器异常，导致续期任务未执行
- `Issuer` 或 `ClusterIssuer` 凭据失效，例如 DNS API Token 过期
- ACME 验证失败，域名解析或 Ingress 配置错误
- TLS Secret 被误删或被手工覆盖
- 系统时间异常导致证书判断失真

## 修复方案

1. `cert-manager` 组件异常：恢复组件并检查控制器日志。

```bash
kubectl rollout restart deployment/cert-manager -n cert-manager
kubectl rollout status deployment/cert-manager -n cert-manager
```

2. `Issuer` 配置错误：修复签发配置或更新凭据。

```bash
kubectl edit issuer <issuer_name> -n <namespace>
kubectl edit clusterissuer <clusterissuer_name>
kubectl edit secret <issuer_secret_name> -n <namespace>
```

3. 手动触发续期或重新签发。

```bash
kubectl annotate certificate <certificate_name> -n <namespace> cert-manager.io/renew-reason=manual
kubectl delete secret <tls_secret_name> -n <namespace>
```

4. Ingress 或域名验证异常：修复域名解析、Ingress 路由或 ACME challenge 暴露。

```bash
kubectl describe ingress <ingress_name> -n <namespace>
kubectl get challenge -A
```

## 验证步骤

1. 确认证书状态已恢复为 `Ready=True`。

```bash
kubectl describe certificate <certificate_name> -n <namespace>
kubectl get certificate -A
```

2. 确认 TLS Secret 已更新，证书有效期延长。

```bash
kubectl get secret <tls_secret_name> -n <namespace> -o yaml
```

3. 复查入口或服务调用是否恢复正常。

```text
prometheus_query(query='certmanager_certificate_expiration_timestamp_seconds{namespace="<namespace>",name="<certificate_name>"}')
```
