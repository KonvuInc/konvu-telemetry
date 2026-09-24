# Architecture

Konvu Telemetry is one Python package with a single resident process. The process reads provider-owned JSONL transcripts, writes private derived JSON under `~/.konvu/telemetry`, and serves bundled dashboard assets on `127.0.0.1:7824`.

## Boundaries

- The collector never modifies provider transcripts.
- The dashboard server binds only to IPv4 loopback and rejects non-local `Host` and `Origin` values.
- The package has no runtime Python dependencies. Its outbound clients send a small allowlisted set of anonymous product events to PostHog and, for a ChatGPT-authenticated Codex login, read the account's current usage state from Codex with the existing local OAuth token. Both use short timeouts; neither sends transcripts or credentials to Konvu.
- Product analytics uses a random install ID. It does not create person profiles and sends no transcripts, prompts, code, paths, command arguments, usage data, raw errors, environment variables, account IDs, or workspace IDs.
- Browser notifications require an open dashboard tab and browser permission.
- A session alerts when it was active in the last twenty minutes, its cost is complete, and its next-ten-prompt forecast exceeds `ALERT_FORECAST_USD`. It alerts again only after `ALERT_FORECAST_RENOTIFY_SECONDS` and only if the forecast has not fallen since the last alert; falling to or below the threshold re-arms it.
- Every usage number a hook prints comes from a precomputed session file; no hook recomputes usage from a transcript. The Codex hooks additionally read the current rollout file for two gating facts that must be current rather than as of the last collection: the firing turn's tool calls and the recorded client.
- Each client reports usage in exactly one place: the Claude Code CLI in its status line, Claude Desktop in a box appended to the reply, the Codex CLI in its `Stop` hook, and Codex Desktop in a box appended to the reply.
- All four surfaces render the same rows from `display.usage_rows()`, which returns unframed content and knows nothing about presentation. The three hook surfaces wrap each row in the `╭─ Konvu usage` / `│ ` / `╰─` frame; the status line prints the rows flat. The two things that legitimately differ are parameters, not branches inside it: the quota string each surface sourced, and an optional context percentage the Claude status line passes because `context_window.used_percentage` in its hook payload is fresher than the snapshot's own figure. An absent, boolean, or non-finite payload percentage falls back to the snapshot.
- Desktop clients collapse a hook `systemMessage` into a hidden notice, so the desktop path uses a `UserPromptSubmit` hook returning `hookSpecificOutput.additionalContext` that asks the model to end its reply with the usage box.
- Claude installs no `Stop` hook. Setup removes the one earlier versions installed and leaves every other `Stop` entry in the file alone; `claude-hook` remains a silent no-op so settings written by an older version keep working.
- Claude is identified as desktop by `CLAUDE_CODE_ENTRYPOINT=claude-desktop` in the hook environment; Codex by its recorded client (Desktop app or VS Code) as opposed to the TUI, read from the rollout's `session_meta` record. An absent, unreadable, or unrecognized client means CLI, so the desktop path is never entered by accident.
- Every usage summary ends with one dashboard line: the `DASHBOARD_PORT` loopback URL when `service.load_health()` reports `healthy`, and otherwise the `konvu-telemetry setup` command that installs and starts the collector serving it. Only `healthy` counts as reachable; `stale`, `starting`, a missing or unreadable health record, and a failed read all show the command, because a dead link costs more than a redundant hint. No surface opens a socket to decide this.
- A usage box is shown when, and only when, the last prompt used a tool. The prompt hooks read the rendered session's `last_task_tool_calls`; the Codex `Stop` hook counts tool calls on the exact turn it fires on, which is more precise for that one hook. A missing, zero, or non-integer count shows nothing. There is no cost floor, prompt-count floor, or rate limit.

## Runtime flow

