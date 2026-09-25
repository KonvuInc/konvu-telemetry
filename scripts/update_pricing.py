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

# Update this reviewed commit in a normal pull request; never consume mutable upstream main.
LITELLM_COMMIT = "29e924502434a29e822a562963790e53d92131b0"
SOURCE = (
    "https://raw.githubusercontent.com/BerriAI/litellm/"
    f"{LITELLM_COMMIT}/model_prices_and_context_window.json"
)
MAX_PAYLOAD_BYTES = 20 * 1024 * 1024

# Provider-published models newer than the pinned LiteLLM snapshot.
OFFICIAL_MODEL_OVERRIDES: dict[str, dict[str, object]] = {
    "claude-opus-5-5": {
        "cache_creation_input_token_cost": 0.000005,
        "cache_creation_input_token_cost_above_1hr": 0.000008,
        "cache_read_input_token_cost": 0.0000002,
        "fastMultiplier": 2.0,
        "input_cost_per_token": 0.000004,
        "litellm_provider": "anthropic",
        "max_input_tokens": 1_000_000,
        "max_output_tokens": 128_000,
        "mode": "chat",
        "output_cost_per_token": 0.00002,
        "search_context_cost_per_query": {
            "search_context_size_medium": 0.01,
        },
        "source": "https://platform.claude.com/docs/en/models/opus-5-5/overview",
    },
    "gpt-6-sol": {
        "cache_creation_input_token_cost": 0.0000025,
        "cache_creation_input_token_cost_above_272k_tokens": 0.000005,
        "cache_creation_input_token_cost_above_272k_tokens_flex": 0.0000025,
        "cache_creation_input_token_cost_above_272k_tokens_priority": 0.00001,
        "cache_creation_input_token_cost_flex": 0.00000125,
        "cache_creation_input_token_cost_priority": 0.000005,
        "cache_read_input_token_cost": 0.0000002,
        "cache_read_input_token_cost_above_272k_tokens": 0.0000004,
        "cache_read_input_token_cost_above_272k_tokens_flex": 0.0000002,
        "cache_read_input_token_cost_above_272k_tokens_priority": 0.0000008,
        "cache_read_input_token_cost_flex": 0.0000001,
        "cache_read_input_token_cost_priority": 0.0000004,
        "fastMultiplier": 2.0,
        "input_cost_per_token": 0.000002,
        "input_cost_per_token_above_272k_tokens": 0.000004,
        "input_cost_per_token_above_272k_tokens_flex": 0.000002,
        "input_cost_per_token_above_272k_tokens_priority": 0.000008,
        "input_cost_per_token_flex": 0.000001,
        "input_cost_per_token_priority": 0.000004,
        "litellm_provider": "openai",
        "max_input_tokens": 922_000,
        "max_output_tokens": 128_000,
        "mode": "chat",
        "output_cost_per_token": 0.00001,
        "output_cost_per_token_above_272k_tokens": 0.000015,
        "output_cost_per_token_above_272k_tokens_flex": 0.0000075,
        "output_cost_per_token_above_272k_tokens_priority": 0.00003,
        "output_cost_per_token_flex": 0.000005,
        "output_cost_per_token_priority": 0.00002,
        "search_context_cost_per_query": {
            "search_context_size_medium": 0.01,
        },
        "source": "https://developers.openai.com/api/docs/models/gpt-6-sol",
    },
    "gpt-6-luna": {
        "cache_creation_input_token_cost": 0.000000125,
        "cache_creation_input_token_cost_above_272k_tokens": 0.00000025,
        "cache_creation_input_token_cost_above_272k_tokens_flex": 0.000000125,
        "cache_creation_input_token_cost_above_272k_tokens_priority": 0.0000005,
        "cache_creation_input_token_cost_flex": 0.0000000625,
        "cache_creation_input_token_cost_priority": 0.00000025,
        "cache_read_input_token_cost": 0.00000001,
        "cache_read_input_token_cost_above_272k_tokens": 0.00000002,
        "cache_read_input_token_cost_above_272k_tokens_flex": 0.00000001,
        "cache_read_input_token_cost_above_272k_tokens_priority": 0.00000004,
        "cache_read_input_token_cost_flex": 0.000000005,
        "cache_read_input_token_cost_priority": 0.00000002,
        "fastMultiplier": 2.0,
        "input_cost_per_token": 0.0000001,
        "input_cost_per_token_above_272k_tokens": 0.0000002,
        "input_cost_per_token_above_272k_tokens_flex": 0.0000001,
        "input_cost_per_token_above_272k_tokens_priority": 0.0000004,
        "input_cost_per_token_flex": 0.00000005,
        "input_cost_per_token_priority": 0.0000002,
        "litellm_provider": "openai",
        "max_input_tokens": 922_000,
        "max_output_tokens": 128_000,
        "mode": "chat",
        "output_cost_per_token": 0.0000005,
        "output_cost_per_token_above_272k_tokens": 0.00000075,
        "output_cost_per_token_above_272k_tokens_flex": 0.000000375,
        "output_cost_per_token_above_272k_tokens_priority": 0.0000015,
        "output_cost_per_token_flex": 0.00000025,
        "output_cost_per_token_priority": 0.000001,
        "search_context_cost_per_query": {
            "search_context_size_medium": 0.01,
        },
        "source": "https://developers.openai.com/api/docs/models/gpt-6-luna",
    },
}


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
    normalized = {str(key): value for key, value in parsed.items()}
    normalized.update(OFFICIAL_MODEL_OVERRIDES)
    return normalized


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
