# Anonymous PostHog Telemetry Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add minimal PostHog telemetry that is default-on for fresh installs, disabled for upgrades without a preference, and has a durable opt-out with no impact on collection or dashboard requests.

**Architecture:** `tracking.py` is the only analytics boundary. It persists an anonymous install ID, enabled flag, and queue in the existing private local directory. It sends batches in a daemon thread using the standard library. `installer.py` and `service.py` only invoke named tracking functions.

**Tech Stack:** Python 3.10 standard library, `unittest`, PostHog batch ingest API.

**Spec:** `docs/superpowers/specs/2026-09-22-anonymous-posthog-telemetry-design.md`

## Global Constraints

- Use only the five event names and allowlisted properties in the spec.
- Send no prompts, code, transcripts, raw errors, paths, arguments, environment variables, usage data, user IDs, or workspace IDs.
- Include anonymous `distinct_id`, `$process_person_profile: false`, and `$ip: "0"` on every event.
- `telemetry off` deletes unsent events. Failed sends retain them.
- Delivery uses a daemon thread and a 500 ms timeout.

### Task 1: Tracking state and event queue

**Files:** Create `src/konvu_telemetry/tracking.py`; modify `src/konvu_telemetry/storage.py`; test `tests/test_tracking.py`.

**Interfaces:** `record_setup_completed(duration_seconds)`, `record_dashboard_opened(data_available)`, `record_collector_failure()`, `set_tracking_enabled(enabled)`, `tracking_status()`, and `flush_in_background()`.

- [ ] Write tests that prove fresh installs are enabled only during setup, upgrades without a preference remain disabled, `telemetry off` clears the queue, and an enabled event is queued privately.
- [ ] Run `PYTHONPATH=src python3 -m unittest tests.test_tracking -v` and verify failure because the module does not exist.
- [ ] Add `tracking_state_path()` and `tracking_queue_path()` to `storage.py`, then implement the smallest atomic private state store.
- [ ] Re-run the focused test and commit `feat: add private telemetry event queue`.

### Task 2: Allowlisted PostHog delivery

**Files:** Modify `src/konvu_telemetry/tracking.py`; modify `tests/test_tracking.py`.

**Interfaces:** A sender receives serialized bytes. `send_queued()` removes events only after a 2xx response.

- [ ] Write tests proving the payload has only standard properties plus the event allowlist, and a failed transport retains queued events.
- [ ] Run the focused test and verify it fails because delivery is absent.
- [ ] Implement one exact event-to-properties allowlist and a `urllib.request` sender to PostHog US batch ingest with a 500 ms timeout.
- [ ] Re-run the focused test and commit `feat: send anonymous telemetry batches`.

### Task 3: Lifecycle instrumentation

**Files:** Modify `src/konvu_telemetry/installer.py`, `src/konvu_telemetry/service.py`, `tests/test_installer.py`, and `tests/test_service.py`.

- [ ] Write tests proving a successful setup records a duration bucket, root dashboard requests record `data_available`, and collection failures use the fixed `snapshot` stage.
- [ ] Run focused installer/service tests and verify they fail because the calls are absent.
- [ ] Record only named lifecycle events. Use monotonic setup duration, one daily event after dashboard open, and background delivery only.
- [ ] Re-run focused tests and commit `feat: record telemetry lifecycle events`.

### Task 4: Controls and documentation

**Files:** Modify `src/konvu_telemetry/cli.py`, `README.md`, `ARCHITECTURE.md`, and `tests/test_tracking.py`.

- [ ] Write a failing test for `telemetry on`, `telemetry off`, and `telemetry status`.
- [ ] Run the focused test and verify it fails because `telemetry` is unknown.
- [ ] Add the command and document the default-on behavior, exact event categories, exclusions, anonymous identifier, disabled profiles/IP capture, and opt-out command. Remove the no-outbound-calls claim.
- [ ] Re-run focused tests and commit `docs: explain anonymous telemetry controls`.

### Task 5: Full verification

- [ ] Run `PYTHONPATH=src python3 -m unittest discover -s tests -v`.
- [ ] Run `python3 -m compileall -q src tests && git diff --check`.
- [ ] Commit the design and plan with `docs: plan anonymous telemetry`.
