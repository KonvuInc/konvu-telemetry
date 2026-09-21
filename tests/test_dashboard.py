import unittest
from pathlib import Path


REPOSITORY = Path(__file__).resolve().parents[1]


class DashboardTests(unittest.TestCase):
    def test_refresh_refuses_overlapping_requests(self) -> None:
        source = (REPOSITORY / "src/konvu_telemetry/dashboard/fleet.js").read_text()

        self.assertIn("refreshInFlight: false", source)
        self.assertIn("if (axisDragging || state.refreshInFlight) return;", source)
        self.assertIn("state.refreshInFlight = true;", source)
        self.assertIn("finally {\n    state.refreshInFlight = false;", source)

    def test_security_issue_link_uses_the_repository_owner(self) -> None:
        source = (REPOSITORY / ".github/ISSUE_TEMPLATE/config.yml").read_text()

        self.assertIn(
            "https://github.com/KonvuInc/konvu-telemetry/security/policy", source
        )
        self.assertNotIn("KonvuTeam/konvu-telemetry", source)


if __name__ == "__main__":
    unittest.main()
