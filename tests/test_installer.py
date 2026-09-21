import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from konvu_telemetry import installer


class InstallerTests(unittest.TestCase):
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
            self.assertEqual(rendered.stdout, 'existing:{"session_id":"session"}telemetry')
            self.assertEqual(
                json.loads(path.read_text())["statusLine"]["command"],
                "printf existing:; cat",
            )
            self.assertFalse(wrapper.exists())

    def test_similar_command_name_is_not_treated_as_konvu_owned(self) -> None:
        self.assertFalse(installer.is_konvu_command("/tmp/not-konvu-launcher-helper"))
        self.assertFalse(installer.is_konvu_command("echo konvu-launcher"))
        self.assertFalse(installer.is_konvu_command("/tmp/konvu-launcher codex-hook"))
        self.assertTrue(
            installer.is_konvu_command(f"{installer.launcher_path()} codex-hook")
        )

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
            self.assertEqual(
                path.read_text(), f'#!/bin/sh\nunset PYTHONPATH\nexec {console} "$@"\n'
            )

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
