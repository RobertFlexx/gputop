from __future__ import annotations

import contextlib
import io
import json
import unittest
from unittest.mock import patch

from gputop.cli import main
from gputop.model import GPU, Process, Snapshot


class CliTests(unittest.TestCase):
    def test_human_output_removes_controls_and_json_preserves_data(self) -> None:
        snapshot = Snapshot(
            [GPU("gpu:1", "GPU\x1b[2J", "AMD")],
            [Process(1, "process\x1b]52;bad\x07", "gpu:1")],
        )
        for flag in ("--once", "--doctor", "--json"):
            with self.subTest(flag=flag), patch("gputop.cli.Collector") as collector:
                collector.return_value.collect.return_value = snapshot
                output = io.StringIO()
                with contextlib.redirect_stdout(output):
                    self.assertEqual(main(["--demo", flag]), 0)
                self.assertNotIn("\x1b", output.getvalue())
                if flag == "--json":
                    self.assertEqual(
                        json.loads(output.getvalue())["gpus"][0]["name"],
                        snapshot.gpus[0].name,
                    )

    def test_nonfinite_and_out_of_range_intervals_are_rejected(self) -> None:
        for value in ("nan", "inf", "0", "61"):
            with self.subTest(value=value), contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit) as error:
                    main(["--interval", value])
                self.assertEqual(error.exception.code, 2)

    def test_one_shot_sampling_accounts_for_collection_time(self) -> None:
        with (
            patch("gputop.cli.Collector") as collector,
            patch("gputop.cli.time.monotonic", side_effect=[0, 2, 2, 4]),
            patch("gputop.cli.time.sleep") as sleep,
            contextlib.redirect_stdout(io.StringIO()) as output,
        ):
            collector.return_value.collect.return_value = Snapshot(
                [GPU("gpu:1", "Test", "AMD")]
            )
            self.assertEqual(main(["--json", "--interval", "1"]), 0)
        sleep.assert_called_once_with(0.0)
        payload = json.loads(output.getvalue())
        self.assertEqual(payload["sample_interval_s"], 2)
        self.assertEqual(payload["collection_duration_s"], 2)
