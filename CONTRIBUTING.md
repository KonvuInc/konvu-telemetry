# Contributing

Run the same checks as CI before opening a pull request:

```sh
python3 -m ruff check src tests
python3 -m ruff format --check src tests
python3 -m compileall -q src
node --check src/konvu_telemetry/dashboard/fleet.js
PYTHONPATH=src python3 -m unittest discover -s tests -v
```

Changes must preserve the local-only contract: no transcript, prompt, source-code, or usage upload.
