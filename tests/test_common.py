from __future__ import annotations

import subprocess
import sys
import unittest
from unittest.mock import patch

from gputop.collectors.common import (
    clamp,
    command,
    integer,
    number,
    size,
    terminal_text,
)


class ParsingTests(unittest.TestCase):
    def test_scientific_notation_and_units(self) -> None:
        self.assertEqual(number("1.25e3 MiB"), 1250)
        self.assertEqual(number("12.5%"), 12.5)
        self.assertEqual(number("1,024 bytes"), 1024)

    def test_missing_malformed_and_nonfinite_values(self) -> None:
        for value in (
            None,
            True,
            "N/A",
            "[Not Supported]",
            "nan",
            "inf",
            "1e999",
            "GPU 12",
            "1.2.3",
        ):
            with self.subTest(value=value):
                self.assertIsNone(number(value))
        self.assertIsNone(clamp(float("nan")))
        self.assertIsNone(clamp(float("inf")))
        self.assertEqual(size(float("nan")), "--")

    def test_integer_precision_and_invalid_fractions(self) -> None:
        self.assertEqual(integer("9007199254740993 ns"), 9007199254740993)
        self.assertEqual(integer("1e3"), 1000)
        for value in ("1.5", "1.000000000000000001", "1e999999", "nan", False):
            self.assertIsNone(integer(value))

    def test_terminal_controls_are_removed_but_unicode_names_survive(self) -> None:
        self.assertEqual(terminal_text("café GPU"), "café GPU")
        self.assertEqual(terminal_text("x\x1b[2J\n\r\t\x07\u202e"), "x?[2J?????")


class CommandTests(unittest.TestCase):
    def test_decoding_errors_do_not_discard_output(self) -> None:
        result = command([sys.executable, "-c", "import os; os.write(1, b'GPU\\xff')"])
        self.assertEqual(result, "GPU\ufffd")

    def test_failures_are_unavailable_readings(self) -> None:
        for error in (OSError("missing"), subprocess.TimeoutExpired(["tool"], 1)):
            with patch("gputop.collectors.common.subprocess.run", side_effect=error):
                self.assertIsNone(command(["tool"]))

    def test_commands_have_no_shell_or_interactive_input(self) -> None:
        with patch("gputop.collectors.common.subprocess.run") as run:
            run.return_value.returncode = 0
            run.return_value.stdout = "ok"
            self.assertEqual(command(["tool", "$(not-a-command)"]), "ok")
        self.assertFalse(run.call_args.kwargs.get("shell", False))
        self.assertEqual(run.call_args.kwargs["stdin"], subprocess.DEVNULL)
