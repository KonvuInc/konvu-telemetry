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
const BURNING_FORECAST_USD = 10;
const previewName = new URLSearchParams(location.search).get("preview");
const previewMode = ["subscription", "states", "topics"].includes(previewName);
const state = {
  payload: null,
  view: "ledger",
  provider: "all",
  sort: "forecast",
  selected: null,
  chart: "cumulative",
  inspectorTab: "overview",
  topicMode: "topics",
  error: false,
  refreshInFlight: false,
  nextRefreshAt: null,
  refreshIntervalMs: null,
  snapshotEtag: null,
  detailRequest: 0,
  now: Date.now(),
  lastOpener: null,
};
let healthTimer = null;
const $ = (selector) => document.querySelector(selector);
const esc = (value) => String(value ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);
const finite = (value) => typeof value === "number" && Number.isFinite(value);
const nonnegative = (value) => finite(value) && value >= 0;
const oneDecimal = (value) => Math.round((value + Number.EPSILON) * 10) / 10;
const percentage = (value) => (finite(value) ? oneDecimal(value).toFixed(1) + "%" : "—");
const money = (value) => (nonnegative(value) ? "$" + oneDecimal(value).toLocaleString("en-US", { minimumFractionDigits: 1, maximumFractionDigits: 1 }) : "—");
const additional = (value) => (nonnegative(value) ? "+" + money(value) : "—");
const compactMoney = (value) =>
  nonnegative(value) ? "$" + (value >= 1000 ? oneDecimal(value / 1000).toFixed(1) + "k" : oneDecimal(value).toFixed(1)) : "—";
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
const compactDuration = (seconds) => {
  seconds = Math.max(0, seconds);
  for (const [unit, size] of [["d", 86400], ["h", 3600], ["m", 60], ["s", 1]])
    if (seconds >= size || unit === "s") return Number(oneDecimal(seconds / size)) + unit;
};
const resetCountdown = (timestamp) => {
  const resetAt = Date.parse(timestamp);
  if (!finite(resetAt)) return null;
  return compactDuration((resetAt - state.now) / 1000);
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
const showsMoney = (s) => s.usage_mode === "exhausted" || s.usage_mode === "api_billed";
const cost = (s) => {
  if (!showsMoney(s) || s.cost_status === "unavailable") return null;
  if (s.usage_mode === "api_billed") return nonnegative(s.total_cost_usd) ? s.total_cost_usd : null;
  if (s.out_of_plan_spend_status === undefined) return null;
  return nonnegative(s.out_of_plan_spend_usd) ? s.out_of_plan_spend_usd : null;
};
function planExitCostOffset(s) {
  if (s.usage_mode !== "exhausted") return 0;
  if (!nonnegative(s.total_cost_usd) || !nonnegative(s.out_of_plan_spend_usd)) return 0;
  return Math.max(0, s.total_cost_usd - s.out_of_plan_spend_usd);
}
function chartSpend(s, value) {
  return Math.max(0, value - planExitCostOffset(s));
}
const chartSpendLabel = (s) => (s.usage_mode === "exhausted" ? "spend since plan exit" : "recorded spend");
const forecast = (s) => (cost(s) !== null && nonnegative(s.projected_next_10_tasks_usd) ? s.projected_next_10_tasks_usd : null);
const quotaShare = (s, period = "five_hour") => {
  const windows = s.quota_attribution?.windows;
  const row = Array.isArray(windows) ? windows.find((w) => w?.period === period && finite(w.estimated_percent)) : null;
  return row ? row.estimated_percent : null;
};
const context = (s) =>
  nonnegative(s.context_tokens) && finite(s.context_window_tokens) && s.context_window_tokens > 0
    ? (s.context_tokens / s.context_window_tokens) * 100
    : null;
const count = (s) => (Number.isInteger(s.task_count) && s.task_count >= 0 ? s.task_count : series(s).length);
const startTime = (s) => s.session_started_at || series(s)[0]?.started_at || null;
const compacts = (s) => (Array.isArray(s.compact_events) ? s.compact_events.filter((e) => finite(Date.parse(e.timestamp))) : []);
function displayTitle(s) {
  let title = String(s.title || "")
    .replace(/\s+/g, " ")
    .trim()
    .replace(/^#{1,6}\s+/, "");
  if (!title || /^(Codex|Claude) session [a-f0-9]/.test(title)) return providerName(s.provider) + " session " + s.id.slice(0, 8);
  if (title.startsWith("Browser comments:")) {
    const comment = title.match(/Comment:\s*([\s\S]+)/);
    return comment ? comment[1].slice(0, 180) : "Browser feedback · " + providerName(s.provider);
  }
  return title;
}
function liveWindowMs() {
  const seconds = state.payload?.live_activity_window_seconds;
  return (finite(seconds) && seconds > 0 ? seconds : 20 * 60) * 1000;
}
function liveWindowLabel() {
  const minutes = Math.round(liveWindowMs() / 60000);
  return minutes + (minutes === 1 ? " minute" : " minutes");
}
function activity(s) {
  const sinceActivity = elapsed(s.last_activity_at);
  const live = finite(sinceActivity) && sinceActivity >= -60000 && sinceActivity <= liveWindowMs();
  const a = s.activity;
  if (a?.state === "running") return { live, label: "Running", kind: "running", explicit: true };
  if (a?.state === "idle") return { live, label: "Idle", kind: "idle", explicit: true };
  return { live, label: live ? "Recently active" : "Inactive", kind: "unknown", explicit: false };
}
function comparableRows(s) {
  const rows = series(s);
  const model = s.forecast_basis?.model || s.model,
    effort = s.forecast_basis?.effort || s.reasoning_effort,
    speed = s.forecast_basis?.speed || s.speed;
  if (!model || !effort || !speed) return [];
  const recent = [];
  for (let i = rows.length - 1; i >= 0; i--) {
    const q = rows[i];
    if (i === rows.length - 1 && q.completed === false) continue;
    if (q.completed !== true || q.priced !== true || q.model !== model || q.reasoning_effort !== effort || q.speed !== speed) break;
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
      ? "Recent prompts compared with earlier prompts in this session, on the same model, effort, and speed."
      : "A cost trend needs six completed, priced prompts with recorded matching model, effort, and speed.",
    score: f || 0,
  };
  if (c && c.ratio >= 2 && c.after - c.before >= 0.1) {
    result.severity = 2;
    result.label = "Cost per prompt is rising";
    result.action = "Review the more expensive prompts";
    result.evidence =
      "Last 3 prompts averaged " + money(c.after) + " vs " + money(c.before) + " before (" + c.ratio.toFixed(1) + "×), on the same model, effort, and speed.";
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
      const value = (s) => {
        switch (state.sort) {
          case "forecast":
            return showsMoney(s) ? forecast(s) : shareAhead(s);
          case "spent":
            return cost(s);
          case "share":
            return showsMoney(s) ? cost(s) : shareOf(s);
          case "context":
            return context(s);
          case "activity":
            return Date.parse(s.last_activity_at);
          default:
            return assess(s).severity * 1000 + assess(s).score;
        }
      };
      return (value(b) ?? -1) - (value(a) ?? -1) || keyOf(a).localeCompare(keyOf(b));
    });
}
const effort = (s) => s.reasoning_effort || s.effort || null;
function configurationLabel(s) {
  return [s.model || providerName(s.provider), effort(s) ? effort(s) + " effort" : "Effort unrecorded"].filter(Boolean).join(" · ");
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
function subagentContext(a) {
  return nonnegative(a.context_tokens) ? a.context_tokens : a.entry_context_tokens;
}
function subagentSpend(s, item) {
  if (!subagentUsesDollars(s)) return item.cost_usd;
  return item.out_of_plan_cost_usd ?? item.out_of_plan_subagent_cost_usd;
}
function subagentNode(s, a, index = null) {
  const id = typeof a.id === "string" ? a.id : "",
    label = subagentLabel(a) + (index === null ? "" : " " + (index + 1)),
    metric = subagentMetric(s, subagentSpend(s, a));
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
    (typeof a.description === "string" && a.description ? " · " + esc(a.description) : "") +
    '</small></div><div class="agent-context">' +
    tokens(subagentContext(a)) +
    "<small>" +
    (nonnegative(subagentContext(a)) ? "current context" : "Not recorded") +
    '</small></div>' +
    '<div class="agent-cost">' + metric.value + "<small>" + metric.note + "</small></div>" +
    "</div>"
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
    const priced = children.map((a) => subagentSpend(s, a)).filter(nonnegative);
    return priced.length ? priced.reduce((sum, cost) => sum + cost, 0) : -1;
  };
  const groups = Array.from(grouped.entries()).sort((a, b) => groupCost(b[1]) - groupCost(a[1]) || a[0].localeCompare(b[0]));
  const branches = groups
    .map(([label, children]) => {
      children.sort(
        (a, b) =>
          (nonnegative(subagentSpend(s, b)) ? subagentSpend(s, b) : -1) - (nonnegative(subagentSpend(s, a)) ? subagentSpend(s, a) : -1) || String(a.id || "").localeCompare(String(b.id || "")),
      );
      if (children.length === 1) return '<div class="agent-branch">' + subagentNode(s, children[0]) + "</div>";
      const key = keyOf(s) + "|" + label,
        live = children.filter((a) => a.live === true).length,
        contexts = children.map(subagentContext).filter(nonnegative),
        priced = children.map((a) => subagentSpend(s, a)).filter(nonnegative);
      const contextRange = contexts.length
        ? Math.min(...contexts) === Math.max(...contexts)
          ? tokens(contexts[0])
          : tokens(Math.min(...contexts)) + "–" + tokens(Math.max(...contexts))
        : "—";
      const contextNote =
        contexts.length === children.length
          ? "current context each"
          : contexts.length
            ? "current context · " + contexts.length + "/" + children.length + " recorded"
            : "Not recorded";
      const metric = subagentMetric(
        s,
        priced.length ? priced.reduce((sum, cost) => sum + cost, 0) : null,
      );
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
        '</small></div>' +
        '<div class="agent-cost">' + metric.value + "<small>" + metric.note + "</small></div>" +
        '</summary><div class="agent-children">' +
        children.map((a, i) => subagentNode(s, a, i)).join("") +
        "</div></details>"
      );
    })
    .join("");
  return (
    '<div class="agent-map"><div class="agent-map-header"><span>Agent</span><span>Context received</span><span>' + subagentMetricHeading(s) + '</span></div><div class="agent-root"><i></i>This session</div><div class="agent-branches">' +
    branches +
    "</div></div>"
  );
}
function contextMapMatchesCurrent(s) {
  const mapped = s.context_map?.observed_context_tokens;
  const current = s.context_tokens;
  if (!nonnegative(mapped) || !nonnegative(current) || mapped === 0 || current === 0) return true;
  return Math.max(mapped, current) <= Math.min(mapped, current) * 1.5;
}
function analysisFor(s) {
  const analysis = s.context_map?.analysis;
  if (analysis?.state !== "ready" || !Array.isArray(analysis.themes) || !contextMapMatchesCurrent(s)) return null;
  const technical = analysis.technical_categories;
  const mapped = s.context_map?.observed_context_tokens;
  if (Array.isArray(technical) && nonnegative(mapped) && mapped > 0) {
    const estimated = technical.reduce((sum, row) => sum + (nonnegative(row?.tokens) ? row.tokens : 0), 0);
    if (estimated > 0 && Math.max(estimated, mapped) > Math.min(estimated, mapped) * 1.5) return null;
  }
  return analysis;
}
/* Shown once compactable context fills 20 points of the context window (same rule as the
   status line): the AI's keep and drop lists as a ready /compact command. */
