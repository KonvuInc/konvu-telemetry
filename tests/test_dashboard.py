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

    def test_security_reporting_uses_the_private_inbox(self) -> None:
        issue_template = (REPOSITORY / ".github/ISSUE_TEMPLATE/config.yml").read_text()
        security_policy = (REPOSITORY / "SECURITY.md").read_text()

        self.assertIn("mailto:security@konvu.com", issue_template)
        self.assertIn("security@konvu.com", security_policy)


if __name__ == "__main__":
    unittest.main()
