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

Setup starts the local collector, opens the dashboard at `http://127.0.0.1:7824`, and adds the Claude Code status line plus, for each provider, a `Stop` hook for the terminal CLI and a `UserPromptSubmit` hook for the desktop app. The desktop apps hide hook messages behind a collapsed notice, so there the usage summary is appended to the reply itself; only one of the two hooks reports per turn. Restart both desktop apps after setup; in Codex, open `/hooks` and trust the Konvu hooks.

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
- Uses browser notifications only when you enable them in the local dashboard. An active session alerts once its next ten prompts are forecast above $4, then repeats at most every five minutes and only while the forecast has not come down.

Costs are estimates, not provider invoices. See [ACCURACY.md](ACCURACY.md) for the exact accounting, forecast, median, context, and subagent methodology. Linux and Windows are not supported in this first release.

## Privacy

Konvu Telemetry has no account, API key, analytics service, or outbound network calls. It stores derived usage data in `~/.konvu/telemetry`; provider transcript files are never changed.

See [SECURITY.md](SECURITY.md) for security reporting, [CONTRIBUTING.md](CONTRIBUTING.md) for development, [ARCHITECTURE.md](ARCHITECTURE.md) for implementation details, and [PERFORMANCE.md](PERFORMANCE.md) for reproducible resource measurements.

## License

MIT. Third-party pricing attribution is in [NOTICE](NOTICE).
