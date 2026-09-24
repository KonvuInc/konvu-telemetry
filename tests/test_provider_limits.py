import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch
from urllib.error import HTTPError

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from konvu_telemetry.provider_limits import (
    FetchResult,
    ProviderLimitPoller,
    _claude_token,
    _codex_executable,
    _secure_file,
    decode_claude_usage,
    decode_codex_usage,
    fetch_claude_limits,
    read_claude_access_token,
)
from konvu_telemetry.fleet_telemetry import enrich_snapshot


class FakeResponse:
    def __init__(self, body: object) -> None:
        self.payload = json.dumps(body).encode()
        self.headers: dict[str, str] = {}

    def read(self, size: int = -1) -> bytes:
        return self.payload[:size] if size >= 0 else self.payload

    def __enter__(self) -> "FakeResponse":
        return self

    def __exit__(self, *args: object) -> None:
        return None


class ProviderLimitsTests(unittest.TestCase):
    def test_claude_decodes_provider_percentage_points(self) -> None:
        snapshot = decode_claude_usage(
            {
                "five_hour": {"utilization": 1.0, "resets_at": "2026-01-01T01:00:00Z"},
                "seven_day": {"utilization": 24.5, "resets_at": "2026-01-07T00:00:00Z"},
            },
            "2026-01-01T00:00:00+00:00",
        )
        assert snapshot is not None
        windows = snapshot["windows"]
        assert isinstance(windows, list)
        self.assertEqual([window["used_percent"] for window in windows], [1.0, 24.5])
        self.assertEqual(
            [window["period"] for window in windows], ["five_hour", "weekly"]
        )

    def test_claude_rejects_out_of_range_values_instead_of_clamping(self) -> None:
        self.assertIsNone(
            decode_claude_usage(
                {"five_hour": {"utilization": 101}},
                "2026-01-01T00:00:00+00:00",
            )
        )

    def test_codex_prefers_multi_bucket_limits_and_preserves_denials(self) -> None:
        snapshot = decode_codex_usage(
            {
                "ordinaryUsageAllowed": False,
                "accountId": "must-not-be-persisted",
                "rateLimits": {
                    "primary": {"usedPercent": 99, "windowDurationMins": 300}
                },
                "rateLimitsByLimitId": {
                    "codex": {
                        "limitName": "Codex",
                        "normalModelSlug": "gpt-6",
                        "primary": {
                            "usedPercent": 12,
                            "windowDurationMins": 300,
                            "resetsAt": 1_767_229_200,
                        },
                        "secondary": {
                            "usedPercent": 34,
                            "windowDurationMins": 10_080,
                            "resetsAt": 1_767_830_400,
                        },
                        "individualLimit": {
                            "remainingPercent": 40,
                            "used": "60",
                            "limit": "100",
                            "resetsAt": 1_767_830_400,
                        },
                        "spendControlReached": True,
                        "rateLimitReachedType": "workspace_member_usage_limit_reached",
                    }
                },
            },
            "2026-01-01T00:00:00+00:00",
        )
        assert snapshot is not None
        windows = snapshot["windows"]
        assert isinstance(windows, list)
        self.assertEqual([row["used_percent"] for row in windows], [12.0, 34.0, 60.0])
        self.assertTrue(all(row["limit_id"] == "codex" for row in windows))
        self.assertFalse(snapshot["ordinary_usage_allowed"])
        self.assertTrue(snapshot["spend_control_reached"])
        self.assertNotIn("account_id", snapshot)
        self.assertNotIn("accountId", snapshot)

    def test_codex_keeps_denial_even_when_no_percentage_window_exists(self) -> None:
        snapshot = decode_codex_usage(
            {
                "ordinaryUsageAllowed": False,
                "rateLimits": {
                    "limitId": "premium",
                    "rateLimitReachedType": "rate_limit_reached",
                },
            },
            "2026-01-01T00:00:00+00:00",
        )
        assert snapshot is not None
        self.assertEqual(snapshot["windows"], [])
        self.assertFalse(snapshot["ordinary_usage_allowed"])
        self.assertEqual(snapshot["limit_states"], {"premium": "rate_limit_reached"})

    def test_claude_fetch_uses_token_once_without_returning_it(self) -> None:
        requests: list[object] = []

        def open_request(request: object, timeout: float) -> FakeResponse:
            requests.append(request)
            self.assertEqual(timeout, 10.0)
            return FakeResponse({"five_hour": {"utilization": 7}})

        result = fetch_claude_limits(
            1_767_225_600,
            opener=open_request,
            token_reader=lambda: "secret-token",
        )
        self.assertIsNotNone(result.snapshot)
        self.assertNotIn("secret-token", json.dumps(result.snapshot))
        self.assertEqual(len(requests), 1)

    def test_claude_fetch_honors_retry_after_without_reading_error_body(self) -> None:
        error = HTTPError(
            "https://example.test", 429, "rate limited", {"Retry-After": "90"}, None
        )
        result = fetch_claude_limits(
            opener=lambda request, timeout: (_ for _ in ()).throw(error),
            token_reader=lambda: "secret-token",
        )
        self.assertEqual(result, FetchResult(None, 90.0))

    def test_credential_file_must_be_private_and_token_shape_is_exact(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "credentials.json"
            path.write_text('{"claudeAiOauth":{"accessToken":"token"}}')
            path.chmod(0o600)
            self.assertEqual(_claude_token(_secure_file(path)), "token")
            path.chmod(0o644)
            self.assertIsNone(_secure_file(path))

    def test_poller_runs_every_two_minutes_and_clears_failed_results(self) -> None:
        claude = Mock(side_effect=[FetchResult({"windows": []}), FetchResult(None)])
        codex = Mock(return_value=FetchResult({"windows": []}))
        poller = ProviderLimitPoller(claude, codex)

        self.assertEqual(set(poller.refresh(100)), {"claude", "codex"})
        poller.refresh(219)
        self.assertEqual(claude.call_count, 1)
        snapshots = poller.refresh(220)
        self.assertIsNone(snapshots["claude"])
        self.assertEqual(claude.call_count, 2)

    def test_poller_respects_provider_retry_after(self) -> None:
        claude = Mock(return_value=FetchResult(None, 300))
        codex = Mock(return_value=FetchResult(None))
        poller = ProviderLimitPoller(claude, codex)

        poller.refresh(100)
        poller.refresh(220)
        self.assertEqual(claude.call_count, 1)
        poller.refresh(400)
        self.assertEqual(claude.call_count, 2)

    def test_poller_backs_off_repeated_transient_failures(self) -> None:
        claude = Mock(return_value=FetchResult(None))
        codex = Mock(return_value=FetchResult(None, unavailable=True))
        poller = ProviderLimitPoller(claude, codex)

        poller.refresh(100)
        poller.refresh(220)
        poller.refresh(340)
        self.assertEqual(claude.call_count, 2)
        poller.refresh(460)
        self.assertEqual(claude.call_count, 3)

    def test_secure_file_does_not_follow_symlinks(self) -> None:
        if not hasattr(os, "O_NOFOLLOW"):
            self.skipTest("platform has no O_NOFOLLOW")
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "target"
            target.write_text("secret")
            target.chmod(0o600)
            link = Path(directory) / "link"
            link.symlink_to(target)
            self.assertIsNone(_secure_file(link))

    def test_keychain_is_a_fallback_when_the_private_file_is_missing(self) -> None:
        with (
            patch(
                "konvu_telemetry.provider_limits.Path.home",
                return_value=Path("/missing"),
            ),
            patch("konvu_telemetry.provider_limits._keychain_credential") as keychain,
        ):
            keychain.return_value = '{"claudeAiOauth":{"accessToken":"token"}}'
            self.assertEqual(read_claude_access_token(), "token")

    def test_codex_executable_ignores_path_and_uses_a_validated_install(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            injected = home / "injected" / "codex"
            installed = home / ".local" / "bin" / "codex"
            injected.parent.mkdir()
            installed.parent.mkdir(parents=True)
            injected.write_text("untrusted")
            installed.write_text("trusted")
            injected.chmod(0o700)
            installed.chmod(0o700)
            with (
                patch.dict(os.environ, {"PATH": str(injected.parent)}),
                patch("konvu_telemetry.provider_limits.Path.home", return_value=home),
                patch.object(
                    Path,
                    "resolve",
                    autospec=True,
                    side_effect=lambda path, strict=False: (
                        path
                        if path == installed
                        else (_ for _ in ()).throw(FileNotFoundError())
                    ),
                ),
            ):
                self.assertEqual(_codex_executable(), str(installed))

    def test_codex_executable_rejects_a_writable_install(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            installed = home / ".local" / "bin" / "codex"
            installed.parent.mkdir(parents=True)
            installed.write_text("unsafe")
            installed.chmod(0o722)
            with (
                patch("konvu_telemetry.provider_limits.Path.home", return_value=home),
                patch.object(
                    Path,
                    "resolve",
                    autospec=True,
                    side_effect=lambda path, strict=False: (
                        path
                        if path == installed
                        else (_ for _ in ()).throw(FileNotFoundError())
                    ),
                ),
            ):
                self.assertIsNone(_codex_executable())

    def test_provider_results_replace_local_estimates_and_fail_closed(self) -> None:
        snapshot: dict[str, object] = {"sessions": []}
        fresh = {"source": "provider_api", "windows": [{"used_percent": 7.0}]}
        with patch(
            "konvu_telemetry.fleet_telemetry._claude_quota_snapshot",
            return_value={"source": "claude_statusline", "windows": []},
        ):
            enrich_snapshot(
                snapshot,
                [],
                [],
                100.0,
                {"claude": fresh, "codex": None},
            )
        self.assertEqual(snapshot["account_quotas"], {"claude": fresh})


if __name__ == "__main__":
    unittest.main()
