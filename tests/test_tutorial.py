import os
import stat
import tempfile
import unittest
from io import StringIO
from pathlib import Path
from unittest import mock

from bk.cli import _maybe_print_first_use_tip
from bk.config import Config
from bk.tutorial import (
    CLI_TIP,
    TUI_TOUR,
    mark_onboarding_seen,
    onboarding_marker_path,
    onboarding_seen,
    run_cli_tutorial,
    tutorial_pages,
)


class TtyBuffer(StringIO):
    def isatty(self):
        return True


class TutorialTests(unittest.TestCase):
    def config(self, root: Path) -> Config:
        return Config(
            data_dir=root / "data",
            gpu_count=8,
            slot_minutes=5,
            max_shared_users=4,
        )

    def test_noninteractive_tutorial_is_complete_plain_and_read_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            output = StringIO()

            result = run_cli_tutorial(
                self.config(root),
                input_stream=StringIO(),
                output=output,
                environment={"TERM": "xterm-256color"},
            )

            text = output.getvalue()
            self.assertEqual(result, "done")
            self.assertEqual(text.count("GPUbk tutorial "), len(tutorial_pages(self.config(root))))
            self.assertIn("bk 1 30m", text)
            self.assertIn("bk tutorial --tui", text)
            self.assertNotIn("\x1b[", text)
            self.assertFalse((root / "data").exists())

    def test_tutorial_examples_follow_server_gpu_and_share_limits(self):
        config = Config(
            data_dir=Path("/tmp/gpubk-tutorial-policy"),
            gpu_count=1,
            max_shared_users=3,
        )
        text = "\n".join(
            command
            for page in tutorial_pages(config)
            for command, _description in page.commands
        )

        self.assertIn("bk 1 1h30m --mem 12g", text)
        self.assertIn("bk slots 1 1h", text)
        self.assertIn("bk 1 1h --share 2", text)
        self.assertIn("bk 1 1h --gpu 0", text)
        self.assertNotIn("bk 2 ", text)
        self.assertNotRegex(text, r"--share\s+\S*/\S*")
        self.assertNotRegex(text, r"--share\s+\S*%")

    def test_interactive_tutorial_recovers_input_and_can_open_tui_tour(self):
        with tempfile.TemporaryDirectory() as tmp:
            output = TtyBuffer()
            result = run_cli_tutorial(
                self.config(Path(tmp)),
                input_stream=TtyBuffer("wrong\n\nback\nt\n"),
                output=output,
                environment={"TERM": "xterm-256color"},
                interactive=True,
            )

            self.assertEqual(result, "tui")
            self.assertIn("Choose Enter, b, t, or q.", output.getvalue())
            self.assertIn("\x1b[", output.getvalue())

    def test_onboarding_markers_are_private_versioned_and_separate(self):
        with tempfile.TemporaryDirectory() as tmp:
            environment = {"HOME": "/ignored", "XDG_STATE_HOME": tmp}
            cli_path = onboarding_marker_path(CLI_TIP, environment=environment)
            tui_path = onboarding_marker_path(TUI_TOUR, environment=environment)

            self.assertNotEqual(cli_path, tui_path)
            self.assertFalse(onboarding_seen(CLI_TIP, environment=environment))
            written = mark_onboarding_seen(CLI_TIP, environment=environment)

            self.assertEqual(written, cli_path)
            self.assertTrue(onboarding_seen(CLI_TIP, environment=environment))
            self.assertFalse(onboarding_seen(TUI_TOUR, environment=environment))
            self.assertEqual(stat.S_IMODE(cli_path.stat().st_mode), 0o600)
            self.assertEqual(stat.S_IMODE(cli_path.parent.stat().st_mode), 0o700)
            self.assertEqual(
                cli_path.read_text(encoding="ascii"),
                "gpubk-onboarding-v1:cli-tip\n",
            )

    def test_onboarding_marker_does_not_follow_a_symbolic_link(self):
        if not hasattr(os, "symlink"):
            self.skipTest("symbolic links are unavailable")
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            environment = {"HOME": "/ignored", "XDG_STATE_HOME": str(root / "state")}
            path = onboarding_marker_path(CLI_TIP, environment=environment)
            path.parent.mkdir(parents=True, mode=0o700)
            target = root / "target"
            target.write_text("keep\n", encoding="ascii")
            path.symlink_to(target)

            self.assertFalse(onboarding_seen(CLI_TIP, environment=environment))
            with self.assertRaises(OSError):
                mark_onboarding_seen(CLI_TIP, environment=environment)
            self.assertEqual(target.read_text(encoding="ascii"), "keep\n")

    def test_unknown_onboarding_marker_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "unknown onboarding"):
            onboarding_marker_path("future")

    def test_tui_first_run_and_manual_replay_both_request_tutorial(self):
        config = self.config(Path("/tmp/gpubk-tutorial-test"))
        store = mock.sentinel.store
        with (
            mock.patch("bk.tui.onboarding_seen", return_value=False),
            mock.patch("bk.tui.curses.wrapper", return_value=0) as wrapper,
        ):
            from bk.tui import run_tui

            self.assertEqual(run_tui(config, store), 0)
            self.assertTrue(wrapper.call_args.args[-1])

        with (
            mock.patch("bk.tui.onboarding_seen") as seen,
            mock.patch("bk.tui.curses.wrapper", return_value=0) as wrapper,
        ):
            self.assertEqual(run_tui(config, store, show_tutorial=True), 0)
            seen.assert_not_called()
            self.assertTrue(wrapper.call_args.args[-1])

    def test_first_use_tip_is_terminal_only_and_marks_itself_seen(self):
        terminal = TtyBuffer()
        with (
            mock.patch("bk.cli.sys.stdout", terminal),
            mock.patch("bk.cli.onboarding_seen", return_value=False),
            mock.patch("bk.cli.mark_onboarding_seen") as mark,
        ):
            _maybe_print_first_use_tip("first tip")

        self.assertEqual(terminal.getvalue(), "first tip\n")
        mark.assert_called_once_with(CLI_TIP)

        with (
            mock.patch("bk.cli.sys.stdout", StringIO()),
            mock.patch("bk.cli.mark_onboarding_seen") as mark,
        ):
            _maybe_print_first_use_tip("hidden")
        mark.assert_not_called()
