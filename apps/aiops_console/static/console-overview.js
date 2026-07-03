(function () {
  "use strict";

  const data = window.AIOPS_CONSOLE_OVERVIEW_FIXTURES || {};
  const summary = data.summary || {};
  const nodes = {
    incidentList: document.getElementById("overviewIncidentList"),
    incidentCount: document.getElementById("incidentCount"),
    toolList: document.getElementById("overviewToolList")
  };

  function render() {
    renderIncidents(summary.incidents || []);
    renderTools(summary.tools || []);
  }

  function renderIncidents(incidents) {
    nodes.incidentList.replaceChildren();
    nodes.incidentCount.textContent = String(incidents.length);
    if (!incidents.length) {
      nodes.incidentList.appendChild(emptyState("No active incidents in this fixture."));
      return;
    }

    incidents.forEach((incident, index) => {
      const row = document.createElement("article");
      row.className = `incident-row${index === 0 ? " active" : ""}`;
      row.dataset.filterSource = "incidents";
      row.dataset.tags = incident.tags || "";

      const body = document.createElement("div");
      const title = document.createElement("div");
      title.className = "incident-title";
      const strong = document.createElement("strong");
      strong.textContent = incident.title || "Untitled incident";
      title.append(strong, statusPill(statusClass(incident.severity), incident.severity));

      const summaryText = document.createElement("p");
      summaryText.textContent = `${incident.service || "unknown service"} - ${incident.status || "unknown status"}`;
      body.append(title, summaryText);

      const meta = document.createElement("div");
      meta.className = "incident-meta";
      [incident.incident_id, incident.age, incident.impact].forEach((value) => {
        const span = document.createElement("span");
        span.textContent = value || "-";
        meta.appendChild(span);
      });

      row.append(body, meta);
      nodes.incidentList.appendChild(row);
    });
  }

  function renderTools(tools) {
    nodes.toolList.replaceChildren();
    if (!tools.length) {
      nodes.toolList.appendChild(emptyState("No Agent tool calls are available."));
      return;
    }

    tools.forEach((tool) => {
      const row = document.createElement("article");
      row.className = "tool-row";

      const body = document.createElement("div");
      const title = document.createElement("div");
      title.className = "tool-title";
      const strong = document.createElement("strong");
      strong.textContent = tool.name || "unknown.tool";
      title.append(strong, statusPill(tool.status, tool.status));

      const input = document.createElement("p");
      input.textContent = `Input: ${tool.query || "not recorded"}.`;
      const observation = document.createElement("p");
      observation.textContent = `Observation: ${tool.observation || "not recorded"}.`;

      const refs = document.createElement("div");
      refs.className = "ref-list";
      refs.append(refChip(tool.ref), refChip(formatDuration(tool.duration_ms)));

      body.append(title, input, observation, refs);
      row.appendChild(body);
      nodes.toolList.appendChild(row);
    });
  }

  function statusPill(status, text) {
    const span = document.createElement("span");
    span.className = `status-pill ${status || "neutral"}`;
    span.textContent = text || "unknown";
    return span;
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

  function statusClass(severity) {
    if (severity === "critical") {
      return "blocked";
    }
    if (severity === "high") {
      return "partial";
    }
    return "neutral";
  }

  function formatDuration(value) {
    return typeof value === "number" ? `${value}ms` : "duration unknown";
  }

  render();
})();
