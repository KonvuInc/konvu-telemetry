#!/usr/bin/env python3
"""Refresh and validate the bundled LiteLLM pricing snapshot."""

from __future__ import annotations

import argparse
from hashlib import sha256
import json
import math
import os
from pathlib import Path
import re
import tempfile
from urllib.request import Request, urlopen

SOURCE = "https://raw.githubusercontent.com/BerriAI/litellm/main/model_prices_and_context_window.json"
MAX_PAYLOAD_BYTES = 20 * 1024 * 1024


def valid_rate(value: object) -> bool:
    return (
        not isinstance(value, bool)
        and isinstance(value, (int, float))
        and math.isfinite(float(value))
        and value >= 0
    )


def validated_payload(raw: bytes) -> dict[str, object]:
    parsed: object = json.loads(raw)
    if not isinstance(parsed, dict) or len(parsed) < 1_000:
        raise ValueError("LiteLLM pricing must contain at least 1,000 model entries")
    required = ("claude-sonnet-4-6", "gpt-5.4")
    for model in required:
        values = parsed.get(model)
        if not isinstance(values, dict) or not all(
            valid_rate(values.get(field))
            for field in ("input_cost_per_token", "output_cost_per_token")
        ):
            raise ValueError(f"LiteLLM pricing is missing required rates for {model}")
    return {str(key): value for key, value in parsed.items()}


def atomic_write(path: Path, contents: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        dir=path.parent, prefix=f".{path.name}."
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(contents)
            handle.flush()
            os.fsync(handle.fileno())
        temporary.chmod(0o644)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", default=SOURCE)
    arguments = parser.parse_args()
    request = Request(
        arguments.source, headers={"User-Agent": "konvu-telemetry-pricing"}
    )
    with urlopen(request, timeout=30) as response:
        payload = response.read(MAX_PAYLOAD_BYTES + 1)
    if len(payload) > MAX_PAYLOAD_BYTES:
        raise ValueError("LiteLLM pricing exceeds the 20 MiB safety limit")
    parsed = validated_payload(payload)
    rendered = (
        json.dumps(parsed, allow_nan=False, indent=4, sort_keys=True) + "\n"
    ).encode()
    digest = sha256(rendered).hexdigest()
    root = Path(__file__).resolve().parents[1]
    destination = root / "src" / "konvu_telemetry" / "pricing.json"
    notice = root / "NOTICE"
    notice_text = notice.read_text(encoding="utf-8")
    updated_notice, replacements = re.subn(
        r"^SHA-256: [0-9a-f]{64}$",
        f"SHA-256: {digest}",
        notice_text,
        flags=re.MULTILINE,
    )
    if replacements != 1:
        raise ValueError("NOTICE must contain exactly one pricing SHA-256")
    atomic_write(destination, rendered)
    atomic_write(notice, updated_notice.encode())
    print(f"Updated {len(parsed)} model entries; SHA-256 {digest}")


if __name__ == "__main__":
    main()
