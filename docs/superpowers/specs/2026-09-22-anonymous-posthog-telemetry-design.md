# Anonymous PostHog Telemetry Design

## Goal

Measure setup success, activation, repeat dashboard use, and collector failures without collecting usage data, source data, account data, or command contents.

## Scope

The feature is default-on for fresh installs. A local `konvu-telemetry telemetry off` command permanently stops collection and deletes unsent analytics events. `telemetry on` re-enables it, and `telemetry status` reports the local state. No account, workspace, login, group analytics, feature flags, person profiles, session replay, or autocapture is introduced.

## Event contract

All events use one random UUID generated per install as `distinct_id`, plus `$process_person_profile: false` and `$ip: "0"`.

| Event | When | Allowed properties |
| --- | --- | --- |
| `telemetry setup completed` | Setup finishes successfully | `cli_version`, `os_family`, `duration_bucket` |
| `first snapshot ready` | First snapshot containing session data is written | `cli_version`, `os_family` |
| `dashboard opened` | The root local dashboard document is requested | `cli_version`, `os_family`, `data_available` |
| `telemetry active day` | First dashboard open on a local calendar day | `cli_version`, `os_family` |
| `collector failed` | A snapshot collection attempt fails | `cli_version`, `os_family`, `stage` |

The collector queues events locally under `~/.konvu/telemetry` with `0600` permissions. It submits batches in a background thread with a 500 ms timeout. Collection and dashboard requests never wait for the network. A successful submit removes only the events included in that batch. Failure leaves the batch queued for a later attempt.

The client uses only Python's standard library to POST batches to PostHog US ingest with the supplied public project key. The project must discard IP addresses; its replay and autocapture features remain disabled. The client itself sends no URL, hostname, environment, error text, paths, raw duration, raw command, usage metrics, transcript content, or user-supplied values.

## Boundaries

`tracking.py` is the only module allowed to construct or send analytics payloads. `installer.py` records successful setup after all installation steps succeed. `service.py` records an opaque collector failure category and dashboard events. The existing local telemetry and provider transcript code never passes data into tracking.

The README and architecture document change their privacy statements to describe the small outbound telemetry flow and the opt-out command accurately.

## Validation

Unit tests exercise persisted opt-out state, allowlisted payload construction, queue retention on transport failure, queue removal after success, event de-duplication for the daily event, and service/installer integration boundaries. The complete existing test suite must remain green.
