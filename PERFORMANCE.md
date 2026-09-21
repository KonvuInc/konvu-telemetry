# Performance

The resident service incrementally parses appended transcript bytes, bounds cached files, and refreshes once per minute by default. Pricing is parsed once and reused until its file changes.

Run the dependency-free synthetic benchmark:

```sh
PYTHONPATH=src python3 scripts/benchmark.py --sessions 50 --prompts 20
```

The fixture contains no real prompts, paths, or usage data. It reports cold collection time, unchanged incremental refresh time, peak Python allocations, and output session counts. Record the command, hardware, Python version, commit, and JSON output when changing a collection hot path; compare cold and incremental results before merging.

CI runs the same fixture with deliberately generous ceilings of 5 seconds cold, 2 seconds incremental, and 64 MiB of traced allocations. These limits catch severe regressions without treating runner noise as a benchmark result.

On a 471,500-byte fixture with 50 sessions and 20 prompts each, the audit baseline on macOS and Python 3.9 was 0.41 seconds cold, 0.13 seconds unchanged incremental, and 8.1 MB peak traced Python allocations. Hardware and operating-system load affect these values; use them to detect large regressions, not as universal limits.

For an empty corpus on macOS with system Python 3.9, the v0.1.0 service used about 32 MiB resident memory and 0% idle CPU after startup. This is a reference observation, not a cross-platform limit.
