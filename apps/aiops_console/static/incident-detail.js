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
    timelineList: document.getElementById("timeline-list"),
    evidenceGrid: document.getElementById("evidence-grid"),
    evidenceCount: document.getElementById("evidence-count"),
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
    renderActions(data.actions || []);
    renderAudit(data.audit || {});
  }

  function renderIncident(incident) {
    const labels = incident.source && incident.source.labels ? incident.source.labels : {};
    nodes.title.textContent = incident.title || "Incident detail";
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
      ["Raw evidence", permissions.can_view_raw_evidence],
      ["Cost", permissions.can_view_cost],
      ["Approve", permissions.can_approve]
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
      value.textContent = allowed ? "Allowed" : "Blocked";

      item.append(name, value);
      nodes.accessList.appendChild(item);
    });

    const reason = permissions.blocked_reason || "No blocked reason returned by Gateway.";
    nodes.accessBlockedReason.textContent = `blocked_reason: ${reason}`;
  }

  function renderDiagnosis(diagnosis) {
    if (!diagnosis) {
      setStatus(nodes.diagnosisStatus, "empty");
      nodes.diagnosisSummary.textContent = "No diagnosis session has been persisted for this incident yet.";
      nodes.diagnosisSession.textContent = "-";
      nodes.diagnosisConfidence.textContent = "-";
      nodes.diagnosisRootCause.textContent = "No conclusion";
      nodes.diagnosisAlert.textContent = "No data state: timeline and audit remain visible while diagnosis and evidence panels stay empty.";
      return;
    }

    setStatus(nodes.diagnosisStatus, diagnosis.status);
    nodes.diagnosisSummary.textContent = diagnosis.summary || "No summary available.";
    nodes.diagnosisSession.textContent = diagnosis.session_id || "-";
    nodes.diagnosisConfidence.textContent = formatConfidence(valuePath(diagnosis, "root_cause.confidence"));
    nodes.diagnosisRootCause.textContent = valuePath(diagnosis, "root_cause.statement") || "-";

    if (diagnosis.status === "failed") {
      const failure = diagnosis.failure || {};
      nodes.diagnosisAlert.textContent = `Diagnosis failed: ${failure.message || "No conclusion was produced."}`;
      return;
    }
    if (diagnosis.status === "partial") {
      const missing = (diagnosis.missing_evidence || []).join(", ") || "unknown";
      nodes.diagnosisAlert.textContent = `Partial state: available evidence is shown and missing evidence is marked (${missing}).`;
      return;
    }
    nodes.diagnosisAlert.textContent = "Conclusion is summarized only. Full reasoning traces are intentionally hidden.";
  }

  function renderTimeline(timeline) {
    nodes.timelineList.replaceChildren();
    if (!timeline.length) {
      nodes.timelineList.appendChild(emptyState("No timeline events are available."));
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
      title.append(event.title || event.type || "Timeline event", statusPill(event.status));

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
      nodes.evidenceGrid.appendChild(emptyState("No Prometheus, Loki, K8s, or Topology evidence has been collected."));
      return;
    }

    evidence.forEach((item) => {
      const card = document.createElement("article");
      card.className = "evidence-card";

      const head = document.createElement("div");
      head.className = "evidence-head";
      const kind = document.createElement("div");
      kind.className = "evidence-kind";
      kind.textContent = item.kind || "evidence";
      head.append(kind, statusPill(item.status));

      const summary = document.createElement("p");
      summary.className = "summary-text";
      summary.textContent = item.summary || "No summary available.";

      const query = document.createElement("div");
      query.className = "query-text";
      query.textContent = item.query && item.query.display ? item.query.display : "No query display.";

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

  function renderActions(actions) {
    nodes.actionsList.replaceChildren();
    if (!actions.length) {
      nodes.actionsList.appendChild(emptyState("No action proposal is available for this incident."));
      return;
    }

    actions.forEach((action) => {
      const row = document.createElement("article");
      row.className = "action-row";

      const body = document.createElement("div");
      const title = document.createElement("div");
      title.className = "action-title";
      title.textContent = action.summary || "Action proposal";
      const note = document.createElement("div");
      note.className = "readonly-note";
      note.textContent = `risk=${action.risk_level || "unknown"} approval_required=${Boolean(action.approval_required)} approval=${action.approval_id || "none"}`;
      body.append(title, note);

      const badge = statusPill(action.execution_enabled ? "enabled" : "readonly");
      badge.textContent = "read-only";

      row.append(body, badge);
      nodes.actionsList.appendChild(row);
    });
  }

  function renderAudit(audit) {
    setStatus(nodes.auditStatus, audit.status || "empty");
    nodes.auditSummary.textContent = audit.summary || "No audit summary is available.";
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
      return "not recorded";
    }
    const date = new Date(value);
    if (Number.isNaN(date.getTime())) {
      return value;
    }
    return date.toISOString().replace("T", " ").replace(".000Z", "Z");
  }

  scenarioButtons.forEach((button) => {
    button.addEventListener("click", () => setScenario(button.dataset.scenario));
  });
  setScenario(new URLSearchParams(window.location.search).get("scenario") || "complete");
})();