const COMPACT_RECOMMEND_WINDOW_POINTS = 20;
const COMPACT_MIN_COVERAGE_PERCENT = 70;
function compactAdvice(s) {
  const analysis = analysisFor(s);
  if (!analysis || typeof analysis.compact_command !== "string" || (s.provider === "codex" && typeof analysis.compact_prompt !== "string")) return "";
  // Only clearly dead context (finished, replaced, rejected, noise) counts toward the hint.
  const waste = clampPercent(analysis.droppable_percent);
  // Advice built on a small reviewed slice of the window is not shown (same rule as the status line).
  if (clampPercent(analysis.coverage_percent) < COMPACT_MIN_COVERAGE_PERCENT) return "";
  const used = context(s);
  if (!nonnegative(used) || (waste / 100) * clampPercent(used) < COMPACT_RECOMMEND_WINDOW_POINTS) return "";
  const terminal = (label, command) => '<div class="compact-terminal"><div class="compact-terminal-bar"><span>' + label + '</span><button type="button" class="compact-advice-copy" data-copy-compact="' + esc(command) + '">Copy</button></div><pre class="compact-advice-command"><code>' + esc(command) + '</code></pre></div>';
  const steps = s.provider === 'codex'
    ? '<div class="compact-steps"><div><h4>1 · Send this message to Codex</h4>' + terminal('Message', 'My next message will be /compact. Please follow these priorities:\n' + analysis.compact_prompt) + '</div><div><h4>2 · Then send this command</h4>' + terminal('Command', '/compact') + '</div></div>'
    : terminal('Claude Code', analysis.compact_command);
  return '<section class="compact-advice" aria-label="Compact suggestion"><div class="compact-advice-head"><h3>Compact this session</h3><span>' + percentage(waste) + ' finished or replaced</span></div>' + steps + '</section>';
}
function clampPercent(value) {
  return nonnegative(value) ? Math.min(100, Math.max(0, value)) : 0;
}
function contextWindowSlices(s) {
  const analysis = analysisFor(s);
  const observed = context(s);
  if (!nonnegative(observed)) return { analysis: null, used: null, portions: [], background: '#edeaf2' };
  const used = clampPercent(observed);
  // Two bands: needed, and compactable (anything rated below needed, drifting included).
  const weights = analysis ? [clampPercent(analysis.relevant_percent), 0, clampPercent(clampPercent(analysis.drifting_percent) + clampPercent(analysis.stale_percent))] : [];
  const rated = weights.reduce((sum, value) => sum + value, 0);
  const scale = rated > 100 ? 100 / rated : 1;
  const portions = weights.map((value) => used * value * scale / 100);
  const slices = analysis
    ? [['#2c9d75', portions[0]], ['#e5ad34', portions[1]], ['#dc5b65', portions[2]], ['#b9b1c9', Math.max(0, used - portions.reduce((sum, value) => sum + value, 0))], ['#edeaf2', 100 - used]]
    : [['#b9b1c9', used], ['#edeaf2', 100 - used]];
  let position = 0;
  const background = 'conic-gradient(' + slices.map(([color, amount]) => {
    const from = position;
    position = Math.min(100, position + amount);
    return color + ' ' + from.toFixed(2) + '% ' + position.toFixed(2) + '%';
  }).join(',') + ')';
  return { analysis, used, portions, background };
}
function donut(s) {
  const slices = contextWindowSlices(s);
  if (slices.used === null) return '<div class="context-donut" role="img" aria-label="Context use unavailable"><i class="context-donut-ring" style="background:#edeaf2"></i><span>—<small>window used</small></span></div>';
  const runs = slices.analysis?.run_events;
  const lastRun = (Array.isArray(runs) && runs.length ? runs[runs.length - 1] : null) || slices.analysis?.last_run;
  const iteration = lastRun?.iteration;
  const review = slices.analysis ? 'AI reviewed' + (finite(iteration) && iteration > 0 ? ' after prompt ' + iteration : '') : 'No AI review';
  const label = percentage(slices.used) + ' of context window used; ' + review + (slices.analysis ? '; colors show relevance as a share of the full window' : '');
  return '<div class="context-donut" role="img" aria-label="' + esc(label) + '"><i class="context-donut-ring" style="background:' + slices.background + '"></i><span>' + percentage(slices.used) + '<small>window used</small><small class="context-review-note">' + esc(review) + '</small></span></div>';
}
function roundedDollarCeiling(value) {
  const total = Math.max(0.01, value),
    unit = 10 ** Math.floor(Math.log10(total));
  return [1, 2, 5, 10].map((n) => n * unit).find((n) => n >= total);
}
function ledgerDollarScale(rows) {
  return roundedDollarCeiling(Math.max(1, ...rows.map((s) => (cost(s) ?? 0) + (forecast(s) ?? 0))));
}
/* Same three-stage idea as the burning cash, but measured in share of a window:
   10% of a limit in ten prompts is where it stops being noise. */
function quotaFireStage(percent) {
  return !nonnegative(percent) || percent < 10 ? 0 : percent < 15 ? 1 : percent < 25 ? 2 : 3;
}
/* The one fire in this codebase. burningCashIcon adds bills under it; the quota
   fire uses it bare. Classes match the existing bonfire animations. */
function bonfireFlames(stage, scale) {
  return (
    '<g transform="' + scale + '"><g class="bonfire-tongue flame-back"><path fill="#E34D1C" d="M14 43C1 31 15 23 8 13c11 3 9 12 15 15C16 13 33 12 29 1c17 9 9 19 15 24 6-5 3-12 7-16 0 12 17 17 7 31-8 11-33 12-44 3z"/></g>' +
    '<g class="bonfire-tongue flame-left"><path fill="#FF8D22" d="M16 43C5 35 15 28 12 20c9 4 4 9 11 12-4-11 6-15 5-23 12 13-1 20 6 29l-3 9z"/></g>' +
    '<g class="bonfire-tongue flame-right"><path fill="#FFAB27" d="M29 44c-8-10 10-15 8-26 9 6 4 13 8 16 6-5 4-10 6-13 9 13 4 23-10 27z"/></g>' +
    '<g class="bonfire-tongue flame-core"><path fill="#FFE58F" d="M23 42c-4-8 10-13 8-22 11 9-2 13 5 18 1-4 4-6 6-7 3 13-10 16-19 11z"/></g></g>'
  );
}
function bonfireFront(scale) {
  return (
    '<g transform="' + scale + '"><g class="bonfire-tongue flame-front">' +
    '<path fill="#F47720" d="M25 48c-5-5 0-9-3-15 9 4 3 9 8 10 1-5 7-7 6-13 9 9 0 19-11 18z"/>' +
    '<path fill="#FFD268" d="M28 47c-2-4 4-6 3-10 6 6 2 10-3 10z"/></g></g>'
  );
}
function bonfireEmbers(stage) {
  if (stage === 1) return "";
  return (
    '<g fill="#F88822"><circle class="bonfire-ember" cx="13" cy="18" r="1.2"/><circle class="bonfire-ember ember-mid" cx="35" cy="10" r="1"/>' +
    (stage === 3 ? '<circle class="bonfire-ember ember-late" cx="52" cy="18" r="1.1"/>' : "") + "</g>"
  );
}
/* Traced from the reference flame: a tall body, a hooked tip curling left, and
   a plain teardrop core. Stages add whole flames behind the front one in
   deeper reds, so the group reads as one fire getting bigger. */
const QF_OUT =
  '<path fill="$1" d="M36 2c3 12-1 20-7 26-4 4-8 7-8 12 0 4 3 6 6 5 4-2 5-7 4-12 10 8 17 21 17 33 0 18-15 32-34 32S-16 84-16 66c0-12 6-21 11-29 4-6 8-12 8-19 0-4-1-8-3-11 9 3 16 9 19 17 2-5 3-10 2-15 5 2 10 6 13 10 1-4 2-9 2-15Z"/>';
const QF_CORE =
  '<path fill="$2" d="M9 30c8 7 15 17 15 26 0 9-7 15-15 15S-6 65-6 56c0-9 7-19 15-26Z"/>';
const QF_HOT = ["#FF5722", "#FFC13B"];
const QF_DEEP = ["#B52F07", "#E0530F"];
/* Placement sits on the outer group; the flicker owns the inner one, because
   .bonfire-tongue animates transform and would otherwise erase the placement. */
function qfFlame(colors, place) {
  return (
    '<g transform="' + place + '"><g class="bonfire-tongue">' +
    QF_OUT.replace("$1", colors[0]) + QF_CORE.replace("$2", colors[1]) +
    "</g></g>"
  );
}
const QF_STAGES = [
  null,
  { box: "-20 0 60 100", body: () => qfFlame(QF_HOT, "translate(0 0)") },
  {
    box: "-44 -6 88 106",
    body: () => qfFlame(QF_DEEP, "translate(-34 22) scale(.6)") + qfFlame(QF_HOT, "translate(0 0)"),
  },
  {
    box: "-50 -10 116 110",
    body: () =>
      qfFlame(QF_DEEP, "translate(-40 24) scale(.58)") +
      qfFlame(QF_DEEP, "translate(40 14) scale(.68)") +
      qfFlame(QF_HOT, "translate(0 0)"),
  },
];
function quotaFireIcon(percent) {
  const stage = quotaFireStage(percent);
  if (!stage) return "";
  const art = QF_STAGES[stage];
  const label =
    ["", "Warming up", "Burning through your limit", "Tearing through your limit"][stage] +
    ": +" + percentage(percent) + " of the limit over the next 10 prompts";
  return (
    '<span class="quota-fire fire-' + stage + '" role="img" aria-label="' + esc(label) + '" title="' + esc(label) + '">' +
    '<svg viewBox="' + art.box + '" aria-hidden="true">' + art.body() + "</svg></span>"
  );
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
/* A forecast far above what a session has already spent is the drift signal.
   One chevron per stage reads faster than an illustration and never becomes
   decoration. */
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
    '<span class="cash-alert burn-stage-' + stage + '" role="img" aria-label="' + esc(label) + '" title="' + esc(label) + '">' +
    '<svg class="bills-bonfire" viewBox="0 0 64 64" aria-hidden="true">' +
    '<ellipse class="bonfire-glow" cx="32" cy="50" rx="' + (stage === 1 ? 18 : 28) + '" ry="7" fill="#FF8A24" opacity=".15"/>' +
    bonfireFlames(stage, flameScale) +
    pile.map(bonfireBill).join("") +
    bonfireFront(stage === 1 ? "translate(7 12) scale(.8)" : "") +
    bonfireEmbers(stage) +
    "</svg></span>"
  );
}
function providerQuotaState(provider, account) {
  if (!account || account.status === "fetching") {
    return {
      kind: "loading",
      label: "Loading " + providerName(provider) + " limits…",
      detail: "Konvu is fetching the subscription limits reported by your local " + providerName(provider) + " account.",
    };
  }
  const errorCode = Number.isInteger(account.error_code) ? " Error " + account.error_code + "." : "";
  if (account.status === "unavailable") {
    if (["credentials_unavailable", "authentication_failed"].includes(account.failure)) {
      const command = provider === "claude" ? "claude /login" : "codex login";
      return {
        kind: "login",
        label: "Sign in to " + providerName(provider),
        detail: "Run " + command + ". Konvu uses this local login only to fetch subscription limits; credentials never leave your computer." + errorCode,
      };
    }
    return { kind: "error", label: "Couldn’t load " + providerName(provider) + " limits · retrying", detail: "Konvu will retry automatically." + errorCode };
  }
  if (account.status === "stale") {
    return { kind: "stale", label: providerName(provider) + " limits delayed · retrying", detail: "The displayed percentages are from the last successful fetch." + errorCode };
  }
  return { kind: "ready", label: providerName(provider) + " limits ready", detail: "" };
}
function accountQuotaFor(s) {
  if (previewMode && s.preview_account_quota) return s.preview_account_quota;
  return state.payload?.account_quotas?.[s.provider];
}
function sessionDataState(s, metric = "forecast") {
  const providerState = providerQuotaState(s.provider, accountQuotaFor(s));
  if (providerState.kind !== "ready" && metric !== "plan") {
    if (providerState.kind === "login") return providerState;
    const noun = metric === "share" ? "Share" : "Forecast";
    const labels = {
      loading: noun + " starts after limits load",
      login: noun + " unavailable until sign-in",
      error: noun + " paused while retrying",
      stale: noun + " paused while limits refresh",
    };
    return { ...providerState, label: labels[providerState.kind] || providerState.label };
  }
  if (providerState.kind !== "ready") return providerState;
  const reason = s.quota_attribution?.reason;
  const noun = metric === "share" ? "Share" : "Forecast";
  // Each state waits on something specific, so say which. No countdown is
  // possible: the limit moves with usage, not with the clock.
  const labels = {
    window_reset: ["Ready after your next prompts", "The limit window just reset, so Konvu needs fresh activity in it."],
    establishing_baseline: ["Ready after the next plan update", "Konvu needs two plan readings before it can estimate this session."],
    waiting_for_quota_change: ["Ready when your usage ticks up 1%", "Your prompts are recorded. Plans report whole percentages, so Konvu waits for the next one."],
    waiting_for_activity: ["Ready after a few more prompts", "This session has not used enough yet for Konvu to estimate it."],
  };
  const [label, detail] = labels[reason] || ["Ready after the next plan update", "Konvu needs another reading."];
  return { kind: "loading", label, detail };
}
/* A fixed popover needs real coordinates. Placed above the trigger when there
   is room, below otherwise, and clamped to the viewport so it is never cut. */
