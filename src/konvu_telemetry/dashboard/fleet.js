"use strict";
const COLORS = {
  claude: "#DB5F37",
  codex: "#5730AB",
  ink: "#2D1266",
  purple: "#412187",
  muted: "#A99FC0",
  line: "#E7E3EE",
  mint: "#A4E4D9",
  pink: "#FF7397",
  green: "#287824",
  orange: "#DB5F37",
  red: "#A60808",
};
const BURNING_FORECAST_USD = 4;
const state = {
  payload: null,
  view: "ledger",
  baselineMode: "matched",
  provider: "all",
  sort: "forecast",
  selected: null,
  chart: "cumulative",
  error: false,
  now: Date.now(),
  lastOpener: null,
};
const $ = (selector) => document.querySelector(selector);
const esc = (value) => String(value ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);
const finite = (value) => typeof value === "number" && Number.isFinite(value);
const nonnegative = (value) => finite(value) && value >= 0;
const money = (value) => (nonnegative(value) ? "$" + value.toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 }) : "—");
const additional = (value) => (nonnegative(value) ? "+" + money(value) : "—");
const compactMoney = (value) =>
  nonnegative(value) ? "$" + (value >= 1000 ? (value / 1000).toFixed(1) + "k" : value >= 10 ? Math.round(value) : value.toFixed(1)) : "—";
const tokens = (value) =>
  nonnegative(value) ? (value >= 1e6 ? (value / 1e6).toFixed(1) + "M" : value >= 1000 ? (value / 1000).toFixed(1) + "k" : String(value)) : "—";
const duration = (seconds) => {
  if (!finite(seconds)) return "—";
  seconds = Math.max(0, seconds);
  for (const [unit, n] of [
    ["w", 604800],
    ["d", 86400],
    ["h", 3600],
    ["m", 60],
    ["s", 1],
  ])
    if (seconds >= n || unit === "s") return Math.floor(seconds / n) + unit;
};
const age = (timestamp) => {
  const time = Date.parse(timestamp);
  return finite(time) ? duration((state.now - time) / 1000) : "—";
};
const timeLabel = (timestamp) => {
  const d = new Date(timestamp);
  return finite(d.getTime()) ? d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" }) : "unknown time";
};
const elapsed = (timestamp) => state.now - Date.parse(timestamp);
const providerName = (p) => (p === "claude" ? "Claude" : p === "codex" ? "Codex" : "Unknown");
const keyOf = (s) => s.provider + ":" + s.id;
const series = (s) =>
  Array.isArray(s.iterations)
    ? s.iterations
        .filter((q) => q && nonnegative(q.cumulative_cost_usd) && nonnegative(q.cost_usd))
        .sort((a, b) => Date.parse(a.started_at) - Date.parse(b.started_at))
    : [];
const cost = (s) => (!nonnegative(s.total_cost_usd) || s.cost_status === "unavailable" ? null : s.total_cost_usd);
const forecast = (s) => (cost(s) !== null && nonnegative(s.projected_next_10_tasks_usd) ? s.projected_next_10_tasks_usd : null);
const context = (s) =>
  nonnegative(s.context_tokens) && finite(s.context_window_tokens) && s.context_window_tokens > 0
    ? Math.min(100, (s.context_tokens / s.context_window_tokens) * 100)
    : null;
