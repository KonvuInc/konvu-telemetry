import base64
from dataclasses import replace
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from konvu_telemetry.codex_billing import (
    codex_account,
    codex_credit_rate,
    codex_credits,
    fetch_codex_usage,
    normalize_codex_usage,
)
from konvu_telemetry.models import Usage, UsageEvent


def encoded_token(claims: dict[str, object]) -> str:
    payload = base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip("=")
    return f"header.{payload}.signature"


def codex_event(speed: str = "standard") -> UsageEvent:
    return UsageEvent(
        provider="codex",
        session_id="session",
        message_id="message",
        timestamp=0,
        model="openai/gpt-5.5-codex",
        usage=Usage(1_000, 3_000, 2_000, 0, 4_000, 0, speed, 5_000),
        tool_calls=0,
        is_subagent=False,
        agent_id=None,
        effort="high",
    )


class Response:
    def __init__(self, body: object) -> None:
        self.body = json.dumps(body).encode()

    def __enter__(self) -> "Response":
        return self

    def __exit__(self, *args: object) -> None:
        return None

    def read(self, size: int) -> bytes:
        return self.body[:size]


class CodexBillingTests(unittest.TestCase):
    def test_credit_rates_accept_known_variants_and_reject_other_tiers(self) -> None:
        self.assertIsNotNone(codex_credit_rate("openai/gpt-5.5-codex"))
        self.assertIsNotNone(codex_credit_rate("gpt-5.5-2026-04-23"))
        self.assertIsNone(codex_credit_rate("gpt-5.5-pro"))
        self.assertIsNone(codex_credit_rate("gpt-5.5-unrecognized"))

    def test_credit_calculation_includes_cache_and_reasoning(self) -> None:
        self.assertEqual(codex_credits(codex_event()), 6.425)
        self.assertEqual(codex_credits(codex_event("fast")), 16.0625)

    def test_gpt_6_fast_mode_uses_the_published_multiplier(self) -> None:
        event = replace(codex_event("fast"), model="gpt-6-sol")
        self.assertEqual(codex_credits(event), 5.425)

    def test_account_is_detected_from_local_auth_without_exposing_tokens(self) -> None:
        claims = {"https://api.openai.com/auth": {"chatgpt_plan_type": "plus"}}
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "auth.json"
            path.write_text(
                json.dumps(
                    {
                        "auth_mode": "chatgpt",
                        "tokens": {
                            "id_token": encoded_token(claims),
                            "access_token": "secret",
                        },
                    }
                )
            )
            self.assertEqual(
                codex_account(path),
                {
                    "billing_mode": "chatgpt_subscription",
                    "plan_type": "plus",
                    "plan_label": "Plus",
                },
            )

    def test_api_key_auth_is_not_misreported_as_a_subscription(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "auth.json"
            path.write_text(
                json.dumps({"auth_mode": "api_key", "OPENAI_API_KEY": "secret"})
            )
            self.assertEqual(codex_account(path)["billing_mode"], "api_key")

    def test_usage_response_normalizes_windows_and_credit_balance(self) -> None:
        account = {
            "billing_mode": "chatgpt_subscription",
            "plan_type": "plus",
            "plan_label": "Plus",
        }
        result = normalize_codex_usage(
            {
                "rate_limit": {
                    "primary_window": {
                        "used_percent": 25,
                        "limit_window_seconds": 604800,
                        "reset_at": 1_800_000_000,
                    }
                },
                "credits": {"has_credits": True, "unlimited": False, "balance": 12.5},
            },
            "2026-01-01T00:00:00+00:00",
            account,
        )
        self.assertIsNotNone(result)
        assert result is not None
        self.assertEqual(result["windows"][0]["remaining_percent"], 75)
        self.assertEqual(result["credits"]["balance"], 12.5)

    def test_empty_usage_response_falls_back_to_transcript_data(self) -> None:
        account = {
            "billing_mode": "chatgpt_subscription",
            "plan_type": "plus",
            "plan_label": "Plus",
        }
        self.assertIsNone(
            normalize_codex_usage({}, "2026-01-01T00:00:00+00:00", account)
        )

    def test_live_fetch_uses_local_oauth_and_returns_normalized_data(self) -> None:
        claims = {"https://api.openai.com/auth": {"chatgpt_plan_type": "plus"}}
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "auth.json"
            path.write_text(
                json.dumps(
                    {
                        "auth_mode": "chatgpt",
                        "tokens": {
                            "id_token": encoded_token(claims),
                            "access_token": "secret",
                            "account_id": "account",
                        },
                    }
                )
            )
            with patch(
                "konvu_telemetry.codex_billing.urlopen",
                return_value=Response({"credits": {"balance": 9}}),
            ) as request:
                result = fetch_codex_usage(1_800_000_000, path)
        self.assertIsNotNone(result)
        assert result is not None
        self.assertEqual(result["credits"]["balance"], 9)
        self.assertEqual(
            request.call_args.args[0].headers["Authorization"], "Bearer secret"
        )


if __name__ == "__main__":
    unittest.main()
