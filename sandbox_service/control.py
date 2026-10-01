"""Per-execution cancellation control shared by the watchdog and host calls.

The wall-clock deadline is enforced from a separate watchdog thread. Fuel and
epoch interrupts stop tight guest loops, but a guest parked *inside* a blocking
host function (e.g. waiting on a network query) never reaches an interruption
point. Host functions therefore register an abort hook with this object; when
the deadline passes or the client disconnects / the server shuts down, the hook
aborts the in-flight operation for real instead of letting it run on.
"""

from __future__ import annotations

import threading
import time


class ExecutionControl:
    def __init__(self, timeout_seconds: float) -> None:
        self._stop_at = time.monotonic() + max(timeout_seconds, 0.0)
        self._cancelled = threading.Event()
        self._lock = threading.Lock()
        self._abort_hook = None
        self._reason = ""

    def remaining_seconds(self) -> float:
        """Seconds left on the wall-clock budget; <=0 means expired."""
        return self._stop_at - time.monotonic()

    def cancel(self, reason: str = "execution cancelled") -> None:
        """External cancellation (client disconnect / server shutdown)."""
        with self._lock:
            self._reason = reason
        self._cancelled.set()
        self._fire_hook()

    def expire(self) -> None:
        """Deadline reached: abort any blocked host call."""
        with self._lock:
            self._reason = "wall-clock timeout exceeded"
        self._cancelled.set()
        self._fire_hook()

    @property
    def reason(self) -> str:
        with self._lock:
            return self._reason

    def is_stopped(self) -> bool:
        return self._cancelled.is_set() or self.remaining_seconds() <= 0

    def set_abort_hook(self, hook) -> None:
        with self._lock:
            self._abort_hook = hook
        if self._cancelled.is_set():
            self._fire_hook()

    def clear_abort_hook(self) -> None:
        with self._lock:
            self._abort_hook = None

    def _fire_hook(self) -> None:
        with self._lock:
            hook = self._abort_hook
        if hook is not None:
            hook()