function placeStatePopover(trigger) {
  const popover = trigger.querySelector(".state-popover");
  if (!popover) return;
  popover.style.visibility = "hidden";
  popover.style.display = "block";
  const anchor = trigger.getBoundingClientRect();
  const box = popover.getBoundingClientRect();
  const gap = 7;
  const above = anchor.top - box.height - gap;
  const top = above >= 8 ? above : anchor.bottom + gap;
  const left = Math.max(8, Math.min(anchor.left, window.innerWidth - box.width - 8));
  popover.style.top = Math.round(top) + "px";
  popover.style.left = Math.round(left) + "px";
  popover.style.visibility = "";
}
function bindStatePopovers() {
  for (const event of ["pointerenter", "focusin"]) {
    document.addEventListener(
      event,
      (e) => {
        const trigger = e.target instanceof Element ? e.target.closest(".data-state") : null;
        if (trigger) placeStatePopover(trigger);
      },
      true,
    );
  }
}
function dataStateMarkup(data) {
  // A spinner would promise the value is arriving on its own; it is not. The
  // figure needs another plan reading, so say that and let the tooltip explain
  // which one is missing.
  if (data.kind === "loading") {
    return (
      '<span class="pending-sentence" tabindex="0" title="' + esc(data.detail) + '">' +
      "Not enough data to compute</span>"
    );
  }
  const marker = data.kind === "loading"
    ? '<i class="state-spinner" aria-hidden="true"></i>'
    : ["login", "error"].includes(data.kind)
      ? '<i class="state-warning" aria-hidden="true">!</i>'
      : "";
  return (
    '<span class="data-state ' + esc(data.kind) + '"' + (data.detail ? ' tabindex="0" title="' + esc(data.detail) + '" aria-label="' + esc(data.label + ". " + data.detail) + '"' : "") + '>' + marker +
    '<span>' + esc(data.label) + "</span>" +
    (data.detail ? '<span class="state-popover" role="tooltip">' + esc(data.detail) + "</span>" : "") +
    "</span>"
  );
}
function spendVisual(s, scale) {
  const spent = cost(s),
    next = forecast(s);
  if (s.usage_mode === "included" || s.usage_mode === "unknown") {
    const included = s.usage_mode === "included";
    const ahead = shareAhead(s);
    const window = quotaWindowName(s);
    const dataState = sessionDataState(s, "forecast");
    if (!included) {
      return '<div class="spending included-copy">' + dataStateMarkup(sessionDataState(s, "plan")) + "</div>";
    }
    const status = s.quota_status === "stale"
      ? "Included — limits delayed"
      : "Included in your plan";
    // The forecast keeps its place under the heading; it is only nudged up in
    // size and weight so it stops disappearing into the row.
    // Keeps main's shared percentage() so precision matches the rest of the
    // page; only the number is emphasised.
    const line = finite(ahead)
      ? '<small class="plan-next">Next 10 prompts: <b>+' +
        percentage(ahead) + "</b> of " + esc(window) + " limit</small>"
      : '<small class="plan-next">Next 10 prompts: ' + dataStateMarkup(dataState) + "</small>";
    return (
      '<div class="spending included-copy"><div class="included-copy-text"><strong>' +
      esc(status) + "</strong>" + line + "</div>" +
      (finite(ahead) ? quotaFireIcon(ahead) : "") + "</div>"
    );
  }
  const values =
      '<div class="spend-values"><strong>' +
      money(spent) +
      (s.quota_status === "stale" ? '<small>Plan status last known</small>' : "") +
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
/* Share is drawn, not written, and it stays meaningful for both provider
   states: a quota session owns part of a limit, a paying session owns part of
   the money. Same arc, same reading, two different denominators. */
/* Deliberately not a donut: context already owns that shape. A share is a slice
   of one whole, so it is drawn as a slice of a full-width track. */
function shareBar(value, sentence, title) {
  return (
    '<div class="share-bar" title="' + esc(title) + '">' +
    '<div class="share-line"><b>' + percentage(value) + "</b>" +
    "<span>" + esc(sentence) + "</span></div>" +
    '<i><em style="width:' + Math.max(1, Math.min(100, value)) + '%"></em></i></div>'
  );
}
function responsibilityCell(s) {
  if (showsMoney(s)) {
    const total = allRows().filter(showsMoney).reduce((sum, row) => sum + (cost(row) ?? 0), 0);
    const mine = cost(s);
    if (!nonnegative(mine)) return '<span class="tiny" title="No spend recorded for this session yet.">Not recorded</span>';
    // Nothing has been billed yet, so no session owns a share of zero.
    if (total <= 0) return '<span class="tiny" title="Nothing has been billed beyond the plan yet.">Nothing spent yet</span>';
    const share = (mine / total) * 100;
    return shareBar(share, "of all money spent", money(mine) + " of " + money(total) + " spent beyond plan");
  }
  const share = shareOf(s);
  if (!finite(share)) return dataStateMarkup(sessionDataState(s, "share"));
  const window = quotaWindowName(s);
  return shareBar(share, "of your " + window + " limit", "This session is responsible for " + percentage(share) + " of the " + window + " limit");
}
function subagentCell(s) {
  const total = Number.isInteger(s.subagent_total) && s.subagent_total >= 0 ? s.subagent_total : "—";
  const live = Number.isInteger(s.active_subagents) && s.active_subagents >= 0 ? s.active_subagents : "—";
  const metric = subagentMetric(s, subagentSpend(s, s));
  return (
    '<div class="subagent-cell"><span><strong>' +
    total +
    '</strong> spawned · <strong class="' +
    (live > 0 ? "subagents-live" : "") +
    '">' +
    live +
    '</strong> live</span>' +
    (metric.value !== "—" ? '<small title="' + esc(metric.title) + '">' + metric.value + (subagentUsesDollars(s) ? " est. cost" : " est. context") + "</small>" : "") +
    "</div>"
  );
}
/* Being inside the plan or paying for it is a property of the PROVIDER: when
   Codex runs out of quota every Codex session is paying at once. Sessions are
   therefore split by that state, not by provider, so a table only ever holds
   one currency — percent of a limit, or dollars — and sorting inside it stays
   provider-agnostic. When every provider agrees, there is nothing to split and
   one table is shown. */
/* A provider counts as paying once money has actually been recorded against
   it. Quota hitting 100% is not enough on its own: the five-hour window
   refills constantly, so keying off it alone made rows jump between the two
   tables on every refresh and put "$0.00" under a "Spending real money"
   heading. The exhausted state still shows — as a note on the plan table —
   until the first cent lands. */
function providerHasSpend(provider, rows) {
  return rows.some((s) => s.provider === provider && (cost(s) ?? 0) > 0);
}
function providerExhausted(provider, rows) {
  if (state.payload?.account_quotas?.[provider]?.ordinary_usage_allowed === false) return true;
  return rows.some((s) => s.provider === provider && showsMoney(s));
}
function providerState(provider, rows) {
  return providerExhausted(provider, rows) && providerHasSpend(provider, rows);
}
function planGroups(rows) {
  const providers = [...new Set(rows.map((s) => s.provider))];
  const paying = new Set(providers.filter((p) => providerState(p, rows)));
  const groups = [
    { paying: false, rows: rows.filter((s) => !paying.has(s.provider)), providers: providers.filter((p) => !paying.has(p)) },
    { paying: true, rows: rows.filter((s) => paying.has(s.provider)), providers: providers.filter((p) => paying.has(p)) },
  ]
    .filter((g) => g.rows.length)
    .sort((a, b) => (b.paying ? 1 : 0) - (a.paying ? 1 : 0));
  for (const g of groups) {
    g.spend = g.rows.reduce((sum, s) => sum + (cost(s) ?? 0), 0);
    g.next = g.rows.reduce((sum, s) => sum + (forecast(s) ?? 0), 0);
  }
  return groups;
}
function groupHeading(group) {
  const n = group.rows.length;
  const waiting = !group.paying && group.providers.some((p) => providerExhausted(p, group.rows));
  return (
    '<div class="group-heading ' + (group.paying ? "paying" : "included") + '">' +
    "<h2>" + (group.paying ? "Spending real money" : "Within your plan") + "</h2>" +
    '<span class="head-count">' + n + " session" + (n === 1 ? "" : "s") + "</span>" +
    (waiting ? '<span class="head-warn" title="Quota is spent, so the next prompts will be charged. Nothing has been billed yet.">Quota spent — billing starts on the next prompt</span>' : "") +
    ('<div class="heading-controls"><div class="quota-inline">' + quotaInline(
      group.providers,
      group.providers.some((provider) => providerExhausted(provider, group.rows))
    ) + "</div>" +
        '<label class="sort-control">Sort<select class="sort-select" aria-label="Sort sessions">' +
        (group.paying
          ? [["spent", "Most spent"], ["forecast", "Highest forecast"], ["share", "Share of spend"], ["context", "Context used"], ["activity", "Last activity"]]
          : [["share", "Share of limit"], ["forecast", "Highest forecast"], ["context", "Context used"], ["activity", "Last activity"]])
          .map(([v, t]) => '<option value="' + v + '"' + (state.sort === v ? " selected" : "") + ">" + t + "</option>")
          .join("") +
        "</select></label></div>") +
    "</div>"
  );
}
/* ============ fleet summary ============
   One banner only. Real money outranks quota, so the moment a single session
   bills, every number in the banner is computed from paying sessions alone —
   a subscription session must never contribute to a dollar figure. */
const avg = (values) => (values.length ? values.reduce((a, b) => a + b, 0) / values.length : null);
const shareOf = (s) => quotaShare(s, s.provider === "codex" ? "weekly" : "five_hour");
function subagentUsesDollars(s) {
  return providerState(s.provider, allRows());
}
function subagentContextShare(s, costValue) {
  const sessionContext = context(s);
  if (!nonnegative(costValue) || !finite(sessionContext) || !nonnegative(s.total_cost_usd) || s.total_cost_usd <= 0) return null;
  return (costValue / s.total_cost_usd) * sessionContext;
}
function subagentMetric(s, costValue) {
  if (!nonnegative(costValue)) return { value: "—", note: "Not recorded", title: "No recorded subagent usage." };
  if (subagentUsesDollars(s)) {
    return {
      value: money(costValue),
      note: "API-equivalent since plan exit",
      title: "API-equivalent subagent cost recorded since the plan was observed exhausted.",
    };
  }
  const share = subagentContextShare(s, costValue);
  if (!finite(share)) return { value: "—", note: "Context share unavailable", title: "The session context is not available yet." };
  return {
    value: percentage(share),
    note: "estimated of session context",
    title: "Estimated from this subagent's recorded API-equivalent share of the session, multiplied by the session's current context usage.",
  };
}
function subagentMetricHeading(s) {
  return subagentUsesDollars(s) ? "API-equivalent ↓" : "Estimated context share ↓";
}
/* The collector reports "observing" while it still lacks the history to
   attribute usage. Naming that beats a bare dash, which reads like a bug. */
function windowUsedPercent(s) {
  const period = s.provider === "codex" ? "weekly" : "five_hour";
  const windows = state.payload?.account_quotas?.[s.provider]?.windows;
  const row = Array.isArray(windows) ? windows.find((w) => w?.period === period) : null;
  return row && finite(row.used_percent) ? row.used_percent : null;
}
const quotaWindowName = (s) => (s.provider === "codex" ? "weekly" : "5-hour");
function shareAhead(s) {
  const period = s.provider === "codex" ? "weekly" : "five_hour";
  const row = s.quota_attribution?.windows?.find((w) => w?.period === period && finite(w.projected_next_10_percent));
  return row ? row.projected_next_10_percent : null;
}
/* Four bands, worst last, so one scale covers every percentage on the page. */
function band(value, warn, high, bad) {
  if (!finite(value)) return "none";
  return value >= bad ? "bad" : value >= high ? "high" : value >= warn ? "warning" : "good";
}
function kpiTile(label, value, tone, note, wide) {
  return (
    '<div class="kpi-tile' + (wide ? " kpi-lead" : "") + '"><span class="kpi-label">' + esc(label) + "</span>" +
    '<b class="metric-' + tone + (wide ? " metric-primary" : " metric-secondary") + '">' + value + "</b>" +
    (note || "") + "</div>"
  );
}
/* The culprit is a session, so it is shown as one — provider mark and title,
   not a bare string that reads like a stray caption. */
function kpiSession(s, extra) {
  return (
    '<button class="kpi-session" type="button" data-session="' + esc(keyOf(s)) + '" title="' + esc(displayTitle(s)) + '">' +
    '<img src="/' + (s.provider === "claude" ? "claude" : "codex") + '.png" alt="" width="12" height="12">' +
    "<em>" + esc(displayTitle(s)) + "</em>" + (extra ? "<i>" + esc(extra) + "</i>" : "") + "</button>"
  );
}
const kpiFootnote = (text) => '<small class="kpi-note">' + esc(text) + "</small>";
function planTiles(rows) {
  const heaviest = rows.filter((s) => finite(shareOf(s))).sort((a, b) => shareOf(b) - shareOf(a))[0];
  const drifting = rows.filter((s) => finite(shareAhead(s))).sort((a, b) => shareAhead(b) - shareAhead(a))[0];
  const avgContext = avg(rows.map(context).filter(finite));
  const windowOf = (s) => (s.provider === "codex" ? "weekly" : "5-hour");
  const topSubagent = rows
    .map((s) => ({ session: s, share: subagentContextShare(s, s.subagent_cost_usd) }))
    .filter(({ share }) => finite(share))
    .sort((a, b) => b.share - a.share)[0];
  return [
    kpiTile("Spent beyond plan", money(0), "good", kpiFootnote("nothing is billing"), true),
    heaviest ? kpiTile("Using most of your " + windowOf(heaviest) + " limit", percentage(shareOf(heaviest)), band(shareOf(heaviest), 10, 20, 35), kpiSession(heaviest)) : "",
    drifting ? kpiTile("Growing fastest", "+" + percentage(shareAhead(drifting)), band(shareAhead(drifting), 4, 8, 12), kpiSession(drifting)) : "",
    finite(avgContext) ? kpiTile("Average context used", percentage(avgContext), band(avgContext, 50, 70, 88), kpiFootnote("across " + rows.length + " sessions")) : "",
    topSubagent ? kpiTile("Most subagent context", percentage(topSubagent.share), band(topSubagent.share, 3, 6, 10), kpiSession(topSubagent.session, "estimated")) : "",
  ].filter(Boolean);
}
function moneyTiles(rows) {
  const spend = rows.reduce((sum, s) => sum + (cost(s) ?? 0), 0);
  const priciest = rows.filter((s) => finite(cost(s))).sort((a, b) => cost(b) - cost(a))[0];
  const hottest = rows.filter((s) => finite(forecast(s))).sort((a, b) => forecast(b) - forecast(a))[0];
  const avgContext = avg(rows.map(context).filter(finite));
  const topSubagent = rows
    .filter((s) => nonnegative(subagentSpend(s, s)))
    .sort((a, b) => subagentSpend(b, b) - subagentSpend(a, a))[0];
  return [
    kpiTile("Spent beyond plan", money(spend), spend > 0 ? "bad" : "good", kpiFootnote("across " + rows.length + " billing sessions"), true),
    priciest ? kpiTile("Most expensive session", money(cost(priciest)), band((cost(priciest) / (spend || 1)) * 100, 25, 45, 65), kpiSession(priciest)) : "",
    hottest ? kpiTile("Biggest forecast", additional(forecast(hottest)), band(forecast(hottest), 5, 12, 20), kpiSession(hottest)) : "",
    finite(avgContext) ? kpiTile("Average context used", percentage(avgContext), band(avgContext, 50, 70, 88), kpiFootnote("across " + rows.length + " billing sessions")) : "",
    topSubagent ? kpiTile("Highest subagent spend", money(subagentSpend(topSubagent, topSubagent)), band((subagentSpend(topSubagent, topSubagent) / (spend || 1)) * 100, 15, 30, 45), kpiSession(topSubagent, "since plan exit")) : "",
  ].filter(Boolean);
}
/* Reuses planGroups so the banner and the tables can never disagree about
   which sessions are paying. */
function kpiBanner(rows) {
  const paying = planGroups(rows).find((g) => g.paying);
  const tiles = paying ? moneyTiles(paying.rows) : planTiles(rows);
  return tiles.length ? '<div class="kpi-grid">' + tiles.join("") + "</div>" : "";
}
/* Light meters, one bordered pill per provider, logo as the only label. */
function quotaInline(providers, showCodexCredits = false) {
  const quotas = state.payload?.account_quotas || {};
  return (providers && providers.length ? providers : ["claude", "codex"])
    .map((id) => {
      const quotaState = providerQuotaState(id, quotas[id]);
      const wanted = id === "codex"
        ? (showCodexCredits ? ["weekly", "monthly"] : ["weekly"])
        : ["five_hour", "weekly"];
      const windows = Array.isArray(quotas[id]?.windows) ? quotas[id].windows : [];
      const meters = windows
        .filter((w) => w && finite(w.used_percent) && wanted.includes(w.period))
        .sort((a, b) => wanted.indexOf(a.period) - wanted.indexOf(b.period))
        .map((w) => {
          const used = Math.max(0, Math.min(100, w.used_percent));
          const label = w.period === "five_hour" ? "5h" : w.period === "weekly" ? "week" : "credits";
          const reset = resetCountdown(w.resets_at);
          return '<span class="qm"><span class="qm-main"><i>' + label + '</i><u><em style="width:' + used + '%"></em></u><b>' + percentage(used) + "</b></span>" +
            (reset ? '<small>Resets in ' + reset + "</small>" : "") + "</span>";
        })
        .join("");
      return (
        '<div class="quota-pill' + (quotas[id]?.ordinary_usage_allowed === false ? " exhausted" : "") + '" title="' + esc(quotaState.detail) + '">' +
        '<span class="quota-legend"><img src="/' + esc(id) + '.png" alt="' + providerName(id) + '" width="12" height="12">' +
        (quotas[id]?.status === "stale" ? "Update delayed" : "Usage limit") + "</span>" +
        (meters || dataStateMarkup(quotaState)) + "</div>"
      );
    })
    .join("");
}
function ledgerRow(s, scale) {
  const next = forecast(s),
    burning = s.notification?.hot === true;
  return (
    '<tr class="' + (burning ? "burning-row" : "") + '" data-session="' + esc(keyOf(s)) +
    '"><td><div class="indexed-title">' + titleCell(s) + "</div></td><td>" + spendVisual(s, scale) +
    "</td><td>" + responsibilityCell(s) + "</td><td>" + donut(s) + "</td><td>" + subagentCell(s) +
    '</td><td class="time-cell"><span>' + age(s.last_activity_at) + " ago</span><small>" +
    age(startTime(s)) + " old</small></td></tr>"
  );
}
function ledgerTable(group, scale) {
  const spendHead = group.paying ? "Spend <span>/ next 10 prompts</span>" : "Plan <span>/ next 10 prompts</span>";
  const shareHead = group.paying ? "Responsible for" : "Responsible for";
  return (
    '<section class="ledger-block ' + (group.paying ? "paying" : "included") + '">' + groupHeading(group) +
    '<div class="table-shell"><table class="session-ledger compact-ledger layout-1"><thead><tr><th>Session</th><th>' +
    spendHead + "</th><th>" + shareHead + "</th><th>Context</th><th>Subagents</th><th>Activity <span>/ age</span></th></tr></thead><tbody>" +
    group.rows.map((s) => ledgerRow(s, scale)).join("") +
    "</tbody></table></div></section>"
  );
}
function ledger(rows) {
  const scale = ledgerDollarScale(rows);
  const key = '<div class="context-ring-key"><span>Context rings</span><span><i class="context-key-needed"></i>Needed</span><span title="AI estimate: not needed for the current work. Review before compacting."><i class="context-key-old"></i>Not needed now</span><span><i class="context-key-unreviewed"></i>Unreviewed</span><span><i class="context-key-free"></i>Free space</span><small>When enabled, AI reviews after 10 prompts or a major context change.</small></div>';
  return key + planGroups(rows).map((group) => ledgerTable(group, scale)).join("");
}

function curvePoints(b) {
  return Array.isArray(b)
    ? b
        .filter((p) => p && nonnegative(p.iterations) && nonnegative(p.median_cost_usd))
        .slice()
        .sort((a, b) => a.iterations - b.iterations)
    : [];
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
  const endpointX = Math.min(xmax, rows.at(-1).iterations);
  const xs = [0, ...rows.map((p) => p.iterations).filter((n) => n > 0 && n < endpointX), endpointX];
  const curve = xs.map((iterations) => ({ iterations, median_cost_usd: medianCostAt(rows, iterations) }));
  let exitX = endpointX;
  if (medianCostAt(rows, endpointX) > ymax) {
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
    percentage(context(s)) +
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
  const measurable = rows.filter((s) => cost(s) !== null && count(s) > 0);
  if (!measurable.length)
    return '<div class="empty"><h3>Subscription usage is included</h3><p>Money is hidden while these sessions remain within their provider allowance.</p></div>';
  const
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
  if (previewMode) q.set("preview", previewName);
  if (state.provider !== "all") q.set("tool", state.provider);
  if (state.sort !== "forecast") q.set("sort", state.sort);
  if (state.selected) q.set("session", state.selected);
  if (state.selected && state.inspectorTab === "context") q.set("tab", "context");
  history.replaceState(null, "", location.pathname + (q.size ? "?" + q : "") + location.hash);
}
function initialUrl() {
  const q = new URLSearchParams(location.search);
  state.provider = ["claude", "codex"].includes(q.get("tool")) ? q.get("tool") : "all";
  state.sort = ["activity", "spent", "forecast", "share", "context"].includes(q.get("sort")) ? q.get("sort") : "forecast";
  state.selected = q.get("session");
  state.inspectorTab = q.get("tab") === "context" ? "context" : "overview";
  saveUrl();
}
function render() {
  if (axisDragging) return;
  hideGraphTip();
  state.now = Date.now();
  const rows = sortedRows(),
    stale = state.error || !state.payload || elapsed(state.payload.generated_at) > 120000;
  $("#live-dot").className = "live-dot" + (rows.length && !stale && !state.error ? " active" : "");
  $("#live-count").textContent = rows.length;
  renderFreshness(stale);
  document.querySelectorAll("[data-provider]").forEach((b) => b.setAttribute("aria-pressed", String(b.dataset.provider === state.provider)));
  $("#kpi-banner").innerHTML = rows.length ? kpiBanner(rows) : "";
  $("#fleet").innerHTML = rows.length
    ? ledger(rows)
    : '<div class="empty"><h3>No live sessions</h3><p>No activity in the last ' + liveWindowLabel() +
      (state.provider !== "all" ? " for " + providerName(state.provider) : "") +
      ". New sessions appear automatically.</p></div>";
  renderInspector();
}
function renderFreshness(stale) {
  state.now = Date.now();
  const remainingSeconds = state.nextRefreshAt === null ? null : Math.max(0, Math.ceil((state.nextRefreshAt - state.now) / 1000));
  const progress = state.refreshInFlight || state.nextRefreshAt === null || state.refreshIntervalMs === null ? 0 : Math.max(0, Math.min(1, 1 - (state.nextRefreshAt - state.now) / state.refreshIntervalMs));
  const button = $("#freshness");
  button.style.setProperty("--progress", String(progress));
  button.disabled = state.refreshInFlight;
  button.title = state.refreshInFlight
    ? "Refreshing now"
    : (state.error ? "Collector unreachable. " : "") + (remainingSeconds === null ? "Waiting for collector schedule" : "Refresh now; next collector sync in " + remainingSeconds + "s");
  button.setAttribute("aria-label", button.title);
  button.classList.toggle("stale", Boolean(stale));
}
/* ============ settings: display cadence ============
   The cadence governs the usage box drawn inside a turn, in Codex CLI,
   Codex desktop and Claude desktop. A custom rule
   needs code, so the dashboard stores the wording and hands back a prompt for
   the user's own agent rather than pretending it took effect. */
const cadenceState = { options: [], cadence: "", custom_rule: "", context_analysis_enabled: false, context_analysis_consent: "unset", context_analysis_allow_paid: false, loaded: false, request: 0 };
async function loadPreferences() {
  if (previewMode) return;
  const request = ++cadenceState.request;
  cadenceState.loaded = false;
  renderSaveState();
  renderAnalysisToggles();
  try {
    const response = await fetch("/api/preferences", { cache: "no-store" });
    if (!response.ok || request !== cadenceState.request) return;
    const value = await response.json();
    if (request !== cadenceState.request) return;
    Object.assign(cadenceState, value, { loaded: true });
    renderCadenceOptions();
    renderAnalysisToggles();
  } catch {
    return;
  }
}
/* A custom cadence with no rule would save a setting that cannot do anything,
   so it is blocked rather than accepted and quietly ignored. */
const MIN_CUSTOM_RULE = 3;
function customRuleIsUsable() {
  return (($("#cadence-rule")?.value || "").trim().length >= MIN_CUSTOM_RULE);
}
function renderSaveState() {
  const save = $("#cadence-save");
  if (!save) return;
  const blocked = cadenceState.cadence === "custom" && !customRuleIsUsable();
  save.disabled = !cadenceState.loaded || blocked;
  const status = $("#cadence-status");
  // Flagged on the element rather than recognised by its text, so an unrelated
  // message (a failed save) is never mistaken for this one and cleared.
  if (status && blocked) { status.textContent = "Describe your rule first"; status.dataset.hint = "blocked"; }
  else if (status && status.dataset.hint === "blocked") { status.textContent = ""; delete status.dataset.hint; }
}
function renderCadenceOptions() {
  const host = $("#cadence-options");
  if (!host || !cadenceState.loaded) return;
  const focusWasInRule = document.activeElement?.id === "cadence-rule";
  const typed = focusWasInRule ? $("#cadence-rule").value : null;
  const rows = cadenceState.options
    .filter((option) => option.id !== "custom")
    .map(
      (option) =>
        '<button class="cadence-option" role="radio" type="button" data-cadence="' +
        esc(option.id) + '" aria-checked="' + String(option.id === cadenceState.cadence) + '">' +
        esc(option.label) + "</button>"
    )
    .join("");
  // The custom row is a field, not a label: typing in it is the clearest
  // signal that custom is what you want, so it selects itself. Its label still
  // comes from the server so it cannot drift from the command's wording.
  const customLabel =
    cadenceState.options.find((option) => option.id === "custom")?.label || "Custom rule";
  const custom =
    '<div class="cadence-option cadence-custom-row" role="radio" data-cadence="custom" aria-checked="' +
    String(cadenceState.cadence === "custom") + '">' + esc(customLabel) +
    '<input id="cadence-rule" type="text" autocomplete="off" maxlength="2000" placeholder="describe your rule" value="' +
    esc(cadenceState.custom_rule || "") + '"></div>';
  host.innerHTML = rows + custom;
  if (focusWasInRule) {
    const rule = $("#cadence-rule");
    if (rule) {
      rule.value = typed;
      rule.focus();
      rule.setSelectionRange(typed.length, typed.length);
    }
  }
  renderSaveState();
}
function renderAnalysisToggles() {
  const enabled = $("#context-analysis-enabled");
  const paid = $("#context-analysis-allow-paid");
  if (enabled) {
    enabled.checked = cadenceState.context_analysis_enabled === true;
    enabled.disabled = !cadenceState.loaded;
  }
  if (paid) {
    paid.checked = cadenceState.context_analysis_allow_paid === true;
    paid.disabled = !cadenceState.loaded || !enabled?.checked;
  }
}
function toggleAnalysisInfo() {
  const button = $("#analysis-info");
  const popover = $("#analysis-popover");
  if (!button || !popover) return;
  popover.hidden = !popover.hidden;
  button.setAttribute("aria-expanded", String(!popover.hidden));
}
function openSettings() {
  $("#settings-panel").hidden = false;
  $("#settings-backdrop").hidden = false;
  document.querySelector("main").inert = true;
  loadPreferences();
}
function closeSettings() {
  $("#settings-panel").hidden = true;
  $("#settings-backdrop").hidden = true;
  document.querySelector("main").inert = false;
  $("#settings-open")?.focus();
}
function openPromptModal(text) {
  $("#cadence-prompt-text").textContent = text;
  $("#prompt-modal").hidden = false;
  $("#prompt-backdrop").hidden = false;
  $("#cadence-copy")?.focus();
}
function closePromptModal() {
  $("#prompt-modal").hidden = true;
  $("#prompt-backdrop").hidden = true;
  document.querySelector("main").inert = false;
  $("#settings-open")?.focus();
}
async function saveCadence() {
  const status = $("#cadence-status");
  if (cadenceState.cadence === "custom" && !customRuleIsUsable()) {
    renderSaveState();
    $("#cadence-rule")?.focus();
    return;
  }
  const body = {
    cadence: cadenceState.cadence,
    custom_rule: $("#cadence-rule")?.value || "",
    context_analysis_enabled: $("#context-analysis-enabled")?.checked === true,
    context_analysis_allow_paid: $("#context-analysis-allow-paid")?.checked === true,
  };
  try {
    const response = await fetch("/api/preferences", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    if (!response.ok) throw new Error("save failed");
    const saved = await response.json();
    Object.assign(cadenceState, saved);
    renderCadenceOptions();
    if (status) status.textContent = "";
    // Saving is done, so the settings dialog has nothing left to say. A custom
    // rule still needs a prompt handed over, which gets its own dialog.
    $("#settings-panel").hidden = true;
    $("#settings-backdrop").hidden = true;
    if (saved.agent_prompt) openPromptModal(saved.agent_prompt);
    else closePromptModal();
  } catch {
    if (status) status.textContent = "Couldn’t save — is the collector running?";
  }
}
function bindSettings() {
  $("#settings-open")?.addEventListener("click", openSettings);
  $("#settings-close")?.addEventListener("click", closeSettings);
  $("#settings-backdrop")?.addEventListener("click", closeSettings);
  $("#cadence-save")?.addEventListener("click", saveCadence);
  $("#analysis-info")?.addEventListener("click", toggleAnalysisInfo);
  $("#context-analysis-enabled")?.addEventListener("change", () => {
    const paid = $("#context-analysis-allow-paid");
    if (paid) paid.disabled = !$("#context-analysis-enabled").checked;
  });
  // Copy is the only way out: the prompt is the whole point of the dialog.
  $("#cadence-copy")?.addEventListener("click", async () => {
    const text = $("#cadence-prompt-text").textContent || "";
    try {
      await navigator.clipboard?.writeText(text);
    } catch {
      // Clipboard can be refused; the prompt is still on screen to copy by hand.
    }
    closePromptModal();
  });
  document.addEventListener("input", (event) => {
    if (!(event.target instanceof Element) || event.target.id !== "cadence-rule") return;
    if (cadenceState.cadence !== "custom") {
      cadenceState.cadence = "custom";
      renderCadenceOptions();
      return;
    }
    renderSaveState();
  });
  document.addEventListener("click", (event) => {
    const option = event.target instanceof Element ? event.target.closest("[data-cadence]") : null;
    if (!option) return;
    cadenceState.cadence = option.dataset.cadence;
    $("#cadence-status").textContent = "";
    renderCadenceOptions();
  });
  document.addEventListener("keydown", (event) => {
    // The prompt dialog deliberately ignores Escape.
    if (event.key === "Escape" && !$("#settings-panel").hidden) closeSettings();
  });
  loadPreferences();
}
function bindEvents() {
  $("#freshness").addEventListener("click", refreshNow);
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
  document.addEventListener("click", async (event) => {
    const copy = event.target instanceof Element ? event.target.closest("[data-copy-compact]") : null;
    if (!copy) return;
    try {
      await navigator.clipboard?.writeText(copy.getAttribute("data-copy-compact") || "");
      copy.textContent = "Copied";
    } catch {
      copy.textContent = "Select and copy";
    }
    setTimeout(() => { copy.textContent = "Copy"; }, 1600);
  });
  document.addEventListener("click", (event) => {
    const target = event.target;
    if (!(target instanceof Element)) return;
    const provider = target.closest("[data-provider]"),
      session = target.closest("[data-session]"),
      chart = target.closest("[data-chart]"),
      inspectorTab = target.closest("[data-inspector-tab]"),
      topicMode = target.closest("[data-topic-mode]"),
      contextExpand = target.closest("[data-context-expand]"),
      contextClose = target.closest("[data-context-close]");
    if (contextClose) {
      document.querySelector("#context-breakdown-dialog")?.close();
      return;
    }
    if (contextExpand) {
      openContextBreakdown();
      return;
    }
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
    } else if (topicMode) {
      state.topicMode = topicMode.dataset.topicMode;
      renderInspector();
      document.querySelector('[data-topic-mode="' + state.topicMode + '"]')?.focus();
    } else if (inspectorTab) {
      state.inspectorTab = inspectorTab.dataset.inspectorTab;
      saveUrl();
      renderInspector();
      document.querySelector('[data-inspector-tab="' + state.inspectorTab + '"]')?.focus();
    }
  });
  $("#close").addEventListener("click", closeSession);
  $("#backdrop").addEventListener("click", closeSession);
  document.addEventListener("change", (event) => {
    if (!(event.target instanceof HTMLSelectElement) || !event.target.classList.contains("sort-select")) return;
    state.sort = event.target.value;
    saveUrl();
    render();
  });
  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape" && document.querySelector("#context-breakdown-dialog")?.open) {
      event.preventDefault();
      document.querySelector("#context-breakdown-dialog").close();
      return;
    }
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
async function refresh(triggerCollector = false, healthAlreadyRefreshed = false) {
  if (axisDragging || state.refreshInFlight) return;
  state.refreshInFlight = true;
  renderFreshness(state.error || !state.payload || elapsed(state.payload?.generated_at) > 120000);
  try {
    if (triggerCollector && !previewMode) {
      const refreshResponse = await fetch("/api/refresh", { method: "POST", cache: "no-store" });
      if (!refreshResponse.ok) throw new Error("Collector refresh failed");
    }
    const health = previewMode || healthAlreadyRefreshed ? Promise.resolve(false) : refreshHealth();
    const headers = state.snapshotEtag ? { "If-None-Match": state.snapshotEtag } : {};
    const previewPath = previewName === "states" ? "/onboarding-states-preview.json" : previewName === "topics" ? "/conversation-topics-preview.json" : "/subscription-preview.json";
    const response = await fetch(previewMode ? previewPath : "/api/live-sessions", { cache: "no-store", headers });
    await health;
    if (response.status === 304) {
      state.error = false;
    } else {
      if (!response.ok) throw new Error("Collector request failed");
      const payload = await response.json();
      if (!payload || !Array.isArray(payload.sessions) || !finite(Date.parse(payload.generated_at))) throw new Error("Invalid snapshot");
      if (previewMode) {
        payload.generated_at = new Date().toISOString();
        payload.sessions.forEach((session, index) => {
          session.last_activity_at = new Date(Date.now() - index * 45000).toISOString();
        });
      }
      state.snapshotEtag = response.headers.get("ETag");
      state.payload = payload;
      if (state.selected) await loadSessionDetails(state.selected);
      state.error = false;
      if (!previewMode) browserAlerts(payload);
    }
  } catch {
    state.error = true;
  } finally {
    state.refreshInFlight = false;
  }
  const active = document.activeElement,
    key = active?.dataset?.session,
    chart = active?.dataset?.chart,
    axis = active?.dataset?.axis;
  render();
  if (key) {
    Array.from(document.querySelectorAll("[data-session]"))
      .find((b) => b.dataset.session === key && b.matches("button,g"))
      ?.focus({ preventScroll: true });
  } else if (chart) document.querySelector('[data-chart="' + chart + '"]')?.focus({ preventScroll: true });
  else if (axis) document.querySelector('[data-axis="' + axis + '"]')?.focus({ preventScroll: true });
}
async function refreshHealth() {
  if (previewMode) return false;
  try {
    const response = await fetch("/healthz", { cache: "no-store" });
    const health = await response.json();
    const nextRefreshAt = Date.parse(health?.next_poll_at);
    const intervalMs = Number(health?.interval_seconds) * 1000;
    if (!Number.isFinite(nextRefreshAt) || !Number.isFinite(intervalMs) || intervalMs <= 0) return false;
    const advanced = nextRefreshAt !== state.nextRefreshAt;
    state.nextRefreshAt = nextRefreshAt;
    state.refreshIntervalMs = intervalMs;
    scheduleHealthRefresh();
    return advanced;
  } catch {
    return false;
  }
}
function scheduleHealthRefresh() {
  if (healthTimer !== null) window.clearTimeout(healthTimer);
  if (state.nextRefreshAt === null) return;
  healthTimer = window.setTimeout(async () => {
    const advanced = await refreshHealth();
    if (advanced) await refresh(false, true);
  }, Math.max(1000, state.nextRefreshAt - Date.now() + 100));
}
async function refreshNow() {
  await refresh(true);
}
function contextCompactions(s) {
  return compacts(s)
    .filter((event) => nonnegative(event.cumulative_cost_usd))
    .map((event) => ({ ...event, chart_cost_usd: chartSpend(s, event.cumulative_cost_usd) }))
    .filter((event) => s.usage_mode !== "exhausted" || event.chart_cost_usd > 0);
}
function compactionMarkers(marks, x, T, bottom, value, spendLabel) {
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
            money(event.chart_cost_usd) +
            " " +
            spendLabel +
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
  const rawRows = series(s);
  const rows = rawRows.map((row, index) => {
    const prior = index > 0 ? chartSpend(s, rawRows[index - 1].cumulative_cost_usd) : 0;
    const current = chartSpend(s, row.cumulative_cost_usd);
    return { ...row, chart_cost_usd: Math.max(0, current - prior) };
  }).filter((row) => s.usage_mode !== "exhausted" || row.chart_cost_usd > 0);
  if (!rows.length) return '<div class="empty"><p>No recorded paid prompt costs yet.</p></div>';
  const W = 530,
    H = 240,
    L = 52,
    R = 14,
    T = 28,
    B = 44,
    n = Math.max(1, ...rows.map((q) => q.iteration));
  const values = rows.map((q) => q.chart_cost_usd),
    max = Math.max(0.01, ...values) * 1.1;
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
        y(q.chart_cost_usd) +
        '" width="' +
        Math.max(0.5, (W - L - R) / n - 2) +
        '" height="' +
        (y(0) - y(q.chart_cost_usd)) +
        '" rx="1" fill="' +
        (q.completed === false ? COLORS.muted : COLORS.purple) +
        '"><title>Prompt ' +
        q.iteration +
        ": " +
        money(q.chart_cost_usd) +
        "</title></rect>",
    )
    .join("");
  const marks = contextCompactions(s).filter((e) => e.iteration > 0);
  svg += compactionMarkers(marks, x, T, H - B, (e) => e.iteration - 0.5, chartSpendLabel(s));
  svg +=
    '<text x="' +
    (L + W - R) / 2 +
    '" y="' +
    (H - 5) +
    '" text-anchor="middle">Prompt number</text><text transform="translate(11 ' +
    (T + H - B) / 2 +
    ') rotate(-90)" text-anchor="middle">' + (s.usage_mode === "exhausted" ? "Cost since plan exit ($)" : "Cost ($)") + '</text>';
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
    .map((q) => ({ ...q, chart_cost_usd: chartSpend(s, q.cumulative_cost_usd), order: 0 }))
    .filter((q) => s.usage_mode !== "exhausted" || q.chart_cost_usd > 0);
  for (const event of marks) {
    for (const [key, order] of [
      ["pre_tokens", 1],
      ["post_tokens", 2],
    ])
      if (nonnegative(event[key]) && event[key] > 0)
        rows.push({
          timestamp: event.timestamp,
          chart_cost_usd: event.chart_cost_usd,
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
  const xmax = Math.max(0.01, ...rows.map((q) => q.chart_cost_usd), ...marks.map((e) => e.chart_cost_usd)) * 1.05;
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
    rows.map((q) => x(q.chart_cost_usd) + "," + y(q.context_tokens / 1000)).join(" ") +
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
          money(q.chart_cost_usd) +
          " " + chartSpendLabel(s) + " · " +
          new Date(q.timestamp).toLocaleString() +
          (q.context_observed_at && q.context_observed_at !== q.timestamp ? " · context last observed " + timeLabel(q.context_observed_at) : "");
      return (
        '<circle class="context-cost-point" cx="' +
        x(q.chart_cost_usd) +
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
  svg += compactionMarkers(marks, x, T, H - B, (e) => e.chart_cost_usd, chartSpendLabel(s));
  svg +=
    '<text x="' +
    (L + W - R) / 2 +
    '" y="' +
    (H - 5) +
    '" text-anchor="middle">' + (s.usage_mode === "exhausted" ? "Spend since plan exit ($)" : "Total spent ($)") + '</text><text transform="translate(11 ' +
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
/* Included sessions have no cost to plot, so they get the question that does
   matter for them: how context fills up prompt after prompt, and whether a
   compaction bought any headroom back. Paid sessions keep their spend chart. */
function contextRows(s) {
  return Array.isArray(s.iterations)
    ? s.iterations
        .filter((q) => q && finite(q.iteration) && nonnegative(q.context_tokens))
        .sort((a, b) => a.iteration - b.iteration)
    : [];
}
function contextGraph(s) {
  const rows = contextRows(s);
  if (rows.length < 2) return '<p class="graph-empty">Not enough prompts recorded yet to draw a trend.</p>';
  const W = 900, H = 180, L = 50, R = 20, T = 15, B = 34;
  const lastIter = rows[rows.length - 1].iteration;
  const firstIter = rows[0].iteration;
  const span = Math.max(1, lastIter - firstIter);
  const windowTokens =
    finite(s.context_window_tokens) && s.context_window_tokens > 0
      ? s.context_window_tokens
      : Math.max(...rows.map((r) => r.context_tokens)) * 1.15;
  const x = (it) => L + ((it - firstIter) / span) * (W - L - R);
  const y = (v) => T + (1 - Math.min(1, v / windowTokens)) * (H - T - B);
  const points = rows.map((r) => [x(r.iteration), y(r.context_tokens)]);
  const line = points.map(([px, py], i) => (i ? "L" : "M") + px.toFixed(1) + " " + py.toFixed(1)).join(" ");
  const area = line + " L" + points[points.length - 1][0].toFixed(1) + " " + (H - B) + " L" + points[0][0].toFixed(1) + " " + (H - B) + " Z";
  const grid = [0, 0.25, 0.5, 0.75, 1]
    .map((f) => {
      const py = T + (1 - f) * (H - T - B);
      return (
        '<line class="cg-grid" x1="' + L + '" x2="' + (W - R) + '" y1="' + py.toFixed(1) + '" y2="' + py.toFixed(1) + '"/>' +
        '<text class="cg-ylab" x="' + (L - 8) + '" y="' + (py + 3).toFixed(1) + '">' + percentage(f * 100) + "</text>"
      );
    })
    .join("");
  const ticks = [firstIter, Math.round(firstIter + span / 2), lastIter]
    .filter((v, i, a) => a.indexOf(v) === i)
    .map((it) => '<text class="cg-xlab" x="' + x(it).toFixed(1) + '" y="' + (H - B + 18) + '">' + it + "</text>")
    .join("");
  const compactIterations = [...new Set(contextCompactions(s)
    .map((event) => event.iteration)
    .filter((iteration) => finite(iteration) && iteration >= firstIter && iteration <= lastIter))];
  const lastCompact = compactIterations.at(-1);
  const compactionLines = compactIterations
    .map((iteration) => {
      const row = rows.find((item) => item.iteration === iteration);
      const point = row ? '<rect class="cg-compact-point" x="' + (x(iteration) - 3).toFixed(1) + '" y="' + (y(row.context_tokens) - 3).toFixed(1) + '" width="6" height="6" rx="1"/>' : '';
      return '<g role="img" aria-label="Compaction at prompt ' + iteration + '"><title>Compaction at prompt ' + iteration + '</title><line class="cg-compact' + (iteration === lastCompact ? ' latest' : '') + '" x1="' + x(iteration).toFixed(1) + '" x2="' + x(iteration).toFixed(1) + '" y1="' + T + '" y2="' + (H - B) + '"/>' + point + '</g>';
    }).join("");
  const analysis = s.context_map?.analysis;
  const runs = Array.isArray(analysis?.run_events) ? analysis.run_events : analysis?.last_run ? [analysis.last_run] : [];
  const analysisIterations = runs.map((run) => {
    if (finite(run?.iteration) && run.iteration > 0) return run.iteration;
    const started = Date.parse(run?.started_at || run?.completed_at);
    if (!Number.isFinite(started)) return null;
    const matching = rows.filter((row) => Date.parse(row.started_at) <= started);
    return matching.length ? matching[matching.length - 1].iteration : firstIter;
  }).filter((iteration) => finite(iteration) && iteration >= firstIter && iteration <= lastIter);
  const analysisCounts = new Map();
  for (const iteration of analysisIterations) analysisCounts.set(iteration, (analysisCounts.get(iteration) || 0) + 1);
  const lastAnalysis = analysisIterations.at(-1);
  const analysisLines = [...analysisCounts]
    .map(([iteration, count]) => {
      const label = count + (count === 1 ? ' AI analysis' : ' AI analyses') + ' after prompt ' + iteration;
      return '<g class="cg-analysis" role="img" aria-label="' + label + '"><title>' + label + '</title><line x1="' + x(iteration).toFixed(1) + '" x2="' + x(iteration).toFixed(1) + '" y1="' + T + '" y2="' + (H - B) + '"/><circle cx="' + x(iteration).toFixed(1) + '" cy="' + T + '" r="3"/>' + '</g>';
    }).join("");
  const key = '<figcaption class="context-graph-key">' +
    (compactIterations.length ? '<span><i class="cg-compact-swatch"></i><strong>Compaction</strong><small>last at prompt ' + lastCompact + '</small></span>' : '') +
    (analysisIterations.length ? '<span><i class="cg-analysis-swatch"></i><strong>AI analysis</strong><small>' + analysisIterations.length + (analysisIterations.length === 1 ? ' run' : ' runs') + ' · last after prompt ' + lastAnalysis + '</small></span>' : '') +
    '</figcaption>';
  const end = points[points.length - 1];
  const nowPct = (rows[rows.length - 1].context_tokens / windowTokens) * 100;
  return (
    '<figure class="context-graph">' + key + '<svg viewBox="0 0 ' + W + " " + H + '" role="img" aria-label="Context used against prompt number; dotted vertical lines mark compactions and green vertical lines mark AI analyses">' +
    grid + '<path class="cg-area" d="' + area + '"/>' + compactionLines + analysisLines +
    '<path class="cg-line" d="' + line + '"/>' +
    '<circle class="cg-end" cx="' + end[0].toFixed(1) + '" cy="' + end[1].toFixed(1) + '" r="3.5"/>' +
    '<text class="cg-end-label" x="' + Math.min(end[0] + 8, W - R - 30) + '" y="' + Math.max(end[1] - 8, T + 10) + '">' + percentage(nowPct) + "</text>" +
    ticks +
    '<text class="cg-axis" x="' + ((L + W - R) / 2) + '" y="' + (H - 2) + '">Prompt</text>' +
    '</svg></figure>'
  );
}
function tokenBreakdown(s) {
  const u = s.token_usage || {},
    parts = [
      ["New input", u.input, COLORS.purple],
      ["Cache reads", u.cache_read, COLORS.mint],
      ["Cache writes", u.cache_write, COLORS.muted],
      ["Output", u.output, COLORS.pink],
      ["Reasoning output", u.reasoning_output, COLORS.orange],
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
function subscriptionStats(s) {
  const ahead = shareAhead(s);
  const share = shareOf(s);
  const window = quotaWindowName(s);
  const providerState = providerQuotaState(s.provider, accountQuotaFor(s));
  const subscription = providerState.kind !== "ready"
    ? dataStateMarkup(providerState)
    : s.usage_mode === "included"
    ? "Included"
    : dataStateMarkup(sessionDataState(s, "plan"));
  const next = providerState.kind !== "ready"
    ? dataStateMarkup(providerState)
    : finite(ahead)
    ? "+" + percentage(ahead) + " of " + window + " limit"
    : dataStateMarkup(sessionDataState(s, "forecast"));
  const responsibility = providerState.kind !== "ready"
    ? dataStateMarkup(providerState)
    : finite(share)
    ? percentage(share) + " of " + window + " limit"
    : dataStateMarkup(sessionDataState(s, "share"));
  return '<div class="inspector-stats subscription-stats">' +
    '<div><span>Subscription</span><strong>' + subscription + '</strong></div>' +
    '<div><span>Next 10 prompts</span><strong>' + next + '</strong></div>' +
    '<div><span>Responsible for</span><strong>' + responsibility + '</strong></div>' +
    '<div><span>Context</span><strong>' + percentage(context(s)) + '</strong><small>' + tokens(s.context_tokens) + ' / ' + tokens(s.context_window_tokens) + '</small></div>' +
    '</div>';
}
const contextCategoryLabels = {
  prompts: "User prompts",
  previous_compact: "Previous compacts",
  assistant_output: "Conversation and reasoning",
  skills_and_instructions: "Session instructions and tools",
  repository_and_files: "Local code read",
  file_changes: "Local code written",
  documents: "Local documents read",
  local_system_data: "Local system data read",
  local_logs: "Local logs read",
  tests_and_builds: "Tests and build output",
  images: "Images loaded",
  web_and_external: "Web content fetched",
  external_service_data: "External service data fetched",
  production_systems: "Production data fetched",
  subagent_handoffs: "Subagent results",
  other_tool_output: "Unclassified context",
  starting_context: "Pre-existing context",
  provider_internal: "Provider-managed context",
};
function contextCategoryLabel(category) {
  return contextCategoryLabels[category] || "Other context";
}
function analysisUsageText(session) {
  const usage = session.analysis_usage;
  if (!usage || !Number.isInteger(usage.run_count) || usage.run_count < 1) return '';
  const prefix = 'AI analysis of this session: ' + usage.run_count + (usage.run_count === 1 ? ' run' : ' runs');
  const dollars = (value) => value < 0.0001 ? '<$0.0001' : '$' + value.toFixed(value < 0.01 ? 4 : 2);
  if (showsMoney(session)) {
    if (nonnegative(usage.spending_usd) && usage.spending_usd > 0 && usage.unknown_mode_run_count === 0) {
      return prefix + ' · ~' + dollars(usage.spending_usd) + ' paid';
    }
    if (nonnegative(usage.cost_usd) && usage.cost_usd > 0 && usage.unpriced_run_count === 0) {
      return prefix + ' · ~' + dollars(usage.cost_usd) + ' at API rates (billing unconfirmed)';
    }
    if (usage.included_run_count === usage.run_count) return prefix + ' · included in plan';
    return prefix + ' · dollar estimate pending';
  }
  const period = session.provider === 'claude' ? 'five_hour' : 'weekly';
  const limitName = session.provider === 'claude' ? '5-hour' : 'weekly';
  const limit = Array.isArray(usage.limit) ? usage.limit.find((row) => row?.period === period) : null;
  const measured = nonnegative(limit?.measured_percent) ? limit.measured_percent : 0;
  const pending = limit?.unreported;
  if (measured > 0) {
    const percent = measured < 0.01 ? measured.toFixed(3) + '%' : measured < 0.1 ? measured.toFixed(2) + '%' : percentage(measured);
    return prefix + ' · ' + percent + ' of ' + limitName + ' limit' + (pending ? ' measured so far' : '');
  }
  if (nonnegative(pending?.below_percent) && nonnegative(pending?.share_of_unreported_work) && pending.share_of_unreported_work > 0) {
    const upper = Math.max(0.01, Math.ceil(pending.below_percent * pending.share_of_unreported_work * 100) / 100);
    return prefix + ' · <' + upper.toFixed(2) + '% of ' + limitName + ' limit, pending';
  }
  return prefix + ' · usage share pending';
}
function contextHero(s) {
  const { analysis, used, portions, background } = contextWindowSlices(s);
  if (used === null) return '<div class="context-hero"><div class="context-hero-main"><div class="context-hero-ring" role="img" aria-label="Context use unavailable" style="background:#edeaf2"><div class="context-hero-ring-center"><strong>—</strong><span>window used</span></div></div><div class="context-hero-copy"><p class="context-rating-pending">Waiting for a provider context checkpoint.</p></div></div></div>';
  const ringLabel = percentage(used) + ' of the full context window used' + (analysis ? '; ' + percentage(portions[0]) + ' needed, ' + percentage(portions[2]) + ' not needed now, of the full window' : '; relevance not rated');
  const ring = '<div class="context-hero-ring" role="img" aria-label="' + esc(ringLabel) + '" style="background:' + background + '"><div class="context-hero-ring-center"><strong>' + percentage(used) + '</strong><span>window used</span></div></div>';
  const parts = analysis ? [['Needed', portions[0], 'relevant', 'Still needed for the current work'], ['Not needed now', portions[2], 'stale', 'AI estimate: not needed for the current work. Review before compacting.']] : [];
  const unreviewed = used - portions.reduce((sum, value) => sum + value, 0);
  if (analysis && used > 0 && unreviewed / used * 100 > 0.1) parts.push(['Unreviewed', unreviewed, 'unreviewed', 'Not rated by AI']);
  const legend = parts.map(([label, value, kind, meaning]) => '<div class="context-mix-row ' + kind + '" title="' + esc(meaning) + '"><i></i><span>' + label + '</span><strong>' + percentage(used ? value / used * 100 : 0) + '</strong></div>').join('');
  return '<div class="context-hero"><div class="context-hero-main">' + ring + '<div class="context-hero-copy">' + (analysis ? '<div class="context-mix-scope">Of the context in use</div><div class="context-mix-list">' + legend + '</div>' : '<p class="context-rating-pending">AI relevance analysis is pending. The ring shows how much of the window is occupied.</p>') + '</div></div></div>';
}
function topicTiles(rows, contextTokens, analyzed, sourceTypes) {
  if (!rows.length) return '<p class="context-topic-empty">No context sources were recorded for this checkpoint.</p>';
  const columns = 3;
  function tile(row, alone) {
    const share = row.tokens / Math.max(1, contextTokens) * 100;
    const shareLabel = share > 0 && share < 0.1 ? (share < 0.01 ? share.toFixed(3) : share.toFixed(2)) + '%' : percentage(share);
    const label = row.label || (sourceTypes ? contextCategoryLabel(row.id) : 'Other work');
    const relevant = nonnegative(row.relevant_tokens) ? row.relevant_tokens : 0;
    const drifting = nonnegative(row.drifting_tokens) ? row.drifting_tokens : 0;
    const stale = nonnegative(row.stale_tokens) ? row.stale_tokens : 0;
    const rated = analyzed && relevant + drifting + stale > 0;
    const weights = [relevant, 0, drifting + stale].map((value) => row.tokens > 0 ? value / row.tokens * 100 : 0);
    const scale = weights.reduce((sum, value) => sum + value, 0) > 100 ? 100 / weights.reduce((sum, value) => sum + value, 0) : 1;
    const [good, drift, old] = weights.map((value) => value * scale);
    const middle = good + drift;
    const end = middle + old;
    const mix = rated ? 'linear-gradient(90deg,#84b7a5 0 ' + good.toFixed(2) + '%,#ddbd72 ' + good.toFixed(2) + '% ' + middle.toFixed(2) + '%,#d5929b ' + middle.toFixed(2) + '% ' + end.toFixed(2) + '%,#c8c3d1 ' + end.toFixed(2) + '% 100%)' : '';
    const detail = rated ? (old > 0.1 ? percentage(old) + ' not needed now' : '') : 'Relevance pending';
    const title = label + (row.other_count ? ' (' + row.other_count + ' more)' : '') + ' · ' + shareLabel + ' of used context' + (row.id === 'unassigned_topic' ? ' · AI assigned no conversation topic' : detail ? ' · ' + detail : '');
    const other = row.id === 'others';
    const unassigned = row.id === 'unassigned_topic';
    const tag = other ? 'button' : 'article';
    const action = other ? '<span class="context-others-action">View all ' + (sourceTypes ? 'sources' : 'topics') + ' <span aria-hidden="true">→</span></span>' : '';
    const aria = other ? 'View all ' + row.all_count + ' ' + (sourceTypes ? 'source types' : 'conversation topics') + '. ' + title : title;
    const tileDetail = unassigned ? '' : detail;
    const width = alone ? 'flex:none;width:' + (columns === 3 ? '34%' : '50%') : 'flex:' + Math.max(1, row.tokens) + ' 1 0;min-width:' + (columns === 3 ? '26%' : '34%');
    return '<' + tag + (other ? ' type="button" data-context-expand="true"' : '') + ' class="context-topic-tile' + (other ? ' context-topic-others' : '') + (unassigned ? ' context-topic-unassigned' : '') + '" style="' + width + '" title="' + esc(title) + '" aria-label="' + esc(aria) + '"><span class="context-topic-mixbar" aria-hidden="true"' + (mix ? ' style="background:' + mix + '"' : '') + '></span><strong>' + shareLabel + '</strong><h4>' + esc(label) + '</h4>' + (tileDetail ? '<small>' + esc(tileDetail) + '</small>' : '') + action + '</' + tag + '>';
  }
  const groups = [];
  for (let index = 0; index < rows.length; index += columns) groups.push(rows.slice(index, index + columns));
  const total = rows.reduce((sum, row) => sum + row.tokens, 0);
  const body = groups.map((group) => {
    const weight = group.reduce((sum, row) => sum + row.tokens, 0) / Math.max(1, total);
    const height = 82 + 26 * weight;
    return '<div class="context-topic-row" style="height:' + height.toFixed(1) + 'px">' + group.map((row) => tile(row, group.length === 1)).join('') + '</div>';
  }).join('');
  return '<div class="context-topic-mosaic" role="group" aria-label="Share of used context by ' + (sourceTypes ? 'source type' : 'conversation topic') + '">' + body + '</div>';
}
function namedThemes(analysis) {
  return Array.isArray(analysis?.themes) ? analysis.themes.filter((row) => row?.label !== 'Not reviewed yet') : [];
}
function contextTopics(s, full = false) {
  const map = s.context_map;
  if (!map || map.state !== 'ready' || !map.categories || typeof map.categories !== 'object') return '<section class="context-section context-topic-section"><p class="context-topic-empty">Waiting for the next complete provider checkpoint.</p></section>';
  if (!contextMapMatchesCurrent(s)) return '<section class="context-section context-topic-section"><p class="context-topic-empty">Waiting for an updated breakdown.</p></section>';
  const observed = nonnegative(map.observed_context_tokens) ? map.observed_context_tokens : 0;
  const sources = Object.entries(map.categories).filter(([, value]) => nonnegative(value) && value > 0).map(([id, value]) => ({ id, tokens: value }));
  const known = sources.reduce((sum, row) => sum + row.tokens, 0);
  if (observed > known) sources.push({ id: 'provider_internal', tokens: observed - known });
  const analysis = analysisFor(s);
  const themes = namedThemes(analysis);
  const mode = themes.length && state.topicMode === 'topics' ? 'topics' : 'sources';
  const technical = Array.isArray(analysis?.technical_categories) ? analysis.technical_categories.filter((row) => row && nonnegative(row.tokens) && row.tokens > 0) : [];
  const ratedSources = mode === 'sources' && technical.length > 0;
  const rows = mode === 'topics' ? [...themes] : ratedSources ? [...technical] : sources;
  const assigned = (mode === 'topics' ? analysis.themes : rows).reduce((sum, row) => sum + row.tokens, 0);
  if (mode === 'sources' && observed > assigned) rows.push({ id: 'provider_internal', label: 'Other context', tokens: observed - assigned });
  const unassigned = mode === 'topics' ? Math.max(0, observed - assigned) : 0;
  rows.sort((a, b) => b.tokens - a.tokens);
  function preview(limit) {
    const hidden = rows.slice(limit);
    if (!hidden.length) return rows;
    return [...rows.slice(0, limit), {
      id: 'others', label: 'Others', other_count: hidden.length, all_count: rows.length,
      tokens: hidden.reduce((sum, row) => sum + row.tokens, 0),
      relevant_tokens: hidden.reduce((sum, row) => sum + (nonnegative(row.relevant_tokens) ? row.relevant_tokens : 0), 0),
      drifting_tokens: hidden.reduce((sum, row) => sum + (nonnegative(row.drifting_tokens) ? row.drifting_tokens : 0), 0),
      stale_tokens: hidden.reduce((sum, row) => sum + (nonnegative(row.stale_tokens) ? row.stale_tokens : 0), 0),
    }];
  }
  const unassignedRow = unassigned > 0 ? [{ id: 'unassigned_topic', label: 'No named topic', tokens: unassigned }] : [];
  const shown = full ? [...rows, ...unassignedRow] : [...preview(unassignedRow.length ? 7 : 8), ...unassignedRow];
  return '<section class="context-section context-topic-section ' + (mode === 'topics' ? 'mode-topics' : 'mode-sources') + '">' + topicTiles(shown, Math.max(observed, assigned), mode === 'topics' || ratedSources, mode === 'sources') + '</section>';
}
function openContextBreakdown() {
  const existing = document.querySelector('#context-breakdown-dialog');
  if (existing) { existing.querySelector('[data-context-close]')?.focus(); return; }
  const session = allRows().find((row) => keyOf(row) === state.selected || row.id === state.selected);
  if (!session) return;
  const mode = namedThemes(analysisFor(session)).length && state.topicMode === 'topics' ? 'Conversation topics' : 'Source types';
  const dialog = document.createElement('dialog');
  dialog.id = 'context-breakdown-dialog';
  dialog.setAttribute('aria-labelledby', 'context-breakdown-title');
  dialog.innerHTML = '<div class="context-modal-content context-visual"><div class="context-modal-head"><h2 id="context-breakdown-title">All ' + mode.toLowerCase() + '</h2><button type="button" class="context-modal-close" data-context-close="true" aria-label="Close full context view">×</button></div>' + contextTopics(session, true) + '</div>';
  document.body.append(dialog);
  dialog.addEventListener('click', (event) => { if (event.target === dialog) dialog.close(); });
  dialog.addEventListener('close', () => dialog.remove(), { once: true });
  dialog.showModal();
  dialog.querySelector('[data-context-close]')?.focus();
}
function contextTopicChooser(s) {
  const analysis = analysisFor(s);
  if (!namedThemes(analysis).length) return '';
  const mode = state.topicMode === 'topics' ? 'topics' : 'sources';
  return '<div class="context-topic-chooser" role="group" aria-label="Context grouping"><button type="button" data-topic-mode="topics" aria-pressed="' + String(mode === 'topics') + '">Conversation topics</button><button type="button" data-topic-mode="sources" aria-pressed="' + String(mode === 'sources') + '">Source types</button></div>';
}
function contextTimeline(s) {
  const analysis = analysisFor(s);
  if (!analysis) return '';
  const phases = Array.isArray(analysis.phases) ? analysis.phases : [];
  if (!phases.length) return '';
  const lastPrompt = Math.max(1, s.context_map?.iteration || 1, ...phases.map((phase) => Number.isInteger(phase.end_iteration) ? phase.end_iteration : 0));
  const segments = phases.map((phase, index) => {
    const from = Number.isInteger(phase.start_iteration) ? Math.max(1, phase.start_iteration) : 1;
    const to = Number.isInteger(phase.end_iteration) ? Math.max(from, phase.end_iteration) : lastPrompt;
    return { label: phase.label || 'Other work', summary: phase.summary || '', from, to, index };
  });
  const lastEnd = Math.max(0, ...segments.map((segment) => segment.to));
  if (lastEnd < lastPrompt) segments.push({ label: 'Recent prompts', summary: '', from: lastEnd + 1, to: lastPrompt, index: segments.length });
  const track = segments.map((segment) => {
    const length = Math.max(1, segment.to - segment.from + 1);
    return '<div class="context-journey-segment phase-' + Math.min(segment.index, 5) + (segment.to === lastPrompt ? ' current' : '') + (length / lastPrompt < 0.07 ? ' narrow' : '') + '" style="left:' + ((segment.from - 1) / lastPrompt * 100).toFixed(2) + '%;width:' + (length / lastPrompt * 100).toFixed(2) + '%" title="' + esc(segment.label + ' · prompts ' + segment.from + '–' + segment.to + (segment.summary ? ' · ' + segment.summary : '')) + '"><span>' + String(segment.index + 1).padStart(2, '0') + '</span></div>';
  }).join('');
  const key = segments.map((segment) => '<div class="context-journey-item"><span class="context-journey-index phase-' + Math.min(segment.index, 5) + '">' + String(segment.index + 1).padStart(2, '0') + '</span><strong>' + esc(segment.label) + '</strong></div>').join('');
  return '<section class="context-section context-journey"><div class="context-section-head"><h3>Work so far</h3></div>' + (segments.length ? '<div class="context-journey-axis"><span>First prompt</span><span>Now · prompt ' + lastPrompt + '</span></div><div class="context-journey-track" role="img" aria-label="Work topics across ' + lastPrompt + ' prompts">' + track + '</div><div class="context-journey-key">' + key + '</div>' : '') + '</section>';
}
function renderInspector() {
  const panel = $("#inspector"),
    s = allRows().find((s) => keyOf(s) === state.selected || s.id === state.selected);
  const wasOpen = !panel.hidden;
  panel.hidden = !s;
  $("#backdrop").hidden = !s;
  for (const node of [document.querySelector("main"), document.querySelector("header")]) node.inert = !!s;
  document.body.style.overflow = s ? "hidden" : "";
  if (!s) {
    if (state.selected && state.payload) {
      state.selected = null;
      saveUrl();
    }
    return;
  }
  const rows = series(s),
    last = rows.at(-1),
    scroll = $("#inspector-body").scrollTop;
  const included = !showsMoney(s);
  const signal = included ? null : assess(s);
  const priorSpend = document.querySelector('#spend-details');
  const spendOpen = priorSpend?.open === true && priorSpend.dataset.sessionKey === keyOf(s);
  const stats = included
    ? subscriptionStats(s)
    : '<div class="inspector-stats"><div><span>Prompts</span><strong>' + count(s) + '</strong><small>in this session</small></div><div><span>Recorded spend</span><strong>' + money(cost(s)) + '</strong><small>' + (last?.completed === false ? 'Current prompt ' : 'Last prompt ') + money(last?.priced === false ? null : last?.cost_usd) + (last?.completed === false ? ' so far' : '') + '</small></div><div><span>Next 10 prompts</span><strong>' + additional(forecast(s)) + '</strong><small>estimated additional</small></div></div>';
  const agents = Array.isArray(s.subagents) ? s.subagents : [];
  const history = '<section class="context-section context-history"><div class="context-section-head"><div><h3>How context fills up</h3></div></div>' + contextGraph(s) + '</section>';
  const analysisCost = analysisUsageText(s);
  const paidDetails = included ? '' : '<details id="spend-details" class="inspector-extra" data-session-key="' + esc(keyOf(s)) + '"' + (spendOpen ? ' open' : '') + '><summary>Spending trend</summary><div class="section-title"><h3>How this session is spending</h3><div class="mini-tabs"><button data-chart="cumulative" class="' + (state.chart === 'cumulative' ? 'on' : '') + '">Cumulative</button><button data-chart="prompt" class="' + (state.chart === 'prompt' ? 'on' : '') + '">Per prompt</button></div></div>' + sessionGraph(s) + '</details>';
  const warning = signal?.severity ? '<div class="inspector-signal"><strong>' + esc(signal.action) + '</strong><p>' + esc(signal.evidence) + '</p></div>' : '';
  const grouping = namedThemes(analysisFor(s)).length && state.topicMode === 'topics' ? 'Conversation topics' : 'Source types';
  const content = warning + '<section class="context-visual"><div class="context-visual-head"><h3>Current context</h3><div class="context-visual-topic-head"><h3>' + grouping + '</h3>' + contextTopicChooser(s) + '</div></div><div class="context-visual-body">' + contextHero(s) + contextTopics(s) + '</div>' + (analysisCost ? '<p class="context-analysis-usage">' + esc(analysisCost) + '</p>' : '') + '</section>' + compactAdvice(s) + contextTimeline(s) + history + paidDetails +
    '<section id="subagent-section" class="inspector-extra inspector-static"><div class="section-title"><h3>Subagent activity</h3><span class="tiny">' + agents.length + ' spawned · ' + agents.filter((agent) => agent.live === true).length + ' live</span></div>' + (subagentDetails(s) || '<p>No subagents were recorded.</p>') + '</section>' +
    '<section class="inspector-extra inspector-static"><div class="section-title"><h3>Recorded token traffic</h3></div>' + tokenBreakdown(s) + '</section>';
  const sessionActivity = activity(s);
  const activityText = sessionActivity.kind === 'running' ? '<span class="inspector-live">Running</span><span>Active ' + age(s.last_activity_at) + ' ago</span>' : '<span>Last activity ' + age(s.last_activity_at) + ' ago</span>';
  $("#inspector-body").innerHTML = (previewName === "topics" ? '<div class="topic-demo-label">Demo session · sample data <a href="/">Live dashboard</a></div>' : '') + '<h2 class="inspector-title" id="inspector-title" title="' + esc(displayTitle(s)) + '">' + esc(displayTitle(s)) + '</h2>' +
    '<div class="inspector-meta">' + activityText + '<span>' + providerName(s.provider) + ' ' + esc(s.client || 'local') + '</span><span>' + esc(s.model || 'Model unavailable') + (effort(s) ? ' · ' + esc(effort(s)) + ' effort' : '') + '</span><span>Started ' + age(startTime(s)) + ' ago</span>' + (included ? '<span>Included plan</span>' : '') + '</div>' +
    '<div class="inspector-top-details">' + stats + '</div>' + content;
  $("#inspector-body").scrollTop = scroll;
  if (!wasOpen) $("#close").focus();
}
async function loadSessionDetails(id) {
  if (previewMode) return;
  const session = allRows().find((row) => keyOf(row) === id || row.id === id);
  if (!session) return;
  const request = ++state.detailRequest;
  try {
    const query = new URLSearchParams({ provider: session.provider, session: session.id });
    const response = await fetch("/api/session?" + query, { cache: "no-store" });
    if (!response.ok) return;
    const detail = await response.json();
    if (request !== state.detailRequest) return;
    if (!detail || detail.id !== session.id || detail.provider !== session.provider) return;
    const index = state.payload.sessions.findIndex((row) => keyOf(row) === keyOf(session));
    if (index >= 0) state.payload.sessions[index] = { ...session, ...detail };
  } catch {
    return;
  }
}
async function openSession(id, opener) {
  hideGraphTip();
  state.lastOpener = opener;
  state.selected = id;
  state.chart = "cumulative";
  state.inspectorTab = "overview";
  state.topicMode = "topics";
  $("#inspector-body").scrollTop = 0;
  saveUrl();
  renderInspector();
  await loadSessionDetails(id);
  if (state.selected === id) renderInspector();
}
function closeSession() {
  state.detailRequest++;
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
    if (quotas?.status === "stale") continue;
    for (const window of Array.isArray(quotas?.windows) ? quotas.windows : []) {
      if (
        provider === "codex" &&
        window?.period === "monthly" &&
        !providerExhausted(provider, payload.sessions || [])
      ) {
        continue;
      }
      if (!finite(window?.used_percent)) continue;
      const threshold = [100, 80, 50].find((value) => window.used_percent >= value);
      const period = window.period || duration(window.window_minutes * 60);
      const limitId = typeof window.limit_id === "string" && window.limit_id ? window.limit_id : "default";
      const key = "konvu-quota-alert-" + provider + "-" + limitId + "-" + period;
      if (!threshold) {
        localStorage.removeItem(key);
        continue;
      }
      if (localStorage.getItem(key) === null) {
        const legacyPrefix = "konvu-quota-alert-" + provider + "-" + period + "-";
        for (let index = 0; index < localStorage.length; index++) {
          const legacyKey = localStorage.key(index);
          if (!legacyKey?.startsWith(legacyPrefix)) continue;
          const priorThreshold = Number(localStorage.getItem(legacyKey) || 0);
          if (priorThreshold >= threshold) localStorage.setItem(key, String(priorThreshold));
        }
      }
      if (Number(localStorage.getItem(key) || 0) >= threshold) continue;
      localStorage.setItem(key, String(threshold));
      const notification = new Notification(
        providerName(provider) + " " + period + " limit at " + percentage(window.used_percent),
        {
          body: percentage(window.used_percent) + " of your " + period + " subscription limit is used.",
          icon: "/konvu-ghost.svg",
          requireInteraction: true,
          tag: key,
        }
      );
      notification.onclick = () => {
        window.focus();
        const session = (payload.sessions || []).find((item) => item?.provider === provider);
        if (session) openSession(keyOf(session));
        notification.close();
      };
    }
  }
  for (const session of payload.sessions || []) {
    if (payload.account_quotas?.[session.provider]?.status === "stale") continue;
    if (!showsMoney(session) || !finite(session.projected_next_10_tasks_usd) || session.projected_next_10_tasks_usd < 10) continue;
    const key = "konvu-alert-" + session.provider + "-" + session.id;
    const previous = JSON.parse(localStorage.getItem(key) || "null");
    const now = Date.now(), forecastUsd = session.projected_next_10_tasks_usd, spentUsd = cost(session);
    if (previous && now - previous.at < 300000 && forecastUsd < previous.forecast + 5) continue;
    localStorage.setItem(key, JSON.stringify({ at: now, forecast: forecastUsd }));
    const notification = new Notification(providerName(session.provider) + " session running hot", {
      body:
        "🔥 " +
        (forecastUsd >= 50 ? "💸💸💸" : forecastUsd >= 30 ? "💸💸" : "💸") + " " + money(forecastUsd) +
        " forecast for the next 10 prompts" +
        (finite(spentUsd) ? "\n💸 " + money(spentUsd) + " spent since plan exit" : ""),
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
bindStatePopovers();
bindNotificationPanel();
bindSettings();
refresh();
setInterval(() => {
  renderFreshness(state.error || !state.payload || elapsed(state.payload.generated_at) > 120000);
}, 1000);
