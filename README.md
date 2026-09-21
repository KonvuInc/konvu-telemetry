# Konvu Telemetry

Konvu Telemetry is a local-first usage monitor for Claude Code and Codex CLI. It reads their existing local session transcripts, writes derived session files under `~/.konvu/telemetry`, and serves a dashboard only on `127.0.0.1`.

No prompts, transcripts, source code, API keys, or usage data leave the machine. The package has no runtime Python dependencies and makes no outbound network requests.

## Install

After the first public GitHub release and Homebrew tap are published:

```sh
brew install konvuinc/tap/konvu-telemetry && konvu-telemetry setup
```

Open `http://127.0.0.1:7824` or run `konvu-telemetry dashboard`. Setup installs a per-user macOS LaunchAgent, starts the collector, and opens the dashboard.

For development from a checkout:

```sh
python3 -m pip install -e .
konvu-telemetry setup
```

## Update and uninstall

```sh
brew upgrade konvu-telemetry && konvu-telemetry setup
```

Run setup again after an upgrade so the private launcher and service definition point to the current installation.

```sh
konvu-telemetry uninstall && brew uninstall konvu-telemetry
```

Uninstall removes the LaunchAgent, private launcher, Claude status line, and Codex hook entries owned by Konvu. It preserves `~/.konvu/telemetry` so users can inspect or delete their local history themselves.

## What setup changes

- Creates `~/.konvu/telemetry` with user-only permissions.
- Installs `~/Library/LaunchAgents/com.konvu.telemetry.plist` and refreshes active logs every minute.
- Adds a Codex CLI Stop hook without removing existing hooks. Open `/hooks` in a new Codex CLI session and trust the Konvu hook before it can run.
- Adds the Claude Code status line only when no non-Konvu status line exists.
- Creates timestamped backups of changed Claude and Codex configuration files.

Setup validates configuration before changing anything and rolls back all files and prior service state if installation fails.

## Compatibility

| Platform or client | First-release support |
| --- | --- |
| macOS 15, Apple Silicon | Supported and tested in CI |
| macOS 15, Intel | Supported and tested in CI |
| Earlier macOS | Expected to work with Python 3.9+, not release-tested |
| Python | 3.9 minimum; 3.14 Homebrew runtime tested |
| Linux and Windows | Collector code may run manually; setup and background service are unsupported |
| Claude Code | Transcript monitoring and status line |
| Codex CLI | Transcript monitoring and trusted Stop hook |
| Claude Desktop | No advertised integration |
| Codex Desktop | No advertised hook integration |

The first public release should claim only macOS 15 and the two CLI clients. Broader platforms need native service installers and clean-machine CI before they are advertised.

## Dashboard and alerts

The dashboard is a local HTTP server bound to `127.0.0.1:7824`. It has no account, cookie, token, or URL secret. Any local process or local account able to connect to that port can read the derived dashboard API; raw transcript files are not served. The server validates local `Host` and `Origin` headers.

Browser notifications require the dashboard tab to remain open, the browser to remain running, and notification permission to be granted. If notifications are blocked or the tab is closed, collection continues but browser alerts stop.

Check collector state with:

```sh
konvu-telemetry status
```

## Accounting

Claude streamed records are deduplicated by message ID. Codex cumulative counters are converted to positive deltas. Prices come from the bundled local snapshot identified in [NOTICE](NOTICE).

Costs are estimates, not provider invoices. A session with an unknown billable model is marked partial or unavailable; its totals, forecasts, comparisons, and spend alerts are suppressed. Context is the latest token count recorded by the provider, not a reconstructed prompt size. Forecasts use local completed prompt history and are hidden when the source session is incomplete.

## Local data

- `live-sessions.json` contains the current cross-provider dashboard snapshot.
- `sessions/<provider>-<session-id>.json` contains a lightweight display snapshot.
- `baselines.json` contains local 60-day medians.
- `health.json` contains collector freshness and the most recent error.
- `normalized-events.json` is written only by `konvu-telemetry normalize`.

Derived session files expire after seven days. Provider transcripts are never altered. See [ARCHITECTURE.md](ARCHITECTURE.md) for module ownership, every touched path, and failure behavior.

## Development

```sh
python3 -m ruff check src tests
python3 -m ruff format --check src tests
python3 -m mypy src/konvu_telemetry
python3 -m compileall -q src
node --check src/konvu_telemetry/dashboard/fleet.js
PYTHONPATH=src python3 -m unittest discover -s tests -v
```

See [CONTRIBUTING.md](CONTRIBUTING.md), [SECURITY.md](SECURITY.md), and [CHANGELOG.md](CHANGELOG.md).

## License

MIT. Third-party pricing-data attribution is in [NOTICE](NOTICE).
