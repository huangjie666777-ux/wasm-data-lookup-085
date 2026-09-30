"""Bounded in-memory capture of guest stdout/stderr."""

from __future__ import annotations


class OutputLimit(Exception):
    """Raised from inside a host call once the total output budget is exceeded.

    This propagates as a real wasm trap and terminates execution; the already
    captured prefix is preserved and reported as a truncated result.
    """


class StreamCapture:
    """Collects stdout and stderr under one shared byte ceiling."""

    def __init__(self, max_total: int) -> None:
        self._max_total = max_total
        self._total = 0
        self._stdout = bytearray()
        self._stderr = bytearray()
        self.truncated = False

    def write(self, target: bytearray, data: bytes) -> int:
        """Append up to the remaining budget; mark/raise on overflow.

        Returns the number of bytes actually accepted (a short write signals
        truncation to the guest). Raises OutputLimit immediately after a write
        that crosses the boundary, so runaway output is terminated for real.
        """
        remaining = self._max_total - self._total
        if remaining <= 0:
            self.truncated = True
            raise OutputLimit("combined output byte limit exceeded")
        take = min(len(data), remaining)
        target.extend(data[:take])
        self._total += take
        if take < len(data):
            self.truncated = True
            raise OutputLimit("combined output byte limit exceeded")
        return take

    def write_stdout(self, data: bytes) -> int:
        return self.write(self._stdout, data)

    def write_stderr(self, data: bytes) -> int:
        return self.write(self._stderr, data)

    @property
    def stdout(self) -> bytes:
        return bytes(self._stdout)

    @property
    def stderr(self) -> bytes:
        return bytes(self._stderr)
