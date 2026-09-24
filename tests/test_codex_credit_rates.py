from dataclasses import replace
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from konvu_telemetry.codex_credit_rates import (
    codex_credit_equivalent,
    codex_credit_equivalent_status,
    codex_credit_rate,
)
from konvu_telemetry.models import Usage, UsageEvent


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


class CodexCreditRateTests(unittest.TestCase):
    def test_credit_rates_accept_known_variants_and_reject_other_tiers(self) -> None:
        self.assertIsNotNone(codex_credit_rate("openai/gpt-5.5-codex"))
        self.assertIsNotNone(codex_credit_rate("gpt-5.5-2026-04-23"))
        self.assertIsNone(codex_credit_rate("gpt-5.5-pro"))
        self.assertIsNone(codex_credit_rate("gpt-5.5-unrecognized"))

    def test_credit_calculation_includes_cache_and_reasoning(self) -> None:
        self.assertEqual(codex_credit_equivalent(codex_event()), 6.425)
        self.assertEqual(codex_credit_equivalent(codex_event("fast")), 16.0625)

    def test_gpt_6_fast_mode_uses_the_published_multiplier(self) -> None:
        event = replace(codex_event("fast"), model="gpt-6-sol")
        self.assertEqual(codex_credit_equivalent(event), 5.425)

    def test_unknown_models_make_the_equivalent_partial(self) -> None:
        unknown = replace(codex_event(), model="gpt-unknown", message_id="unknown")
        self.assertEqual(
            codex_credit_equivalent_status([codex_event(), unknown]),
            ("partial", 1),
        )


if __name__ == "__main__":
    unittest.main()
