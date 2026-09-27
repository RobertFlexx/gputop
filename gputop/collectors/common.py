from __future__ import annotations

import csv
import io
import math
import re
import subprocess
from decimal import Decimal, InvalidOperation
from pathlib import Path

_NUMBER = re.compile(r"[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?(?=$|\s|%)")


def command(args: list[str], timeout: float = 2.5) -> str | None:
    try:
        result = subprocess.run(
            args,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    return result.stdout if result.returncode == 0 else None


def read(path: Path) -> str | None:
    try:
        return path.read_text(errors="replace").strip()
    except (OSError, UnicodeError):
        return None


def _numeric_text(value: object) -> str | None:
    if value is None or isinstance(value, bool):
        return None
    text = str(value).strip().replace(",", "")
    match = _NUMBER.match(text)
    return match.group() if match else None


def number(value: object) -> float | None:
    """Parse a finite number, optionally followed by a unit."""
    text = _numeric_text(value)
    if text is None:
        return None
    try:
        result = float(text)
    except (ValueError, OverflowError):
        return None
    return result if math.isfinite(result) else None


def integer(value: object) -> int | None:
    """Preserve integer precision for counters and reject fractional values."""
    text = _numeric_text(value)
    if text is None:
        return None
    try:
        return int(text)
    except ValueError:
        pass
    try:
        result = Decimal(text)
        # Bound malformed exponents before allocating an enormous integer.
        if not result.is_finite() or not math.isfinite(float(result)):
            return None
        return int(result) if result == result.to_integral_value() else None
    except (InvalidOperation, ValueError, OverflowError):
        return None


def csv_rows(text: str) -> list[list[str]]:
    return [
        [field.strip() for field in row] for row in csv.reader(io.StringIO(text)) if row
    ]


def clamp(value: float | None, low: float = 0.0, high: float = 100.0) -> float | None:
    if value is None or (not isinstance(value, int) and not math.isfinite(value)):
        return None
    return max(low, min(high, value))


def terminal_text(value: str) -> str:
    """Keep names and driver output from injecting terminal control sequences."""
    if value.isprintable():
        return value
    return "".join(char if char.isprintable() else "?" for char in value)


def size(value: float | int | None) -> str:
    if value is None:
        return "--"
    try:
        amount = float(value)
    except OverflowError:
        return "--"
    if not math.isfinite(amount):
        return "--"
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if abs(amount) < 1024 or unit == "TiB":
            return f"{amount:.0f}{unit}" if unit == "B" else f"{amount:.1f}{unit}"
        amount /= 1024
    return "--"
