import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from konvu_telemetry import cli
from konvu_telemetry.tracking import TrackingStore


class TrackingStoreTests(unittest.TestCase):
    def test_opt_out_clears_queued_events(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            store = TrackingStore(
                directory / "tracking-state.json",
                directory / "tracking-queue.json",
                sender=lambda _payload: None,
            )

            store.record("dashboard opened", {"data_available": True})
            store.set_enabled(False)

            self.assertFalse(store.status().enabled)
            self.assertEqual(
                json.loads((directory / "tracking-queue.json").read_text()), []
            )

    def test_successful_delivery_sends_only_allowlisted_properties(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            sent: list[dict[str, object]] = []
            store = TrackingStore(
                directory / "tracking-state.json",
                directory / "tracking-queue.json",
                sender=lambda payload: sent.append(json.loads(payload)),
            )

            store.record(
                "dashboard opened",
                {"data_available": True, "path": "/private/project"},
            )
            store.send_queued()

            properties = sent[0]["batch"][0]["properties"]
            self.assertTrue(properties["data_available"])
            self.assertFalse(properties["$process_person_profile"])
            self.assertEqual(properties["$ip"], "0")
            self.assertNotIn("path", properties)
            self.assertEqual(
                json.loads((directory / "tracking-queue.json").read_text()), []
            )

    def test_active_day_is_recorded_once_per_calendar_day(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            store = TrackingStore(
                directory / "tracking-state.json",
                directory / "tracking-queue.json",
                sender=lambda _payload: None,
            )

            store.record_active_day("2026-09-22")
            store.record_active_day("2026-09-22")

            events = json.loads((directory / "tracking-queue.json").read_text())
            self.assertEqual([event["event"] for event in events], ["telemetry active day"])

    def test_cli_opt_out_disables_tracking(self) -> None:
        with (
            patch("konvu_telemetry.cli.set_tracking_enabled") as set_enabled,
            patch("konvu_telemetry.cli.tracking_status") as status,
        ):
            status.return_value.enabled = False
            cli.main(["telemetry", "off"])

        set_enabled.assert_called_once_with(False)


if __name__ == "__main__":
    unittest.main()
