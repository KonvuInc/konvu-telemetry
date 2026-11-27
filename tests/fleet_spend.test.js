"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");

const source = fs.readFileSync(path.join(__dirname, "../src/konvu_telemetry/dashboard/fleet.js"), "utf8");
const browser = { location: { search: "" }, URLSearchParams };
vm.runInNewContext(
  source.slice(0, source.lastIndexOf("\ninitialUrl();")) +
    "\nglobalThis.helpers = { state, cost, money, planGroups, groupHeading, promptCostGraph, sessionGraph, fleetGraph, analysisUsageText };",
  browser,
);
const { state, cost, money, planGroups, groupHeading, promptCostGraph, sessionGraph, fleetGraph, analysisUsageText } = browser.helpers;

test("stale quota groups sessions under unknown plan status", () => {
  const session = { id: "one", provider: "claude", usage_mode: "unknown" };
  state.payload = {
    sessions: [session],
    account_quotas: {
      claude: {
        status: "stale",
        ordinary_usage_allowed: false,
        windows: [{ period: "five_hour", used_percent: 100 }],
      },
    },
  };
  const groups = planGroups([session]);
  assert.equal(groups.length, 1);
  assert.equal(groups[0].mode, "unknown");
  assert.match(groupHeading(groups[0]), /<h2>Plan status unavailable<\/h2>/);
});

test("incomplete costs stay out of money and both spending charts", () => {
  const session = {
    id: "one",
    provider: "claude",
    usage_mode: "exhausted",
    cost_status: "partial",
    out_of_plan_spend_status: "since_observed_plan_exit",
    out_of_plan_spend_usd: 4,
    total_cost_usd: 10,
    iterations: [{ iteration: 1, started_at: "2026-01-01T00:00:00Z", cost_usd: 10, cumulative_cost_usd: 10 }],
  };
  assert.equal(cost(session), null);
  assert.match(promptCostGraph(session), /Spend estimate incomplete/);
  assert.match(sessionGraph(session), /Spend estimate incomplete/);
  session.cost_status = "complete";
  session.out_of_plan_spend_status = "incomplete_baseline";
  assert.equal(cost(session), null);
  assert.match(sessionGraph(session), /Spend estimate incomplete/);
});

test("fleet graph explains incomplete paying spend and unknown plan state", () => {
  const paying = { id: "paid", provider: "claude", usage_mode: "exhausted", cost_status: "partial", total_cost_usd: 2, task_count: 1 };
  assert.match(fleetGraph([paying]), /Spend estimate incomplete/);
  assert.doesNotMatch(fleetGraph([paying]), /Subscription usage is included/);
  const apiBilled = { ...paying, usage_mode: "api_billed" };
  assert.match(fleetGraph([apiBilled]), /Spend estimate incomplete/);
  const unknown = { id: "unknown", provider: "claude", usage_mode: "unknown", task_count: 1 };
  assert.match(fleetGraph([unknown]), /Plan status unavailable/);
});

test("unpriced analysis is marked incomplete and tiny amounts remain visible", () => {
  const session = {
    usage_mode: "exhausted",
    analysis_usage: { run_count: 2, unpriced_run_count: 1, spending_usd: 0.04 },
  };
  assert.match(analysisUsageText(session), /dollar estimate incomplete/);
  assert.equal(money(0.02), "<$0.1");
});