const count = (s) => (Number.isInteger(s.task_count) && s.task_count >= 0 ? s.task_count : series(s).length);
const startTime = (s) => s.session_started_at || series(s)[0]?.started_at || null;
const compacts = (s) => (Array.isArray(s.compact_events) ? s.compact_events.filter((e) => finite(Date.parse(e.timestamp))) : []);
function displayTitle(s) {
  let title = String(s.title || "")
    .replace(/\s+/g, " ")
    .trim();
  if (!title || /^(Codex|Claude) session [a-f0-9]/.test(title)) return providerName(s.provider) + " session " + s.id.slice(0, 8);
  if (title.startsWith("# Browser comments:")) {
    const comment = title.match(/Comment:\s*([\s\S]+)/);
    return comment ? comment[1].slice(0, 180) : "Browser feedback · " + providerName(s.provider);
  }
  return title;
}
function activity(s) {
  const sinceActivity = elapsed(s.last_activity_at);
  const live = finite(sinceActivity) && sinceActivity >= -60000 && sinceActivity <= 20 * 60000;
  const a = s.activity;
  if (a?.state === "running") return { live, label: "Running", kind: "running", explicit: true };
  if (a?.state === "idle") return { live, label: "Idle", kind: "idle", explicit: true };
  return { live, label: live ? "Recently active" : "Inactive", kind: "unknown", explicit: false };
}
function comparableRows(s) {
  const rows = series(s);
  const model = s.forecast_basis?.model || s.model,
    effort = s.forecast_basis?.effort || s.reasoning_effort;
  if (!model || !effort) return [];
  const recent = [];
  for (let i = rows.length - 1; i >= 0; i--) {
    const q = rows[i];
    if (i === rows.length - 1 && q.completed === false) continue;
    if (q.completed !== true || q.priced !== true || q.model !== model || q.reasoning_effort !== effort) break;
    recent.unshift(q);
  }
  return recent;
}
function comparison(s) {
  const rows = comparableRows(s);
  if (rows.length < 6) return null;
  const recent = rows.slice(-3),
    prior = rows.slice(-6, -3);
  const average = (a) => a.reduce((v, q) => v + q.cost_usd, 0) / a.length;
  const before = average(prior),
    after = average(recent);
  return before > 0 ? { ratio: after / before, before, after, prior, recent } : null;
}
function assess(s) {
  const c = comparison(s),
    f = forecast(s);
  const result = {
    severity: 0,
    label: c ? "No sharp cost rise" : "Limited comparison data",
    action: "Review session",
    evidence: c
      ? "Recent prompts compared with earlier prompts in this session, on the same model and effort."
      : "A cost trend needs six completed, priced prompts with recorded matching model and effort.",
    score: f || 0,
  };
  if (c && c.ratio >= 2 && c.after - c.before >= 0.1) {
    result.severity = 2;
    result.label = "Cost per prompt is rising";
    result.action = "Review the more expensive prompts";
    result.evidence =
      "Last 3 prompts averaged " + money(c.after) + " vs " + money(c.before) + " before (" + c.ratio.toFixed(1) + "×), on the same model and effort.";
    result.score = 100 + (f || 0);
  }
  const repeated = compacts(s).filter((e) => elapsed(e.timestamp) >= 0 && elapsed(e.timestamp) < 1800000);
  if (repeated.length >= 3 && result.severity) {
    result.severity = 3;
    result.action = "Review rising cost and repeated compaction";
    result.evidence += " " + repeated.length + " recorded compactions in 30 minutes.";
  }
  return result;
}
function allRows() {
  return Array.isArray(state.payload?.sessions) ? state.payload.sessions : [];
}
function liveRows() {
  return allRows().filter((s) => activity(s).live && (state.provider === "all" || s.provider === state.provider));
}
function sortedRows() {
  return liveRows()
    .slice()
    .sort((a, b) => {
      const av =
        state.sort === "forecast"
          ? forecast(a)
          : state.sort === "spent"
            ? cost(a)
            : state.sort === "activity"
              ? Date.parse(a.last_activity_at)
              : assess(a).severity * 1000 + assess(a).score;
      const bv =
        state.sort === "forecast"
          ? forecast(b)
          : state.sort === "spent"
            ? cost(b)
            : state.sort === "activity"
              ? Date.parse(b.last_activity_at)
              : assess(b).severity * 1000 + assess(b).score;
      return (bv ?? -1) - (av ?? -1) || keyOf(a).localeCompare(keyOf(b));
    });
}
const effort = (s) => s.reasoning_effort || s.effort || null;
const speed = (s) => s.service_tier || s.speed || null;
function matchesConfiguration(a, b) {
  const configuration = a.comparison_configuration;
  return (
    !!configuration &&
    configuration.model === b.model &&
    configuration.effort === b.effort &&
    (!speed(b) || !configuration.speed || configuration.speed === speed(b))
  );
}
function configurationLabel(s) {
  return [s.model || providerName(s.provider), effort(s) ? effort(s) + " effort" : "Effort unrecorded", speed(s)].filter(Boolean).join(" · ");
}
function titleCell(s) {
  return (
    '<div class="session-cell"><img class="provider-icon" src="/' +
    (s.provider === "claude" ? "claude" : "codex") +
    '.png" alt="' +
    providerName(s.provider) +
    '"><div class="session-copy"><button class="session-title" data-session="' +
    esc(keyOf(s)) +
    '" title="' +
    esc(displayTitle(s)) +
    '">' +
    esc(displayTitle(s)) +
    '</button><div class="session-meta">' +
    esc(configurationLabel(s)) +
    "</div></div></div>"
  );
}
function contextColor(pct) {
  return pct === null ? COLORS.muted : pct >= 90 ? COLORS.red : pct >= 60 ? COLORS.orange : COLORS.green;
}
const subagentOpenGroups = new Set();
function subagentLabel(a) {
  const label = String(a.label || "Subagent")
    .replace(/^\/?root\//, "")
    .replace(/_/g, " ");
  return label.charAt(0).toUpperCase() + label.slice(1);
}
function subagentNode(a, index = null) {
  const id = typeof a.id === "string" ? a.id : "",
    label = subagentLabel(a) + (index === null ? "" : " " + (index + 1));
  return (
    '<div class="agent-node"><div class="agent-identity"><span class="subagent-name" title="' +
    esc(a.label || label) +
    '">' +
    esc(label) +
    '</span><small title="' +
    esc(id) +
    '"><i class="agent-status ' +
    (a.live === true ? "live" : "") +
    '"></i>' +
    (a.live === true ? "Live" : "Inactive") +
    (id ? " · " + esc(id.slice(0, 13)) : "") +
    '</small></div><div class="agent-context">' +
    tokens(a.entry_context_tokens) +
    "<small>" +
    (nonnegative(a.entry_context_tokens) ? "tokens" : "Not recorded") +
    '</small></div><div class="agent-cost">' +
    money(a.cost_usd) +
    "<small>" +
    (nonnegative(a.cost_usd) ? "recorded" : "Not recorded") +
    "</small></div></div>"
  );
}
function subagentDetails(s) {
  const agents = Array.isArray(s.subagents) ? s.subagents.filter((a) => a && typeof a === "object") : [];
  if (!agents.length) return "";
  const grouped = new Map();
  for (const agent of agents) {
    const key = String(agent.label || "Subagent");
    if (!grouped.has(key)) grouped.set(key, []);
    grouped.get(key).push(agent);
  }
  const groupCost = (children) => {
    const priced = children.filter((a) => nonnegative(a.cost_usd));
    return priced.length ? priced.reduce((sum, a) => sum + a.cost_usd, 0) : -1;
  };
  const groups = Array.from(grouped.entries()).sort((a, b) => groupCost(b[1]) - groupCost(a[1]) || a[0].localeCompare(b[0]));
  const branches = groups
    .map(([label, children]) => {
      children.sort(
        (a, b) =>
          (nonnegative(b.cost_usd) ? b.cost_usd : -1) - (nonnegative(a.cost_usd) ? a.cost_usd : -1) || String(a.id || "").localeCompare(String(b.id || "")),
      );
      if (children.length === 1) return '<div class="agent-branch">' + subagentNode(children[0]) + "</div>";
      const key = keyOf(s) + "|" + label,
        live = children.filter((a) => a.live === true).length,
        contexts = children.map((a) => a.entry_context_tokens).filter(nonnegative),
        priced = children.filter((a) => nonnegative(a.cost_usd));
      const contextRange = contexts.length
        ? Math.min(...contexts) === Math.max(...contexts)
          ? tokens(contexts[0])
          : tokens(Math.min(...contexts)) + "–" + tokens(Math.max(...contexts))
        : "—";
      const contextNote =
        contexts.length === children.length
          ? "tokens each"
          : contexts.length
            ? "tokens · " + contexts.length + "/" + children.length + " recorded"
            : "Not recorded";
      const costLabel = priced.length ? money(priced.reduce((sum, a) => sum + a.cost_usd, 0)) : "—",
        costNote = priced.length === children.length ? "total" : priced.length ? priced.length + "/" + children.length + " priced" : "Not recorded";
      return (
        '<details class="agent-branch agent-group" data-agent-group="' +
        esc(key) +
        '"' +
        (subagentOpenGroups.has(key) ? " open" : "") +
        '><summary class="agent-node"><div class="agent-identity"><span class="subagent-name"><i class="agent-chevron" aria-hidden="true"></i>' +
        esc(subagentLabel(children[0])) +
        ' <b class="agent-count">' +
        children.length +
        "</b></span><small>" +
        live +
        " live · " +
        (children.length - live) +
        ' inactive</small></div><div class="agent-context">' +
        contextRange +
        "<small>" +
        contextNote +
        '</small></div><div class="agent-cost">' +
        costLabel +
        "<small>" +
        costNote +
        '</small></div></summary><div class="agent-children">' +
        children.map((a, i) => subagentNode(a, i)).join("") +
        "</div></details>"
      );
    })
    .join("");
  return (
    '<div class="section-title"><h3>Subagents</h3><span class="tiny">' +
    agents.length +
    " spawned · " +
    agents.filter((a) => a.live === true).length +
    ' live</span></div><div class="agent-map"><div class="agent-map-header"><span>Agent</span><span>Context received</span><span>Spent ↓</span></div><div class="agent-root"><i></i>This session</div><div class="agent-branches">' +
    branches +
    "</div></div>"
  );
}
function donut(s) {
  const pct = context(s),
    r = 15,
    c = 2 * Math.PI * r;
  return (
    '<div class="context-donut" title="' +
    tokens(s.context_tokens) +
    " of " +
    tokens(s.context_window_tokens) +
    ' context tokens"><svg viewBox="0 0 38 38" aria-hidden="true"><circle class="donut-track" cx="19" cy="19" r="15"/><circle class="donut-fill" style="stroke:' +
    contextColor(pct) +
    '" cx="19" cy="19" r="15" stroke-dasharray="' +
    (c * (pct ?? 0)) / 100 +
    " " +
    c +
    '" transform="rotate(-90 19 19)"/></svg><span style="color:' +
    contextColor(pct) +
    '">' +
    (pct === null ? "—" : Math.round(pct) + "%") +
    "</span></div>"
  );
}
function checkpointComparison(s) {
  const curve = configurationCurve(s);
  const matched = state.baselineMode === "matched";
  const points = matched ? curve?.points || [] : curvePoints(state.payload?.baselines?.providers?.[s.provider]);
  const prompts = count(s),
    spent = cost(s),
    first = points[0],
    last = points.at(-1);
  const median = medianCostAt(points, prompts);
  if (prompts <= 0 || spent === null || !first || !last || !nonnegative(median) || median <= 0) return null;
  return {
    ratio: spent / median,
    matched,
    iteration: prompts,
    samples: prompts <= first.iterations ? first.sessions : last.sessions,
    actual: spent,
    median,
    early: prompts < first.iterations,
    extrapolated: prompts > last.iterations,
  };
}
function recordedBaselineComparison(s) {
  const scope = state.baselineMode === "matched" ? "model_effort_speed" : "provider";
  const baseline = s.baselines?.[scope] || (s.baseline?.scope === scope ? s.baseline : null);
  const overhead = baseline?.cost_overhead_percent;
  if (!finite(overhead)) return null;
  const ratio = 1 + overhead / 100;
  if (ratio < 0) return null;
  return {
    ratio,
    matched: baseline.scope === "model_effort_speed",
    recorded: true,
    median: baseline.median_cost_usd,
    samples: baseline.sample_sessions,
  };
}
function spendComparison(s) {
  const c = recordedBaselineComparison(s) || checkpointComparison(s);
  if (!c) {
    const prompts = Number(s.task_count || 0);
    return {
      ratio: null,
      label:
        prompts < 10
          ? "Median starts after 10 prompts"
          : state.baselineMode === "matched" && !configurationCurve(s)?.points.length
            ? "Need 3 matching model + effort + speed sessions"
            : "Waiting for a comparable checkpoint",
      detail:
        prompts < 10
          ? "Your median has no checkpoint before 10 prompts yet."
          : "A comparison needs recorded spend at the same iteration count as the selected median. Model, effort, and speed comparisons require at least three matching sessions.",
    };
  }
  const label = (c.matched ? "model + effort + speed median" : providerName(s.provider) + " general median") + (c.early ? " estimate" : "");
  return {
    ratio: c.ratio,
    label: c.ratio.toFixed(2) + "× " + label,
    detail: c.recorded
      ? "Current recorded spend against the same local median used in the CLI."
      : money(c.actual) +
        " recorded at " +
        c.iteration +
        " prompts vs. " +
        money(c.median) +
        " median across " +
        c.samples +
        " sessions in the last " +
        (state.payload?.baselines?.lookback_days || 60) +
        " days." +
        (c.early
          ? " This is prorated from the first 10-prompt checkpoint."
          : c.extrapolated
            ? " This extends the same median curve past its final checkpoint."
            : c.matched
              ? ""
              : " General usage includes all models, efforts, and speeds within this provider."),
  };
}
function roundedDollarCeiling(value) {
  const total = Math.max(0.01, value),
    unit = 10 ** Math.floor(Math.log10(total));
  return [1, 2, 5, 10].map((n) => n * unit).find((n) => n >= total);
}
function ledgerDollarScale(rows) {
  return roundedDollarCeiling(Math.max(1, ...rows.map((s) => (cost(s) ?? 0) + (forecast(s) ?? 0))));
}
function burningCashStage(next) {
  return !nonnegative(next) || next < BURNING_FORECAST_USD ? 0 : next < 10 ? 1 : next < 20 ? 2 : 3;
}
function bonfireBill([x, y, angle, scale = 1]) {
  return (
    '<g class="bonfire-bill" transform="translate(' +
    x +
    " " +
    y +
    ") rotate(" +
    angle +
    " 14 7) scale(" +
    scale +
    ')"><rect width="28" height="15" rx="1" fill="#A8D68D" stroke="#286746" stroke-width="1.1"/><rect x="2.5" y="2.5" width="23" height="10" rx="1" fill="none" stroke="#3C8050" stroke-width=".7"/><ellipse cx="14" cy="7.5" rx="5.5" ry="6" fill="#E7F3CA"/><text x="14" y="11.7" text-anchor="middle" fill="#225E3B" font-family="Arial,sans-serif" font-weight="700" font-size="12">$</text><path d="M4 6h3m-3 3h3m14-3h3m-3 3h3" stroke="#327446" stroke-width=".8"/></g>'
  );
}
function burningCashIcon(next) {
  const stage = burningCashStage(next);
  if (!stage) return "";
  const pile =
    stage === 1
      ? [[14, 35, -9, 1.25]]
      : stage === 2
        ? [
            [18, 31, -17],
            [5, 40, -10],
            [29, 40, 14],
          ]
        : [
            [17, 24, -14],
            [6, 32, -26],
            [30, 31, 25],
            [19, 35, 7],
            [2, 43, -8],
            [20, 43, 4],
            [36, 42, 13],
          ];
  const flameScale = stage === 1 ? "translate(14 19) scale(.6 .55)" : stage === 2 ? "translate(5 7) scale(.85 .82)" : "";
  const label =
    ["", "One burning bill", "Three burning bills", "A burning mountain of bills"][stage] + ": " + money(next) + " forecast for the next 10 prompts";
  return (
    '<span class="cash-alert burn-stage-' +
    stage +
    '" role="img" aria-label="' +
    esc(label) +
    '" title="' +
    esc(label) +
    '"><svg class="bills-bonfire" viewBox="0 0 64 64" aria-hidden="true"><ellipse class="bonfire-glow" cx="32" cy="50" rx="' +
    (stage === 1 ? 18 : 28) +
    '" ry="7" fill="#FF8A24" opacity=".15"/><g transform="' +
    flameScale +
    '"><g class="bonfire-tongue flame-back"><path fill="#E34D1C" d="M14 43C1 31 15 23 8 13c11 3 9 12 15 15C16 13 33 12 29 1c17 9 9 19 15 24 6-5 3-12 7-16 0 12 17 17 7 31-8 11-33 12-44 3z"/></g><g class="bonfire-tongue flame-left"><path fill="#FF8D22" d="M16 43C5 35 15 28 12 20c9 4 4 9 11 12-4-11 6-15 5-23 12 13-1 20 6 29l-3 9z"/></g><g class="bonfire-tongue flame-right"><path fill="#FFAB27" d="M29 44c-8-10 10-15 8-26 9 6 4 13 8 16 6-5 4-10 6-13 9 13 4 23-10 27z"/></g><g class="bonfire-tongue flame-core"><path fill="#FFE58F" d="M23 42c-4-8 10-13 8-22 11 9-2 13 5 18 1-4 4-6 6-7 3 13-10 16-19 11z"/></g></g>' +
    pile.map(bonfireBill).join("") +
    '<g transform="' +
    (stage === 1 ? "translate(7 12) scale(.8)" : "") +
    '"><g class="bonfire-tongue flame-front"><path fill="#F47720" d="M25 48c-5-5 0-9-3-15 9 4 3 9 8 10 1-5 7-7 6-13 9 9 0 19-11 18z"/><path fill="#FFD268" d="M28 47c-2-4 4-6 3-10 6 6 2 10-3 10z"/></g></g>' +
    (stage === 1
      ? ""
      : '<g fill="#F88822"><circle class="bonfire-ember" cx="13" cy="18" r="1.2"/><circle class="bonfire-ember ember-mid" cx="35" cy="10" r="1"/>' +
        (stage === 3 ? '<circle class="bonfire-ember ember-late" cx="52" cy="18" r="1.1"/>' : "") +
        "</g>") +
    "</svg></span>"
  );
}
function spendVisual(s, scale) {
  const spent = cost(s),
    next = forecast(s),
    values =
      '<div class="spend-values"><strong>' +
      money(spent) +
      '</strong><span class="forecast-label">' +
      burningCashIcon(next) +
      '<b class="forecast-amount">' +
      additional(next) +
      "</b><small>in next 10 prompts</small></span></div>";
  const visual =
    spent === null
      ? '<span class="tiny">Recorded cost unavailable</span>'
      : '<div class="blue-track" role="img" aria-label="' +
        esc(money(spent) + " spent, " + additional(next) + " next 10. Full bar " + money(scale)) +
        '"><i class="blue-spent" style="width:' +
        (spent / scale) * 100 +
        '%"></i>' +
        (next === null ? "" : '<i class="blue-future" style="width:' + (next / scale) * 100 + '%"></i>') +
        "</div>";
  return '<div class="spending">' + values + visual + "</div>";
}
function medianCell(s, comparison) {
  const ratio = comparison.ratio;
  if (ratio === null) return '<div class="median-cell missing" title="' + esc(comparison.detail) + '">—<small>' + esc(comparison.label) + "</small></div>";
  return (
    '<div class="median-cell" title="' +
    esc(comparison.detail) +
    '"><strong>' +
    ratio.toLocaleString("en-US", { maximumFractionDigits: 2 }) +
    "×</strong><small>$ spent</small></div>"
  );
}
function subagentCell(s) {
  const total = Number.isInteger(s.subagent_total) && s.subagent_total >= 0 ? s.subagent_total : "—";
  const live = Number.isInteger(s.active_subagents) && s.active_subagents >= 0 ? s.active_subagents : "—";
  return (
    '<div class="subagent-cell"><span><strong>' +
    total +
    '</strong> spawned · <strong class="' +
    (live > 0 ? "subagents-live" : "") +
    '">' +
    live +
    '</strong> live</span><small title="Recorded subagent cost from the local collector">' +
    money(s.subagent_cost_usd) +
    " spent</small></div>"
  );
}
function ledger(rows) {
  const scale = ledgerDollarScale(rows);
  return (
    '<div class="table-shell"><table class="session-ledger compact-ledger layout-1"><thead><tr><th>Session</th><th>Spent <span>/ next 10 prompts</span></th><th>Vs. median</th><th>Context</th><th>Subagents</th><th>Activity <span>/ age</span></th></tr></thead><tbody>' +
    rows
      .map((s) => {
        const comparison = spendComparison(s),
          next = forecast(s),
          burning = s.notification?.hot === true;
        return (
          '<tr class="' +
          (burning ? "burning-row" : "") +
          '" data-session="' +
          esc(keyOf(s)) +
          '"><td><div class="indexed-title">' +
          titleCell(s) +
          "</div></td><td>" +
          spendVisual(s, scale) +
          "</td><td>" +
          medianCell(s, comparison) +
          "</td><td>" +
          donut(s) +
          "</td><td>" +
          subagentCell(s) +
          '</td><td class="time-cell"><span>' +
          age(s.last_activity_at) +
          " ago</span><small>" +
          age(startTime(s)) +
          " old</small></td></tr>"
        );
      })
      .join("") +
    "</tbody></table></div>"
  );
}

function curvePoints(b) {
  return Array.isArray(b)
    ? b
        .filter((p) => p && nonnegative(p.iterations) && nonnegative(p.median_cost_usd))
        .slice()
        .sort((a, b) => a.iterations - b.iterations)
    : [];
}
function configurationCurve(s) {
  const configs = state.payload?.baselines?.configurations?.[s.provider];
  if (!configs || typeof configs !== "object") return null;
  const match = Object.values(configs).find((b) => b && matchesConfiguration(s, b));
  return match ? { ...match, points: curvePoints(match.checkpoints).filter((p) => p.sessions >= 3) } : null;
}
function graphBaselines() {
  return ["claude", "codex"]
    .filter((p) => state.provider === "all" || state.provider === p)
    .map((p) => ({
      label: "Your " + providerName(p) + " median",
      color: graphColor(p),
      provider: p,
      points: curvePoints(state.payload?.baselines?.providers?.[p]),
    }))
    .filter((b) => b.points.length);
}
function medianCostAt(points, n) {
  const marks = [
    [0, 0],
    ...curvePoints(points)
      .sort((a, b) => a.iterations - b.iterations)
      .map((r) => [r.iterations, r.median_cost_usd]),
  ];
  if (marks.length < 2) return null;
  const [lx, ly] = marks[marks.length - 1],
    [px, py] = marks[marks.length - 2];
  if (n >= lx) return ly + ((ly - py) / Math.max(1, lx - px)) * (n - lx);
  for (let k = 1; k < marks.length; k++) {
    if (n > marks[k][0]) continue;
    const f = (n - marks[k - 1][0]) / Math.max(1e-9, marks[k][0] - marks[k - 1][0]);
    return marks[k - 1][1] + f * (marks[k][1] - marks[k - 1][1]);
  }
  return ly;
}
function medianGeometry(points, xmax, ymax) {
  const rows = curvePoints(points);
  if (!rows.length) return null;
  const xs = [0, ...rows.map((p) => p.iterations).filter((n) => n > 0 && n < xmax), xmax];
  const curve = xs.map((iterations) => ({ iterations, median_cost_usd: medianCostAt(rows, iterations) }));
  let exitX = xmax;
  if (medianCostAt(rows, xmax) > ymax) {
    const lo = xs.reduce((acc, n) => (medianCostAt(rows, n) <= ymax ? n : acc), 0);
    const hi = xs.find((n) => medianCostAt(rows, n) > ymax);
    if (hi !== undefined) {
      const fraction = (ymax - medianCostAt(rows, lo)) / Math.max(1e-9, medianCostAt(rows, hi) - medianCostAt(rows, lo));
      exitX = lo + (hi - lo) * fraction;
    }
  }
  return { curve, endpoint: { iterations: exitX, median_cost_usd: Math.min(medianCostAt(rows, exitX), ymax) } };
}

const axisPos = { x: 1, y: 1 };
let axisDragging = null;
const rampLimit = (pos, full) => {
  const floor = Math.max(full / 400, 0.05);
  return pos >= 1 ? full : floor * Math.pow(full / floor, pos);
};
function axisHandles(W, H, L, R, T, B, xmax, ymax) {
  const hx = L + axisPos.x * (W - L - R),
    hy = T + (1 - axisPos.y) * (H - T - B);
  return (
    '<g class="axis-handle" data-axis="x" tabindex="0" role="slider" aria-label="Iteration axis scale" aria-orientation="horizontal" aria-valuemin="6" aria-valuemax="100" aria-valuenow="' +
    Math.round(axisPos.x * 100) +
    '" aria-valuetext="Show up to ' +
    Math.round(xmax) +
    ' iterations"><rect class="axis-hit" x="' +
    L +
    '" y="' +
    (H - B - 12) +
    '" width="' +
    (W - L - R) +
    '" height="24"/><line class="axis-track" x1="' +
    L +
    '" x2="' +
    (W - R) +
    '" y1="' +
    (H - B) +
    '" y2="' +
    (H - B) +
    '"/><line class="axis-fill" x1="' +
    L +
    '" x2="' +
    hx +
    '" y1="' +
    (H - B) +
    '" y2="' +
    (H - B) +
    '"/><rect class="axis-knob" x="' +
    (hx - 3) +
    '" y="' +
    (H - B - 6) +
    '" width="6" height="12" rx="3"/></g><g class="axis-handle" data-axis="y" tabindex="0" role="slider" aria-label="Spend axis scale" aria-orientation="vertical" aria-valuemin="6" aria-valuemax="100" aria-valuenow="' +
    Math.round(axisPos.y * 100) +
    '" aria-valuetext="Show up to ' +
    money(ymax) +
    '"><rect class="axis-hit" x="' +
    (L - 12) +
    '" y="' +
    T +
    '" width="24" height="' +
    (H - T - B) +
    '"/><line class="axis-track" x1="' +
    L +
    '" x2="' +
    L +
    '" y1="' +
    T +
    '" y2="' +
    (H - B) +
    '"/><line class="axis-fill" x1="' +
    L +
    '" x2="' +
    L +
    '" y1="' +
    hy +
    '" y2="' +
    (H - B) +
    '"/><rect class="axis-knob" x="' +
    (L - 6) +
    '" y="' +
    (hy - 3) +
    '" width="12" height="6" rx="3"/></g>'
  );
}
function repaintGraph() {
  const svg = $("#plotsvg");
  if (!svg) return;
  const focused = document.activeElement?.dataset?.axis;
  const template = document.createElement("template");
  template.innerHTML = fleetGraph(sortedRows());
  svg.innerHTML = template.content.querySelector("#plotsvg").innerHTML;
  $("#reset-axes").hidden = axisPos.x === 1 && axisPos.y === 1;
  if (focused && !axisDragging) svg.querySelector('[data-axis="' + focused + '"]')?.focus({ preventScroll: true });
}
function resetGraphAxes() {
  axisPos.x = 1;
  axisPos.y = 1;
  repaintGraph();
}
function bindGraphControls() {
  document.addEventListener("pointerdown", (event) => {
    const handle = event.target.closest?.(".axis-handle");
    if (!handle || event.button !== 0) return;
    const svg = handle.closest("svg");
    event.preventDefault();
    hideGraphTip();
    axisDragging = { axis: handle.dataset.axis, svg, pointerId: event.pointerId };
    svg.setPointerCapture(event.pointerId);
    moveAxis(event);
  });
  function moveAxis(event) {
    if (!axisDragging || event.pointerId !== axisDragging.pointerId) return;
    const { svg, axis } = axisDragging,
      matrix = svg.getScreenCTM();
    if (!matrix) return;
    const point = svg.createSVGPoint();
    point.x = event.clientX;
    point.y = event.clientY;
    const local = point.matrixTransform(matrix.inverse());
    const position = axis === "x" ? (local.x - 65) / (1100 - 65 - 40) : 1 - (local.y - 25) / (450 - 25 - 48);
    axisPos[axis] = Math.min(1, Math.max(0.06, position));
    repaintGraph();
  }
  document.addEventListener("pointermove", moveAxis);
  const release = (event) => {
    if (!axisDragging || event.pointerId !== axisDragging.pointerId) return;
    const { svg, pointerId } = axisDragging;
    axisDragging = null;
    if (svg.hasPointerCapture(pointerId)) svg.releasePointerCapture(pointerId);
  };
  document.addEventListener("pointerup", release);
  document.addEventListener("pointercancel", release);
  document.addEventListener("lostpointercapture", release);
  document.addEventListener("dblclick", (event) => {
    if (event.target.closest?.("#plotsvg")) resetGraphAxes();
  });
  document.addEventListener("click", (event) => {
    if (event.target.closest?.("#reset-axes")) resetGraphAxes();
  });
  document.addEventListener("keydown", (event) => {
    const axis = event.target.dataset?.axis;
    if (!axis || !["ArrowLeft", "ArrowRight", "ArrowUp", "ArrowDown", "Home", "End"].includes(event.key)) return;
    event.preventDefault();
    axisPos[axis] =
      event.key === "Home"
        ? 0.06
        : event.key === "End"
          ? 1
          : Math.max(0.06, Math.min(1, axisPos[axis] + (["ArrowRight", "ArrowUp"].includes(event.key) ? 0.025 : -0.025)));
    repaintGraph();
  });
  document.addEventListener("pointerover", (event) => {
    const point = event.target.closest?.(".fleet-point");
    if (point && !axisDragging) showGraphTip(point, event.clientX, event.clientY);
  });
  document.addEventListener("pointermove", (event) => {
    if (!axisDragging && event.target.closest?.(".fleet-point")) positionGraphTip(event.clientX, event.clientY);
  });
  document.addEventListener("pointerout", (event) => {
    const point = event.target.closest?.(".fleet-point");
    if (point && !point.contains(event.relatedTarget)) hideGraphTip();
  });
  document.addEventListener("focusin", (event) => {
    if (event.target.matches?.(".fleet-point")) {
      const r = event.target.getBoundingClientRect();
      showGraphTip(event.target, r.x + r.width / 2, r.y);
    }
  });
  document.addEventListener("focusout", (event) => {
    if (event.target.matches?.(".fleet-point")) hideGraphTip();
  });
}
function hideGraphTip() {
  highlightGraphHistory(null);
  const tip = $("#graph-tooltip");
  if (tip) tip.hidden = true;
}
function positionGraphTip(x, y) {
  const tip = $("#graph-tooltip");
  if (!tip || tip.hidden) return;
  tip.style.left = Math.max(8, Math.min(innerWidth - tip.offsetWidth - 8, x + 16)) + "px";
  tip.style.top = Math.max(8, Math.min(innerHeight - tip.offsetHeight - 8, y + 14)) + "px";
}
function showGraphTip(point, x, y) {
  const s = allRows().find((s) => keyOf(s) === point.dataset.session),
    tip = $("#graph-tooltip");
  if (!s || !tip) return;
  highlightGraphHistory(point.dataset.session);
  tip.innerHTML =
    "<strong>" +
    esc(displayTitle(s)) +
    "</strong><span>" +
    esc(configurationLabel(s)) +
    "</span><dl><dt>Spent</dt><dd>" +
    money(cost(s)) +
    "</dd><dt>Iterations</dt><dd>" +
    count(s) +
    "</dd><dt>Next 10 prompts</dt><dd>" +
    additional(forecast(s)) +
    "</dd><dt>Context</dt><dd>" +
    (context(s) === null ? "—" : Math.round(context(s)) + "%") +
    "</dd></dl>";
  tip.hidden = false;
  positionGraphTip(x, y);
}

const RAMP = [
  [0, [40, 120, 36]],
  [0.5, [201, 162, 39]],
  [0.75, [219, 95, 55]],
  [1, [166, 8, 8]],
];
const rampColor = (t) => {
  t = Math.max(0, Math.min(1, t));
  for (let k = 1; k < RAMP.length; k++) {
    if (t > RAMP[k][0]) continue;
    const [t0, c0] = RAMP[k - 1],
      [t1, c1] = RAMP[k];
    const f = (t - t0) / (t1 - t0);
    return "rgb(" + c0.map((v, j) => Math.round(v + (c1[j] - v) * f)).join(",") + ")";
  }
  return "rgb(166,8,8)";
};

function tickValues(lo, hi, want) {
  const span = hi - lo;
  if (!(span > 0)) return [lo];
  const raw = span / want;
  const e = Math.pow(10, Math.floor(Math.log10(raw)));
  const step = [1, 2, 5, 10].map((m) => m * e).find((v) => v >= raw) || e * 10;
  const out = [];
  for (let v = Math.ceil(lo / step) * step; v <= hi + step * 1e-6; v += step) out.push(v);
  return out;
}
const graphColor = (provider) => (provider === "codex" ? "#6078FF" : COLORS.claude);
const axisMoney = (n) =>
  n === 0 ? "$0" : n >= 1 && Number.isInteger(Number(n.toPrecision(10))) ? "$" + Math.round(n).toLocaleString("en-US") : Number((n * 100).toPrecision(6)) + "¢";
function healthySlopeAt(provider, iterations) {
  const marks = [{ iterations: 0, median_cost_usd: 0 }, ...curvePoints(state.payload?.baselines?.providers?.[provider])];
  for (let k = 1; k < marks.length; k++) {
    if (iterations > marks[k].iterations && k < marks.length - 1) continue;
    return (marks[k].median_cost_usd - marks[k - 1].median_cost_usd) / Math.max(1, marks[k].iterations - marks[k - 1].iterations);
  }
  return null;
}
function graphArrowMarker(id, color) {
  return (
    '<marker id="' +
    id +
    '" viewBox="0 0 11 9" refX="9.4" refY="4.5" markerWidth="8.5" markerHeight="7" markerUnits="userSpaceOnUse" orient="auto"><path d="M0.6 0.5L10.4 4.5L0.6 8.5L3.4 4.5z" fill="' +
    color +
    '" stroke="#fff" stroke-width="1.3" stroke-linejoin="round" paint-order="stroke"/></marker>'
  );
}
function sessionGraphMarks(rows, x, y, W, R, T) {
  const trails = [],
    arrows = [],
    marks = [],
    heads = new Map();
  for (const s of rows) {
    const sx = x(count(s)),
      sy = y(cost(s)),
      next = forecast(s),
      key = esc(keyOf(s));
    const history = [{ iteration: 0, cumulative_cost_usd: 0 }, ...series(s)],
      every = Math.max(1, Math.ceil(history.length / 160));
    const sampled = history.filter((_, i) => i % every === 0 || i === history.length - 1);
    if (sampled.length > 1)
      trails.push(
        '<polyline class="history-trail" data-history="' +
          key +
          '" stroke="' +
          graphColor(s.provider) +
          '" points="' +
          sampled.map((p, i) => x(p.iteration ?? i) + "," + y(p.cumulative_cost_usd)).join(" ") +
          '"/>',
      );
    if (next > 0) {
      const slope = healthySlopeAt(s.provider, count(s)),
        color = slope > 0 ? rampColor(next / 10 / slope / 2) : COLORS.muted,
        id = color.replace(/[^0-9a-z]/gi, "");
      heads.set(id, color);
      const dx = x(count(s) + 10) - sx,
        dy = y(cost(s) + next) - sy,
        len = Math.hypot(dx, dy) || 1,
        ux = dx / len,
        uy = dy / len;
      const ax = sx + ux * 12,
        ay = sy + uy * 12,
        room = Math.min(ux > 0 ? (W - R - 3 - ax) / ux : Infinity, uy < 0 ? (T + 3 - ay) / uy : Infinity);
      if (room > 0) arrows.push({ sx: ax, sy: ay, ux, uy, color, id, room, heat: next / Math.max(1, count(s)) });
    }
    marks.push(
      '<g class="fleet-point" data-session="' +
        key +
        '" tabindex="0" role="button" aria-label="' +
        esc(displayTitle(s) + ", " + money(cost(s)) + " spent, " + count(s) + " iterations") +
        '"><title>' +
        esc(displayTitle(s) + " · " + money(cost(s)) + " · " + count(s) + " iterations") +
        '</title><circle class="point-focus" cx="' +
        sx +
        '" cy="' +
        sy +
        '" r="13"/><image href="/' +
        s.provider +
        '.png" x="' +
        (sx - 8) +
        '" y="' +
        (sy - 8) +
        '" width="16" height="16"/></g>',
    );
  }
  const clusters = [];
  for (const a of arrows) {
    const near = clusters.find((g) => Math.hypot(g.sx - a.sx, g.sy - a.sy) < 14);
    if (near) near.items.push(a);
    else clusters.push({ sx: a.sx, sy: a.sy, items: [a] });
  }
  for (const g of clusters)
    g.items
      .sort((a, b) => b.heat - a.heat)
      .forEach((a, k) => {
        a.shaft = Math.min(a.room, Math.max(8, 23 - k * 7));
      });
  const defs = "<defs>" + Array.from(heads, ([id, color]) => graphArrowMarker("ar-" + id, color)).join("") + "</defs>";
  const arrowSvg = arrows
    .sort((a, b) => a.heat - b.heat)
    .map((a) => {
      const line = ' x1="' + a.sx + '" y1="' + a.sy + '" x2="' + (a.sx + a.ux * a.shaft) + '" y2="' + (a.sy + a.uy * a.shaft) + '"';
      return '<line class="forecast-casing"' + line + '/><line class="forecast-line"' + line + ' stroke="' + a.color + '" marker-end="url(#ar-' + a.id + ')"/>';
    })
    .join("");
  return defs + trails.join("") + arrowSvg + marks.join("");
}
function highlightGraphHistory(key) {
  document.querySelectorAll("#plotsvg [data-history]").forEach((path) => path.classList.toggle("active", path.dataset.history === key));
}

function viewIcon(view) {
  const shape =
    view === "graph" ? '<path d="M3 3v14h15M6 12l4-4 3 2 5-6"/>' : '<rect x="3" y="3" width="15" height="14" rx="2"/><path d="M3 8h15M3 12h15M8 3v14"/>';
  return (
    '<svg class="view-icon" viewBox="0 0 21 21" aria-hidden="true" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round">' +
    shape +
    "</svg>"
  );
}
function graphLegend(baselines) {
  const medianKeys = baselines
    .map(
      (b) =>
        '<span class="graph-median-key" title="Your ' +
        providerName(b.provider) +
        ' median across models and efforts"><svg viewBox="0 0 32 12" aria-hidden="true">' +
        (b.provider === "codex"
          ? '<defs><linearGradient id="codex-legend-line" gradientUnits="userSpaceOnUse" x1="1" y1="6" x2="31" y2="6"><stop offset="0" stop-color="#434FFF"/><stop offset=".5" stop-color="#7A9DFF"/><stop offset="1" stop-color="#A6A5FF"/></linearGradient></defs>'
          : "") +
        '<path d="M1 6H31" fill="none" stroke="' +
        (b.provider === "codex" ? "url(#codex-legend-line)" : b.color) +
        '" stroke-width="1.9" stroke-dasharray="5 4"/></svg>' +
        providerName(b.provider) +
        " median</span>",
    )
    .join("");
  const arrowKeys = [
    [0, "Below median"],
    [0.5, "At median"],
    [1, "2× median+"],
  ]
    .map(([value, label], i) => {
      const color = rampColor(value),
        id = "legend-arrow-" + i,
        line = ' x1="4" y1="18" x2="34" y2="6"';
      return (
        '<span class="graph-arrow-key"><svg viewBox="0 0 40 24" aria-hidden="true"><defs>' +
        graphArrowMarker(id, color) +
        '</defs><line class="forecast-casing"' +
        line +
        '/><line class="forecast-line"' +
        line +
        ' stroke="' +
        color +
        '" marker-end="url(#' +
        id +
        ')"/></svg>' +
        label +
        "</span>"
      );
    })
    .join("");
  return (
    '<div class="graph-key" aria-label="Graph legend"><div class="graph-median-keys">' +
    (medianKeys || '<span class="tiny">Median unavailable</span>') +
    '</div><div class="graph-forecast-key" title="Arrow direction shows predicted spending over the next 10 prompts. Color compares predicted cost per prompt with your provider median at this stage."><span class="graph-key-label">Next 10 prompts</span>' +
    arrowKeys +
    "</div></div>"
  );
}

function fleetGraph(rows) {
  const measurable = rows.filter((s) => cost(s) !== null && count(s) > 0),
    W = 1100,
    H = 450,
    L = 65,
    R = 40,
    T = 25,
    B = 48;
  const fullX = Math.max(10, ...measurable.map((s) => count(s) + (forecast(s) !== null ? 10 : 0))) * 1.08,
    fullY = Math.max(1, ...measurable.map((s) => cost(s) + (forecast(s) ?? 0))) * 1.12;
  const xmax = Math.max(5, rampLimit(axisPos.x, fullX)),
    ymax = Math.max(0.01, rampLimit(axisPos.y, fullY));
  const x = (n) => L + (n / xmax) * (W - L - R),
    y = (n) => H - B - (n / ymax) * (H - T - B),
    baselines = graphBaselines();
  let svg =
    '<defs><clipPath id="fleet-clip"><rect x="' +
    L +
    '" y="' +
    T +
    '" width="' +
    (W - L - R) +
    '" height="' +
    (H - T - B) +
    '"/></clipPath><linearGradient id="codexline" x1="0" y1="1" x2="1" y2="0"><stop offset="0" stop-color="#434FFF"/><stop offset="0.5" stop-color="#7A9DFF"/><stop offset="1" stop-color="#A6A5FF"/></linearGradient></defs>';
  for (const v of tickValues(0, ymax, ymax >= 1 ? Math.min(5, Math.floor(ymax)) : 5))
    svg +=
      '<line class="grid-line" x1="' +
      L +
      '" x2="' +
      (W - R) +
      '" y1="' +
      y(v) +
      '" y2="' +
      y(v) +
      '"/><text x="' +
      (L - 12) +
      '" y="' +
      (y(v) + 3) +
      '" text-anchor="end">' +
      axisMoney(v) +
      "</text>";
  for (const n of tickValues(0, xmax, 5)) svg += '<text x="' + x(n) + '" y="' + (H - B + 22) + '" text-anchor="middle">' + Math.round(n) + "</text>";
  svg +=
    '<text x="' +
    W / 2 +
    '" y="' +
    (H - 4) +
    '" text-anchor="middle">Iterations</text><text transform="translate(15 ' +
    H / 2 +
    ') rotate(-90)" text-anchor="middle">Spent</text><g clip-path="url(#fleet-clip)">';
  for (const b of baselines) {
    const geometry = medianGeometry(b.points, xmax, ymax);
    if (!geometry) continue;
    const map = (points) => points.map((p) => x(p.iterations) + "," + y(p.median_cost_usd)).join(" ");
    const label = b.label + (b.label.includes("median") ? "" : " median");
    const tip =
      label +
      " usage — " +
      b.points.map((p) => p.iterations + " iterations ≈ " + money(p.median_cost_usd)).join(", ") +
      ", across your own sessions over the last " +
      (state.payload?.baselines?.lookback_days || 60) +
      " days. The curve continues the last measured slope beyond available checkpoints.";
    svg +=
      '<polyline class="baseline-line" stroke="' +
      (b.provider === "codex" ? "url(#codexline)" : b.color) +
      '" points="' +
      map(geometry.curve) +
      '"><title>' +
      esc(tip) +
      "</title></polyline>";
  }
  svg += sessionGraphMarks(measurable, x, y, W, R, T);
  svg += "</g>" + axisHandles(W, H, L, R, T, B, xmax, ymax);
  const missing = rows.length - measurable.length;
  return (
    '<div class="graph-toolbar"><span>Spend against iterations <small>Drag either axis to zoom · hover a session to trace its history.</small></span><button id="reset-axes"' +
    (axisPos.x === 1 && axisPos.y === 1 ? " hidden" : "") +
    ' title="You can also double-click the graph">Reset axes ↺</button></div><svg id="plotsvg" class="fleet-graph" viewBox="0 0 ' +
    W +
    " " +
    H +
    '" role="group" aria-label="Spend against iterations for every live session">' +
    svg +
    "</svg>" +
    graphLegend(baselines) +
    (missing ? '<p class="graph-note">' + missing + " sessions lack plot data; available in the ledger.</p>" : "")
  );
}
function saveUrl() {
  const q = new URLSearchParams();
  if (state.view === "graph") q.set("view", "graph");
  if (state.provider !== "all") q.set("tool", state.provider);
  if (state.sort !== "forecast") q.set("sort", state.sort);
  if (state.baselineMode !== "matched") q.set("compare", state.baselineMode);
  if (state.selected) q.set("session", state.selected);
  history.replaceState(null, "", location.pathname + (q.size ? "?" + q : "") + location.hash);
}
function initialUrl() {
  const q = new URLSearchParams(location.search);
  state.view = q.get("view") === "graph" ? "graph" : "ledger";
  state.provider = ["claude", "codex"].includes(q.get("tool")) ? q.get("tool") : "all";
  state.sort = ["activity", "spent", "forecast"].includes(q.get("sort")) ? q.get("sort") : "forecast";
  state.baselineMode = q.get("compare") === "provider" ? "provider" : "matched";
  state.selected = q.get("session");
  $("#sort").value = state.sort;
  saveUrl();
}
function renderAccounts() {
  const quotas = state.payload?.account_quotas;
  let html = "";
  for (const provider of ["codex", "claude"]) {
    const q = quotas?.[provider];
    for (const w of Array.isArray(q?.windows) ? q.windows : []) {
      if (!finite(w.used_percent) || !finite(w.window_minutes) || w.window_minutes <= 0) continue;
      html +=
        "<span>" +
        providerName(provider) +
        " · " +
        duration(w.window_minutes * 60) +
        " <b>" +
        Math.max(0, Math.min(100, 100 - w.used_percent)).toFixed(0) +
        "% left</b></span>";
    }
  }
  $("#account-strip").innerHTML = html;
  $("#account-strip").hidden = !html;
}
function render() {
  if (axisDragging) return;
  hideGraphTip();
  state.now = Date.now();
  const rows = sortedRows(),
    stale = state.error || !state.payload || elapsed(state.payload.generated_at) > 120000;
  $("#live-dot").className = "live-dot" + (rows.length && !stale && !state.error ? " active" : "");
  $("#live-count").textContent = rows.length;
  $("#freshness").textContent = state.error ? "Collector unreachable" : "Updated " + age(state.payload?.generated_at) + " ago";
  $("#freshness").classList.toggle("stale", stale);
  document.querySelectorAll("[data-provider]").forEach((b) => b.setAttribute("aria-pressed", String(b.dataset.provider === state.provider)));
  $("#baseline-mode").value = state.baselineMode;
  $("#ledger-toolbar").hidden = state.view === "graph";
  $("#view-switch").innerHTML = state.view === "graph" ? viewIcon("ledger") + "<span>View as ledger</span>" : viewIcon("graph") + "<span>View as graph</span>";
  $("#fleet").innerHTML = rows.length
    ? state.view === "graph"
      ? fleetGraph(rows)
      : ledger(rows)
    : '<div class="empty"><h3>No live sessions</h3><p>No activity in the last 20 minutes' +
      (state.provider !== "all" ? " for " + providerName(state.provider) : "") +
      ". New sessions appear automatically.</p></div>";
  renderAccounts();
  renderInspector();
}
function bindEvents() {
  document.addEventListener(
    "toggle",
    (event) => {
      const group = event.target;
      if (!(group instanceof HTMLDetailsElement) || !group.isConnected || !group.dataset.agentGroup) return;
      if (group.open) subagentOpenGroups.add(group.dataset.agentGroup);
      else subagentOpenGroups.delete(group.dataset.agentGroup);
    },
    true,
  );
  bindGraphControls();
  document.addEventListener("click", (event) => {
    const target = event.target;
    if (!(target instanceof Element)) return;
    const provider = target.closest("[data-provider]"),
      session = target.closest("[data-session]"),
      chart = target.closest("[data-chart]");
    if (provider) {
      axisPos.x = 1;
      axisPos.y = 1;
      state.provider = provider.dataset.provider;
      saveUrl();
      render();
    } else if (session) openSession(session.dataset.session, session.matches("button,g") ? session : session.querySelector("button"));
    else if (chart) {
      state.chart = chart.dataset.chart;
      renderInspector();
      document.querySelector('[data-chart="' + state.chart + '"]')?.focus();
    }
  });
  $("#view-switch").addEventListener("click", () => {
    state.view = state.view === "ledger" ? "graph" : "ledger";
    saveUrl();
    render();
  });
  $("#close").addEventListener("click", closeSession);
  $("#backdrop").addEventListener("click", closeSession);
  $("#sort").addEventListener("change", (event) => {
    state.sort = event.target.value;
    saveUrl();
    render();
  });
  document.addEventListener("change", (event) => {
    if (event.target.id === "baseline-mode") {
      state.baselineMode = event.target.value;
      saveUrl();
      render();
      $("#baseline-mode")?.focus();
    }
  });
  document.addEventListener("keydown", (event) => {
    if (state.selected) {
      if (event.key === "Escape") {
        event.preventDefault();
        closeSession();
      } else if (event.key === "Tab") {
        const nodes = Array.from($("#inspector").querySelectorAll('button,a[href],select,input,[tabindex="0"]')),
          first = nodes[0],
          last = nodes.at(-1);
        if (event.shiftKey && document.activeElement === first) {
          event.preventDefault();
          last.focus();
        } else if (!event.shiftKey && document.activeElement === last) {
          event.preventDefault();
          first.focus();
        }
      }
    } else if ((event.key === "Enter" || event.key === " ") && event.target.matches("g[data-session]")) {
      event.preventDefault();
      openSession(event.target.dataset.session, event.target);
    }
  });
  window.addEventListener("popstate", () => {
    initialUrl();
    render();
  });
}
async function refresh() {
  if (axisDragging) return;
  try {
    const response = await fetch("/api/live-sessions", { cache: "no-store" });
    if (!response.ok) throw new Error("Collector request failed");
    const payload = await response.json();
    if (!payload || !Array.isArray(payload.sessions) || !finite(Date.parse(payload.generated_at))) throw new Error("Invalid snapshot");
    state.payload = payload;
    state.error = false;
    browserAlerts(payload);
  } catch {
    state.error = true;
  }
  const active = document.activeElement,
    key = active?.dataset?.session,
    chart = active?.dataset?.chart,
    baseline = active?.id === "baseline-mode",
    axis = active?.dataset?.axis;
  render();
  if (key) {
    Array.from(document.querySelectorAll("[data-session]"))
      .find((b) => b.dataset.session === key && b.matches("button,g"))
      ?.focus({ preventScroll: true });
  } else if (chart) document.querySelector('[data-chart="' + chart + '"]')?.focus({ preventScroll: true });
  else if (baseline) $("#baseline-mode")?.focus({ preventScroll: true });
  else if (axis) document.querySelector('[data-axis="' + axis + '"]')?.focus({ preventScroll: true });
}
function contextCompactions(s) {
  return compacts(s).filter((event) => nonnegative(event.cumulative_cost_usd));
}
function compactionMarkers(marks, x, T, bottom, value) {
  const groups = new Map();
  for (const event of marks) {
    const pixel = x(value(event)),
      key = Math.round(pixel / 9);
    if (groups.has(key)) groups.get(key).marks.push(event);
    else groups.set(key, { x: pixel, marks: [event] });
  }
  return Array.from(groups.values())
    .map((group) => {
      const text = group.marks
        .map(
          (event) =>
            "Compacted " +
            new Date(event.timestamp).toLocaleString() +
            " · " +
            money(event.cumulative_cost_usd) +
            " recorded by this event" +
            (event.iteration ? " · during prompt " + event.iteration : " · before the first prompt") +
            (nonnegative(event.pre_tokens)
              ? " · " + tokens(event.pre_tokens) + (nonnegative(event.post_tokens) ? " → " + tokens(event.post_tokens) : "") + " context tokens"
              : nonnegative(event.observed_pre_tokens)
                ? " · last observed context " + tokens(event.observed_pre_tokens) + " at " + timeLabel(event.context_observed_at)
                : ""),
        )
        .join("\n");
      return (
        '<g class="compact-marker" tabindex="0" role="img" aria-label="' +
        esc(text) +
        '"><title>' +
        esc(text) +
        '</title><line x1="' +
        group.x +
        '" x2="' +
        group.x +
        '" y1="' +
        T +
        '" y2="' +
        bottom +
        '"/><circle cx="' +
        group.x +
        '" cy="' +
        (T - 7) +
        '" r="4"/><text x="' +
        group.x +
        '" y="' +
        (T - 5) +
        '" text-anchor="middle">' +
        (group.marks.length > 1 ? group.marks.length : "↧") +
        '</text><rect x="' +
        (group.x - 5) +
        '" y="2" width="10" height="' +
        (bottom - 2) +
        '" fill="transparent"/></g>'
      );
    })
    .join("");
}
function promptCostGraph(s) {
  const rows = series(s);
  if (!rows.length) return '<div class="empty"><p>No recorded prompt costs yet.</p></div>';
  const W = 530,
    H = 240,
    L = 52,
    R = 14,
    T = 28,
    B = 44,
    n = Math.max(1, ...rows.map((q) => q.iteration));
  const values = rows.map((q) => q.cost_usd),
    c = comparison(s),
    max = Math.max(0.01, ...values, c?.before || 0) * 1.1;
  const step = tickValues(0, max, 5)[1] || max,
    ymax = Math.ceil(max / step) * step;
  const x = (i) => L + (i / n) * (W - L - R),
    y = (v) => H - B - (v / ymax) * (H - T - B);
  let svg = "";
  for (const v of tickValues(0, ymax, 5))
    svg +=
      '<line x1="' +
      L +
      '" x2="' +
      (W - R) +
      '" y1="' +
      y(v) +
      '" y2="' +
      y(v) +
      '" stroke="' +
      COLORS.line +
      '"/><text x="' +
      (L - 7) +
      '" y="' +
      (y(v) + 3) +
      '" text-anchor="end">' +
      axisMoney(v) +
      "</text>";
  for (const i of [...new Set([0, Math.round(n / 4), Math.round(n / 2), Math.round((n * 3) / 4), n])])
    svg += '<text x="' + x(i) + '" y="' + (H - B + 16) + '" text-anchor="middle">' + i + "</text>";
  svg += rows
    .map(
      (q) =>
        '<rect class="prompt-cost-bar" x="' +
        (x(q.iteration - 1) + 1) +
        '" y="' +
        y(q.cost_usd) +
        '" width="' +
        Math.max(0.5, (W - L - R) / n - 2) +
        '" height="' +
        (y(0) - y(q.cost_usd)) +
        '" rx="1" fill="' +
        (q.completed === false ? COLORS.muted : COLORS.purple) +
        '"><title>Prompt ' +
        q.iteration +
        ": " +
        money(q.cost_usd) +
        "</title></rect>",
    )
    .join("");
  const marks = contextCompactions(s).filter((e) => e.iteration > 0);
  svg += compactionMarkers(marks, x, T, H - B, (e) => e.iteration - 0.5);
  svg +=
    '<text x="' +
    (L + W - R) / 2 +
    '" y="' +
    (H - 5) +
    '" text-anchor="middle">Prompt number</text><text transform="translate(11 ' +
    (T + H - B) / 2 +
    ') rotate(-90)" text-anchor="middle">Cost ($)</text>';
  return (
    '<svg class="session-graph" viewBox="0 0 ' +
    W +
    " " +
    H +
    '" role="img" aria-label="Cost per prompt for this session">' +
    svg +
    '</svg><div class="legend-inline"><span><i></i>Cost per prompt</span>' +
    (marks.length ? '<span class="compact-key"><i></i>Compacted · ' + marks.length + " events</span>" : "") +
    "</div>"
  );
}
function sessionGraph(s) {
  if (state.chart === "prompt") return promptCostGraph(s);
  const marks = contextCompactions(s),
    history = Array.isArray(s.context_history) ? s.context_history : [];
  const rows = history
    .filter((q) => nonnegative(q.context_tokens) && q.context_tokens > 0 && nonnegative(q.cumulative_cost_usd) && finite(Date.parse(q.timestamp)))
    .map((q) => ({ ...q, order: 0 }));
  for (const event of marks) {
    for (const [key, order] of [
      ["pre_tokens", 1],
      ["post_tokens", 2],
    ])
      if (nonnegative(event[key]) && event[key] > 0)
        rows.push({
          timestamp: event.timestamp,
          cumulative_cost_usd: event.cumulative_cost_usd,
          context_tokens: event[key],
          iteration: event.iteration,
          order,
          compaction: true,
        });
  }
  rows.sort((a, b) => Date.parse(a.timestamp) - Date.parse(b.timestamp) || a.order - b.order);
  if (!rows.length) return '<div class="empty"><p>No recorded context history yet.</p></div>';
  const W = 530,
    H = 240,
    L = 52,
    R = 14,
    T = 28,
    B = 44;
  const ceiling = (v) => {
    const step = tickValues(0, v, 5)[1] || v;
    return Math.ceil(v / step) * step;
  };
  const xmax = ceiling(Math.max(0.01, ...rows.map((q) => q.cumulative_cost_usd), ...marks.map((e) => e.cumulative_cost_usd)) * 1.05);
  const ymax = ceiling(Math.max(1, ...rows.map((q) => q.context_tokens / 1000)) * 1.04);
  const x = (v) => L + (v / xmax) * (W - L - R),
    y = (v) => H - B - (v / ymax) * (H - T - B);
  let svg = "";
  for (const v of tickValues(0, ymax, 5))
    svg +=
      '<line x1="' +
      L +
      '" x2="' +
      (W - R) +
      '" y1="' +
      y(v) +
      '" y2="' +
      y(v) +
      '" stroke="' +
      COLORS.line +
      '"/><text x="' +
      (L - 7) +
      '" y="' +
      (y(v) + 3) +
      '" text-anchor="end">' +
      Number(v.toPrecision(6)).toLocaleString("en-US") +
      "k</text>";
  for (const v of tickValues(0, xmax, 5)) svg += '<text x="' + x(v) + '" y="' + (H - B + 16) + '" text-anchor="middle">' + axisMoney(v) + "</text>";
  svg +=
    '<polyline class="context-cost-path" points="' +
    rows.map((q) => x(q.cumulative_cost_usd) + "," + y(q.context_tokens / 1000)).join(" ") +
    '" fill="none" stroke="' +
    COLORS.purple +
    '" stroke-width="1.4" stroke-linejoin="round" opacity=".7"/>';
  svg += rows
    .map((q, i) => {
      const latest = i === rows.length - 1,
        title =
          (q.compaction ? "Compaction " + (q.order === 1 ? "before" : "after") : "Prompt " + q.iteration) +
          " · " +
          tokens(q.context_tokens) +
          " context tokens · " +
          money(q.cumulative_cost_usd) +
          " recorded spend · " +
          new Date(q.timestamp).toLocaleString() +
          (q.context_observed_at && q.context_observed_at !== q.timestamp ? " · context last observed " + timeLabel(q.context_observed_at) : "");
      return (
        '<circle class="context-cost-point" cx="' +
        x(q.cumulative_cost_usd) +
        '" cy="' +
        y(q.context_tokens / 1000) +
        '" r="' +
        (latest ? 3.8 : 1.5) +
        '" fill="' +
        (q.compaction ? COLORS.pink : COLORS.purple) +
        '" stroke="white" stroke-width="' +
        (latest ? 1.3 : 0.4) +
        '" opacity="' +
        (latest ? 1 : 0.65) +
        '"><title>' +
        esc(title) +
        "</title></circle>"
      );
    })
    .join("");
  svg += compactionMarkers(marks, x, T, H - B, (e) => e.cumulative_cost_usd);
  svg +=
    '<text x="' +
    (L + W - R) / 2 +
    '" y="' +
    (H - 5) +
    '" text-anchor="middle">Total spent ($)</text><text transform="translate(11 ' +
    (T + H - B) / 2 +
    ') rotate(-90)" text-anchor="middle">Context tokens (k)</text>';
  return (
    '<svg class="session-graph" viewBox="0 0 ' +
    W +
    " " +
    H +
    '" role="img" aria-label="Context tokens against cumulative spending for this session">' +
    svg +
    '</svg><div class="legend-inline"><span><i></i>Cumulative spend</span>' +
    (marks.length ? '<span class="compact-key"><i></i>Compacted · ' + marks.length + " events</span>" : "") +
    '</div><p class="context-chart-note">Context follows recorded usage. Compaction markers show spend recorded by the event; the next context reading can arrive later.</p>'
  );
}
function tokenBreakdown(s) {
  const u = s.token_usage || {},
    parts = [
      ["New input", u.input, COLORS.purple],
      ["Cache reads", u.cache_read, COLORS.mint],
      ["Cache writes", u.cache_write, COLORS.muted],
      ["Output", u.output, COLORS.pink],
    ];
  const total = parts.reduce((a, p) => a + (nonnegative(p[1]) ? p[1] : 0), 0);
  return (
    '<div class="token-bar">' +
    parts.map((p) => '<i style="width:' + (total ? ((p[1] || 0) / total) * 100 : 0) + "%;background:" + p[2] + '"></i>').join("") +
    '</div><div class="token-key">' +
    parts.map((p) => '<span><i style="background:' + p[2] + '"></i>' + p[0] + " <b>" + tokens(p[1]) + "</b></span>").join("") +
    '</div><p class="detail-foot" style="margin-top:8px">Token traffic across the recorded session. Cache reads can repeat the same tokens; this is not a breakdown of the current context.</p>'
  );
}
function renderInspector() {
  const panel = $("#inspector"),
    s = allRows().find((s) => keyOf(s) === state.selected || s.id === state.selected);
  const wasOpen = !panel.hidden;
  panel.hidden = !s;
  $("#backdrop").hidden = !s;
  for (const node of [document.querySelector("main"), document.querySelector("header"), $("#account-strip")]) node.inert = !!s;
  document.body.style.overflow = s ? "hidden" : "";
  if (!s) {
    if (state.selected && state.payload) {
      state.selected = null;
      saveUrl();
    }
    return;
  }
  const a = assess(s),
    pct = context(s),
    rows = series(s),
    last = rows.at(-1),
    scroll = $("#inspector-body").scrollTop;
  $("#inspector-body").innerHTML =
    '<span class="eyebrow">' +
    providerName(s.provider) +
    " · " +
    esc(s.client || "local") +
    '</span><h2 class="inspector-title" id="inspector-title">' +
    esc(displayTitle(s)) +
    '</h2><div class="inspector-meta">' +
    esc(s.model || "Model unavailable") +
    " · " +
    esc(effort(s) ? effort(s) + " effort" : "Effort not recorded") +
    "<br>" +
    activity(s).label +
    " · last activity " +
    age(s.last_activity_at) +
    " ago · " +
    age(startTime(s)) +
    ' old</div><div class="inspector-stats"><div><span>Recorded spend</span><strong>' +
    money(cost(s)) +
    "</strong><small>" +
    count(s) +
    " prompts</small></div><div><span>" +
    (last?.completed === false ? "Current prompt" : "Last prompt") +
    "</span><strong>" +
    money(last?.priced === false ? null : last?.cost_usd) +
    "</strong><small>" +
    (last?.completed === false ? "still accumulating" : "recorded cost") +
    "</small></div><div><span>Next 10 prompts</span><strong>" +
    additional(forecast(s)) +
    "</strong><small>additional estimate</small></div><div><span>Context</span><strong>" +
    (pct !== null ? Math.round(pct) + "%" : "—") +
    "</strong><small>" +
    tokens(s.context_tokens) +
    " / " +
    tokens(s.context_window_tokens) +
    "</small></div></div>" +
    (a.severity ? '<div class="inspector-signal"><strong>' + esc(a.action) + "</strong><p>" + esc(a.evidence) + "</p></div>" : "") +
    '<div class="section-title"><h3>How this session is spending</h3><div class="mini-tabs"><button data-chart="cumulative" class="' +
    (state.chart === "cumulative" ? "on" : "") +
    '">Cumulative</button><button data-chart="prompt" class="' +
    (state.chart === "prompt" ? "on" : "") +
    '">Per prompt</button></div></div>' +
    sessionGraph(s) +
    '<div class="section-title"><h3>Where the tokens went</h3><span class="tiny">Recorded token traffic</span></div>' +
    tokenBreakdown(s) +
    subagentDetails(s);
  $("#inspector-body").scrollTop = scroll;
  if (!wasOpen) $("#close").focus();
}
function openSession(id, opener) {
  hideGraphTip();
  state.lastOpener = opener;
  state.selected = id;
  state.chart = "cumulative";
  $("#inspector-body").scrollTop = 0;
  saveUrl();
  renderInspector();
}
function closeSession() {
  state.selected = null;
  saveUrl();
  renderInspector();
  if (state.lastOpener?.isConnected) state.lastOpener.focus();
  else $("#view-switch")?.focus();
}

const notificationUi = { busy: false, testSent: false };
function notificationPermission() {
  return "Notification" in window ? Notification.permission : "unsupported";
}
function notificationButton() {
  const permission = notificationPermission(),
    allowed = permission === "granted";
  const status = {
    granted: "Browser permission allowed",
    default: "Browser permission needed",
    denied: "Blocked in this browser",
    unsupported: "Unavailable in this browser",
  };
  $("#notification-status").textContent = status[permission] || status.unsupported;
  $("#notification-status").dataset.state = permission;
  $("#konvu-alert-control").dataset.state = permission;
  $("#notification-enable").hidden = permission !== "default";
  $("#notification-enable").disabled = notificationUi.busy;
  $("#notification-test").disabled = !allowed || notificationUi.busy;
  $("#notification-help").textContent =
    permission === "denied"
      ? "Open this site’s browser settings, set Notifications to Allow, then reload."
      : permission === "unsupported"
        ? "Open this dashboard in a regular browser, such as Chrome or Safari, to enable alerts."
        : allowed
          ? "This site can send notifications. Check delivery with a test below."
          : "Click Allow notifications below, then choose Allow in your browser.";
  $("#notification-delivery").textContent =
    notificationUi.testSent && allowed
      ? "Test sent. Did it appear? If not, check your computer’s notification and Focus settings."
      : "Computer notification settings cannot be checked here. Send a test to verify delivery.";
}
function closeNotificationPanel(restoreFocus = false) {
  $("#notification-panel").hidden = true;
  $("#konvu-alert-control").setAttribute("aria-expanded", "false");
  if (restoreFocus) $("#konvu-alert-control").focus();
}
function bindNotificationPanel() {
  $("#konvu-alert-control").addEventListener("click", () => {
    const open = $("#notification-panel").hidden;
    $("#notification-panel").hidden = !open;
    $("#konvu-alert-control").setAttribute("aria-expanded", String(open));
    if (open) {
      notificationButton();
      $("#notification-close").focus();
    }
  });
  $("#notification-close").addEventListener("click", () => closeNotificationPanel(true));
  document.addEventListener("click", (event) => {
    if (!$("#notification-panel").hidden && !event.target.closest(".notification-wrap")) closeNotificationPanel();
  });
  document.addEventListener(
    "keydown",
    (event) => {
      if (event.key === "Escape" && !$("#notification-panel").hidden) {
        event.preventDefault();
        event.stopImmediatePropagation();
        closeNotificationPanel(true);
      }
    },
    true,
  );
  window.addEventListener("focus", notificationButton);
  $("#notification-enable").addEventListener("click", async () => {
    if (notificationPermission() !== "default") return;
    notificationUi.busy = true;
    notificationButton();
    $("#notification-feedback").textContent = "";
    try {
      await Notification.requestPermission();
    } catch {
      $("#notification-feedback").textContent = "Permission could not be requested. Open this dashboard in your regular browser and try again.";
    } finally {
      notificationUi.busy = false;
      notificationButton();
    }
  });
  $("#notification-test").addEventListener("click", () => {
    notificationButton();
    if (notificationPermission() !== "granted") return;
    $("#notification-feedback").textContent = "";
    try {
      const test = new Notification("Konvu notification test", {
        body: "Your coding-agent alerts can reach this computer.",
        icon: "/konvu-ghost.svg",
        tag: "konvu-browser-test",
        requireInteraction: true,
      });
      test.onerror = () => {
        $("#notification-feedback").textContent = "The browser could not deliver the test. Check notification settings on your computer.";
      };
      notificationUi.testSent = true;
      notificationButton();
    } catch {
      $("#notification-feedback").textContent = "The test could not be sent. Check browser and computer notification settings.";
    }
  });
  notificationButton();
}

function browserAlerts(payload) {
  if (notificationPermission() !== "granted") return;
  for (const [provider, quotas] of Object.entries(payload.account_quotas || {})) {
    for (const alert of quotas?.notifications || []) {
      if (!alert?.hot || !Number.isInteger(alert.sequence) || alert.sequence < 1) continue;
      const key = "konvu-quota-alert-" + provider + "-" + alert.window;
      if (Number(localStorage.getItem(key) || 0) >= alert.sequence) continue;
      localStorage.setItem(key, String(alert.sequence));
      const notification = new Notification(
        providerName(provider) + " " + alert.window + " limit at " + alert.used_percent + "%",
        {
          body: alert.used_percent + "% of your " + alert.window + " limit is used.",
          icon: "/konvu-ghost.svg",
          requireInteraction: true,
          tag: key,
        }
      );
      notification.onclick = () => {
        window.focus();
        notification.close();
      };
    }
  }
  for (const session of payload.sessions || []) {
    const alert = session.notification;
    if (!alert?.hot || !Number.isInteger(alert.sequence) || alert.sequence < 1) continue;
    const key = "konvu-alert-" + session.provider + "-" + session.id;
    if (Number(localStorage.getItem(key) || 0) >= alert.sequence) continue;
    localStorage.setItem(key, String(alert.sequence));
    const overhead = Math.round(session.baseline?.cost_overhead_percent || 0);
    const notification = new Notification(providerName(session.provider) + " session running hot", {
      body:
        "💸 $" +
        Number(session.total_cost_usd || 0).toFixed(1) +
        " total · $" +
        Number(session.projected_next_10_tasks_usd || 0).toFixed(1) +
        " for the next 10 prompts\n🔥 " +
        overhead +
        "% above your usual burn",
      icon: "/konvu-ghost.svg",
      requireInteraction: true,
      tag: key,
    });
    notification.onclick = () => {
      window.focus();
      openSession(keyOf(session));
      notification.close();
    };
  }
}

initialUrl();
bindEvents();
bindNotificationPanel();
refresh();
setInterval(refresh, 15000);
