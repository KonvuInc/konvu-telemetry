# Contributing

Run the core CI checks before opening a pull request:

```sh
python3 -m ruff check src tests scripts
python3 -m ruff format --check src tests scripts
python3 -m mypy src/konvu_telemetry
python3 -m compileall -q src
node --check src/konvu_telemetry/dashboard/fleet.js
PYTHONPATH=src python3 -m unittest discover -s tests -v
PYTHONPATH=src python3 scripts/benchmark.py --sessions 50 --prompts 20 --max-cold-seconds 5 --max-incremental-seconds 4 --max-peak-mib 64
```

Changes must never upload transcripts, prompts, source code, usage data, account data, paths, environment variables, or raw errors. Anonymous product events must remain allowlisted in `tracking.py` and must not block collection or dashboard requests.
