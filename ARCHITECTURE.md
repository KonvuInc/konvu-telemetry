# Architecture

Konvu Telemetry is one Python package with a single resident process. The process reads provider-owned JSONL transcripts, writes private derived JSON under `~/.konvu/telemetry`, and serves bundled dashboard assets on `127.0.0.1:7824`.

## Boundaries

- The collector never modifies provider transcripts.
- The dashboard server binds only to IPv4 loopback and rejects non-local `Host` and `Origin` values.
- The package has no runtime Python dependencies. Its only outbound client sends a small allowlisted set of anonymous product events to PostHog in a background thread with a 500 ms timeout.
- Product analytics uses a random install ID. It does not create person profiles and sends no transcripts, prompts, code, paths, command arguments, usage data, raw errors, environment variables, account IDs, or workspace IDs.
- Browser notifications require an open dashboard tab and browser permission.
- A session alerts when it was active in the last twenty minutes, its cost is complete, and its next-ten-prompt forecast exceeds `ALERT_FORECAST_USD`. It alerts again only after `ALERT_FORECAST_RENOTIFY_SECONDS` and only if the forecast has not fallen since the last alert; falling to or below the threshold re-arms it.
- Claude and Codex integrations read precomputed session files; they do not parse transcripts in a hook invocation.

## Runtime flow

1. `installer.py` installs a private launcher, merges supported integrations, and starts a per-user LaunchAgent.
2. `service.py` owns the collector loop, health record, and localhost HTTP server.
3. `live.py` and `fleet_telemetry.py` incrementally read appended transcript bytes and retain bounded metadata for active files.
4. `parsers.py` converts provider records into the provider-neutral types in `models.py`.
5. `pricing.py` applies the bundled local price table and marks missing prices explicitly.
6. `analytics.py` derives prompt series, personal baselines, forecasts, compaction state, and alert decisions. Expired valid baselines remain usable while one background refresh rebuilds them.
7. `snapshot.py` writes a bounded dashboard summary plus detailed per-session documents. Unchanged detail documents are not rewritten.
8. `display.py` renders the Claude status line and Codex Stop-hook output from per-session documents.
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
| `~/.konvu/telemetry/tracking-queue.json` | Pending anonymous analytics events | `0600` |
| `~/.konvu/telemetry/collector*.log` | LaunchAgent stdout and stderr | User-owned |
| `~/Library/LaunchAgents/com.konvu.telemetry.plist` | Per-user service definition | User-owned |
| `~/.claude/settings.json` | Optional Claude status line merge | `0600` after write |
| `~/.codex/hooks.json` | Optional Codex Stop hook merge | `0600` after write |

Raw transcripts remain in `~/.claude/projects` and `~/.codex/sessions`. Normalized session files expire after seven days. Uninstall removes the service, launcher, and Konvu-owned config entries but preserves telemetry data for manual inspection or deletion.

## Configuration

| Variable | Override |
| --- | --- |
| `KONVU_LIVE_USAGE_HOME` | Derived telemetry directory |
| `KONVU_LIVE_USAGE_CLAUDE_DIR` | Claude transcript roots, separated by the platform path separator |
| `KONVU_LIVE_USAGE_CODEX_DIR` | Codex transcript roots, separated by the platform path separator |
| `KONVU_TELEMETRY_PRICING_PATH` | Local pricing JSON file |

Run `konvu-telemetry telemetry off` to disable anonymous product analytics and delete pending events. `telemetry on` restores the default behavior, and `telemetry status` prints the local state.

## Failure behavior

- Writes use a temporary file and atomic replacement.
- Setup validates configuration first and restores prior files and service state if any step fails.
- Uninstall refuses to delete the service definition when launchd still reports it running.
- Unknown billable models mark the session partial; their iterations are omitted from forecasts and cost comparisons while complete iterations remain usable.
- Oversized transcript records are scanned with bounded prefix and suffix buffers; large payload text is not retained.
- Dashboard responses use ETags, and the browser fetches detailed history only for the open session.
- Hooks only read the collector's existing session output; they never force collection.
- A second server cannot bind the same port and exits before starting another collector loop.

JSON is sufficient for the first release because the process writes one bounded current snapshot, small state files, and one file per session. SQLite becomes useful when the product needs arbitrary historical queries, migrations, or concurrent writers.
