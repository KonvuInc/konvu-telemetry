"""Tests for quota timer display helpers."""

from __future__ import annotations

import ctypes
import os
import re
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from konvu_telemetry.display import (
    ANSI_ESCAPE,
    compact_advice,
    compact_duration,
    context_meter,
    compact_status_meter,
    hook_forecast_row,
    hook_quota_meters,
    relevance_context_meter,
    statusline_rows,
    usage_box_lines,
)
from konvu_telemetry.codex_runtime import (
    codex_tui_renders_ansi,
    process_info,
    run_text,
)


class DisplayTests(unittest.TestCase):
    """Keep CLI and hook quota timers readable and tied to their limit."""

    def test_compact_duration_steps_down_before_rounding_to_zero(self) -> None:
        self.assertEqual(compact_duration(3.2 * 86_400), "3.2d")
        self.assertEqual(compact_duration(0.1 * 86_400), "2.4h")
        self.assertEqual(compact_duration(2.5 * 3_600), "2.5h")
        self.assertEqual(compact_duration(0.1 * 3_600), "6m")
        self.assertEqual(compact_duration(2.2 * 60), "2.2m")
        self.assertEqual(compact_duration(0.1 * 60), "6s")

    def test_cli_meter_centers_the_reset_label_inside_its_fill(self) -> None:
        with patch.dict(os.environ, {"NO_COLOR": "", "TERM": "xterm-256color"}):
            rendered = compact_status_meter("5h", 8.0, 14, "4.2h")

        self.assertIn("reset: 4.2h", ANSI_ESCAPE.sub("", rendered))
        self.assertIn("\x1b[48;5;", rendered)

    def test_cli_meter_keeps_the_bar_when_zero_usage_has_no_reset(self) -> None:
        with patch.dict(os.environ, {"NO_COLOR": "", "TERM": "xterm-256color"}):
            colored = compact_status_meter("5h", 0.0, 14)
        with patch.dict(os.environ, {"NO_COLOR": "1", "TERM": "dumb"}):
            plain = compact_status_meter("5h", 0.0, 14)
        self.assertIn("\x1b[48;5;", colored)
        self.assertEqual(ANSI_ESCAPE.sub("", colored), "5h " + " " * 14 + " 0%")
        self.assertEqual(plain, "5h " + "─" * 14 + " 0%")
        self.assertNotIn("□", colored + plain)

    def test_hook_meters_put_each_reset_next_to_its_limit_name(self) -> None:
        quotas = {
            "claude": {
                "windows": [
                    {"period": "five_hour", "resets_at": "2030-01-01T01:00:00+00:00"},
                    {"period": "weekly", "resets_at": "2030-01-02T00:00:00+00:00"},
                ]
            }
        }
        with (
            patch("konvu_telemetry.display.time.time", return_value=1_893_456_000),
            patch(
                "konvu_telemetry.display.stored_provider_quotas", return_value=quotas
            ),
        ):
            rendered = hook_quota_meters(
                "8.0% 5-hour limit; 35.0% weekly limit", "claude"
            )

        self.assertEqual(
            rendered,
            "5h · reset: 1h [█░░░░░░] 8.0%  Week · reset: 1d [██░░░░░] 35.0%",
        )

    def test_paid_claude_hud_shows_spend_instead_of_limit_meters(self) -> None:
        quotas = {
            "claude": {
                "windows": [
                    {
                        "period": "five_hour",
                        "used_percent": 100.0,
                        "resets_at": "2030-01-01T01:00:00+00:00",
                    },
                    {
                        "period": "weekly",
                        "used_percent": 35.0,
                        "resets_at": "2030-01-02T00:00:00+00:00",
                    },
                ]
            }
        }
        session = {
            "usage_mode": "exhausted",
            "out_of_plan_spend_usd": 1.3,
            "projected_next_10_tasks_usd": 0.8,
        }
        with (
            patch.dict(os.environ, {"NO_COLOR": "1"}),
            patch("konvu_telemetry.display.time.time", return_value=1_893_456_000),
            patch(
                "konvu_telemetry.display.stored_provider_quotas", return_value=quotas
            ),
            patch("konvu_telemetry.display.dashboard_line", return_value="dashboard"),
        ):
            rows = statusline_rows(session, 52.4)

        self.assertIn("● Paying", rows[0])
        self.assertIn("Limits reset in 1h", rows[0])
        self.assertNotIn("5h ", "\n".join(rows))
        self.assertNotIn("Week ", "\n".join(rows))
        self.assertIn("Estimated paid spend $1.3", "\n".join(rows))
        self.assertIn("$2.1 forecasted in next 10 prompts", "\n".join(rows))
        self.assertIn("Context", "\n".join(rows))
        self.assertIn("52%", "\n".join(rows))
        self.assertNotIn("□", "\n".join(rows))

    def test_paid_claude_hud_shows_spend_before_forecast_is_ready(self) -> None:
        session = {
            "usage_mode": "exhausted",
            "out_of_plan_spend_usd": 0.4,
            "projected_next_10_tasks_usd": None,
        }
        with (
            patch.dict(os.environ, {"NO_COLOR": "1"}),
            patch("konvu_telemetry.display.stored_provider_quotas", return_value={}),
            patch("konvu_telemetry.display.dashboard_line", return_value="dashboard"),
        ):
            rows = statusline_rows(session, 10.0)

        self.assertIn("Estimated paid spend $0.4", "\n".join(rows))
        self.assertNotIn("Subscription forecast unavailable", "\n".join(rows))

    def test_paid_claude_hud_colors_only_paying_red(self) -> None:
        quotas = {
            "claude": {
                "windows": [
                    {
                        "period": "weekly",
                        "used_percent": 100.0,
                        "resets_at": "2030-01-02T00:00:00+00:00",
                    }
                ]
            }
        }
        with (
            patch.dict(os.environ, {"NO_COLOR": "", "TERM": "xterm-256color"}),
            patch("konvu_telemetry.display.time.time", return_value=1_893_456_000),
            patch(
                "konvu_telemetry.display.stored_provider_quotas", return_value=quotas
            ),
            patch("konvu_telemetry.display.dashboard_line", return_value="dashboard"),
        ):
            rows = statusline_rows({"usage_mode": "exhausted"}, 0.0)

        self.assertIn("\x1b[1;38;5;203m● Paying\x1b[0m", rows[0])
        self.assertIn("\x1b[38;5;245mLimits reset in 1d\x1b[0m", rows[0])

    def test_paid_hooks_keep_spend_when_forecast_is_unavailable(self) -> None:
        for provider in ("claude", "codex"):
            with self.subTest(provider=provider):
                row = {
                    "provider": provider,
                    "usage_mode": "exhausted",
                    "out_of_plan_spend_usd": 0.4,
                    "projected_next_10_tasks_usd": None,
                }
                self.assertEqual(hook_forecast_row(row), "💸 Current spend $0.4")
                row["cost_status"] = "unavailable"
                self.assertIsNone(hook_forecast_row(row))

    def test_included_hook_omits_forecast_without_attribution(self) -> None:
        row = {"provider": "codex", "usage_mode": "included"}
        self.assertIsNone(hook_forecast_row(row))
        with patch("konvu_telemetry.display.dashboard_line", return_value="dashboard"):
            lines = usage_box_lines(row, "35.0% weekly limit", "codex")
        self.assertNotIn("forecast", "\n".join(lines).lower())

    def test_every_claude_hud_state_uses_the_thin_context_bar(self) -> None:
        for usage_mode in ("included", "api_billed", "exhausted", None):
            with self.subTest(usage_mode=usage_mode):
                session = {"provider": "claude", "usage_mode": usage_mode}
                with (
                    patch(
                        "konvu_telemetry.display.stored_provider_quotas",
                        return_value={},
                    ),
                    patch(
                        "konvu_telemetry.display.dashboard_line",
                        return_value="dashboard",
                    ),
                ):
                    rows = statusline_rows(session, 7.0)

                plain = [ANSI_ESCAPE.sub("", row) for row in rows]
                self.assertIn("Context ━─────────────── 7%", plain)
                self.assertNotIn("■", "".join(plain))

    def test_paid_codex_hook_keeps_credits_and_moves_reset_to_paying_line(self) -> None:
        quotas = {
            "codex": {
                "windows": [
                    {
                        "period": "weekly",
                        "used_percent": 100.0,
                        "resets_at": "2030-01-02T00:00:00+00:00",
                    },
                    {
                        "period": "monthly",
                        "used_percent": 23.0,
                        "resets_at": "2030-02-01T00:00:00+00:00",
                    },
                ]
            }
        }
        session = {
            "usage_mode": "exhausted",
            "context_tokens": 681,
            "context_window_tokens": 1000,
        }
        with (
            patch("konvu_telemetry.display.time.time", return_value=1_893_456_000),
            patch(
                "konvu_telemetry.display.stored_provider_quotas", return_value=quotas
            ),
            patch("konvu_telemetry.display.dashboard_line", return_value="dashboard"),
        ):
            rows = usage_box_lines(
                session,
                "100.0% weekly limit · 23.0% monthly limit",
                "codex",
            )

        self.assertEqual(rows[1], "│ 🔴 Paying · Limits reset in 1d")
        self.assertIn("Credits [██░░░░░] 23.0%", rows[2])
        self.assertEqual(rows[3], "│ 🧠 Context ⬜⬜⬜⬜⬜⬜⬜▫️▫️▫️ 68.1%")
        self.assertNotIn("Week", "\n".join(rows))
        self.assertNotIn("reset:", "\n".join(rows))


