# Web Setup 投影 owner state 而不拥有第二套配置状态

Status: accepted

Web Setup 是可跳过、可恢复的 Platform Administrator 工作流，不建立 step lifecycle 或永久 `setup_completed`。Diagnosis 拥有唯一 Model Provider configuration 和 encrypted credential，Notification Engine 拥有 Destination/Route，Gateway 拥有 Connector Enrollment、Cluster registration、平台级 skip decision 和统一 Platform Status aggregation；Gateway 不复制其他 owner 的配置。

只有 exact verified configuration revision 能进入真实 Diagnosis 或 Notification path。配置先保存为 unverified，再由实际消费进程测试；变更立即使 verification stale。Secret 只在 owner 边界加密持久化，Gateway 负责 browser authorization、fresh-auth、reason、masked audit 和幂等 request correlation，分布式响应不明记录 `outcome_unknown` 并对账而不自动重试 credential mutation。

多个唯一 Connector/Cluster Enrollment 可以主动连接同一 Gateway，本次 Pilot 只验收一个 Cluster。Gateway 不写 Kubernetes Secret；Operator 把 Web 一次性显示的 credential 和 exact IDs 安装到目标 Cluster。远程 Connector 强制 HTTPS，credential rotation 使用 candidate handoff，并在 unfinished mutation 或 Unknown Outcome 时停止。

缺失 integration 只降级直接依赖能力，不阻塞 Console 或提交无关领域事实。Platform Status 对所有 authenticated User 暴露安全 capability summary，而配置、测试、轮换和 skip 仅属于 Platform Administrator。
