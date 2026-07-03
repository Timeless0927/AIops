(function () {
  "use strict";

  function setPressed(buttons, activeButton) {
    buttons.forEach((button) => {
      button.setAttribute("aria-pressed", String(button === activeButton));
    });
  }

  document.querySelectorAll("[data-toggle-target]").forEach((button) => {
    button.addEventListener("click", () => {
      const target = document.getElementById(button.dataset.toggleTarget);
      if (!target) {
        return;
      }
      const isHidden = target.hasAttribute("hidden");
      target.toggleAttribute("hidden", !isHidden);
      button.setAttribute("aria-expanded", String(isHidden));
    });
  });

  document.querySelectorAll("[data-filter-group]").forEach((group) => {
    const buttons = Array.from(group.querySelectorAll("[data-filter]"));
    const items = Array.from(document.querySelectorAll(`[data-filter-source="${group.dataset.filterGroup}"]`));
    buttons.forEach((button) => {
      button.addEventListener("click", () => {
        const filter = button.dataset.filter || "all";
        setPressed(buttons, button);
        items.forEach((item) => {
          const tags = item.dataset.tags || "";
          item.toggleAttribute("hidden", filter !== "all" && !tags.includes(filter));
        });
      });
    });
  });

  document.querySelectorAll("[data-tab-group]").forEach((group) => {
    const buttons = Array.from(group.querySelectorAll("[data-tab]"));
    const panels = Array.from(document.querySelectorAll(`[data-tab-panel="${group.dataset.tabGroup}"]`));
    buttons.forEach((button) => {
      button.addEventListener("click", () => {
        const tab = button.dataset.tab;
        setPressed(buttons, button);
        panels.forEach((panel) => {
          panel.toggleAttribute("hidden", panel.dataset.tab !== tab);
        });
      });
    });
  });

  document.querySelectorAll("[data-approval-state]").forEach((button) => {
    button.addEventListener("click", () => {
      const panel = document.getElementById(button.dataset.approvalState);
      if (!panel) {
        return;
      }
      const isApprover = panel.dataset.mode !== "approver";
      panel.dataset.mode = isApprover ? "approver" : "blocked";
      panel.querySelectorAll("[data-approval-locked]").forEach((locked) => {
        locked.toggleAttribute("disabled", !isApprover);
      });
      panel.querySelectorAll("[data-approval-copy]").forEach((node) => {
        node.textContent = isApprover
          ? "Approver scope matched. Decision controls are enabled for UI review only."
          : "Blocked for this on-call user. Incident Commander or service owner approval is required.";
      });
    });
  });
})();