class ContextRelevanceDisplayTests(unittest.TestCase):
    def test_usage_box_colors_context_and_links_compact_on_the_same_line(self) -> None:
        session = {
            "id": "abc",
            "provider": "codex",
            "usage_mode": "included",
            "context_tokens": 710,
            "context_window_tokens": 1000,
            "context_map": {
                "analysis": {
                    "coverage_percent": 100,
                    "relevant_percent": 15,
                    "stale_percent": 85,
                    "droppable_percent": 84,
                    "compact_command": "/compact Preserve: auth work.",
                }
            },
        }
        with patch("konvu_telemetry.display.dashboard_line", return_value="dashboard"):
            rows = usage_box_lines(session, "", "codex")

        self.assertIn(
            "│ 🧠 Context 🟩🟥🟥🟥🟥🟥🟥▫️▫️▫️ 71.0%  ·  ✂️ /compact "
            "http://127.0.0.1:7824/?session=codex%3Aabc&tab=context",
            rows,
        )

    """The Claude CLI context meter and /compact hint follow the AI ratings."""

    analysis = {
        "coverage_percent": 90,
        "relevant_percent": 50.0,
        "drifting_percent": 0.0,
        "stale_percent": 50.0,
        "droppable_percent": 50.0,
        "compact_command": "/compact Keep the PR 91 review.",
    }

    def test_used_part_of_the_bar_splits_by_relevance(self) -> None:
        with patch.dict(os.environ, {"TERM": "xterm-256color", "NO_COLOR": ""}):
            meter = relevance_context_meter("Context", 50, 16, self.analysis)

        self.assertIn("\x1b[1;38;5;78m━━━━\x1b[0m", meter)
        self.assertIn("\x1b[1;38;5;203m━━━━\x1b[0m", meter)
        self.assertEqual(ANSI_ESCAPE.sub("", meter), "Context ━━━━━━━━──────── 50%")

    def test_compact_link_sits_on_the_context_line_and_opens_the_panel(self) -> None:
        session = {
            "id": "abc",
            "provider": "claude",
            "context_map": {"analysis": self.analysis},
        }
        with patch.dict(os.environ, {"TERM": "xterm-256color", "NO_COLOR": ""}):
            line = context_meter(session, 50)

        self.assertIn("?session=claude%3Aabc&tab=context", line)
        plain = re.sub(r"\x1b\]8;;[^\x1b]*\x1b\\", "", ANSI_ESCAPE.sub("", line))
        self.assertTrue(plain.endswith("·  /compact suggested"))
        self.assertNotIn("drifting or stale", line)

    def test_no_compact_hint_when_little_of_the_context_was_reviewed(self) -> None:
        thin = {**self.analysis, "coverage_percent": 40}

        self.assertIsNone(
            compact_advice({"id": "abc", "context_map": {"analysis": thin}}, 90)
        )

    def test_compact_hint_needs_twenty_window_points_of_waste(self) -> None:
        session = {"id": "abc", "context_map": {"analysis": self.analysis}}

        # Half of 39% used is 19.5 points of the window; half of 40% is 20.
        self.assertIsNone(compact_advice(session, 39))
        self.assertIsNotNone(compact_advice(session, 40))

    def test_context_is_always_the_second_line(self) -> None:
        session = {
            "id": "abc",
            "provider": "claude",
            "usage_mode": "included",
            "context_map": {"analysis": self.analysis},
        }
        with (
            patch.dict(
                os.environ, {"TERM": "xterm-256color", "NO_COLOR": "", "COLUMNS": "200"}
            ),
            patch("konvu_telemetry.display.stored_provider_quotas", return_value={}),
            patch("konvu_telemetry.display.quota_reset_times", return_value={}),
            patch(
                "konvu_telemetry.display.cli_dashboard_line", return_value="dashboard"
            ),
        ):
            rows = statusline_rows(session, 50)

        self.assertTrue(ANSI_ESCAPE.sub("", rows[1]).startswith("Context "))


