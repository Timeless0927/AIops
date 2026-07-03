(function () {
  "use strict";

  const data = window.AIOPS_CONSOLE_OVERVIEW_FIXTURES || {};
  const summary = data.summary || {};
  const nodes = {
    incidentList: document.getElementById("overviewIncidentList"),
    incidentCount: document.getElementById("incidentCount"),
    toolList: document.getElementById("overviewToolList"),
    sessionUser: document.getElementById("sessionUser"),
    sessionStatus: document.getElementById("sessionStatus"),
    loginUsername: document.getElementById("loginUsername"),
    loginPassword: document.getElementById("loginPassword"),
    loginButton: document.getElementById("loginButton"),
    logoutButton: document.getElementById("logoutButton"),
    loginMessage: document.getElementById("loginMessage")
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
      const link = document.createElement("a");
      link.href = `./incident-detail.html?incident_id=${encodeURIComponent(incident.incident_id || "")}`;
      link.textContent = incident.title || "Untitled incident";
      title.append(link, statusPill(statusClass(incident.severity), incident.severity));

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

  function setSession(actor) {
    nodes.sessionUser.textContent = actor && actor.username ? actor.username : "fixture";
    nodes.sessionStatus.textContent = actor ? "live" : "fixture";
    nodes.sessionStatus.className = `status-pill ${actor ? "succeeded" : "neutral"}`;
  }

  function token() {
    return window.sessionStorage.getItem("aiopsConsoleToken");
  }

  function loadLiveIncidents() {
    if (window.location.protocol === "file:" || !token()) {
      setSession(null);
      render();
      return Promise.resolve();
    }
    return fetch("/api/incidents/active", {
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
        renderIncidents(payload.incidents || []);
        nodes.loginMessage.textContent = `Loaded ${payload.incidents ? payload.incidents.length : 0} live incidents from Gateway.`;
      });
  }

  function login() {
    if (window.location.protocol === "file:") {
      nodes.loginMessage.textContent = "Open through Gateway /console/ to log in.";
      return;
    }
    fetch("/auth/login", {
      method: "POST",
      headers: {"Accept": "application/json", "Content-Type": "application/json"},
      credentials: "same-origin",
      body: JSON.stringify({
        username: nodes.loginUsername.value,
        password: nodes.loginPassword.value
      })
    })
      .then((response) => {
        if (!response.ok) {
          throw new Error(`Login failed ${response.status}`);
        }
        return response.json();
      })
      .then((payload) => {
        window.sessionStorage.setItem("aiopsConsoleToken", payload.token);
        setSession(payload.actor || {});
        return loadLiveIncidents();
      })
      .catch((error) => {
        nodes.loginMessage.textContent = error.message;
      });
  }

  function logout() {
    window.sessionStorage.removeItem("aiopsConsoleToken");
    setSession(null);
    renderIncidents(summary.incidents || []);
    nodes.loginMessage.textContent = "Logged out. Fixture incidents are shown.";
  }

  nodes.loginButton.addEventListener("click", login);
  nodes.logoutButton.addEventListener("click", logout);
  render();
  loadLiveIncidents().catch((error) => {
    setSession(null);
    nodes.loginMessage.textContent = `${error.message}. Fixture incidents are shown.`;
  });
})();
