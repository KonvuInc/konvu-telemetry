"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");

const source = fs.readFileSync(
  path.join(__dirname, "../src/konvu_telemetry/dashboard/auth.js"),
  "utf8",
);

async function launch({ search = "", hash = "", response = { ok: true } } = {}) {
  const requests = [];
  const scripts = [];
  const history = [];
  const main = { replaceChildren: (...children) => { main.children = children; } };
  const context = {
    URLSearchParams,
    location: { pathname: "/", search, hash },
    history: { replaceState: (...args) => history.push(args) },
    document: {
      body: { append: (script) => scripts.push(script) },
      createElement: (tagName) => ({ tagName }),
      querySelector: () => main,
    },
    fetch: async (...args) => {
      requests.push(args);
      return response;
    },
  };
  vm.runInNewContext(source, context);
  await new Promise(setImmediate);
  return { requests, scripts, history, main };
}

test("fragment credential is removed and exchanged before loading the dashboard", async () => {
  const result = await launch({ hash: "#token=private-secret" });
  assert.equal(result.history[0][2], "/");
  assert.equal(result.requests[0][0], "/api/auth");
  assert.equal(result.requests[0][1].headers.Authorization, "Bearer private-secret");
  assert.equal(result.scripts[0].src, "/fleet.js");
});

test("an existing browser cookie can load a deep link", async () => {
  const result = await launch({ search: "?session=codex%3Aid" });
  assert.equal(result.requests[0][0], "/api/preferences");
  assert.equal(result.scripts[0].src, "/fleet.js");
});

test("unauthorized browsers do not load the private dashboard", async () => {
  const result = await launch({ response: { ok: false } });
  assert.equal(result.scripts.length, 0);
  assert.match(result.main.children[0].textContent, /konvu-telemetry dashboard/);
});

test("bundled previews work without a credential", async () => {
  const result = await launch({ search: "?preview=states" });
  assert.equal(result.requests.length, 0);
  assert.equal(result.scripts[0].src, "/fleet.js");
});
