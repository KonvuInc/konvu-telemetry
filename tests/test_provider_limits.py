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
    RPCResponse,
    _claude_token,
    _codex_executable,
    _secure_file,
    decode_claude_usage,
    decode_codex_usage,
    fetch_claude_limits,
    fetch_codex_limits,
    read_claude_access_token,
    stored_provider_quotas,
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
        self.assertEqual(
            result,
            FetchResult(None, 90.0, failure="rate_limited", error_code=429),
        )

    @patch("konvu_telemetry.provider_limits._response")
    @patch("konvu_telemetry.provider_limits.subprocess.Popen")
    @patch("konvu_telemetry.provider_limits._codex_executable")
    def test_codex_fetch_reports_missing_login_without_reading_credentials(
        self, executable: Mock, popen: Mock, response: Mock
    ) -> None:
        executable.return_value = "/trusted/codex"
        process = popen.return_value
        process.poll.return_value = 0
        response.side_effect = [
            RPCResponse(result={}),
            RPCResponse(result={"account": None, "requiresOpenaiAuth": True}),
        ]

        result = fetch_codex_limits()

        self.assertEqual(
            result,
            FetchResult(
                None,
                unavailable=True,
                failure="credentials_unavailable",
            ),
        )
        self.assertEqual(process.stdin.write.call_count, 3)

    @patch("konvu_telemetry.provider_limits._response")
    @patch("konvu_telemetry.provider_limits.subprocess.Popen")
    @patch("konvu_telemetry.provider_limits._codex_executable")
    def test_codex_fetch_preserves_safe_auth_error_code(
        self, executable: Mock, popen: Mock, response: Mock
    ) -> None:
        executable.return_value = "/trusted/codex"
        process = popen.return_value
        process.poll.return_value = 0
        response.side_effect = [RPCResponse(result={}), RPCResponse(error_code=401)]

        result = fetch_codex_limits()

        self.assertEqual(
            result,
            FetchResult(
                None,
                unavailable=True,
                failure="authentication_failed",
                error_code=401,
            ),
        )

    @patch("konvu_telemetry.provider_limits._response")
    @patch("konvu_telemetry.provider_limits.subprocess.Popen")
    @patch("konvu_telemetry.provider_limits._codex_executable")
    def test_codex_fetch_does_not_mislabel_rpc_failure_as_auth_failure(
        self, executable: Mock, popen: Mock, response: Mock
    ) -> None:
        executable.return_value = "/trusted/codex"
        process = popen.return_value
        process.poll.return_value = 0
        response.side_effect = [RPCResponse(result={}), RPCResponse(error_code=-32601)]

        self.assertEqual(
            fetch_codex_limits(),
            FetchResult(None, failure="provider_error", error_code=-32601),
        )

    @patch("konvu_telemetry.provider_limits._response")
    @patch("konvu_telemetry.provider_limits.subprocess.Popen")
    @patch("konvu_telemetry.provider_limits._codex_executable")
    def test_codex_fetch_checks_account_then_reads_limits(
        self, executable: Mock, popen: Mock, response: Mock
    ) -> None:
        executable.return_value = "/trusted/codex"
        process = popen.return_value
        process.poll.return_value = 0
        response.side_effect = [
            RPCResponse(result={}),
            RPCResponse(
                result={
                    "account": {"type": "chatgpt"},
                    "requiresOpenaiAuth": True,
                }
            ),
            RPCResponse(
                result={
                    "rateLimits": {
                        "primary": {
                            "usedPercent": 12,
                            "windowDurationMins": 10_080,
                        }
                    }
                }
            ),
        ]

        result = fetch_codex_limits(1_767_225_600)

        assert result.snapshot is not None
        self.assertEqual(result.snapshot["windows"][0]["used_percent"], 12.0)
        self.assertEqual(process.stdin.write.call_count, 4)

    def test_credential_file_must_be_private_and_token_shape_is_exact(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "credentials.json"
            path.write_text('{"claudeAiOauth":{"accessToken":"token"}}')
            path.chmod(0o600)
            self.assertEqual(_claude_token(_secure_file(path)), "token")
            path.chmod(0o644)
            self.assertIsNone(_secure_file(path))

    def test_poller_retains_a_recent_result_during_transient_failures(self) -> None:
        claude = Mock(
            side_effect=[
                FetchResult({"windows": [{"used_percent": 7.0}]}),
                FetchResult(None, failure="network_error"),
            ]
        )
        codex = Mock(return_value=FetchResult({"windows": []}))
        poller = ProviderLimitPoller(claude, codex)

        self.assertEqual(set(poller.refresh(100)), {"claude", "codex"})
        poller.refresh(219)
        self.assertEqual(claude.call_count, 1)
        snapshots = poller.refresh(220)
        self.assertEqual(
            snapshots["claude"],
            {
                "windows": [{"used_percent": 7.0}],
                "status": "stale",
                "failure": "network_error",
            },
        )
        self.assertEqual(claude.call_count, 2)

    def test_poller_retains_canonical_snapshot_when_first_fetch_fails(self) -> None:
        poller = ProviderLimitPoller(
            Mock(return_value=FetchResult(None, failure="network_error")),
            Mock(return_value=FetchResult(None, unavailable=True)),
            initial_snapshots={
                "claude": {
                    "source": "provider_api",
                    "observed_at": "1970-01-01T00:01:40+00:00",
                    "windows": [{"used_percent": 7.0}],
                }
            },
        )

        claude = poller.refresh(220)["claude"]
        self.assertEqual(claude["status"], "stale")
        self.assertEqual(claude["windows"], [{"used_percent": 7.0}])

    @patch("konvu_telemetry.provider_limits.snapshot_path")
    def test_stored_quotas_accept_only_provider_api_results(
        self, mocked_path: Mock
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "live-sessions.json"
            path.write_text(
                json.dumps(
                    {
                        "account_quotas": {
                            "claude": {
                                "source": "provider_api",
                                "windows": [],
                            },
                            "codex": {"source": "local_fallback", "windows": []},
                        }
                    }
                )
            )
            mocked_path.return_value = path
            self.assertEqual(
                stored_provider_quotas(),
                {"claude": {"source": "provider_api", "windows": []}},
            )

    def test_poller_clears_confirmed_unavailability(self) -> None:
        claude = Mock(
            side_effect=[
                FetchResult({"windows": [{"used_percent": 7.0}]}),
                FetchResult(None, failure="network_error"),
                FetchResult(
                    None,
                    unavailable=True,
                    failure="authentication_failed",
                    error_code=401,
                ),
            ]
        )
        poller = ProviderLimitPoller(
            claude,
            Mock(return_value=FetchResult(None, unavailable=True)),
        )

        poller.refresh(100)
        self.assertEqual(poller.refresh(220)["claude"]["status"], "stale")
        unavailable = poller.refresh(340)["claude"]
        self.assertEqual(
            unavailable,
            {
                "source": "provider_api",
                "status": "unavailable",
                "failure": "authentication_failed",
                "error_code": 401,
                "windows": [],
            },
        )

    def test_poller_expires_a_stale_result_after_ten_minutes(self) -> None:
        claude = Mock(
            side_effect=[
                FetchResult({"windows": [{"used_percent": 7.0}]}),
                FetchResult(None, failure="network_error"),
                FetchResult(None, failure="network_error"),
            ]
        )
        poller = ProviderLimitPoller(
            claude,
            Mock(return_value=FetchResult(None, unavailable=True)),
        )

        poller.refresh(100)
        self.assertEqual(poller.refresh(220)["claude"]["status"], "stale")
        unavailable = poller.refresh(701)["claude"]
        self.assertEqual(unavailable["status"], "unavailable")
        self.assertEqual(unavailable["failure"], "network_error")
        self.assertEqual(unavailable["windows"], [])

    def test_poller_drops_expired_windows_and_stale_plan_flags(self) -> None:
        claude = Mock(
            side_effect=[
                FetchResult(
                    {
                        "ordinary_usage_allowed": False,
                        "limit_states": {"five_hour": "exhausted"},
                        "windows": [
                            {
                                "used_percent": 100.0,
                                "resets_at": "1970-01-01T00:03:20+00:00",
                            }
                        ],
                    }
                ),
                FetchResult(None, failure="network_error"),
            ]
        )
        poller = ProviderLimitPoller(
            claude,
            Mock(return_value=FetchResult(None, unavailable=True)),
        )

        poller.refresh(100)
        stale = poller.refresh(220)["claude"]
        self.assertEqual(stale["status"], "stale")
        self.assertEqual(stale["windows"], [])
        self.assertNotIn("ordinary_usage_allowed", stale)
        self.assertNotIn("limit_states", stale)

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

    def test_provider_results_are_the_only_account_quota_source(self) -> None:
        snapshot: dict[str, object] = {"sessions": []}
        fresh = {"source": "provider_api", "windows": [{"used_percent": 7.0}]}
        enrich_snapshot(
            snapshot,
            [],
            [],
            100.0,
            {"claude": fresh, "codex": None},
        )
        self.assertEqual(snapshot["account_quotas"], {"claude": fresh})

        snapshot = {"sessions": []}
        enrich_snapshot(snapshot, [], [], 100.0, {"claude": None, "codex": None})
        self.assertEqual(snapshot["account_quotas"], {})


if __name__ == "__main__":
    unittest.main()
