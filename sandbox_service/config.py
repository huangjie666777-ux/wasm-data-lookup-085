"""Server-wide execution limits. Central place for all default/max budgets."""

from __future__ import annotations

import os
from dataclasses import dataclass


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    return int(raw)


@dataclass(frozen=True)
class Settings:
    # Fuel (instruction budget). Default applies unless the request tightens it.
    default_fuel: int = 10_000_000_000
    max_fuel: int = 10_000_000_000

    # Wall-clock execution time per request, milliseconds.
    default_timeout_ms: int = 2_000
    max_timeout_ms: int = 5_000

    # Linear memory ceiling, bytes.
    default_memory_bytes: int = 16 * 1024 * 1024
    max_memory_bytes: int = 64 * 1024 * 1024

    # Combined stdout + stderr byte ceiling.
    default_output_bytes: int = 1 * 1024 * 1024
    max_output_bytes: int = 4 * 1024 * 1024

    # Max accepted request payload pieces, bytes.
    max_module_bytes: int = 16 * 1024 * 1024
    max_stdin_bytes: int = 4 * 1024 * 1024

    # Concurrency. Full load is rejected immediately (no waiting queue).
    max_concurrency: int = 8

    # Extra grace period after an epoch bump before the watchdog bumps again,
    # and how long shutdown waits for in-flight executions, milliseconds.
    timer_tick_ms: int = 50
    shutdown_grace_ms: int = 1_000


def load_settings() -> Settings:
    return Settings(
        default_fuel=_env_int("SANDBOX_DEFAULT_FUEL", Settings.default_fuel),
        max_fuel=_env_int("SANDBOX_MAX_FUEL", Settings.max_fuel),
        default_timeout_ms=_env_int("SANDBOX_DEFAULT_TIMEOUT_MS", Settings.default_timeout_ms),
        max_timeout_ms=_env_int("SANDBOX_MAX_TIMEOUT_MS", Settings.max_timeout_ms),
        default_memory_bytes=_env_int("SANDBOX_DEFAULT_MEMORY_BYTES", Settings.default_memory_bytes),
        max_memory_bytes=_env_int("SANDBOX_MAX_MEMORY_BYTES", Settings.max_memory_bytes),
        default_output_bytes=_env_int("SANDBOX_DEFAULT_OUTPUT_BYTES", Settings.default_output_bytes),
        max_output_bytes=_env_int("SANDBOX_MAX_OUTPUT_BYTES", Settings.max_output_bytes),
        max_module_bytes=_env_int("SANDBOX_MAX_MODULE_BYTES", Settings.max_module_bytes),
        max_stdin_bytes=_env_int("SANDBOX_MAX_STDIN_BYTES", Settings.max_stdin_bytes),
        max_concurrency=_env_int("SANDBOX_MAX_CONCURRENCY", Settings.max_concurrency),
    )


SETTINGS = load_settings()