class CliHudTests(unittest.TestCase):
    """The shared CLI HUD as the Claude status line and the Codex Stop hook draw it."""

    session = {
        "id": "abc",
        "usage_mode": "included",
        "context_tokens": 100,
        "context_window_tokens": 1000,
    }
    compactable = {
        "coverage_percent": 90.0,
        "relevant_percent": 20.0,
        "stale_percent": 70.0,
        "droppable_percent": 60.0,
        "compact_command": "/compact keep the parser work",
    }

    def rows(
        self,
        provider: str,
        session: dict[str, object] | None = None,
        quotas: dict[str, object] | None = None,
        context_percent: float | None = None,
    ) -> list[str]:
        with (
            patch.dict(os.environ, {"NO_COLOR": "", "TERM": "xterm-256color"}),
            patch("konvu_telemetry.display.time.time", return_value=1_893_456_000),
            patch(
                "konvu_telemetry.display.stored_provider_quotas",
                return_value=quotas or {},
            ),
            patch(
                "konvu_telemetry.display.dashboard_line",
                return_value="🔗 dashboard: http://127.0.0.1:7824/",
            ),
        ):
            return statusline_rows(session or self.session, context_percent, provider)

    def plain(self, rows: list[str]) -> str:
        return ANSI_ESCAPE.sub("", "\n".join(rows))

    def test_claude_hides_the_session_url_behind_the_link_text(self) -> None:
        last = self.rows("claude")[-1]
        self.assertIn("\x1b]8;;http://127.0.0.1:7824/?session=claude%3Aabc\x1b\\", last)
        self.assertIn("🔗 View session in dashboard", last)

    def test_a_session_without_an_id_links_the_whole_dashboard(self) -> None:
        anonymous = {"usage_mode": "included"}
        self.assertIn("🔗 Open live dashboard", self.rows("claude", anonymous)[-1])
        self.assertEqual(
            self.plain(self.rows("codex", anonymous)[-2:]),
            "🔗 Open live dashboard\nhttp://127.0.0.1:7824/",
        )

    def test_codex_writes_its_session_url_on_its_own_row(self) -> None:
        rows = self.rows("codex")
        self.assertNotIn("\x1b]8", "".join(rows))
        self.assertEqual(
            self.plain(rows[-2:]),
            "🔗 View session in dashboard\nhttp://127.0.0.1:7824/?session=codex%3Aabc",
        )
        self.assertIn("\x1b[4;38;5;117mhttp://127.0.0.1:7824/", rows[-1])

    def test_codex_compact_advice_points_its_url_at_the_context_tab(self) -> None:
        session = {**self.session, "context_map": {"analysis": self.compactable}}
        session["context_tokens"] = 800
        rows = self.rows("codex", session)
        self.assertNotIn("\x1b]8", "".join(rows))
        context = next(row for row in rows if "Context" in ANSI_ESCAPE.sub("", row))
        self.assertTrue(ANSI_ESCAPE.sub("", context).endswith("/compact suggested"))
        self.assertEqual(
            self.plain(rows[-2:]),
            "🔗 View /compact steps in dashboard\n"
            "http://127.0.0.1:7824/?session=codex%3Aabc&tab=context",
        )

    def test_claude_keeps_its_empty_limit_meters_but_codex_drops_them(self) -> None:
        self.assertIn("5h", self.plain(self.rows("claude")[:1]))
        self.assertNotIn("5h", self.plain(self.rows("codex")))

    def test_codex_leaves_out_what_it_cannot_measure(self) -> None:
        unknown = {"id": "abc", "usage_mode": "unknown", "context_tokens": 100}
        plain = self.plain(self.rows("codex", unknown))
        self.assertNotIn("Context", plain)
        self.assertNotIn("unavailable", plain)
        self.assertIn(
            "Subscription limits unavailable", self.plain(self.rows("claude", unknown))
        )

    def test_the_fullest_bucket_binds_with_its_own_reset(self) -> None:
        quotas = {
            "codex": {
                "windows": [
                    {
                        "period": "weekly",
                        "used_percent": 80.0,
                        "resets_at": "2030-01-03T00:00:00+00:00",
                    },
                    {
                        "period": "weekly",
                        "used_percent": 10.0,
                        "resets_at": "2030-01-07T00:00:00+00:00",
                    },
                ]
            }
        }
        week = self.plain(self.rows("codex", quotas=quotas)[:1])
        self.assertIn("80%", week)
        self.assertIn("reset: 2", week)
        self.assertNotIn("reset: 6", week)

    def test_codex_forecasts_against_the_weekly_limit(self) -> None:
        session = {
            **self.session,
            "quota_attribution": {
                "windows": [
                    {
                        "period": "weekly",
                        "estimated_percent": 3.1,
                        "projected_next_10_percent": 3.5,
                    }
                ]
            },
        }
        plain = self.plain(self.rows("codex", session))
        self.assertIn("This session 3.1% of weekly limit ━━━▶ 6.6%", plain)
        self.assertNotIn("This session", self.plain(self.rows("claude", session)))

    def test_paying_codex_keeps_its_credit_meter_and_its_own_reset(self) -> None:
        window = {"period": "monthly", "used_percent": 23.0}
        quotas = {"codex": {"windows": [window]}, "claude": {"windows": [window]}}
        session = {**self.session, "usage_mode": "exhausted"}
        with patch(
            "konvu_telemetry.display.paying_reset_in", return_value="1d"
        ) as reset:
            codex = self.rows("codex", session, quotas)
            reset.assert_called_with("codex")
            claude = self.rows("claude", session, quotas)
            reset.assert_called_with("claude")

        self.assertIn("Credits", self.plain(codex[:1]))
        self.assertIn("23%", self.plain(codex[:1]))
        self.assertNotIn("Credits", self.plain(claude))


