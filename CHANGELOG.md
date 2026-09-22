# Changelog

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
