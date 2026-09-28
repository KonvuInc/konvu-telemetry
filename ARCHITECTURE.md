# Architecture

Konvu Telemetry is one Python package with a single resident process. The process reads provider-owned JSONL transcripts, writes private derived JSON under `~/.konvu/telemetry`, and serves bundled dashboard assets on `127.0.0.1:7824`.

## Boundaries

- The collector never modifies provider transcripts.
- The dashboard server binds only to IPv4 loopback and rejects non-local `Host` and `Origin` values.
- The package has no runtime Python dependencies. The collector is the only Konvu component that fetches or writes account limits: Claude calls Anthropic's usage endpoint every ten minutes, while every two minutes a validated installed Codex app-server contacts OpenAI. The Claude credential remains in memory for one request; the Codex credential never enters Konvu Telemetry. Only normalized limits are persisted. Authentication failures clear the provider immediately; transient failures retain unexpired provider windows with an explicit stale status. Claude rate limits back off for fifteen minutes without blocking transcript collection.
- A separate outbound client sends a small allowlisted set of anonymous product events to PostHog with a 500 ms timeout. Setup durably queues its event before the setup process exits; network delivery and resident-process events run in a background thread.
- Product analytics uses a random install ID. It does not create person profiles and sends no transcripts, prompts, code, paths, command arguments, usage data, raw errors, environment variables, account IDs, or workspace IDs.
- Browser notifications require an open dashboard tab and browser permission.
- A paid session is marked hot when it was active in the last twenty minutes, its cost is complete, and its next-ten-prompt forecast reaches `ALERT_FORECAST_USD`. Browser-local state controls notification cooldowns.
- Every usage number a hook prints comes from collector-owned session and account-quota files; no hook fetches provider limits or recomputes usage from a transcript. The Codex hooks additionally read the current rollout file for two gating facts that must be current rather than as of the last collection: the firing turn's tool calls and the recorded client.
- Each client reports usage in exactly one place: the Claude Code CLI in its status line, Claude Desktop in a box appended to the reply, the Codex CLI in its `Stop` hook, and Codex Desktop in a box appended to the reply.
- All four surfaces render the same rows from `display.usage_rows()`, which returns unframed content and knows nothing about presentation. The three hook surfaces wrap each row in the `╭─ Konvu usage` / `│ ` / `╰─` frame; the status line prints the rows flat. The two things that legitimately differ are parameters, not branches inside it: the quota string each surface sourced, and an optional context percentage the Claude status line passes because `context_window.used_percentage` in its hook payload is fresher than the snapshot's own figure. An absent, boolean, or non-finite payload percentage falls back to the snapshot.
- Desktop clients collapse a hook `systemMessage` into a hidden notice, so the desktop path uses a `UserPromptSubmit` hook returning `hookSpecificOutput.additionalContext` that asks the model to end its reply with the usage box.
- Claude installs no `Stop` hook. Setup removes the one earlier versions installed and leaves every other `Stop` entry in the file alone; `claude-hook` remains a silent no-op so settings written by an older version keep working.
- Claude is identified as desktop by `CLAUDE_CODE_ENTRYPOINT=claude-desktop` in the hook environment; Codex by its recorded client (Desktop app or VS Code) as opposed to the TUI, read from the rollout's `session_meta` record. An absent, unreadable, or unrecognized client means CLI, so the desktop path is never entered by accident.
- Every usage summary ends with one dashboard line: the `DASHBOARD_PORT` loopback URL when `service.load_health()` reports `healthy`, and otherwise the `konvu-telemetry setup` command that installs and starts the collector serving it. Only `healthy` counts as reachable; `stale`, `starting`, a missing or unreadable health record, and a failed read all show the command, because a dead link costs more than a redundant hint. No surface opens a socket to decide this.
- When a usage box is shown is the user's choice, stored in `preferences.json` and read fresh on every turn. The choices are after every prompt, after every tool call (the default), only when the binding limit has moved by `jump_percent`, never, and a custom rule. No check outside that choice suppresses a box: the cadence is the only gate, so a widening choice actually widens. The Claude CLI status line is deliberately exempt, because it is ambient and always current.
- Tool counts feed the cadence rather than gating ahead of it. The prompt hooks read the rendered session's `last_task_tool_calls`; the Codex `Stop` hook counts tool calls on the exact turn it fires on, which is more precise for that one hook. A missing or non-integer count reads as zero.
- The `usage-jump` cadence watches one window per provider: the five-hour window for Claude and the weekly one for Codex, taking the busiest limit bucket when the provider reports several. Movement in any other window does not trigger it.
- Its baseline is recorded once the hook has emitted the box rather than when the gate decided to, so a turn that decided to show and then rendered nothing does not consume the jump. The two desktop surfaces inject the box as model context, so "emitted" there means handed to the model, which may still decline to render it.
- Each stored figure carries the identity of the window it came from — provider, period, limit bucket and reset time — so a rolled-over window, a different bucket, or a provider outage re-arms the box instead of silencing it. While the provider's figures are unavailable the box shows once and then waits, rather than firing every turn.
- A custom rule is the user's own `custom_rule.py`, executed on every turn. It lives outside the package so an upgrade cannot replace it, and any failure to import or run it shows the box: staying silent is the worse failure.

