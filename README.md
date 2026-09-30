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

Setup starts the local collector, opens the dashboard at `http://127.0.0.1:7824`, and wires each client to exactly one place: the Claude Code CLI shows usage in its status line, Claude Desktop and Codex Desktop append a usage box to the reply, and the Codex CLI keeps its Stop hook summary. By default the box appears after a turn that called tools, so ordinary questions stay uncluttered; `konvu-telemetry cadence` changes that, and so does the settings panel in the dashboard. Every usage summary ends with a link to the dashboard, or, when the collector is not running, with a reminder to run `konvu-telemetry setup`. Restart both desktop apps after setup; in Codex, open `/hooks` and trust the Konvu hooks.

## Use it

```sh
konvu-telemetry dashboard  # Open the local dashboard
konvu-telemetry status     # Check that collection is running
konvu-telemetry cadence    # Choose how often the usage box appears
konvu-telemetry --version  # Print the installed version
```

`status` reports the installed version alongside collector health, and
`health.collector_version` inside it is the version the running collector was started
from. The two differ between a Homebrew upgrade and the collector restarting that
follows it.

`cadence` with no argument lists the choices: after every prompt, after every tool
call (the default), only when usage jumps, never, or a custom rule you describe and
your coding agent writes. Pass one directly — `konvu-telemetry cadence never` — or
`--status` to see the current one. The same choice lives in the dashboard's settings
panel, and it is stored in your home directory, so upgrading does not reset it.

To update:

```sh
brew upgrade konvu-telemetry
konvu-telemetry setup
```

To remove the service, integrations, local telemetry data, and Homebrew package:

```sh
konvu-telemetry uninstall
```

## What it does

- Tracks Claude Code, Claude Code Desktop, Codex CLI, and Codex Desktop sessions from their local transcripts.
- Estimates spend from bundled model pricing and records subscription-credit equivalents for supported Codex models.
- Runs only on your Mac and serves the dashboard only at `127.0.0.1`.
- Uses browser notifications only when you enable them in the local dashboard. An active paid session alerts once its next ten prompts are forecast at $10 or more; repeat suppression stays in that browser.

Costs are estimates, not provider invoices. See [ACCURACY.md](ACCURACY.md) for the exact accounting, forecast, context, and subagent methodology. Linux and Windows are not supported in this first release.

## Privacy

Konvu Telemetry stores derived usage data in `~/.konvu/telemetry`; provider transcript files are never changed. The background collector is the only component that fetches account limits: every two minutes it calls Anthropic's usage endpoint for Claude and asks Codex's installed local app-server to contact OpenAI using Codex's own login. Konvu Telemetry holds the Claude credential only for that request and never receives the Codex credential. Credentials, raw provider response bodies, and account IDs are never copied to Konvu files or logs. For each limit window the collector keeps only its normalized form: the provider's limit bucket identifier, the window period and length, the percentage used, the reset time, and the provider's limit-reached and spend-control flags.

Separately, setup enables a small amount of anonymous product telemetry to PostHog by default: successful setup, when the first dashboard-visible snapshot is ready, dashboard opens, one active-day event per day when data is visible, and collector failures, at most once per day. An existing telemetry choice remains unchanged.

Events use a random per-install ID so we can measure activation and repeat use. Each event receives its capture time and a random deduplication ID. They do not include prompts, code, transcripts, file paths, command arguments, token usage, raw errors, environment variables, account IDs, or workspace IDs. Events do not create PostHog person profiles and request IP discard. The local queue is capped at 100 events, and failed delivery backs off for up to 24 hours. To disable product telemetry and delete any unsent events, run:

```sh
konvu-telemetry telemetry off
```

Use `konvu-telemetry telemetry on` to re-enable it or `konvu-telemetry telemetry status` to inspect the local preference.

See [SECURITY.md](SECURITY.md) for security reporting, [CONTRIBUTING.md](CONTRIBUTING.md) for development, [ARCHITECTURE.md](ARCHITECTURE.md) for implementation details, and [PERFORMANCE.md](PERFORMANCE.md) for reproducible resource measurements.

## License

MIT. Third-party pricing attribution is in [NOTICE](NOTICE).
