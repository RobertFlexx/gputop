from __future__ import annotations

import curses
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from gputop.model import GPU, Snapshot
from gputop.ui import App


class Screen:
    def __init__(self, height: int = 17, width: int = 54) -> None:
        self.height = height
        self.width = width
        self.erase()

    def getmaxyx(self) -> tuple[int, int]:
        return self.height, self.width

    def erase(self) -> None:
        self.cells = [[" "] * self.width for _ in range(self.height)]

    def addnstr(self, y: int, x: int, value: str, limit: int, attr: int = 0) -> None:
        for offset, char in enumerate(value[:limit]):
            if x + offset < self.width:
                self.cells[y][x + offset] = char

    def row(self, y: int) -> str:
        return "".join(self.cells[y])

    def text(self) -> str:
        return "\n".join(map("".join, self.cells))

    def keypad(self, enabled: bool) -> None:
        pass

    def timeout(self, milliseconds: int) -> None:
        pass

    def refresh(self) -> None:
        pass


class CoreViewTests(unittest.TestCase):
    def make_app(self, gpu: GPU) -> tuple[App, Screen]:
        screen = Screen()
        sampler = SimpleNamespace(
            snapshot=Snapshot(gpus=[gpu]),
            revision=1,
            interval=1.5,
            paused=False,
            refresh=lambda: None,
        )
        with (
            patch.object(curses, "start_color"),
            patch.object(curses, "use_default_colors"),
            patch.object(curses, "init_pair"),
            patch.object(curses, "curs_set"),
        ):
            app = App(screen, sampler)
        app.colors = False
        return app, screen

    def test_core_toggle_shows_missing_readings_without_inventing_load(self) -> None:
        gpu = GPU(
            "mac:0",
            "Apple M5",
            "Apple",
            core_count=8,
            utilization=32,
            core_equivalent_load=2.56,
        )
        app, screen = self.make_app(gpu)
        self.assertTrue(app.key(ord("c")))
        app.draw()
        display = screen.text()
        self.assertIn("GPU cores (8)", display)
        self.assertIn("Per-core usage unavailable; -- means no reading.", display)
        self.assertIn("Core 01", display)
        self.assertIn("Core 08", display)
        self.assertIn("2.6/8 core eq est", display)
        self.assertIn("Core 01 [-------]    --", display)
        app.key(ord("c"))
        app.draw()
        self.assertIn("Processes (0)", screen.text())

    def test_measured_zero_and_partial_readings(self) -> None:
        gpu = GPU(
            "mac:0",
            "Apple test",
            "Apple",
            core_count=4,
            core_utilization={
                "GPU Core 0 Utilization %": 0.0,
                "Shader Core 2 Utilization %": 75.0,
            },
        )
        app, screen = self.make_app(gpu)
        app.key(ord("c"))
        app.draw()
        display = screen.text()
        self.assertIn("Measured per-core utilization: 2/4", display)
        self.assertIn("Core 00 [·······]   0.0%", display)
        self.assertIn("Core 02 [█████··]  75.0%", display)
        self.assertIn("Core 01 [-------]", display)

    def test_core_view_scrolls_and_gpu_switch_resets_position(self) -> None:
        gpu = GPU("mac:0", "Apple test", "Apple", core_count=40)
        app, screen = self.make_app(gpu)
        app.key(ord("c"))
        app.draw()
        self.assertIn("Core 01", screen.text())
        self.assertNotIn("Core 17", screen.text())
        app.key(curses.KEY_NPAGE)
        app.draw()
        self.assertIn("Core 17", screen.text())
        self.assertNotIn("Core 01", screen.text())
        app.key(9)
        self.assertEqual(app.core_offset, 0)


if __name__ == "__main__":
    unittest.main()
