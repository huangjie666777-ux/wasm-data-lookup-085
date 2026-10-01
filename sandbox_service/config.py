"""Server-wide execution limits. Central place for all default/max budgets."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    return int(raw)


@dataclass(frozen=True)
class SourceConfig:
    """A server-configured named data source.

    The address and optional authentication headers are fixed by the operator
    and are never accepted from execution requests or exposed to guests.
    """

    id: str
    url: str
    headers: Mapping[str, str]


def _parse_sources(raw: str, origin: str) -> tuple[SourceConfig, ...]:
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(f"invalid sources config in {origin}: {exc}") from exc
    if not isinstance(data, dict):
        raise ValueError(f"sources config in {origin} must be a JSON object")
    sources: list[SourceConfig] = []
    for source_id, spec in data.items():
        if not isinstance(source_id, str) or not source_id or len(source_id) > 64:
            raise ValueError("source id must be a non-empty string (<=64 chars)")
        if not isinstance(spec, dict) or not isinstance(spec.get("url"), str):
            raise ValueError(f"source '{source_id}' needs a string 'url'")
        url = spec["url"]
        if not (url.startswith("http://") or url.startswith("https://")):
            raise ValueError(f"source '{source_id}' url must be http(s)")
        headers = spec.get("headers", {})
        if not isinstance(headers, dict) or not all(
            isinstance(k, str) and isinstance(v, str) and k and v
            for k, v in headers.items()
        ):
            raise ValueError(f"source '{source_id}' headers must be string pairs")
        sources.append(SourceConfig(id=source_id, url=url, headers=dict(headers)))
    return tuple(sources)


def _load_sources() -> tuple[SourceConfig, ...]:
    raw = os.environ.get("SANDBOX_SOURCES")
    if raw is not None and raw.strip():
        return _parse_sources(raw, "SANDBOX_SOURCES")
    path = Path(__file__).resolve().parent.parent / "sources.json"
    if path.exists():
        return _parse_sources(path.read_text(encoding="utf-8"), str(path))
    # Zero-config local source for the bundled demo; override with
    # SANDBOX_SOURCES or a repo-root sources.json in real deployments.
    return (
        SourceConfig(
            id="specs",
            url="http://127.0.0.1:8090/spec",
            headers={},
        ),
    )


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

    # Controlled data-source queries. Server-side ceilings; per-request
    # budgets may only tighten them. No outbound request happens unless the
    # request explicitly names a source the operator configured.
    default_max_queries: int = 4
    max_max_queries: int = 16
    default_query_response_bytes: int = 64 * 1024
    max_query_response_bytes: int = 1 * 1024 * 1024
    default_query_total_bytes: int = 256 * 1024
    max_query_total_bytes: int = 4 * 1024 * 1024

    # Named data sources configured server side (never client side).
    sources: tuple[SourceConfig, ...] = ()

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
        default_max_queries=_env_int("SANDBOX_DEFAULT_MAX_QUERIES", Settings.default_max_queries),
        max_max_queries=_env_int("SANDBOX_MAX_MAX_QUERIES", Settings.max_max_queries),
        default_query_response_bytes=_env_int(
            "SANDBOX_DEFAULT_QUERY_RESPONSE_BYTES", Settings.default_query_response_bytes
        ),
        max_query_response_bytes=_env_int(
            "SANDBOX_MAX_QUERY_RESPONSE_BYTES", Settings.max_query_response_bytes
        ),
        default_query_total_bytes=_env_int(
            "SANDBOX_DEFAULT_QUERY_TOTAL_BYTES", Settings.default_query_total_bytes
        ),
        max_query_total_bytes=_env_int(
            "SANDBOX_MAX_QUERY_TOTAL_BYTES", Settings.max_query_total_bytes
        ),
        sources=_load_sources(),
    )


SETTINGS = load_settings()
