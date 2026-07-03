(function () {
  "use strict";

  const data = window.AIOPS_CONSOLE_OVERVIEW_FIXTURES || {};
  const summary = data.summary || {};
  const nodes = {
    incidentList: document.getElementById("overviewIncidentList"),
    incidentCount: document.getElementById("incidentCount"),
    toolList: document.getElementById("overviewToolList"),
    notificationList: document.getElementById("overviewNotificationList"),
    sessionUser: document.getElementById("sessionUser"),
    sessionStatus: document.getElementById("sessionStatus"),
    loginUsername: document.getElementById("loginUsername"),
    loginPassword: document.getElementById("loginPassword"),
    gatewayBaseUrl: document.getElementById("gatewayBaseUrl"),
    loginButton: document.getElementById("loginButton"),
    logoutButton: document.getElementById("logoutButton"),
    loginMessage: document.getElementById("loginMessage")
  };

  function render() {
    renderIncidents(summary.incidents || []);
    renderTools(summary.tools || []);
    renderNotifications(summary.notifications || []);
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
      summaryText.textContent = `${incident.service || "未知服务"} - ${incident.status || "未知状态"}`;
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
      input.textContent = `输入：${tool.query || "未记录"}。`;
      const observation = document.createElement("p");
      observation.textContent = `观察：${tool.observation || "未记录"}。`;

      const refs = document.createElement("div");
      refs.className = "ref-list";
      refs.append(refChip(tool.ref), refChip(formatDuration(tool.duration_ms)));

      body.append(title, input, observation, refs);
      row.appendChild(body);
      nodes.toolList.appendChild(row);
    });
  }

  function renderNotifications(notifications) {
    nodes.notificationList.replaceChildren();
    if (!notifications.length) {
      nodes.notificationList.appendChild(emptyState("No notification deliveries are available."));
      return;
    }

    notifications.forEach((notification) => {
      const row = document.createElement("article");
      row.className = "notification-row";

      const title = document.createElement("div");
      title.className = "tool-title";
      const strong = document.createElement("strong");
      strong.textContent = notification.notification_type || "notification";
      title.append(strong, statusPill(notification.delivery_status, notification.delivery_status));

      const summary = document.createElement("p");
      summary.textContent = `${notification.target || "目标未知"} - ${notification.reason || "状态已记录"}。`;

      const refs = document.createElement("div");
      refs.className = "ref-list";
      refs.append(refChip(notification.incident_id), refChip(notification.id));

      row.append(title, summary, refs);
      nodes.notificationList.appendChild(row);
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
    return typeof value === "number" ? `${value}ms` : "耗时未知";
  }

  function setSession(actor) {
    nodes.sessionUser.textContent = actor && actor.username ? actor.username : "fixture";
    nodes.sessionStatus.textContent = actor ? "live" : "fixture";
    nodes.sessionStatus.className = `status-pill ${actor ? "succeeded" : "neutral"}`;
  }

  function initialGatewayBaseUrl() {
    const params = new URLSearchParams(window.location.search);
    return params.get("api_base") || window.sessionStorage.getItem("aiopsGatewayBaseUrl") || "";
  }

  function gatewayBaseUrl() {
    return nodes.gatewayBaseUrl.value.trim().replace(/\/$/, "");
  }

  function apiUrl(path) {
    return `${gatewayBaseUrl()}${path}`;
  }

  function saveGatewayBaseUrl() {
    const value = gatewayBaseUrl();
    if (value) {
      window.sessionStorage.setItem("aiopsGatewayBaseUrl", value);
      return;
    }
    window.sessionStorage.removeItem("aiopsGatewayBaseUrl");
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
    return fetch(apiUrl("/api/incidents/active"), {
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
        nodes.loginMessage.textContent = `已从 Gateway 加载 ${payload.incidents ? payload.incidents.length : 0} 个真实事件。`;
      });
  }

  function login() {
    if (window.location.protocol === "file:") {
      nodes.loginMessage.textContent = "请通过 HTTP 服务访问页面后再登录。";
      return;
    }
    saveGatewayBaseUrl();
    fetch(apiUrl("/auth/login"), {
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
    nodes.loginMessage.textContent = "已退出。当前显示 fixture 事件。";
  }

  nodes.gatewayBaseUrl.value = initialGatewayBaseUrl();
  nodes.loginButton.addEventListener("click", login);
  nodes.logoutButton.addEventListener("click", logout);
  render();
  loadLiveIncidents().catch((error) => {
    setSession(null);
    nodes.loginMessage.textContent = `${error.message}. Fixture incidents are shown.`;
  });
})();
