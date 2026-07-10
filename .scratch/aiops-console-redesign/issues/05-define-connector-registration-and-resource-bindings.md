Type: grilling
Status: resolved

## Question

What is the first-version resource model for Connector-registered clusters, services, teams, namespaces, and user permissions?

## Context

The user rejected free-text cluster/user setup. The agreed direction:

- Connector registers cluster identity and runtime status.
- Admins can modify cluster display name, environment, owner team, and policy.
- Admins cannot manually mark a cluster online or forge Connector status.
- Users can belong to multiple teams.
- Teams own multiple services.
- Services can deploy to multiple clusters/namespaces.
- Permissions bind users/teams to existing resources, rather than storing loose cluster/service/team strings on a user record.

Resolve the minimal data model and admin workflows needed by the redesigned Console.

## Answer

The first-version resource model is binding-first. The Console must not let users create disconnected free-text clusters, services, teams, or user scopes.

### Cluster Registration

Admins cannot manually create clusters.

Rules:

- `cluster_id` is created only by Connector registration.
- The Console must not expose an "添加集群" flow.
- Admins may edit only business/configuration fields on already-registered clusters:
  - `display_name`
  - `environment`
  - `owner_team`
  - policy switches or notes
- Connector heartbeat/runtime reporting owns:
  - online/offline/degraded state
  - `connector_id`
  - last heartbeat
  - runtime failure summary
- Admins cannot mark a cluster online/offline manually.
- If no Connector has registered, the Console shows "暂无已注册集群" and does not provide free-text creation.

The old config/runtime split is directionally reusable, but cluster config upsert must not create a new `cluster_id` from arbitrary Console input.

### Service, Team, And Deployment Target

Use discovery plus confirmation:

Source priority:

1. Connector reports Kubernetes workload/service discovery.
2. CMDB/service catalog returns owner/team.
3. Alert labels provide temporary clues.
4. Admin confirms or corrects the binding.

Core model:

```text
Team owns Service
Service deployed_to DeploymentTarget
DeploymentTarget = cluster + namespace + workload/service identity
Incident targets DeploymentTarget
```

Terms:

- `Team`: service owner group, for example `payments`.
- `Service`: business service identity, for example `checkout`.
- `DeploymentTarget`: a concrete deployment location, for example `prod-a/default/checkout`.

Rules:

- A Team can own many Services.
- A Service can have many DeploymentTargets.
- An Incident should bind to one DeploymentTarget when possible.
- Admins can confirm or correct bindings discovered from Connector/CMDB/labels.
- Admins cannot create a completely nonexistent Service binding without a discovered resource or CMDB/service-catalog record.

### User Permissions

Users should not store free-text `cluster/service/team/namespace` scope lists as their primary permission model.

Use membership and binding:

```text
zhangsan -> team_operator -> payments
payments -> owns -> checkout, payment-api
checkout -> deployed_to -> prod-a/default/checkout
```

First-version role bindings:

- `team_member`: can view incidents/evidence for services owned by the team.
- `team_operator`: can view and start/continue investigations for team-owned services.
- `team_approver`: can confirm scoped actions for team-owned services when policy allows.
- `platform_admin`: can manage platform configuration and resource bindings; does not bypass action policy.
- `platform_auditor`: can read audit/resource state according to audit policy; no mutation.

Permission resolution:

- User -> Team memberships.
- Team -> owned Services.
- Service -> DeploymentTargets.
- Incident -> DeploymentTarget.

If the chain does not connect, the user is not authorized unless they have a platform role that explicitly allows access.

### Unbound Resources

If an alert cannot bind to a DeploymentTarget:

- The incident still appears in `/incidents`.
- The left column shows "资源未绑定".
- Read-only investigation is allowed only within the scope that alert labels can safely determine.
- Execution-class suggested actions are disabled.
- The right column shows "需要确认资源归属".
- Admin/operator can associate the incident with an existing Service/DeploymentTarget.
- If it is truly a new service, it must first be discovered by Connector or imported from CMDB/service catalog, then confirmed. The event page must not invent a new service.

### First-Version Admin Surface

Do not build a full management backend in the first Console redesign.

First version includes only:

- Resource binding status in the incident left column.
- For unbound incidents: "关联到已有服务/部署目标".
- Read-only cluster registration details: Connector identity, heartbeat, display name, environment, owner team.
- A lightweight admin edit surface for registered clusters: `display_name`, `environment`, `owner_team`.

Out of first-version scope:

- Standalone `/clusters`.
- Standalone `/users`.
- Standalone `/settings`.
- Standalone `/policies`.
- Bulk user management.
- Bulk service catalog management.
