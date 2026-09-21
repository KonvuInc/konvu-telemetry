# Accuracy and methodology

Konvu Telemetry reports local estimates from provider-recorded usage. It is not an invoice and does not infer missing billable usage.

## Cost

Each recorded model call is priced from the bundled LiteLLM snapshot using new input, output, cache-write, cache-read, web-search, service-tier, and long-context fields. Anthropic one-hour cache writes use the documented two-times input rate. Unknown billable models make the session partial or unavailable; Konvu never presents a known subtotal as complete.

The snapshot source and SHA-256 are recorded in `NOTICE`. The scheduled pricing workflow validates required models, updates the snapshot and checksum together, and opens a reviewable pull request.

## Prompts, subagents, and context

Prompt boundaries come from explicit provider records. Subagent calls are deduplicated by message identity and included in the parent session total. Their spend is attributed to the parent prompt that spawned them when the provider records that relationship; otherwise timestamps are used and the per-prompt attribution is approximate.

Context is the latest provider-recorded input plus cache traffic for a model call. It is not cumulative token traffic. Codex supplies its context-window size directly. Claude uses conservative 200K display capacity until observed usage proves that an advertised larger model window is active.

## Forecasts and medians

The next-ten forecast is the mean cost of up to ten completed, fully priced prompts matching the current model, reasoning effort, and service speed. At least three comparable prompts are required. After compaction, only post-compaction prompts are comparable; while that history warms up, a context-scaled pre-compaction estimate is labeled separately. The baseline document includes the median held-out percentage error for recent Claude and Codex sessions with at least 20 prompts.

Personal baselines use the median cumulative cost and token traffic of sessions that reached the same checkpoint during the last 30 days. At least five sessions are required. Checkpoint medians are clamped to the previous checkpoint when cohort changes would otherwise make cumulative spend decrease. Values are interpolated only between measured checkpoints and are suppressed outside the observed range.

Run `konvu-telemetry backtest-next-ten` against local Claude history to inspect rolling forecast error. Run `python3 scripts/benchmark.py` for a synthetic performance receipt that never reads real transcripts.

## Known limits

- Provider transcript formats can change before Konvu ships a parser update.
- API-equivalent prices can differ from subscriptions, credits, negotiated rates, taxes, and provider invoices.
- Per-prompt Codex subagent attribution is timestamp-based when no spawn timestamp is recorded.
- Forecasts describe recent local behavior; they are not guarantees and are hidden when evidence is insufficient.
