# Konvu Telemetry

See what Claude Code, Claude Code Desktop, Codex CLI, and Codex Desktop are costing while you work.

Konvu Telemetry reads the session data already on your Mac and shows local spend, context use, forecasts, subagents, and usage trends in a dashboard and CLI status line. Your prompts, code, transcripts, and usage data stay on your machine.

## Install

Requires macOS, Homebrew, and Claude Code or Codex CLI.

```sh
brew tap konvuinc/tap
brew install konvuinc/tap/konvu-telemetry
konvu-telemetry setup
```

Setup starts the local collector, opens the dashboard at `http://127.0.0.1:7824`, and wires each client to exactly one place: the Claude Code CLI shows usage in its status line, Claude Desktop and Codex Desktop append a usage box to the reply, and the Codex CLI keeps its Stop hook summary. The box appears when the last prompt used a tool, so ordinary questions stay uncluttered. Restart both desktop apps after setup; in Codex, open `/hooks` and trust the Konvu hooks.

## Use it

```sh
konvu-telemetry dashboard  # Open the local dashboard
konvu-telemetry status     # Check that collection is running
```

To update:

```sh
brew upgrade konvu-telemetry
konvu-telemetry setup
```

To remove it, clean up the service and integrations before removing the Homebrew package:

```sh
konvu-telemetry uninstall
brew uninstall konvu-telemetry
```

## What it does

- Tracks Claude Code, Claude Code Desktop, Codex CLI, and Codex Desktop sessions from their local transcripts.
- Estimates spend from bundled model pricing and shows forecasts based on local history.
- Runs only on your Mac and serves the dashboard only at `127.0.0.1`.
- Uses browser notifications only when you enable them in the local dashboard. Fresh provider data selects quota alerts while included usage remains and money alerts after confirmed exhaustion; missing or stale quota data sends neither.

Costs are estimates, not provider invoices. See [ACCURACY.md](ACCURACY.md) for the exact accounting, forecast, median, context, and subagent methodology. Linux and Windows are not supported in this first release.

## Privacy

Konvu Telemetry stores derived usage data in `~/.konvu/telemetry`; provider transcript files are never changed. Setup enables a small amount of anonymous product telemetry to PostHog by default: successful setup, when the first dashboard-visible snapshot is ready, dashboard opens, one active-day event per day when data is visible, and collector failures, at most once per day. An existing telemetry choice remains unchanged.

Events use a random per-install ID so we can measure activation and repeat use. Each event receives its capture time and a random deduplication ID. They do not include prompts, code, transcripts, file paths, command arguments, token usage, raw errors, environment variables, account IDs, or workspace IDs. Events do not create PostHog person profiles and request IP discard. The local queue is capped at 100 events, and failed delivery backs off for up to 24 hours. To disable product telemetry and delete any unsent events, run:

```sh
konvu-telemetry telemetry off
```

Use `konvu-telemetry telemetry on` to re-enable it or `konvu-telemetry telemetry status` to inspect the local preference.

See [SECURITY.md](SECURITY.md) for security reporting, [CONTRIBUTING.md](CONTRIBUTING.md) for development, [ARCHITECTURE.md](ARCHITECTURE.md) for implementation details, and [PERFORMANCE.md](PERFORMANCE.md) for reproducible resource measurements.

## License

MIT. Third-party pricing attribution is in [NOTICE](NOTICE).
