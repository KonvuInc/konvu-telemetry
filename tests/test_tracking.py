import json
from contextlib import redirect_stdout
from datetime import datetime
from io import StringIO
from queue import SimpleQueue
import tempfile
from threading import Event, Lock, Thread
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from konvu_telemetry import cli, tracking
from konvu_telemetry.tracking import TrackingStore


class TrackingStoreTests(unittest.TestCase):
    def store(
        self,
        directory: Path,
        sender=lambda _payload: None,
        clock=lambda: 1_000.0,
    ) -> TrackingStore:
        return TrackingStore(
            directory / "tracking-state.json",
            directory / "tracking-queue.json",
            sender=sender,
            clock=clock,
        )

    def enabled_store(
        self,
        directory: Path,
        sender=lambda _payload: None,
        clock=lambda: 1_000.0,
    ) -> TrackingStore:
        store = self.store(directory, sender=sender, clock=clock)
        store.initialize(default_enabled=True)
        return store

    def test_fresh_install_is_enabled_only_after_explicit_initialization(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            store = self.store(Path(temporary))

            self.assertFalse(store.status().enabled)
            store.initialize(default_enabled=True)

            self.assertTrue(store.status().enabled)

    def test_upgrade_without_tracking_state_stays_disabled(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            store = self.store(Path(temporary))

            store.initialize(default_enabled=False)

            self.assertFalse(store.status().enabled)

    def test_existing_telemetry_choice_is_preserved_during_setup(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            store = self.store(Path(temporary))

            store.initialize(default_enabled=False)
            store.initialize(default_enabled=True)

            self.assertFalse(store.status().enabled)

    def test_corrupt_state_fails_closed_without_being_replaced(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            state_path = directory / "tracking-state.json"
            state_path.write_text("{broken")
            store = self.store(directory)

            self.assertFalse(store.status().enabled)
            self.assertEqual(state_path.read_text(), "{broken")

    def test_non_uuid_install_id_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            state_path = directory / "tracking-state.json"
            state_path.write_text(
                json.dumps({"enabled": True, "install_id": "person@example.com"})
            )
            store = self.store(directory)

            self.assertFalse(store.status().enabled)
            store.record("dashboard opened", {"data_available": True})
            self.assertFalse((directory / "tracking-queue.json").exists())

    def test_opt_out_clears_queued_events(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            store = self.enabled_store(directory)

            store.record("dashboard opened", {"data_available": True})
            store.set_enabled(False)

            self.assertFalse(store.status().enabled)
            self.assertEqual(
                json.loads((directory / "tracking-queue.json").read_text()), []
            )

    def test_opt_out_surfaces_a_state_write_failure(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            store = self.enabled_store(Path(temporary))

            with patch(
                "konvu_telemetry.tracking.write_private_json",
                side_effect=OSError("read only"),
            ):
                with self.assertRaisesRegex(OSError, "read only"):
                    store.set_enabled(False)

    def test_opt_out_waits_for_an_in_flight_delivery(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            sending = Event()
            release = Event()
            opted_out = Event()

            def blocked_sender(_payload: bytes) -> None:
                sending.set()
                release.wait(2)

            delivery_store = self.enabled_store(directory, sender=blocked_sender)
            opt_out_store = self.store(directory)
            delivery_store.record("dashboard opened", {"data_available": True})
            delivery = Thread(target=delivery_store.send_queued)
            delivery.start()
            self.assertTrue(sending.wait(1))

            def opt_out() -> None:
                opt_out_store.set_enabled(False)
                opted_out.set()

            disabling = Thread(target=opt_out)
            disabling.start()
            completed_while_sending = opted_out.wait(0.1)
            release.set()
            delivery.join(1)
            disabling.join(1)

            self.assertFalse(completed_while_sending)
            self.assertTrue(opted_out.is_set())
            self.assertFalse(opt_out_store.status().enabled)
            self.assertEqual(
                json.loads((directory / "tracking-queue.json").read_text()), []
            )

    def test_successful_delivery_sends_only_allowlisted_properties(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            sent: list[dict[str, object]] = []
            store = self.enabled_store(
                directory,
                sender=lambda payload: sent.append(json.loads(payload)),
            )

            store.record(
                "dashboard opened",
                {"data_available": True, "path": "/private/project"},
            )
            store.send_queued()

            event = sent[0]["batch"][0]
            properties = event["properties"]
            self.assertTrue(properties["data_available"])
            self.assertFalse(properties["$process_person_profile"])
            self.assertEqual(properties["$ip"], "0")
            self.assertNotIn("path", properties)
            self.assertEqual(properties["$insert_id"], event["uuid"])
            datetime.fromisoformat(event["timestamp"])
            self.assertEqual(
                json.loads((directory / "tracking-queue.json").read_text()), []
            )

    def test_failed_delivery_keeps_queued_events(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)

            def fail_to_send(_payload: bytes) -> None:
                raise OSError("offline")

            store = self.enabled_store(directory, sender=fail_to_send)
            store.record("first snapshot ready", {})
            store.send_queued()

            events = json.loads((directory / "tracking-queue.json").read_text())
            self.assertEqual(
                [event["event"] for event in events], ["first snapshot ready"]
            )

    def test_retry_uses_backoff_and_keeps_the_same_event_id(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            now = [1_000.0]
            sent: list[dict[str, object]] = []

            def fail_to_send(payload: bytes) -> None:
                sent.append(json.loads(payload))
                raise OSError("offline")

            store = self.enabled_store(
                directory,
                sender=fail_to_send,
                clock=lambda: now[0],
            )
            store.record("dashboard opened", {"data_available": True})

            first_delay = store.send_queued()
            second_delay = store.send_queued()
            now[0] += 60
            third_delay = store.send_queued()

            self.assertEqual(len(sent), 2)
            self.assertEqual((first_delay, second_delay, third_delay), (60, 60, 120))
            first = sent[0]["batch"][0]["properties"]["$insert_id"]
            second = sent[1]["batch"][0]["properties"]["$insert_id"]
            self.assertEqual(first, second)

    def test_legacy_queue_is_migrated_before_delivery(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            sent: list[dict[str, object]] = []
            state = {
                "enabled": True,
                "install_id": "00000000-0000-4000-8000-000000000001",
            }
            (directory / "tracking-state.json").write_text(json.dumps(state))
            (directory / "tracking-queue.json").write_text(
                json.dumps(
                    [
                        {
                            "event": "dashboard opened",
                            "properties": {"data_available": True},
                        }
                    ]
                )
            )
            store = self.store(
                directory,
                sender=lambda payload: sent.append(json.loads(payload)),
            )

            store.send_queued()

            event = sent[0]["batch"][0]
            self.assertEqual(event["properties"]["$insert_id"], event["uuid"])
            datetime.fromisoformat(event["timestamp"])

    def test_legacy_first_snapshot_marker_is_preserved(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            state = {
                "enabled": True,
                "install_id": "00000000-0000-4000-8000-000000000001",
                "first_snapshot_ready": True,
            }
            (directory / "tracking-state.json").write_text(json.dumps(state))
            store = self.store(directory)

            store.record_first_snapshot_ready()

            self.assertFalse((directory / "tracking-queue.json").exists())

    def test_active_day_is_recorded_once_per_calendar_day(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            store = self.enabled_store(directory)

            store.record_active_day("2026-09-22")
            store.record_active_day("2026-09-22")

            events = json.loads((directory / "tracking-queue.json").read_text())
            self.assertEqual(
                [event["event"] for event in events], ["telemetry active day"]
            )

    def test_active_day_is_not_marked_when_queue_write_fails(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            store = self.enabled_store(directory)

            with patch(
                "konvu_telemetry.tracking.write_private_json",
                side_effect=OSError("read only"),
            ):
                with self.assertRaisesRegex(OSError, "read only"):
                    store.record_active_day("2026-09-22")

            state = json.loads((directory / "tracking-state.json").read_text())
            self.assertNotIn("active_day", state)

    def test_first_snapshot_event_is_queued_once(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            store = self.enabled_store(directory)

            store.record_first_snapshot_ready()
            store.record_first_snapshot_ready()

            events = json.loads((directory / "tracking-queue.json").read_text())
            self.assertEqual(
                [event["event"] for event in events], ["first snapshot ready"]
            )

    def test_first_snapshot_is_marked_complete_only_after_delivery(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            queue_path = directory / "tracking-queue.json"
            store = self.enabled_store(directory)

            store.record_first_snapshot_ready()
            queue_path.unlink()
            store.record_first_snapshot_ready()
            self.assertTrue(queue_path.is_file())

            store.send_queued()
            store.record_first_snapshot_ready()
            self.assertEqual(json.loads(queue_path.read_text()), [])

    def test_queue_retains_only_the_most_recent_hundred_events(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            store = self.enabled_store(directory)

            for number in range(101):
                store.record("dashboard opened", {"data_available": bool(number % 2)})

            events = json.loads((directory / "tracking-queue.json").read_text())
            self.assertEqual(len(events), 100)

    def test_collector_failure_is_recorded_once_per_day(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            store = self.enabled_store(directory)

            store.record_collector_failure("2026-09-22")
            store.record_collector_failure("2026-09-22")

            events = json.loads((directory / "tracking-queue.json").read_text())
            self.assertEqual([event["event"] for event in events], ["collector failed"])

    def test_collector_failure_is_not_marked_when_queue_write_fails(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            store = self.enabled_store(directory)

            with patch(
                "konvu_telemetry.tracking.write_private_json",
                side_effect=OSError("read only"),
            ):
                with self.assertRaisesRegex(OSError, "read only"):
                    store.record_collector_failure("2026-09-22")

            state = json.loads((directory / "tracking-state.json").read_text())
            self.assertNotIn("collector_failure_day", state)

    def test_invalid_allowlisted_property_value_is_not_queued(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            store = self.enabled_store(directory)

            store.record("dashboard opened", {"data_available": "/private/path"})

            self.assertFalse((directory / "tracking-queue.json").exists())

    def test_setup_event_is_durable_before_background_delivery(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            store = self.store(directory)
            with (
                patch("konvu_telemetry.tracking._store", return_value=store),
                patch("konvu_telemetry.tracking.flush_in_background"),
            ):
                tracking.record_setup_completed(0.5, default_enabled=True)

            events = json.loads((directory / "tracking-queue.json").read_text())
            self.assertEqual(
                [event["event"] for event in events],
                ["telemetry setup completed"],
            )

    def test_empty_dashboard_open_does_not_count_as_an_active_day(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)

            def fail_to_send(_payload: bytes) -> None:
                raise OSError("offline")

            store = self.enabled_store(directory, sender=fail_to_send)

            class ImmediateThread:
                def __init__(self, target, daemon: bool) -> None:
                    self.target = target

                def start(self) -> None:
                    self.target()

            worker_lock = Lock()
            with (
                patch("konvu_telemetry.tracking._store", return_value=store),
                patch("konvu_telemetry.tracking._PENDING_EVENTS", SimpleQueue()),
                patch("konvu_telemetry.tracking._WORKER_LOCK", worker_lock),
                patch("konvu_telemetry.tracking.Thread", ImmediateThread),
            ):
                tracking.record_dashboard_opened(False)

            events = json.loads((directory / "tracking-queue.json").read_text())
            self.assertEqual([event["event"] for event in events], ["dashboard opened"])

    def test_cli_opt_out_disables_tracking(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            with patch.dict("os.environ", {"KONVU_LIVE_USAGE_HOME": str(directory)}):
                with redirect_stdout(StringIO()):
                    cli.main(["telemetry", "on"])
                    cli.main(["telemetry", "off"])

            state = json.loads((directory / "tracking-state.json").read_text())
            self.assertFalse(state["enabled"])

    def test_setup_enables_telemetry_by_default(self) -> None:
        with (
            patch("konvu_telemetry.cli.setup") as configured,
            redirect_stdout(StringIO()),
        ):
            configured.return_value = {}
            cli.main(["setup"])

        configured.assert_called_once_with(60, True)

    def test_cli_opt_out_does_not_report_success_when_write_fails(self) -> None:
        with patch(
            "konvu_telemetry.tracking.TrackingStore.set_enabled",
            side_effect=OSError("read only"),
        ):
            with self.assertRaisesRegex(OSError, "read only"):
                cli.main(["telemetry", "off"])

    def test_worker_lock_is_released_when_thread_start_fails(self) -> None:
        thread = Mock()
        thread.start.side_effect = RuntimeError("no threads")
        worker_lock = Lock()
        with (
            patch("konvu_telemetry.tracking._WORKER_LOCK", worker_lock),
            patch("konvu_telemetry.tracking.Thread", return_value=thread),
        ):
            tracking.flush_in_background()

        self.assertFalse(worker_lock.locked())

    def test_background_delivery_retries_without_another_event(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            now = [1_000.0]
            attempts = [0]
            timers = []

            def sender(_payload: bytes) -> None:
                attempts[0] += 1
                if attempts[0] == 1:
                    raise OSError("offline")

            store = self.enabled_store(
                directory,
                sender=sender,
                clock=lambda: now[0],
            )
            store.record("dashboard opened", {"data_available": True})

            class ImmediateThread:
                def __init__(self, target, daemon: bool) -> None:
                    self.target = target

                def start(self) -> None:
                    self.target()

            class CapturingTimer:
                def __init__(self, interval: float, function) -> None:
                    self.interval = interval
                    self.function = function
                    self.daemon = False

                def start(self) -> None:
                    timers.append(self)

                def is_alive(self) -> bool:
                    return True

            with (
                patch("konvu_telemetry.tracking._store", return_value=store),
                patch("konvu_telemetry.tracking._PENDING_EVENTS", SimpleQueue()),
                patch("konvu_telemetry.tracking._WORKER_LOCK", Lock()),
                patch("konvu_telemetry.tracking._RETRY_TIMER", None, create=True),
                patch("konvu_telemetry.tracking.Thread", ImmediateThread),
                patch("konvu_telemetry.tracking.Timer", CapturingTimer, create=True),
            ):
                tracking.flush_in_background()
                self.assertEqual(timers[0].interval, 60)
                now[0] += 60
                timers[0].function()

            self.assertEqual(attempts[0], 2)
            self.assertEqual(
                json.loads((directory / "tracking-queue.json").read_text()), []
            )

    def test_redirected_delivery_is_not_accepted(self) -> None:
        response = Mock()
        response.status = 200
        response.geturl.return_value = "https://example.com/redirected"
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        with patch("konvu_telemetry.tracking.urlopen", return_value=response):
            with self.assertRaisesRegex(OSError, "redirected"):
                tracking._send_to_posthog(b"{}")

    def test_top_level_help_lists_telemetry_command(self) -> None:
        output = StringIO()
        with redirect_stdout(output), self.assertRaises(SystemExit) as stopped:
            cli.main(["--help"])

        self.assertEqual(stopped.exception.code, 0)
        self.assertIn("telemetry", output.getvalue())



if __name__ == "__main__":
    unittest.main()
