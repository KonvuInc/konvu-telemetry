"use strict";

async function startDashboard() {
  const preview = new URLSearchParams(location.search).get("preview");
  if (["subscription", "states", "topics"].includes(preview)) {
    loadDashboard();
    return;
  }
  const token = new URLSearchParams(location.hash.slice(1)).get("token");
  if (token !== null) {
    history.replaceState(null, "", location.pathname + location.search);
  }
  try {
    const response = token === null
      ? await fetch("/api/preferences", { cache: "no-store" })
      : await fetch("/api/auth", {
          method: "POST",
          headers: { Authorization: "Bearer " + token },
          cache: "no-store",
        });
    if (!response.ok) throw new Error("Dashboard access denied");
    loadDashboard();
  } catch {
    const message = document.createElement("p");
    message.textContent = "Run konvu-telemetry dashboard to open this dashboard.";
    document.querySelector("main").replaceChildren(message);
  }
}

function loadDashboard() {
  const script = document.createElement("script");
  script.src = "/fleet.js";
  document.body.append(script);
}

startDashboard();
