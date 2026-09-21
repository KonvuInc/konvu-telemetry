# Architecture

Konvu Telemetry is one Python package with a single resident process. The process reads provider-owned JSONL transcripts, writes private derived JSON under `~/.konvu/telemetry`, and serves bundled dashboard assets on `127.0.0.1:7824`.

## Boundaries

- The collector never modifies provider transcripts.
- The dashboard server binds only to IPv4 loopback and rejects non-local `Host` and `Origin` values.
- The package has no runtime Python dependencies and contains no outbound network client.
- Browser notifications require an open dashboard tab and browser permission.
- Claude and Codex integrations read precomputed session files; they do not parse transcripts in a hook invocation.

## Runtime flow

1. `installer.py` installs a private launcher, merges supported integrations, and starts a per-user LaunchAgent.
2. `service.py` owns the collector loop, health record, and localhost HTTP server.
3. `live.py` incrementally reads appended transcript bytes and retains bounded in-memory state for active files.
4. `parsers.py` converts provider records into the provider-neutral types in `models.py`.
5. `pricing.py` applies the bundled local price table and marks missing prices explicitly.
6. `analytics.py` derives prompt series, personal baselines, forecasts, compaction state, and alert decisions.
7. `snapshot.py` assembles and atomically writes dashboard and per-session documents.
8. `display.py` renders the Claude status line and Codex Stop-hook output from per-session documents.
9. `dashboard/` contains static HTML, CSS, JavaScript, and images served by the local process.

## Local files

| Path | Purpose | Mode |
| --- | --- | --- |
| `~/.konvu/telemetry/konvu-launcher` | Absolute-path integration launcher | `0700` |
| `~/.konvu/telemetry/live-sessions.json` | Current dashboard snapshot | `0600` |
| `~/.konvu/telemetry/sessions/*.json` | Per-session display snapshots | `0600` |
| `~/.konvu/telemetry/baselines.json` | Local historical medians | `0600` |
| `~/.konvu/telemetry/health.json` | Collector freshness and last error | `0600` |
| `~/.konvu/telemetry/notification-state.json` | Alert suppression state | `0600` |
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

## Failure behavior

- Writes use a temporary file and atomic replacement.
- Setup validates configuration first and restores prior files and service state if any step fails.
- Uninstall refuses to delete the service definition when launchd still reports it running.
- Unknown billable models suppress cost totals, forecasts, comparisons, and spend alerts.
- Oversized transcript records are scanned with bounded prefix and suffix buffers; large payload text is not retained.
- A second server cannot bind the same port and exits before starting another collector loop.

JSON is sufficient for the first release because the process writes one bounded current snapshot, small state files, and one file per session. SQLite becomes useful when the product needs arbitrary historical queries, migrations, or concurrent writers.
