# Changelog

## Unreleased

- End every usage summary with a dashboard line. The Claude Code CLI status line, both desktop boxes, and the Codex CLI box link `http://127.0.0.1:7824/` while the collector is healthy, and otherwise tell you to run `konvu-telemetry setup` to start it. Hooks decide this from the collector's existing health record; they never open a connection.
- Show usage in Claude Desktop and Codex Desktop, which collapse hook system messages into a hidden notice. Setup registers a `UserPromptSubmit` hook per provider that injects the usage box as context, and the reply ends with it.
- Each client now reports usage in exactly one place: the Claude Code CLI in its status line, Claude Desktop in the appended box, the Codex CLI in its Stop hook box, and Codex Desktop in the appended box.
- Stop reporting Claude usage from a Stop hook. Setup removes the Claude `Stop` entry earlier versions installed, leaving any hooks you added yourself untouched, and the `claude-hook` command stays accepted but silent for settings written by older versions.
- Suppress the Codex Stop hook on Codex Desktop so a turn is not reported twice. Codex CLI output is unchanged.
- Show a usage box when, and only when, the last prompt used a tool, on every surface. The previous rule — $10 of spend, five prompts, and a meaningful change since the last box — is gone, along with the rate-limit state it needed. `~/.konvu/telemetry/codex-display-state.json` is no longer written or read and can be deleted.
- Keep showing usage from a session file the collector has not refreshed recently, instead of going silent five minutes after the last recorded activity.

## 0.2.4 - 2026-09-22

- Synchronize manual and scheduled dashboard refreshes.
- Keep Codex guardian reviews attached to their parent sessions.
- Add opt-in anonymous product telemetry.

## 0.2.3 - 2026-09-22

- Improve dashboard refresh and alert controls.
- Restore Codex compaction markers.
- Correct dashboard median comparisons.

## 0.2.2 - 2026-09-21

- Keep Codex quota usage visible alongside session context when quota data is available.
- Preserve monotonic dashboard medians across successive snapshots.
- Remove the redundant hook snapshot refresh fallback.

## 0.2.1 - 2026-09-21

- Alert on a session whenever its next-ten-prompt forecast exceeds $4, instead of also requiring cost to reach 3× the provider median. Sessions with no baseline, which could never alert before, now alert.
- Only alert on sessions active within the last twenty minutes, matching the dashboard's live window.
- Repeat a session alert after five minutes instead of ten, and only while the forecast has not come down since the last alert. Quota alerts keep their ten-minute cadence.

## 0.1.0 - 2026-09-20

- Initial local-first Claude Code and Codex usage monitoring release.
