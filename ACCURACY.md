# Accuracy and methodology

Konvu Telemetry reports local estimates from provider-recorded usage. It is not an invoice and does not infer missing billable usage.

## Cost

Each recorded model call is priced from the bundled LiteLLM snapshot using new input, output, cache-write, cache-read, web-search, service-tier, and long-context fields. Anthropic one-hour cache writes use the documented two-times input rate. Unknown billable models make the session partial or unavailable; Konvu never presents a known subtotal as complete.

The snapshot source and SHA-256 are recorded in `NOTICE`. The scheduled pricing workflow validates required models, updates the snapshot and checksum together, and opens a reviewable pull request.

For ChatGPT-authenticated Codex sessions, Konvu also calculates subscription-credit equivalents from OpenAI's published per-million-token rates. It detects the current local authentication mode and plan, and the resident collector reads Codex quota windows and credit balance with the existing local OAuth token. These values are credit equivalents, not proof that credits were charged; Konvu does not convert credits to dollars because purchase prices can differ by account.

## Prompts, subagents, and context

Prompt boundaries come from explicit provider records. Subagent calls are deduplicated by message identity and included in the parent session total. Their spend is attributed to the parent prompt that spawned them when the provider records that relationship; otherwise timestamps are used and the per-prompt attribution is approximate.

Context is the latest provider-recorded input plus cache traffic for a model call. It is not cumulative token traffic. Codex supplies its context-window size directly. Claude uses the published capacity in the bundled model-price snapshot and labels it as model pricing rather than provider-observed capacity.

## Forecasts and medians

The next-ten forecast first uses the mean cost of up to ten completed, fully priced prompts matching the current model, reasoning effort, and service speed. With fewer than three comparable prompts, it falls back to the provider-wide median forecast from recent local sessions; if no global history exists, any completed prompts in the current session are used. After compaction, only post-compaction prompts are comparable; while that history warms up, a context-scaled pre-compaction estimate is labeled separately. The baseline document includes the median held-out percentage error for recent Claude and Codex sessions with at least 20 prompts.

Personal baselines cumulatively sum the median cost and token traffic at each prompt position across sessions from the last 30 days that reached that prompt. At least five sessions must reach a prompt. The collector stores an exact checkpoint for every prompt through prompt 100, then interpolates only between measured checkpoints and suppresses values outside the observed range.

Hot-session alerts are driven by the next-ten forecast alone: a session alerts when it was active within the last twenty minutes, its cost is fully priced, and that forecast exceeds $4. Medians drive no alert; both the general provider median and the model-and-effort median remain dashboard comparisons only.

Run `konvu-telemetry backtest-next-ten` against local Claude history to inspect rolling forecast error. Run `python3 scripts/benchmark.py` for a synthetic performance receipt that never reads real transcripts.

## Known limits

- Provider transcript formats can change before Konvu ships a parser update.
- API-equivalent prices can differ from subscriptions, credits, negotiated rates, taxes, and provider invoices.
- The current Codex login applies to live usage; historical transcripts do not record which subscription paid for them.
- If the authenticated Codex usage request fails, quota display falls back to the latest transcript observation.
- Per-prompt Codex subagent attribution is timestamp-based when no spawn timestamp is recorded.
- Forecasts describe recent local behavior; they are not guarantees and are hidden when evidence is insufficient.
