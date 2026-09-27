from __future__ import annotations

import csv
import io
import re
import subprocess
from pathlib import Path


def command(args: list[str], timeout: float = 2.5) -> str | None:
    try:
        result = subprocess.run(args, capture_output=True, text=True, timeout=timeout, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return result.stdout if result.returncode == 0 else None


def read(path: Path) -> str | None:
    try:
        return path.read_text(errors="replace").strip()
    except (OSError, UnicodeError):
        return None


def number(value: object) -> float | None:
    if value is None:
        return None
    text = str(value).strip().replace(",", "")
    if not text or text.lower() in {"n/a", "[not supported]", "not supported", "-", "none"}:
        return None
    match = re.search(r"[-+]?(?:\d+(?:\.\d*)?|\.\d+)", text)
    if not match:
        return None
    try:
        return float(match.group())
    except ValueError:
        return None


def integer(value: object) -> int | None:
    result = number(value)
    return int(result) if result is not None else None


def csv_rows(text: str) -> list[list[str]]:
    return [[field.strip() for field in row] for row in csv.reader(io.StringIO(text)) if row]


def clamp(value: float | None, low: float = 0.0, high: float = 100.0) -> float | None:
    return max(low, min(high, value)) if value is not None else None


def size(value: float | int | None) -> str:
    if value is None:
        return "--"
    amount = float(value)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if abs(amount) < 1024 or unit == "TiB":
            return f"{amount:.0f}{unit}" if unit == "B" else f"{amount:.1f}{unit}"
        amount /= 1024
    return "--"