## Runtime flow

1. `installer.py` installs a private launcher, merges supported integrations, and starts a per-user LaunchAgent.
2. `service.py` owns the collector loop, health record, and localhost HTTP server.
3. `live.py` and `fleet_telemetry.py` incrementally read appended transcript bytes and retain bounded metadata for active files.
4. `parsers.py` converts provider records into the provider-neutral types in `models.py`.
5. `pricing.py` applies the bundled local price table; `codex_credit_rates.py` calculates Codex credit equivalents from its published rate table. Both mark missing rates explicitly.
6. `analytics.py` derives prompt series, the provider-level sparse-session forecast fallback, compaction state, and the current hot-session flag. Expired valid forecast fallbacks remain usable while one background refresh rebuilds them.
7. `snapshot.py` writes a bounded dashboard summary plus detailed per-session documents; `provider_limits.py` independently writes the normalized account quota snapshot. Unchanged documents are not rewritten.
8. `display.py` builds one set of usage rows from a per-session document and reads `service.load_health()` for the closing dashboard line, then frames them for the Codex `Stop` hook and both providers' desktop `UserPromptSubmit` context, or prints them flat for the Claude status line.
9. `dashboard/` contains static HTML, CSS, JavaScript, and images served by the local process.

## Local files

| Path | Purpose | Mode |
| --- | --- | --- |
| `~/.konvu/telemetry/konvu-launcher` | Absolute-path integration launcher | `0700` |
| `~/.konvu/telemetry/live-sessions.json` | Bounded summary of sessions active in the dashboard's 20-minute window | `0600` |
| `~/.konvu/telemetry/account-quotas.json` | Latest normalized provider limits and transient failure state | `0600` |
| `~/.konvu/telemetry/sessions/*.json` | Per-session detail loaded on demand by the dashboard | `0600` |
| `~/.konvu/telemetry/baselines.json` | Provider-level sparse-session forecast fallback | `0600` |
| `~/.konvu/telemetry/health.json` | Collector freshness and last error | `0600` |
| `~/.konvu/telemetry/tracking-state.json` | Anonymous install ID and local analytics preference | `0600` |
| `~/.konvu/telemetry/tracking-queue.json` | At most 100 pending anonymous analytics events | `0600` |
| `~/.konvu/telemetry/tracking.lock` | Cross-process lock for analytics state and queue | `0600` |
| `~/.konvu/telemetry/preferences.json` | Chosen display cadence, custom rule text and jump threshold | `0600` |
| `~/.konvu/telemetry/preferences.lock` | Cross-process lock so the command and dashboard cannot revert each other | `0600` |
| `~/.konvu/telemetry/custom_rule.py` | User-authored `should_show(context)` rule, kept outside the package so upgrades cannot erase it | User-owned |
| `~/.konvu/telemetry/shown/*.json` | Per-session record of the usage figure the last box displayed, pruned on the session retention schedule | `0600` |
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
- Unknown billable models mark the session partial; their iterations are omitted from forecasts while complete iterations remain usable.
- Oversized transcript records are scanned with bounded prefix and suffix buffers; large payload text is not retained.
- Dashboard responses use ETags, and the browser fetches detailed history only for the open session.
- Hooks read the collector's existing session output and never force collection. Their only writes are the per-session `shown/` record, and only under the `usage-jump` cadence. Under the custom cadence they also execute the user's own `custom_rule.py`.
- Failed analytics delivery retains stable event IDs and uses exponential backoff capped at 24 hours.
- A second server cannot bind the same port and exits before starting another collector loop.

JSON is sufficient for the first release because the process writes one bounded current snapshot, small state files, and one file per session. SQLite becomes useful when the product needs arbitrary historical queries, migrations, or concurrent writers.
