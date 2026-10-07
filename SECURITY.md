# Security policy

Konvu Telemetry keeps transcripts, prompts, source code, API keys, and local paths on the device. As detailed below, the collector uses the Claude credential in memory to fetch account limits from Anthropic, while Codex's own app-server contacts OpenAI without exposing its credential to Konvu Telemetry. Only normalized usage details are stored locally, with one exception: a session title truncated from each session's first prompt is stored locally so the dashboard can name the session, and it never leaves the machine. Separately, setup sends the anonymous, allowlisted product events listed below. Please do not file public issues containing private local data.

Report security issues privately to security@konvu.com with reproduction steps and the affected version.

## Data handling in detail

Konvu Telemetry stores derived usage data in `~/.konvu/telemetry`; provider transcript files are never changed. The background collector is the only component that fetches account limits: every two minutes it calls Anthropic's usage endpoint for Claude and asks Codex's installed local app-server to contact OpenAI using Codex's own login. Konvu Telemetry holds the Claude credential only for that request and never receives the Codex credential. Credentials, raw provider response bodies, and account IDs are never copied to Konvu files or logs. For each limit window the collector keeps only its normalized form: the provider's limit bucket identifier, the window period and length, the percentage used, the reset time, and the provider's limit-reached and spend-control flags.

Context drift analysis is on by default for sessions within your plan: `konvu-telemetry setup` asks once (default yes, or pass `--context-analysis on|off`), an earlier explicit no is kept, and you can switch it later in the dashboard settings or with `konvu-telemetry context-analysis off`. For each session active in the last 20 minutes, after every ten new prompts, the collector may send bounded excerpts from that session back to the same provider through its authenticated local CLI, in batches of up to 90 context groups per call and at most 30 calls per session (60 overall) per hour. Claude content goes only to Anthropic and Codex content only to OpenAI; Konvu does not receive it. Claude tools are disabled; Codex runs ephemerally from a temporary directory in its read-only sandbox, with shell, web and apps turned off. Each call is limited to 90 seconds. Sessions within the plan are analyzed only while fresh provider limits show less than 90% usage; `--allow-paid` also analyzes sessions billed beyond the plan, which may spend credits. Short topic summaries, relevance scores and timestamps stay in that session's local context map, which is deleted seven days after the session was last active. Provider-reported token use and its API-price estimate are kept for eight days and enter the same quota-attribution ledger as normal sessions under its parent session.

Separately, setup enables a small amount of anonymous product telemetry to PostHog by default: successful setup, when the first dashboard-visible snapshot is ready, dashboard opens, one active-day event per day when data is visible, and collector failures, at most once per day. An existing telemetry choice remains unchanged.

Events use a random per-install ID so we can measure activation and repeat use. Each event receives its capture time and a random deduplication ID. They do not include prompts, code, transcripts, file paths, command arguments, token usage, raw errors, environment variables, account IDs, or workspace IDs. Events do not create PostHog person profiles and request IP discard. The local queue is capped at 100 events, and failed delivery backs off for up to 24 hours. To disable product telemetry and delete any unsent events, run:

```sh
konvu-telemetry telemetry off
```

Use `konvu-telemetry telemetry on` to re-enable it or `konvu-telemetry telemetry status` to inspect the local preference.
