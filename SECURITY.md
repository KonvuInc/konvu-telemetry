# Security policy

Konvu Telemetry keeps transcripts, prompts, source code, API keys, and local paths on the device. As documented in the README, the collector uses the Claude credential in memory to fetch account limits from Anthropic, while Codex's own app-server contacts OpenAI without exposing its credential to Konvu Telemetry. Only normalized usage details are stored locally, with one exception: a session title truncated from each session's first prompt is stored locally so the dashboard can name the session, and it never leaves the machine. Separately, setup sends the anonymous, allowlisted product events documented in the README. Please do not file public issues containing private local data.

Report security issues privately to security@konvu.com with reproduction steps and the affected version.