class CodexRuntimeTests(unittest.TestCase):
    """The color gate asks the Codex TUI running the hook, and fails closed."""

    tui = Path("/Users/me/.codex/packages/standalone/releases/0.162.1/bin/codex")

    def renders(
        self,
        version_output: str,
        has_terminal: bool = True,
        codex: Path | None = None,
    ) -> bool:
        chain = {
            100: (90, Path("/opt/homebrew/bin/python3"), False),
            90: (80, Path("/bin/sh"), False),
            80: (70, codex or self.tui, has_terminal),
        }
        with (
            patch("konvu_telemetry.codex_runtime.os.getppid", return_value=100),
            patch("konvu_telemetry.codex_runtime.process_info", side_effect=chain.get),
            patch(
                "konvu_telemetry.codex_runtime.run_text", return_value=version_output
            ) as probe,
        ):
            rendered = codex_tui_renders_ansi()
        if has_terminal and (codex or self.tui).name == "codex":
            probe.assert_called_with([str(codex or self.tui), "--version"])
        return rendered

    def test_release_tuis_from_the_first_ansi_version_render_colors(self) -> None:
        self.assertTrue(self.renders("codex-cli 0.162.0\n"))
        self.assertTrue(self.renders("codex-cli 0.163.4\n"))
        self.assertFalse(self.renders("codex-cli 0.161.9\n"))
        self.assertFalse(self.renders("codex-cli 0.162\n"))

    def test_prereleases_and_unknown_output_fail_closed(self) -> None:
        self.assertFalse(self.renders("codex-cli 0.162.0-alpha.3\n"))
        self.assertFalse(self.renders(""))

    def test_a_codex_without_a_terminal_is_not_a_tui(self) -> None:
        # The desktop app and editor extensions run Codex as a background app server.
        self.assertFalse(self.renders("codex-cli 0.163.0\n", has_terminal=False))

    def test_no_codex_ancestor_fails_closed(self) -> None:
        self.assertFalse(
            self.renders("codex-cli 0.163.0\n", codex=Path("/usr/bin/codex-helper"))
        )

    def test_linux_reads_parent_terminal_and_real_executable_from_proc(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            proc = Path(root) / "123"
            proc.mkdir()
            (proc / "exe").symlink_to("/usr/local/bin/codex")
            (proc / "stat").write_text("123 (co) de) S 77 77 77 34816 0 0\n")
            self.assertEqual(
                process_info(123, Path(root)),
                (77, Path("/usr/local/bin/codex"), True),
            )
            (proc / "stat").write_text("123 (codex) S 77 77 77 0 0 0\n")
            self.assertEqual(
                process_info(123, Path(root)),
                (77, Path("/usr/local/bin/codex"), False),
            )

    def test_macos_reads_the_real_executable_not_argv0(self) -> None:
        real = b"/Users/me/.codex/packages/standalone/releases/0.159.3/bin/codex"

        def proc_pidpath(
            pid: int, buffer: ctypes.Array[ctypes.c_char], size: int
        ) -> int:
            buffer.value = real
            return len(real)

        libproc = SimpleNamespace(proc_pidpath=proc_pidpath)
        with tempfile.TemporaryDirectory() as root:
            for ps, has_terminal in (("  768 ttys003\n", True), ("  768 ??\n", False)):
                with (
                    patch("konvu_telemetry.codex_runtime.run_text", return_value=ps),
                    patch(
                        "konvu_telemetry.codex_runtime.ctypes.CDLL",
                        return_value=libproc,
                    ),
                ):
                    self.assertEqual(
                        process_info(1234, Path(root)),
                        (768, Path(os.fsdecode(real)), has_terminal),
                    )

    def test_a_failing_probe_reads_as_unknown(self) -> None:
        failed = subprocess.CompletedProcess(
            [], 1, stdout="codex-cli 0.163.0\n", stderr=""
        )
        with patch("konvu_telemetry.codex_runtime.subprocess.run", return_value=failed):
            self.assertIsNone(run_text(["codex", "--version"]))
        with patch("konvu_telemetry.codex_runtime.subprocess.run", side_effect=OSError):
            self.assertIsNone(run_text(["codex", "--version"]))
