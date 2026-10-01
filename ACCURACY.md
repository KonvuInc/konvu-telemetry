# Accuracy and methodology

Konvu Telemetry reports local estimates from provider-recorded usage. It is not an invoice and does not infer missing billable usage.

## Cost

Each recorded model call is priced from the bundled LiteLLM snapshot using new input, output, cache-write, cache-read, web-search, service-tier, and long-context fields. Anthropic one-hour cache writes use the documented two-times input rate. Unknown billable models make the session partial or unavailable; Konvu never presents a known subtotal as complete.

The snapshot source and SHA-256 are recorded in `NOTICE`. The scheduled pricing workflow validates required models, updates the snapshot and checksum together, and opens a reviewable pull request.

For supported Codex model calls, Konvu also calculates subscription-credit equivalents from OpenAI's published per-million-token rates. These values show how many credits the recorded tokens correspond to if credit billing applies; they do not claim that the call was charged, because included limits and flexible-plan controls are account state reported separately under `account_quotas`. Konvu does not convert credits to dollars because purchase prices can differ by plan or agreement.

Account quota windows come only from the providers' live usage endpoints. A transient refresh failure marks the last provider result stale and retains each window until its provider-reported reset; Konvu never substitutes transcript or hook-derived limits.

A session's share of a window is its cut of the percentage points the provider has actually reported. Providers report whole points, so each rise is divided between the sessions that were running, in proportion to the tokens each spent since the previous rise. A rise that no running session recorded work for goes to whoever that window already credits, rather than being discarded. Nothing is added for work done since the last report, so a share holds still between rises and a session that has not yet caused a reported point reads zero.

The next-ten-prompt quota projection is the share the session was credited over its previous ten prompts. Under ten prompts of history, what it has burned so far is scaled to ten. The projection is capped at the window's remaining headroom.

## Prompts, subagents, and context

Prompt boundaries come from explicit provider records. Subagent calls are deduplicated by message identity and included in the parent session total. Their spend is attributed to the parent prompt that spawned them when the provider records that relationship; otherwise timestamps are used and the per-prompt attribution is approximate.

Context is the latest provider-recorded input plus cache traffic for a model call. It is not cumulative token traffic. Codex supplies its context-window size directly. Claude uses the published capacity in the bundled model-price snapshot and labels it as model pricing rather than provider-observed capacity.

The context map incrementally reads the same local transcripts and records only derived categories, bounded labels, token estimates, checkpoints, and byte cursors. Text is counted with pinned provider-family tokenizers: `tiktoken` with `o200k_base` for Codex and `ctok` for Claude. Python 3.12 is the minimum supported version, so every install uses the same tokenizers. Images and native documents use provider-specific weights until the next recorded input checkpoint.

Provider input checkpoints are authoritative for the total, while tokenizers distribute each checkpoint's growth between the additions recorded in that interval. Model changes affect only later additions. Compaction closes the current epoch and starts a new one from the provider-recorded retained context. Any difference that cannot be tied to visible transcript data remains explicitly categorized as provider-managed context; it can include hidden instructions, retained reasoning, framing, oversized records, and estimation error.

## Account limits

Account-limit percentages and reset times are provider-reported rather than estimated. Every two minutes, the collector reads Claude's five-hour and weekly windows from Anthropic's usage endpoint and asks Codex's local app-server for every reported rolling, monthly, model-specific, and denial state. Invalid percentages are rejected, while usage reported above 100 percent (or remaining capacity below zero) is recorded as exactly exhausted so that window is never dropped. Authentication failures clear the provider immediately; transient failures retain unexpired windows with an explicit stale status, while expired windows and their plan flags are removed. A Claude 429 starts an exponential backoff of at least one hour, waits until the next cached reset when later, and adds jitter so clients do not retry together.

## Forecasts

The next-ten forecast first uses the mean cost of up to ten completed, fully priced prompts matching the current model, reasoning effort, and service speed. With fewer than three comparable prompts, it falls back to the provider-wide median forecast from recent local sessions; if no global history exists, any completed prompts in the current session are used. After compaction, only post-compaction prompts are comparable; while that history warms up, a context-scaled pre-compaction estimate is labeled separately.

Hot-session browser alerts are driven by the next-ten forecast alone: a paid session alerts when it was active within the last twenty minutes, its cost is fully priced, and that forecast reaches $10. The browser stores alert cooldown state locally; the collector only marks whether the session is currently hot.

Run `konvu-telemetry backtest-next-ten` against local Claude history to inspect rolling forecast error. Run `python3 scripts/benchmark.py` for a synthetic performance receipt that never reads real transcripts.

## Known limits

- Provider transcript formats can change before Konvu ships a parser update.
- Context-map categories are estimates of responsibility, not proof that the model attended to or used a source. Native image and document attribution is less precise than visible text attribution.
- API-equivalent prices can differ from subscriptions, credits, negotiated rates, taxes, and provider invoices.
- Per-prompt Codex subagent attribution is timestamp-based when no spawn timestamp is recorded.
- Forecasts describe recent local behavior; they are not guarantees and are hidden when evidence is insufficient.
