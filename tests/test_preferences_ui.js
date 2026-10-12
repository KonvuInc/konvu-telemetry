"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

const script = fs.readFileSync(path.join(__dirname, "../src/konvu_telemetry/dashboard/fleet.js"), "utf8");
const start = script.lastIndexOf("\ninitialUrl();");
assert.ok(start > 0);
const fields = {
  "#cadence-rule": { value: "" },
  "#context-analysis-enabled": { checked: true },
  "#context-analysis-allow-paid": { checked: false },
};
const context = {
  document: { querySelector: (selector) => fields[selector] },
  location: { search: "" },
  URLSearchParams,
};
vm.createContext(context);
vm.runInContext(script.slice(0, start), context);
vm.runInContext(`cadenceState.saved = {
  cadence: "every-tool-call", custom_rule: "", context_analysis_enabled: true,
  context_analysis_allow_paid: false,
}; cadenceState.cadence = "every-tool-call";`, context);

assert.deepEqual(JSON.parse(JSON.stringify(vm.runInContext("preferencesPatch()", context))), {});
fields["#cadence-rule"].value = "only when usage jumps";
vm.runInContext('cadenceState.cadence = "custom"', context);
assert.deepEqual(JSON.parse(JSON.stringify(vm.runInContext("preferencesPatch()", context))), {
  cadence: "custom",
  custom_rule: "only when usage jumps",
});
vm.runInContext('cadenceState.cadence = "every-tool-call"', context);
fields["#context-analysis-allow-paid"].checked = true;
assert.deepEqual(JSON.parse(JSON.stringify(vm.runInContext("preferencesPatch()", context))), {
  context_analysis_allow_paid: true,
});
