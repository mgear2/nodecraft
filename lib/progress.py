from __future__ import annotations

import sys
from collections.abc import Callable


class Progress:
    """Small stderr reporter that stays readable in terminals and CI logs."""

    def __init__(self, quiet: bool = False, stream=None):
        self.quiet = quiet
        self.stream = stream or sys.stderr

    def stage(self, message: str) -> None:
        if not self.quiet:
            print(f"[nodecraft] {message}", file=self.stream, flush=True)

    def items(self, label: str, total: int, callback: Callable[[int, int], None] | None = None):
        if self.quiet:
            return _ProgressItems(None, total, callback)
        return _ProgressItems(self, total, callback, label)


class _ProgressItems:
    def __init__(self, reporter, total, callback, label: str = ""):
        self.reporter = reporter
        self.total = total
        self.callback = callback
        self.label = label
        self.current = 0
        self.bytes = 0
        self.files = 0

    def update(self, *, bytes_count: int = 0, is_file: bool = False) -> None:
        self.current += 1
        self.bytes += bytes_count
        self.files += int(is_file)
        if self.callback:
            self.callback(self.current, self.total)
        if self.reporter:
            details = f", {self.files} files, {_format_bytes(self.bytes)}"
            print(
                f"\r[nodecraft] {self.label}: {self.current}/{self.total}{details}",
                end="",
                file=self.reporter.stream,
                flush=True,
            )

    def finish(self) -> None:
        if self.reporter:
            print(file=self.reporter.stream)


def _format_bytes(value: int) -> str:
    units = ("B", "KB", "MB", "GB", "TB")
    amount = float(value)
    for unit in units:
        if amount < 1024 or unit == units[-1]:
            return f"{amount:.1f} {unit}" if unit != "B" else f"{int(amount)} B"
        amount /= 1024
    raise AssertionError("unreachable")
