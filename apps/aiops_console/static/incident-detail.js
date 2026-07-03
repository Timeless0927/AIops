(function () {
  "use strict";

  const fixtures = window.AIOPS_INCIDENT_FIXTURES || {};
  const scenarioButtons = Array.from(document.querySelectorAll(".scenario-button"));

  const nodes = {
    title: document.getElementById("incident-title"),
    incidentId: document.getElementById("incident-id"),
    severity: document.getElementById("incident-severity"),
    status: document.getElementById("incident-status"),
    service: document.getElementById("incident-service"),
    sourceAlert: document.getElementById("source-alert"),
    sourceNamespace: document.getElementById("source-namespace"),
    sourceCluster: document.getElementById("source-cluster"),
    sourceTeam: document.getElementById("source-team"),
    accessList: document.getElementById("access-list"),
    accessBlockedReason: document.getElementById("access-blocked-reason"),
    diagnosisStatus: document.getElementById("diagnosis-status"),
    diagnosisSummary: document.getElementById("diagnosis-summary"),
    diagnosisSession: document.getElementById("diagnosis-session"),
    diagnosisConfidence: document.getElementById("diagnosis-confidence"),
    diagnosisRootCause: document.getElementById("diagnosis-root-cause"),
    diagnosisAlert: document.getElementById("diagnosis-alert"),
    diagnosisMarkdown: document.getElementById("diagnosis-markdown"),
    timelineList: document.getElementById("timeline-list"),
    evidenceGrid: document.getElementById("evidence-grid"),
    evidenceCount: document.getElementById("evidence-count"),
    missingList: document.getElementById("missing-list"),
    actionsList: document.getElementById("actions-list"),
    auditStatus: document.getElementById("audit-status"),
    auditSummary: document.getElementById("audit-summary"),
    auditRefs: document.getElementById("audit-refs")
  };

  function setScenario(name) {
    const data = fixtures[name] || fixtures.complete;
    if (!data) {
      return;
    }
    scenarioButtons.forEach((button) => {
      button.setAttribute("aria-pressed", String(button.dataset.scenario === name));
    });
    render(data);
  }

  function render(data) {
    renderIncident(data.incident);
    renderDiagnosis(data.diagnosis);
    renderTimeline(data.timeline || []);
    renderEvidence(data.evidence || []);
    renderMissingEvidence(data.missing_evidence || []);
    renderActions(data.actions || []);
    renderAudit(data.audit || {});
  }

  function renderIncident(incident) {
    const labels = incident.source && incident.source.labels ? incident.source.labels : {};
    nodes.title.textContent = incident.title || "事件详情";
    nodes.incidentId.textContent = incident.incident_id || "-";
    nodes.severity.textContent = incident.severity || "-";
    nodes.status.textContent = incident.status || "-";
    nodes.service.textContent = valuePath(incident, "service.service_name") || "-";
    nodes.sourceAlert.textContent = valuePath(incident, "source.alert_id") || "-";
    nodes.sourceNamespace.textContent = labels.namespace || "-";
    nodes.sourceCluster.textContent = labels.cluster || "-";
    nodes.sourceTeam.textContent = valuePath(incident, "service.owner_team_name") || "-";
    renderAccess(incident.permissions || {});
  }

  function renderAccess(permissions) {
    const items = [
      ["原始证据", permissions.can_view_raw_evidence],
      ["成本", permissions.can_view_cost],
      ["审批", permissions.can_approve]
    ];
    nodes.accessList.replaceChildren();
    items.forEach(([label, allowed]) => {
      const item = document.createElement("div");
      item.className = `access-item ${allowed ? "allowed" : "blocked"}`;

      const name = document.createElement("span");
      name.className = "access-label";
      name.textContent = label;

      const value = document.createElement("span");
      value.className = "access-value";
      value.textContent = allowed ? "允许" : "受限";

      item.append(name, value);
      nodes.accessList.appendChild(item);
    });

    const reason = permissions.blocked_reason || "Gateway 未返回阻塞原因。";
    nodes.accessBlockedReason.textContent = `blocked_reason: ${reason}`;
  }

  function renderDiagnosis(diagnosis) {
    if (!diagnosis) {
      setStatus(nodes.diagnosisStatus, "empty");
      nodes.diagnosisSummary.textContent = "该事件尚未持久化诊断会话。";
      nodes.diagnosisSession.textContent = "-";
      nodes.diagnosisConfidence.textContent = "-";
      nodes.diagnosisRootCause.textContent = "暂无结论";
      nodes.diagnosisAlert.textContent = "无数据状态：时间线和审计仍可见，诊断和证据面板保持为空。";
      nodes.diagnosisMarkdown.textContent = "";
      return;
    }

    setStatus(nodes.diagnosisStatus, diagnosis.status);
    nodes.diagnosisSummary.textContent = diagnosis.summary || "暂无摘要。";
    nodes.diagnosisSession.textContent = diagnosis.session_id || "-";
    nodes.diagnosisConfidence.textContent = formatConfidence(valuePath(diagnosis, "root_cause.confidence"));
    nodes.diagnosisRootCause.textContent = valuePath(diagnosis, "root_cause.statement") || "-";
    nodes.diagnosisMarkdown.textContent = diagnosis.markdown || "";

    if (diagnosis.status === "failed") {
      const failure = diagnosis.failure || {};
      nodes.diagnosisAlert.textContent = `诊断失败：${failure.message || "未生成结论。"}`;
      return;
    }
    if (diagnosis.status === "partial") {
      const missing = (diagnosis.missing_evidence || []).join(", ") || "unknown";
      nodes.diagnosisAlert.textContent = `部分完成：已展示可用证据，并标记缺失证据（${missing}）。`;
      return;
    }
    nodes.diagnosisAlert.textContent = "这里只展示结论摘要。完整推理轨迹会被隐藏。";
  }

  function renderTimeline(timeline) {
    nodes.timelineList.replaceChildren();
    if (!timeline.length) {
      nodes.timelineList.appendChild(emptyState("暂无时间线事件。"));
      return;
    }

    timeline.forEach((event) => {
      const item = document.createElement("li");
      item.className = "timeline-item";

      const time = document.createElement("div");
      time.className = "timeline-time";
      time.textContent = formatTime(event.occurred_at);

      const body = document.createElement("div");
      const title = document.createElement("div");
      title.className = "timeline-title";
      title.append(event.title || event.type || "时间线事件", statusPill(event.status));

      const summary = document.createElement("p");
      summary.className = "timeline-summary";
      summary.textContent = event.summary || "";

      body.append(title, summary, refs(event.refs || {}));
      item.append(time, body);
      nodes.timelineList.appendChild(item);
    });
  }

  function renderEvidence(evidence) {
    nodes.evidenceGrid.replaceChildren();
    nodes.evidenceCount.textContent = String(evidence.length);
    if (!evidence.length) {
      nodes.evidenceGrid.appendChild(emptyState("尚未收集 Prometheus、Loki、K8s 或 Topology 证据。"));
      return;
    }

    evidence.forEach((item) => {
      const card = document.createElement("article");
      card.className = "evidence-card";

      const head = document.createElement("div");
      head.className = "evidence-head";
      const kind = document.createElement("div");
      kind.className = "evidence-kind";
      kind.textContent = item.kind || "证据";
      head.append(kind, statusPill(item.status));

      const summary = document.createElement("p");
      summary.className = "summary-text";
      summary.textContent = item.summary || "暂无摘要。";

      const query = document.createElement("div");
      query.className = "query-text";
      query.textContent = item.query && item.query.display ? item.query.display : "无查询展示。";

      const ref = document.createElement("div");
      ref.className = "ref-list";
      ref.appendChild(refChip(item.result_ref || item.evidence_id));

      card.append(head, summary, query, ref);
      if (item.failure) {
        const failure = document.createElement("div");
        failure.className = "failure-text";
        failure.textContent = `${item.failure.code}: ${item.failure.message}`;
        card.appendChild(failure);
      }
      nodes.evidenceGrid.appendChild(card);
    });
  }

  function renderMissingEvidence(items) {
    nodes.missingList.replaceChildren();
    if (!items.length) {
      nodes.missingList.appendChild(emptyState("Gateway 未报告缺失证据。"));
      return;
    }

    items.forEach((item) => {
      const row = document.createElement("article");
      row.className = "missing-row";

      const body = document.createElement("div");
      const title = document.createElement("div");
      title.className = "action-title";
      title.textContent = item.source_type || item.tool || "缺失证据";

      const note = document.createElement("div");
      note.className = "readonly-note";
      note.textContent = item.reason || "Gateway 未返回原因。";

      body.append(title, note);
      row.append(body, statusPill(valuePath(item, "audit.error_code") ? "failed" : item.status || "empty"));
      nodes.missingList.appendChild(row);
    });
  }

  function renderActions(actions) {
    nodes.actionsList.replaceChildren();
    if (!actions.length) {
      nodes.actionsList.appendChild(emptyState("该事件暂无动作建议。"));
      return;
    }

    actions.forEach((action) => {
      const row = document.createElement("article");
      row.className = "action-row";

      const body = document.createElement("div");
      const title = document.createElement("div");
      title.className = "action-title";
      title.textContent = action.summary || "动作建议";
      const note = document.createElement("div");
      note.className = "readonly-note";
      note.textContent = `risk=${action.risk_level || "unknown"} approval_required=${Boolean(action.approval_required)} approval=${action.approval_id || "none"}`;
      body.append(title, note);

      const badge = statusPill(action.execution_enabled ? "enabled" : "readonly");
      badge.textContent = "只读";

      row.append(body, badge);
      nodes.actionsList.appendChild(row);
    });
  }

  function renderAudit(audit) {
    setStatus(nodes.auditStatus, audit.status || "empty");
    nodes.auditSummary.textContent = audit.summary || "暂无审计摘要。";
    nodes.auditRefs.replaceChildren();
    (audit.refs || []).forEach((item) => nodes.auditRefs.appendChild(refChip(item)));
  }

  function statusPill(status) {
    const span = document.createElement("span");
    setStatus(span, status || "neutral");
    return span;
  }

  function setStatus(node, status) {
    node.className = `status-pill ${status || "neutral"}`;
    node.textContent = status || "unknown";
  }

  function refs(values) {
    const list = document.createElement("div");
    list.className = "ref-list";
    Object.keys(values).forEach((key) => {
      const value = values[key];
      if (Array.isArray(value)) {
        value.forEach((item) => list.appendChild(refChip(`${key}:${item}`)));
      } else if (value) {
        list.appendChild(refChip(`${key}:${value}`));
      }
    });
    return list;
  }

  function refChip(value) {
    const span = document.createElement("span");
    span.className = "ref-chip";
    span.textContent = value || "-";
    return span;
  }

  function emptyState(text) {
    const box = document.createElement("div");
    box.className = "empty-state";
    box.textContent = text;
    return box;
  }

  function valuePath(object, path) {
    return path.split(".").reduce((value, key) => (value && value[key] !== undefined ? value[key] : null), object);
  }

  function formatConfidence(value) {
    if (typeof value !== "number") {
      return "-";
    }
    return `${Math.round(value * 100)}%`;
  }

  function formatTime(value) {
    if (!value) {
      return "未记录";
    }
    const date = new Date(value);
    if (Number.isNaN(date.getTime())) {
      return value;
    }
    return date.toISOString().replace("T", " ").replace(".000Z", "Z");
  }

  function canLoadGatewayProcess(params) {
    return window.location.protocol !== "file:" && Boolean(params.get("incident_id")) && Boolean(token());
  }

  function gatewayBaseUrl() {
    const params = new URLSearchParams(window.location.search);
    return (params.get("api_base") || window.sessionStorage.getItem("aiopsGatewayBaseUrl") || "").replace(/\/$/, "");
  }

  function gatewayProcessUrl(incidentId) {
    return `${gatewayBaseUrl()}/api/incidents/${encodeURIComponent(incidentId)}/diagnosis-process`;
  }

  function token() {
    return window.sessionStorage.getItem("aiopsConsoleToken");
  }

  function loadGatewayProcess(incidentId) {
    return fetch(gatewayProcessUrl(incidentId), {
      method: "GET",
      headers: {"Accept": "application/json", "Authorization": `Bearer ${token()}`},
      credentials: "same-origin"
    })
      .then((response) => {
        if (!response.ok) {
          throw new Error(`Gateway returned ${response.status}`);
        }
        return response.json();
      })
      .then((payload) => {
        if (!payload || !payload.process) {
          throw new Error("Gateway response missing process payload");
        }
        render(payload.process);
      });
  }

  function loadInitialData() {
    const params = new URLSearchParams(window.location.search);
    if (!canLoadGatewayProcess(params)) {
      setScenario(params.get("scenario") || "complete");
      return;
    }

    loadGatewayProcess(params.get("incident_id"))
      .catch((error) => {
        setScenario(params.get("scenario") || "failed");
        nodes.diagnosisAlert.textContent = `Gateway 加载失败：${error.message}`;
      });
  }

  scenarioButtons.forEach((button) => {
    button.addEventListener("click", () => setScenario(button.dataset.scenario));
  });
  loadInitialData();
})();
