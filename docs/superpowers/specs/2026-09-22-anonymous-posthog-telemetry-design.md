# Anonymous PostHog Telemetry Design

## Goal

Measure setup success, activation, repeat dashboard use, and collector failures without collecting usage data, source data, account data, or command contents.

## Scope

The feature is default-on for fresh installs. Existing installations without a tracking preference remain disabled after upgrading. A local `konvu-telemetry telemetry off` command permanently stops collection and deletes unsent analytics events. `telemetry on` re-enables it, and `telemetry status` reports the local state. Missing or invalid state fails closed. No account, workspace, login, group analytics, feature flags, person profiles, session replay, or autocapture is introduced.

## Event contract

All events use one random UUID generated per install as `distinct_id`, plus `$process_person_profile: false` and `$ip: "0"`. Capture time and a stable random event ID are assigned when an event enters the queue; retries reuse that ID for PostHog deduplication.

| Event | When | Allowed properties |
| --- | --- | --- |
| `telemetry setup completed` | Setup finishes successfully | `cli_version`, `os_family`, `duration_bucket` |
| `first snapshot ready` | First snapshot containing data visible in the dashboard is written | `cli_version`, `os_family` |
| `dashboard opened` | The root local dashboard document is requested | `cli_version`, `os_family`, `data_available` |
| `telemetry active day` | First dashboard open with visible data on a local calendar day | `cli_version`, `os_family` |
| `collector failed` | First snapshot collection failure on a local calendar day | `cli_version`, `os_family`, `stage` |

The collector queues at most 100 events locally under `~/.konvu/telemetry` with `0600` permissions. Setup writes its activation event synchronously so process exit cannot lose it; submission remains asynchronous. Resident collection and dashboard requests queue work in memory and never wait for disk or network I/O. A private cross-process lock prevents telemetry state races between the CLI and resident collector. A successful submit removes only the events included in that batch and marks one-time events delivered. Failure leaves the batch queued and applies exponential backoff capped at 24 hours.

The client uses only Python's standard library to POST batches to PostHog US ingest with the supplied public project key. The project must discard IP addresses; its replay and autocapture features remain disabled. The client itself sends no URL, hostname, environment, error text, paths, raw duration, raw command, usage metrics, transcript content, or user-supplied values.

## Boundaries

`tracking.py` is the only module allowed to construct or send analytics payloads. `installer.py` records successful setup after all installation steps succeed. `service.py` records an opaque collector failure category and dashboard events. The existing local telemetry and provider transcript code never passes data into tracking.

The README and architecture document change their privacy statements to describe the small outbound telemetry flow and the opt-out command accurately.

## Validation

Unit tests exercise fresh-install and upgrade defaults, fail-closed state handling, persisted opt-out errors, allowlisted payload construction, stable event IDs, transport backoff, queue removal after success, daily-event deduplication, and service/installer integration boundaries. Tests isolate the filesystem and network. The complete existing test suite must remain green.
