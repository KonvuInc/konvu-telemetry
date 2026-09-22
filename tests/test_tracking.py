import json
import tempfile
import unittest
from pathlib import Path

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


if __name__ == "__main__":
    unittest.main()