1. `installer.py` installs a private launcher, merges supported integrations, and starts a per-user LaunchAgent.
2. `service.py` owns the collector loop, health record, and localhost HTTP server.
3. `live.py` and `fleet_telemetry.py` incrementally read appended transcript bytes and retain bounded metadata for active files.
4. `parsers.py` converts provider records into the provider-neutral types in `models.py`.
5. `pricing.py` applies the bundled local API price table; `codex_billing.py` applies the published Codex subscription-credit table and reads current local account metadata and usage state.
6. `analytics.py` derives prompt series, personal baselines, forecasts, compaction state, and alert decisions. Expired valid baselines remain usable while one background refresh rebuilds them.
7. `snapshot.py` writes a bounded dashboard summary plus detailed per-session documents. Unchanged detail documents are not rewritten.
8. `display.py` builds one set of usage rows from a per-session document and reads `service.load_health()` for the closing dashboard line, then frames them for the Codex `Stop` hook and both providers' desktop `UserPromptSubmit` context, or prints them flat for the Claude status line.
9. `dashboard/` contains static HTML, CSS, JavaScript, and images served by the local process.

## Local files

| Path | Purpose | Mode |
| --- | --- | --- |
| `~/.konvu/telemetry/konvu-launcher` | Absolute-path integration launcher | `0700` |
| `~/.konvu/telemetry/live-sessions.json` | Bounded summary of sessions active in the dashboard's 20-minute window | `0600` |
| `~/.konvu/telemetry/sessions/*.json` | Per-session detail loaded on demand by the dashboard | `0600` |
| `~/.konvu/telemetry/baselines.json` | Local historical medians | `0600` |
| `~/.konvu/telemetry/health.json` | Collector freshness and last error | `0600` |
| `~/.konvu/telemetry/notification-state.json` | Alert suppression state | `0600` |
| `~/.konvu/telemetry/tracking-state.json` | Anonymous install ID and local analytics preference | `0600` |
| `~/.konvu/telemetry/tracking-queue.json` | At most 100 pending anonymous analytics events | `0600` |
| `~/.konvu/telemetry/tracking.lock` | Cross-process lock for analytics state and queue | `0600` |
| `~/.konvu/telemetry/collector*.log` | LaunchAgent stdout and stderr | User-owned |
| `~/Library/LaunchAgents/com.konvu.telemetry.plist` | Per-user service definition | User-owned |
| `~/.claude/settings.json` | Optional Claude status line plus `UserPromptSubmit` hook merge | `0600` after write |
| `~/.codex/hooks.json` | Optional Codex `Stop` and `UserPromptSubmit` hook merge | `0600` after write |

Raw transcripts remain in `~/.claude/projects` and `~/.codex/sessions`. Normalized session files expire after seven days. Uninstall removes the service, launcher, and Konvu-owned config entries but preserves telemetry data for manual inspection or deletion.

## Configuration

| Variable | Override |
| --- | --- |
| `KONVU_LIVE_USAGE_HOME` | Derived telemetry directory |
| `KONVU_LIVE_USAGE_CLAUDE_DIR` | Claude transcript roots, separated by the platform path separator |
| `KONVU_LIVE_USAGE_CODEX_DIR` | Codex transcript roots, separated by the platform path separator |
| `KONVU_TELEMETRY_PRICING_PATH` | Local pricing JSON file |

Setup enables anonymous product analytics by default and preserves an existing choice. Run `konvu-telemetry telemetry off` to disable it and delete pending events, `konvu-telemetry telemetry on` to re-enable it, or `konvu-telemetry telemetry status` to inspect the local preference.

## Failure behavior

- Writes use a temporary file and atomic replacement.
- Setup validates configuration first and restores prior files and service state if any step fails.
- Uninstall refuses to delete the service definition when launchd still reports it running.
- Unknown billable models mark the session partial; their iterations are omitted from forecasts and cost comparisons while complete iterations remain usable.
- Oversized transcript records are scanned with bounded prefix and suffix buffers; large payload text is not retained.
- Dashboard responses use ETags, and the browser fetches detailed history only for the open session.
- Hooks only read the collector's existing session output; they never force collection.
- Failed analytics delivery retains stable event IDs and uses exponential backoff capped at 24 hours.
- A second server cannot bind the same port and exits before starting another collector loop.

JSON is sufficient for the first release because the process writes one bounded current snapshot, small state files, and one file per session. SQLite becomes useful when the product needs arbitrary historical queries, migrations, or concurrent writers.
