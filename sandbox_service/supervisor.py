"""Execution supervision: concurrency cap, wall-clock watchdog, cancellation.

Design notes
------------
* Wasmtime executes synchronously and can only be interrupted from another
  thread via its engine epoch, so each execution runs in a bounded thread pool
  while an async request waits on its future.
* Active executions are counted with a semaphore. When the cap is reached the
  request is rejected immediately; no unbounded queue is formed.
* A dedicated watchdog thread per execution bumps the engine epoch on timeout
  or external cancellation (client disconnect / server shutdown). The real
  wasm execution traps then, so loops cannot keep running in the background.
* Slot release happens exactly once in the worker's `finally`, regardless of
  the outcome or of timeout/completion races.
"""

from __future__ import annotations

import asyncio
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from dataclasses import dataclass

from .config import SETTINGS, Settings
from .models import ResolvedBudget
from .wasi_instance import ExecutionOutcome, new_engine, run_module


class LoadShedded(Exception):
    """Raised when the service is already at its active-execution limit."""


@dataclass
class _Job:
    module_bytes: bytes
    stdin: bytes
    budget: ResolvedBudget
    argv: list[str]


class ExecutionSupervisor:
    def __init__(self, settings: Settings = SETTINGS) -> None:
        self._settings = settings
        # Exactly one worker thread per permitted active execution: with the
        # semaphore held before submission, the pool never queues work.
        self._pool = ThreadPoolExecutor(
            max_workers=settings.max_concurrency,
            thread_name_prefix="wasm-exec",
        )
        self._slots = threading.BoundedSemaphore(settings.max_concurrency)
        self._active = 0
        self._active_lock = threading.Lock()
        self._cancel_registry_lock = threading.Lock()
        self._cancel_callbacks: set[callable] = set()
        self._shutdown = threading.Event()

    @property
    def active_count(self) -> int:
        return self._active

    async def execute(
        self,
        module_bytes: bytes,
        stdin: bytes,
        budget: ResolvedBudget,
        argv: list[str],
    ) -> ExecutionOutcome:
        """Acquire a slot, run supervised, release once. Cancel-safe."""
        if not self._slots.acquire(blocking=False):
            raise LoadShedded()
        with self._active_lock:
            self._active += 1
        released = False

        def release_slot() -> None:
            nonlocal released
            if released:
                return
            released = True
            with self._active_lock:
                self._active -= 1
            self._slots.release()

        loop = asyncio.get_running_loop()
        cancel_event = threading.Event()
        finish_event = threading.Event()
        job = _Job(module_bytes, stdin, budget, argv)

        def cancel() -> None:
            cancel_event.set()

        with self._cancel_registry_lock:
            self._cancel_callbacks.add(cancel)

        def worker() -> ExecutionOutcome:
            try:
                engine = new_engine()
                deadline = budget.timeout_ms / 1000.0

                def watchdog() -> None:
                    tick = self._settings.timer_tick_ms / 1000.0
                    deadline_at = time.monotonic() + deadline
                    while True:
                        remaining = deadline_at - time.monotonic()
                        wait_for = min(tick, max(remaining, 0.0))
                        if finish_event.wait(wait_for):
                            return
                        if cancel_event.is_set():
                            engine._interrupt_reason = "execution cancelled"
                            break
                        if remaining <= 0:
                            engine._interrupt_reason = "wall-clock timeout exceeded"
                            break
                    engine.increment_epoch()
                    # Some host-call windows can slip past one check; give a
                    # short grace window then bump once more.
                    if finish_event.wait(tick):
                        return
                    engine.increment_epoch()

                watcher = threading.Thread(
                    target=watchdog, name="wasm-watchdog", daemon=True
                )
                watcher.start()
                try:
                    from .validation import compile_module

                    module = compile_module(engine, job.module_bytes)
                    return run_module(engine, module, job.stdin, job.budget, job.argv)
                finally:
                    finish_event.set()
                    watcher.join(timeout=self._settings.timer_tick_ms / 1000.0 + 0.1)
            finally:
                release_slot()

        try:
            future = loop.run_in_executor(self._pool, worker)
            return await future
        except asyncio.CancelledError:
            # Client went away or server is shutting down. Ask the execution
            # to trap for real rather than abandoning it in the background.
            cancel()
            # Do not await the future here: the worker still releases its slot
            # in its own finally; re-raising propagates the cancellation.
            raise
        finally:
            with self._cancel_registry_lock:
                self._cancel_callbacks.discard(cancel)

    def cancel_all(self) -> None:
        with self._cancel_registry_lock:
            callbacks = list(self._cancel_callbacks)
        for cancel in callbacks:
            cancel()

    def shutdown(self) -> None:
        self._shutdown.set()
        self.cancel_all()

    async def await_shutdown(self) -> None:
        """Signal cancellation, then wait for worker threads off the loop."""
        self.shutdown()
        await asyncio.get_running_loop().run_in_executor(None, self._pool.shutdown, True)


@asynccontextmanager
async def lifespan(app):
    supervisor = ExecutionSupervisor()
    app.state.supervisor = supervisor
    try:
        yield
    finally:
        await supervisor.await_shutdown()
