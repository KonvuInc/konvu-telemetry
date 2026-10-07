# Konvu Telemetry

See what Claude Code, Claude Code Desktop, Codex CLI, and Codex Desktop are costing while you work.

Konvu Telemetry reads the session data already on your Mac and shows local spend, context use, forecasts, subagents, and usage trends in a dashboard and CLI status line. Each session inspector shows context use and local source categories; eligible sessions also show AI-rated relevance and conversation topics. Konvu never receives your prompts, code, transcripts, or usage data.

## Install

Requires macOS, Homebrew, and Claude Code or Codex CLI.

```sh
brew tap konvuinc/tap
brew install konvuinc/tap/konvu-telemetry
konvu-telemetry setup
```

Direct Python installs require Python 3.12 or newer. Homebrew installs its own compatible Python.

Setup starts the local collector, opens the dashboard at `http://127.0.0.1:7824`, and wires each client to exactly one place: the Claude Code CLI shows usage in its status line, Claude Desktop and Codex Desktop append a usage box to the reply, and the Codex CLI keeps its Stop hook summary. By default the box appears after a turn that called tools, so ordinary questions stay uncluttered; `konvu-telemetry cadence` changes that, and so does the settings panel in the dashboard. Every usage summary ends with a link to the dashboard, or, when the collector is not running, with a reminder to run `konvu-telemetry setup`. Restart both desktop apps after setup; in Codex, open `/hooks` and trust the Konvu hooks.

## Use it

```sh
konvu-telemetry dashboard  # Open the local dashboard
konvu-telemetry status     # Check that collection is running
konvu-telemetry cadence    # Choose how often the usage box appears
konvu-telemetry context-analysis off  # Disable conversation-drift analysis
konvu-telemetry context-analysis on --allow-paid  # Also analyze sessions beyond the plan
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
- Maps prompts, files, tool results, web data, attachments, instructions, agent output, and compaction summaries into the current context window without storing their contents.
- Uses the matching local Claude or Codex login to group mapped context by topic and estimate what remains relevant. By default it runs only while fresh provider limits confirm the account is within its plan (`--allow-paid` lifts this) and can be disabled with `konvu-telemetry context-analysis off`.
- Estimates spend from bundled model pricing and records subscription-credit equivalents for supported Codex models.
- Runs only on your Mac and serves the dashboard only at `127.0.0.1`.
- Uses browser notifications only when you enable them in the local dashboard. An active paid session alerts once its next ten prompts are forecast at $10 or more; repeat suppression stays in that browser.

## Context drift analysis (optional)

Long sessions fill up with context the agent no longer needs: old file reads, finished
investigations, replaced plans. When you turn this on, Konvu asks a small model (Claude
Haiku for Claude sessions, gpt-6-luna for Codex) to rate each part of an active session's
context as needed or not needed for the current goal. The session inspector shows the
split, and the Claude status line suggests a `/compact` once enough of the context is
finished or replaced.

It is off until you say yes. `konvu-telemetry setup` asks once, on install and after an
update, and you can change it at any time:

```sh
konvu-telemetry context-analysis on      # Turn it on
konvu-telemetry context-analysis off     # Turn it off
konvu-telemetry context-analysis status  # Show the current choice
konvu-telemetry context-analysis on --allow-paid  # Also run for sessions beyond your plan
konvu-telemetry context-analysis on --plan-only   # Back to plan-only (the default)
konvu-telemetry setup --context-analysis on|off   # Answer the setup question up front
```

The same switches live in the dashboard's settings panel.

It costs very little. A review is usually a few cents at API prices ($0.01 to $0.10; the
first review of a very long session can reach about $0.50), and on a Claude or ChatGPT
plan it comes out of the allowance you already have, as a sliver of a 5-hour window. Hard
limits keep it that way:

- It reviews a session after every 10 new prompts, or once its context grows by about 60k
  tokens, and only while the session is active. A long session's first review may need a
  few short catch-up passes, and a `/compact` starts a fresh review of what is left.
- No more than 30 model calls per session and 60 overall per hour, passes on one session
  at least 30 seconds apart, each call capped at 90 seconds (and $0.10 for Claude).
- By default it runs only for sessions within your plan, and only while fresh provider
  limits show under 90% used, so it never eats the end of your allowance. `--allow-paid`
  also lets it run for sessions billed beyond the plan, which may spend credits.
- Failed runs back off exponentially instead of retrying in a loop.

Every run's tokens and estimated price are shown on its session in the dashboard.

Costs are estimates, not provider invoices. See [ACCURACY.md](ACCURACY.md) for the exact accounting, forecast, context, and subagent methodology. Linux and Windows are not supported in this first release.

## Privacy

Konvu Telemetry stores derived usage data in `~/.konvu/telemetry`; provider transcript files are never changed. The background collector is the only component that fetches account limits: every two minutes it calls Anthropic's usage endpoint for Claude and asks Codex's installed local app-server to contact OpenAI using Codex's own login. Konvu Telemetry holds the Claude credential only for that request and never receives the Codex credential. Credentials, raw provider response bodies, and account IDs are never copied to Konvu files or logs. For each limit window the collector keeps only its normalized form: the provider's limit bucket identifier, the window period and length, the percentage used, the reset time, and the provider's limit-reached and spend-control flags.

Context drift analysis is off until you explicitly enable it: `konvu-telemetry setup` asks once (default no, or pass `--context-analysis on|off`), and you can switch it later in the dashboard settings or with `konvu-telemetry context-analysis on`. After every ten new prompts, or once the context grows by about 60k tokens, the collector may send bounded excerpts from that session back to the same provider through its authenticated local CLI, in batches of up to 90 context groups per call and at most 30 calls per session (60 overall) per hour. Claude content goes only to Anthropic and Codex content only to OpenAI; Konvu does not receive it. Claude tools are disabled; Codex runs ephemerally from an empty directory in its read-only sandbox. Each call is limited to 90 seconds. Sessions within the plan are analyzed only while fresh provider limits show less than 90% usage; `--allow-paid` also analyzes sessions billed beyond the plan, which may spend credits. Short topic summaries, relevance scores, timestamps, provider-reported token use, and its API-price estimate are stored locally for eight days; that usage enters the same quota-attribution ledger as normal sessions under its parent session.

Separately, setup enables a small amount of anonymous product telemetry to PostHog by default: successful setup, when the first dashboard-visible snapshot is ready, dashboard opens, one active-day event per day when data is visible, and collector failures, at most once per day. An existing telemetry choice remains unchanged.

Events use a random per-install ID so we can measure activation and repeat use. Each event receives its capture time and a random deduplication ID. They do not include prompts, code, transcripts, file paths, command arguments, token usage, raw errors, environment variables, account IDs, or workspace IDs. Events do not create PostHog person profiles and request IP discard. The local queue is capped at 100 events, and failed delivery backs off for up to 24 hours. To disable product telemetry and delete any unsent events, run:

```sh
konvu-telemetry telemetry off
```

Use `konvu-telemetry telemetry on` to re-enable it or `konvu-telemetry telemetry status` to inspect the local preference.

See [SECURITY.md](SECURITY.md) for security reporting, [CONTRIBUTING.md](CONTRIBUTING.md) for development, [ARCHITECTURE.md](ARCHITECTURE.md) for implementation details, and [PERFORMANCE.md](PERFORMANCE.md) for reproducible resource measurements.

## License

MIT. Third-party pricing attribution is in [NOTICE](NOTICE).
