import json
import os
import plistlib
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from konvu_telemetry import cli, installer


class InstallerTests(unittest.TestCase):
    def test_initial_collection_wait_ignores_pre_setup_health(self) -> None:
        with patch.object(
            installer,
            "load_health",
            side_effect=[
                {"last_success_at": "2026-01-01T00:00:00+00:00"},
                {"last_success_at": "2026-01-01T00:00:02+00:00"},
            ],
        ):
            self.assertTrue(
                installer.wait_for_initial_collection(1767225601.0, timeout_seconds=1)
            )

    def test_cli_uninstall_removes_package_after_local_cleanup(self) -> None:
        calls: list[str] = []
        with (
            patch.object(
                cli,
                "uninstall",
                side_effect=lambda: calls.append("local") or {},
            ),
            patch.object(
                cli,
                "uninstall_homebrew_package",
                side_effect=lambda: calls.append("package") or True,
            ),
            patch("builtins.print"),
        ):
            cli.installer_main(["uninstall"])
        self.assertEqual(calls, ["local", "package"])

    def test_console_launcher_skips_a_stale_py_path_injected_script(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            stale = root / "stale" / "konvu-telemetry"
            current = root / "current" / "bin" / "konvu-telemetry"
            stale.parent.mkdir()
            current.parent.mkdir(parents=True)
            stale.write_text(
                "#!/bin/sh\n"
                'if [ -n "$PYTHONPATH" ]; then\n'
                "  echo claude-prompt-hook codex-prompt-hook\n"
                "else\n"
                "  echo claude-hook codex-hook\n"
                "fi\n"
            )
            current.write_text("#!/bin/sh\necho claude-prompt-hook codex-prompt-hook\n")
            stale.chmod(0o700)
            current.chmod(0o700)
            with (
                patch.object(installer.sys, "argv", [str(stale), "setup"]),
                patch.object(installer.site, "USER_BASE", str(root / "current")),
                patch.object(
                    installer.sysconfig,
                    "get_path",
                    return_value=str(root / "missing"),
                ),
                patch.dict(os.environ, {"PYTHONPATH": str(root / "source")}),
            ):
                self.assertEqual(installer.console_launcher(), current)

    def test_console_launcher_rejects_only_stale_scripts(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            stale = root / "konvu-telemetry"
            stale.write_text("#!/bin/sh\necho claude-hook codex-hook\n")
            stale.chmod(0o700)
            with (
                patch.object(installer.sys, "argv", [str(stale), "setup"]),
                patch.object(installer.site, "USER_BASE", None),
                patch.object(
                    installer.sysconfig,
                    "get_path",
                    return_value=str(root / "missing"),
                ),
                self.assertRaisesRegex(RuntimeError, "current telemetry launcher"),
            ):
                installer.console_launcher()

    def test_successful_setup_records_a_duration_bucket(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            with (
                patch.object(installer.Path, "home", return_value=Path(temporary)),
                patch.object(installer.sys, "platform", "darwin"),
                patch.object(installer, "validate_integrations"),
                patch.object(installer, "install_launcher"),
                patch.object(
                    installer, "install_claude_statusline", return_value="installed"
                ),
                patch.object(
                    installer, "install_claude_prompt_hook", return_value="installed"
                ),
                patch.object(installer, "remove_claude_stop_hook", return_value=False),
                patch.object(installer, "install_codex_hook", return_value="installed"),
                patch.object(
                    installer, "install_codex_prompt_hook", return_value="installed"
                ),
                patch.object(installer, "install_launch_agent"),
                patch.object(installer, "record_setup_completed") as recorded,
            ):
                installer.setup(60, False)

        recorded.assert_called_once()
        self.assertIsInstance(recorded.call_args.args[0], float)
        self.assertTrue(recorded.call_args.kwargs["default_enabled"])

    def test_setup_can_explicitly_enable_telemetry(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary)
            launch_agent = (
                home / "Library" / "LaunchAgents" / f"{installer.LABEL}.plist"
            )
            launch_agent.parent.mkdir(parents=True)
            launch_agent.write_bytes(b"existing")
            with (
                patch.object(installer.Path, "home", return_value=home),
                patch.object(installer.sys, "platform", "darwin"),
                patch.object(installer, "validate_integrations"),
                patch.object(installer, "install_launcher"),
                patch.object(
                    installer, "install_claude_statusline", return_value="installed"
                ),
                patch.object(
                    installer, "install_claude_prompt_hook", return_value="installed"
                ),
                patch.object(installer, "remove_claude_stop_hook", return_value=False),
                patch.object(installer, "install_codex_hook", return_value="installed"),
                patch.object(
                    installer, "install_codex_prompt_hook", return_value="installed"
                ),
                patch.object(installer, "install_launch_agent"),
                patch.object(installer, "record_setup_completed") as recorded,
            ):
                installer.setup(60, False, tracking_enabled=True)

        self.assertTrue(recorded.call_args.kwargs["default_enabled"])

    def test_setup_merges_konvu_hooks_without_removing_existing_hooks(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary)
            claude = home / ".claude"
            codex = home / ".codex"
            claude.mkdir()
            codex.mkdir()
            console = home / "bin" / "konvu"
            console.parent.mkdir()
            console.write_text("#!/bin/sh\nexit 0\n")
            console.chmod(0o700)
            (claude / "settings.json").write_text(
                json.dumps({"hooks": {"UserPromptSubmit": []}})
            )
            (codex / "hooks.json").write_text(
                json.dumps(
                    {
                        "hooks": {
                            "Stop": [
                                {"hooks": [{"type": "command", "command": "keep-me"}]}
                            ]
                        }
                    }
                )
            )
            with (
                patch.object(installer.Path, "home", return_value=home),
                patch.object(installer, "console_launcher", return_value=console),
            ):
                self.assertEqual(installer.install_claude_statusline(), "installed")
                self.assertEqual(installer.install_codex_hook(), "installed")
            settings = json.loads((claude / "settings.json").read_text())
            hooks = json.loads((codex / "hooks.json").read_text())
            self.assertIn(installer.LAUNCHER_NAME, settings["statusLine"]["command"])
            self.assertNotIn("-I", settings["statusLine"]["command"])
            self.assertNotIn("Stop", settings["hooks"])
            self.assertEqual(
                hooks["hooks"]["Stop"][0]["hooks"][0]["command"], "keep-me"
            )
            self.assertIn(
                installer.LAUNCHER_NAME,
                hooks["hooks"]["Stop"][1]["hooks"][0]["command"],
            )
            self.assertEqual(hooks["hooks"]["Stop"][1]["hooks"][0]["timeout"], 5)

    def test_existing_claude_statusline_is_chained_with_telemetry(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary)
            claude = home / ".claude"
            claude.mkdir()
            console = home / "bin" / "konvu"
            console.parent.mkdir()
            console.write_text(
                "#!/bin/sh\n"
                'if [ "$1" = "statusline" ]; then\n'
                "  cat >/dev/null\n"
                "  printf telemetry\n"
                "fi\n"
            )
            console.chmod(0o700)
            path = claude / "settings.json"
            path.write_text(
                json.dumps(
                    {
                        "statusLine": {
                            "type": "command",
                            "command": "printf existing:; cat",
                        }
                    }
                )
            )
            with (
                patch.object(installer.Path, "home", return_value=home),
                patch.object(installer, "console_launcher", return_value=console),
            ):
                self.assertEqual(installer.install_claude_statusline(), "installed")
                wrapper = installer.claude_statusline_path()
                rendered = subprocess.run(
                    [str(wrapper)],
                    input='{"session_id":"session"}',
                    capture_output=True,
                    text=True,
                    check=True,
                )
                self.assertTrue(installer.remove_claude_statusline())
            self.assertEqual(
                rendered.stdout, 'existing:{"session_id":"session"}telemetry'
            )
            self.assertEqual(
                json.loads(path.read_text())["statusLine"]["command"],
                "printf existing:; cat",
            )
            self.assertFalse(wrapper.exists())

    def test_setup_removes_a_legacy_claude_stop_hook_and_keeps_foreign_ones(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary)
            claude_path = home / ".claude" / "settings.json"
            codex_path = home / ".codex" / "hooks.json"
            claude_path.parent.mkdir()
            codex_path.parent.mkdir()
            codex_path.write_text("{}")
            console = home / "bin" / "konvu-telemetry"
            console.parent.mkdir()
            console.write_text("#!/bin/sh\nexit 0\n")
            console.chmod(0o700)
            foreign = {"hooks": [{"type": "command", "command": "keep-me"}]}
            with (
                patch.object(installer.Path, "home", return_value=home),
                patch.object(installer, "console_launcher", return_value=console),
                patch.object(installer.sys, "platform", "darwin"),
                patch.object(installer, "stop_launch_agent"),
                patch.object(installer, "start_launch_agent"),
            ):
                legacy = {
                    "type": "command",
                    "command": f"{installer.launcher_path()} claude-hook",
                    "timeout": 5,
                }
                claude_path.write_text(
                    json.dumps({"hooks": {"Stop": [foreign, {"hooks": [legacy]}]}})
                )
                self.assertEqual(
                    installer.setup(60, False)["claude_stop_hook"], "removed"
                )
                self.assertEqual(
                    json.loads(claude_path.read_text())["hooks"]["Stop"], [foreign]
                )
                # A second run has nothing left to clean up and must not report otherwise.
                self.assertEqual(
                    installer.setup(60, False)["claude_stop_hook"], "absent"
                )
                self.assertEqual(
                    json.loads(claude_path.read_text())["hooks"]["Stop"], [foreign]
                )
                self.assertTrue(installer.uninstall()["claude_statusline"])
            self.assertEqual(
                json.loads(claude_path.read_text())["hooks"]["Stop"], [foreign]
            )

    def test_prompt_hooks_are_idempotent_and_leave_other_hooks_alone(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary)
            claude = home / ".claude"
            codex = home / ".codex"
            claude.mkdir()
            codex.mkdir()
            console = home / "bin" / "konvu"
            console.parent.mkdir()
            console.write_text("#!/bin/sh\nexit 0\n")
            console.chmod(0o700)
            kept = {"hooks": [{"type": "command", "command": "keep-me"}]}
            for path in (claude / "settings.json", codex / "hooks.json"):
                path.write_text(json.dumps({"hooks": {"UserPromptSubmit": [kept]}}))
            with (
                patch.object(installer.Path, "home", return_value=home),
                patch.object(installer, "console_launcher", return_value=console),
            ):
                self.assertEqual(installer.install_claude_prompt_hook(), "installed")
                self.assertEqual(installer.install_claude_prompt_hook(), "updated")
                self.assertEqual(installer.install_codex_prompt_hook(), "installed")
                self.assertEqual(installer.install_codex_prompt_hook(), "updated")
                for path, command in (
                    (claude / "settings.json", "claude-prompt-hook"),
                    (codex / "hooks.json", "codex-prompt-hook"),
                ):
                    groups = json.loads(path.read_text())["hooks"]["UserPromptSubmit"]
                    self.assertEqual(len(groups), 2)
                    self.assertEqual(groups[0], kept)
                    self.assertEqual(groups[1]["hooks"][0]["timeout"], 5)
                    self.assertTrue(
                        installer.is_konvu_hook(
                            groups[1]["hooks"][0]["command"], command
                        )
                    )
                self.assertTrue(installer.remove_claude_prompt_hook())
                self.assertFalse(installer.remove_claude_prompt_hook())
                self.assertTrue(installer.remove_codex_prompt_hook())
                self.assertFalse(installer.remove_codex_prompt_hook())
            for path in (claude / "settings.json", codex / "hooks.json"):
                self.assertEqual(
                    json.loads(path.read_text())["hooks"]["UserPromptSubmit"], [kept]
                )

    def test_setup_rejects_a_malformed_user_prompt_submit_hook_list(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary)
            claude_path = home / ".claude" / "settings.json"
            claude_path.parent.mkdir()
            (home / ".codex").mkdir()
            claude_path.write_text(json.dumps({"hooks": {"UserPromptSubmit": {}}}))
            with patch.object(installer.Path, "home", return_value=home):
                with self.assertRaisesRegex(ValueError, "hooks.UserPromptSubmit"):
                    installer.validate_integrations()

    def test_similar_command_name_is_not_treated_as_konvu_owned(self) -> None:
        self.assertFalse(installer.is_konvu_command("/tmp/not-konvu-launcher-helper"))
        self.assertFalse(installer.is_konvu_command("echo konvu-launcher"))
        self.assertFalse(installer.is_konvu_command("/tmp/konvu-launcher codex-hook"))
        self.assertTrue(
            installer.is_konvu_command(f"{installer.launcher_path()} codex-hook")
        )

    def test_legacy_shell_wrapped_hook_is_removed_without_matching_lookalikes(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary)
            path = home / ".codex" / "hooks.json"
            path.parent.mkdir()
            with patch.object(installer.Path, "home", return_value=home):
                launcher = installer.launcher_path()
                owned = f"sh -c '{launcher} codex-prompt-hook 2>/dev/null || true'"
                lookalike = f"sh -c '{launcher} codex-prompt-hook 2>/dev/null || false'"
                path.write_text(
                    json.dumps(
                        {
                            "hooks": {
                                "UserPromptSubmit": [
                                    {
                                        "hooks": [
                                            {"type": "command", "command": owned},
                                            {"type": "command", "command": lookalike},
                                        ]
                                    }
                                ]
                            }
                        }
                    )
                )
                self.assertTrue(installer.remove_codex_prompt_hook(create_backup=False))
            remaining = json.loads(path.read_text())["hooks"]["UserPromptSubmit"]
            self.assertEqual(
                remaining[0]["hooks"], [{"type": "command", "command": lookalike}]
            )

    def test_homebrew_uninstall_only_runs_for_the_brewed_executable(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            brew = root / "bin" / "brew"
            prefix = root / "Cellar" / "konvu-telemetry" / "1.0.0"
            executable = prefix / "libexec" / "bin" / installer.CONSOLE_COMMAND
            linked = prefix / "bin" / installer.CONSOLE_COMMAND
            executable.parent.mkdir(parents=True)
            linked.parent.mkdir()
            executable.write_text("#!/bin/sh\n")
            linked.symlink_to(executable)
            with (
                patch.object(installer, "homebrew_executable", return_value=brew),
                patch.object(installer.sys, "argv", [str(linked), "uninstall"]),
                patch.object(installer.subprocess, "run") as run,
            ):
                run.side_effect = [
                    subprocess.CompletedProcess([], 0, stdout=f"{prefix}\n"),
                    subprocess.CompletedProcess([], 0),
                ]
                self.assertTrue(installer.uninstall_homebrew_package())
            self.assertEqual(
                run.call_args_list[1].args[0],
                [str(brew), "uninstall", "--formula", installer.CONSOLE_COMMAND],
            )
            self.assertEqual(
                run.call_args_list[1].kwargs["env"]["HOMEBREW_NO_AUTOREMOVE"],
                "1",
            )

    def test_homebrew_executable_rejects_a_writable_install(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            unsafe = Path(temporary) / "brew"
            unsafe.write_text("#!/bin/sh\n")
            unsafe.chmod(0o722)
            with patch.object(
                Path,
                "resolve",
                autospec=True,
                return_value=unsafe,
            ):
                self.assertIsNone(installer.homebrew_executable())

    def test_homebrew_uninstall_ignores_an_executable_outside_the_formula(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            brew = root / "bin" / "brew"
            prefix = root / "Cellar" / "konvu-telemetry" / "1.0.0"
            executable = root / "elsewhere" / installer.CONSOLE_COMMAND
            executable.parent.mkdir()
            executable.write_text("#!/bin/sh\n")
            with (
                patch.object(installer, "homebrew_executable", return_value=brew),
                patch.object(installer.sys, "argv", [str(executable), "uninstall"]),
                patch.object(installer.subprocess, "run") as run,
            ):
                run.return_value = subprocess.CompletedProcess(
                    [], 0, stdout=f"{prefix}\n"
                )
                self.assertFalse(installer.uninstall_homebrew_package())
            self.assertEqual(run.call_count, 1)

    def test_private_launcher_uses_absolute_console_script(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary)
            console = home / "bin" / "konvu"
            console.parent.mkdir()
            console.write_text("#!/bin/sh\nexit 0\n")
            console.chmod(0o700)
            with (
                patch.object(installer.Path, "home", return_value=home),
                patch.object(installer, "console_launcher", return_value=console),
            ):
                path = installer.install_launcher()
                command = installer.collector_command()
            self.assertEqual(command, [str(path)])
            self.assertEqual(path.stat().st_mode & 0o777, 0o700)
            script = path.read_text()
            self.assertTrue(script.startswith("#!/bin/sh\nunset PYTHONPATH\n"))
            self.assertIn(f"[ -x {console} ] || exit 0\n", script)
            self.assertIn(f'exec {console} "$@"\n', script)
            self.assertNotIn(" konvu ", script.replace(str(console), ""))

    def test_launcher_never_fails_a_hook_invocation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary)
            console = home / "bin" / "konvu"
            console.parent.mkdir()
            # An older build that does not know the subcommand: usage on stderr, exit 2.
            console.write_text(
                "#!/bin/sh\n"
                'case "$1" in\n'
                "*-prompt-hook)\n"
                "  echo 'usage: konvu-telemetry: invalid choice' >&2\n"
                "  exit 2\n"
                "  ;;\n"
                "esac\n"
                "printf output\n"
            )
            console.chmod(0o700)
            with (
                patch.object(installer.Path, "home", return_value=home),
                patch.object(installer, "console_launcher", return_value=console),
            ):
                launcher = installer.install_launcher()
            for command in ("claude-prompt-hook", "codex-prompt-hook"):
                result = subprocess.run(
                    [str(launcher), command], capture_output=True, text=True
                )
                self.assertEqual(result.returncode, 0, command)
                self.assertEqual(result.stdout, "", command)
                self.assertEqual(result.stderr, "", command)
            working = subprocess.run(
                [str(launcher), "codex-hook"], capture_output=True, text=True
            )
            self.assertEqual((working.returncode, working.stdout), (0, "output\n"))
            # The status line is not a hook and keeps its own stdout untouched.
            statusline = subprocess.run(
                [str(launcher), "statusline"], capture_output=True, text=True
            )
            self.assertEqual((statusline.returncode, statusline.stdout), (0, "output"))

    def test_private_launcher_exits_cleanly_after_package_removal(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary)
            console = home / "bin" / "konvu"
            console.parent.mkdir()
            console.write_text("#!/bin/sh\nexit 0\n")
            console.chmod(0o700)
            with (
                patch.object(installer.Path, "home", return_value=home),
                patch.object(installer, "console_launcher", return_value=console),
            ):
                launcher = installer.install_launcher()
            console.unlink()
            result = subprocess.run([str(launcher), "statusline"], check=False)
            self.assertEqual(result.returncode, 0)

    def test_launch_agent_stops_restarting_after_package_removal(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary)
            console = home / "bin" / "konvu"
            console.parent.mkdir()
            console.write_text("#!/bin/sh\nexit 0\n")
            console.chmod(0o700)
            with (
                patch.object(installer.Path, "home", return_value=home),
                patch.object(installer, "console_launcher", return_value=console),
                patch.object(installer.sys, "platform", "darwin"),
                patch.object(installer, "stop_launch_agent"),
                patch.object(installer, "start_launch_agent"),
            ):
                installer.install_launch_agent(60)
                payload = plistlib.loads(installer.launch_agent_path().read_bytes())
            self.assertEqual(payload["KeepAlive"], {"SuccessfulExit": False})
            self.assertEqual(payload["ThrottleInterval"], 30)

    def test_private_launcher_ignores_an_untrusted_working_directory(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary)
            console = home / "bin" / "konvu"
            console.parent.mkdir()
            output = home / "executed"
            console.write_text(f'#!/bin/sh\nprintf console > "{output}"\n')
            console.chmod(0o700)
            untrusted = home / "untrusted"
            untrusted.mkdir()
            (untrusted / "konvu").write_text(
                f'#!/bin/sh\nprintf untrusted > "{output}"\n'
            )
            (untrusted / "konvu").chmod(0o700)
            with (
                patch.object(installer.Path, "home", return_value=home),
                patch.object(installer, "console_launcher", return_value=console),
            ):
                launcher = installer.install_launcher()
            environment = dict(os.environ, PYTHONPATH=str(untrusted))
            subprocess.run(
                [str(launcher), "statusline"],
                cwd=untrusted,
                env=environment,
                check=True,
            )
            self.assertEqual(output.read_text(), "console")

    def test_setup_rolls_back_integrations_when_service_install_fails(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary)
            claude_path = home / ".claude" / "settings.json"
            codex_path = home / ".codex" / "hooks.json"
            claude_path.parent.mkdir()
            codex_path.parent.mkdir()
            claude_path.write_text('{"existing": true}\n')
            codex_path.write_text('{"hooks": {}}\n')
            console = home / "bin" / "konvu-telemetry"
            console.parent.mkdir()
            console.write_text("#!/bin/sh\nexit 0\n")
            console.chmod(0o700)
            with (
                patch.object(installer.Path, "home", return_value=home),
                patch.object(installer, "console_launcher", return_value=console),
                patch.object(installer.sys, "platform", "darwin"),
                patch.object(
                    installer,
                    "install_launch_agent",
                    side_effect=RuntimeError("bootstrap failed"),
                ),
                patch.object(installer, "stop_launch_agent"),
            ):
                with self.assertRaisesRegex(RuntimeError, "bootstrap failed"):
                    installer.setup(60, False)
            self.assertEqual(claude_path.read_text(), '{"existing": true}\n')
            self.assertEqual(codex_path.read_text(), '{"hooks": {}}\n')
            self.assertFalse(
                (home / ".konvu" / "telemetry" / installer.LAUNCHER_NAME).exists()
            )

    def test_uninstall_keeps_files_when_launch_agent_cannot_stop(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary)
            plist = home / "Library" / "LaunchAgents" / f"{installer.LABEL}.plist"
            plist.parent.mkdir(parents=True)
            plist.write_bytes(b"installed")

            def stop(strict: bool = True) -> bool:
                if strict:
                    raise RuntimeError("still running")
                return False

            with (
                patch.object(installer.Path, "home", return_value=home),
                patch.object(installer, "stop_launch_agent", side_effect=stop),
            ):
                with self.assertRaisesRegex(RuntimeError, "still running"):
                    installer.uninstall()
            self.assertEqual(plist.read_bytes(), b"installed")

    def test_setup_is_idempotent_and_uninstall_restores_existing_config(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary)
            claude_path = home / ".claude" / "settings.json"
            codex_path = home / ".codex" / "hooks.json"
            claude_path.parent.mkdir()
            codex_path.parent.mkdir()
            original_statusline = {
                "type": "command",
                "command": "printf existing",
                "refreshInterval": 10,
            }
            claude_path.write_text(json.dumps({"statusLine": original_statusline}))
            codex_path.write_text(
                json.dumps(
                    {
                        "hooks": {
                            "Stop": [
                                {"hooks": [{"type": "command", "command": "keep-me"}]}
                            ]
                        }
                    }
                )
            )
            console = home / "bin" / "konvu-telemetry"
            console.parent.mkdir()
            console.write_text("#!/bin/sh\nexit 0\n")
            console.chmod(0o700)
            with (
                patch.object(installer.Path, "home", return_value=home),
                patch.object(installer, "console_launcher", return_value=console),
                patch.object(installer.sys, "platform", "darwin"),
                patch.object(installer, "stop_launch_agent"),
                patch.object(installer, "start_launch_agent"),
                patch.object(installer, "record_setup_completed"),
            ):
                installer.setup(30, False)
                installer.setup(60, False)
                installed = json.loads(codex_path.read_text())
                self.assertEqual(len(installed["hooks"]["Stop"]), 2)
                self.assertEqual(len(installed["hooks"]["UserPromptSubmit"]), 1)
                self.assertEqual(
                    len(
                        json.loads(claude_path.read_text())["hooks"]["UserPromptSubmit"]
                    ),
                    1,
                )
                self.assertEqual(
                    json.loads(installer.claude_statusline_state_path().read_text())[
                        "statusLine"
                    ],
                    original_statusline,
                )
                result = installer.uninstall()
                owned_paths = (
                    installer.launch_agent_path(),
                    installer.launcher_path(),
                    installer.claude_statusline_path(),
                    installer.claude_statusline_original_path(),
                    installer.claude_statusline_state_path(),
                )
            self.assertEqual(
                result,
                {
                    "claude_statusline": True,
                    "claude_stop_hook": False,
                    "claude_prompt_hook": True,
                    "codex_hook": True,
                    "codex_prompt_hook": True,
                },
            )
            self.assertEqual(
                json.loads(claude_path.read_text())["statusLine"], original_statusline
            )
            restored = json.loads(codex_path.read_text())
            self.assertEqual(
                restored["hooks"]["Stop"],
                [{"hooks": [{"type": "command", "command": "keep-me"}]}],
            )
            for path in owned_paths:
                self.assertFalse(path.exists())
            self.assertFalse((home / ".konvu").exists())
            self.assertEqual(list(claude_path.parent.glob("*.konvu-backup-*")), [])
            self.assertEqual(list(codex_path.parent.glob("*.konvu-backup-*")), [])
