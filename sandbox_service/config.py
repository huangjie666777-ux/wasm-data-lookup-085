"""Server-wide execution limits. Central place for all default/max budgets."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from typing import Optional


@dataclass(frozen=True)
class ResourceSource:
    """A server-configured, named data source.

    The guest ever sees the source id; the fixed URL and optional auth header
    never leave the host.
    """

    source_id: str
    url: str
    # Optional header name/value attached to every GET. Never logged.
    auth_header_name: Optional[str]
    auth_header_value: str
    # Name of the single query parameter that carries the business key.
    param_name: str = "key"


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

    # Controlled data-source queries.
    default_query_count: int = 4
    max_query_count: int = 16
    default_query_response_bytes: int = 64 * 1024
    max_query_response_bytes: int = 1024 * 1024
    default_query_total_bytes: int = 256 * 1024
    max_query_total_bytes: int = 4 * 1024 * 1024
    # Max UTF-8 business key length accepted from a guest.
    max_query_key_bytes: int = 4096
    # Wall-clock grace (ms) a query gets after the overall deadline before it
    # must give up; normally query waits are bounded by the remaining budget.
    query_close_grace_ms: int = 250

    # Max accepted request payload pieces, bytes.
    max_module_bytes: int = 16 * 1024 * 1024
    max_stdin_bytes: int = 4 * 1024 * 1024

    # Concurrency. Full load is rejected immediately (no waiting queue).
    max_concurrency: int = 8

    # Extra grace period after an epoch bump before the watchdog bumps again,
    # and how long shutdown waits for in-flight executions, milliseconds.
    timer_tick_ms: int = 50
    shutdown_grace_ms: int = 1_000

    # Named data sources (id -> ResourceSource); empty means default no network.
    sources: tuple[ResourceSource, ...] = ()


def _parse_sources() -> tuple[ResourceSource, ...]:
    """Load named sources from SANDBOX_SOURCES JSON env var.

    Schema: a JSON object mapping id -> {
      "url": "http://...fixed...",
      "auth_header_name": "Authorization",   # optional
      "auth_header_value": "Bearer ...",      # optional; never exposed
      "param": "model"                          # optional, default "key"
    }
    Only http(s) URLs are accepted; configuration errors fail fast.
    """
    raw = os.environ.get("SANDBOX_SOURCES", "").strip()
    if not raw:
        return ()
    data = json.loads(raw)
    if not isinstance(data, dict):
        raise ValueError("SANDBOX_SOURCES must be a JSON object of id -> config")
    sources: list[ResourceSource] = []
    seen: set[str] = set()
    for source_id, cfg in data.items():
        if not isinstance(source_id, str) or not source_id or len(source_id) > 64:
            raise ValueError("source id must be a non-empty string (<=64 chars)")
        if source_id in seen:
            raise ValueError(f"duplicate source id {source_id!r}")
        seen.add(source_id)
        if not isinstance(cfg, dict):
            raise ValueError(f"source {source_id!r} config must be an object")
        url = cfg.get("url")
        if not isinstance(url, str) or not url.lower().startswith(("http://", "https://")):
            raise ValueError(f"source {source_id!r} url must be an http(s) URL")
        header_name = cfg.get("auth_header_name")
        header_value = cfg.get("auth_header_value", "")
        if header_name is not None and (
            not isinstance(header_name, str)
            or not header_name
            or not all(0x21 <= ord(c) <= 0x7E for c in header_name)
        ):
            raise ValueError(f"source {source_id!r} has invalid auth_header_name")
        if not isinstance(header_value, str):
            raise ValueError(f"source {source_id!r} auth_header_value must be a string")
        param = cfg.get("param", "key")
        if not isinstance(param, str) or not param or len(param) > 64:
            raise ValueError(f"source {source_id!r} param must be a short string")
        sources.append(
            ResourceSource(
                source_id=source_id,
                url=url,
                auth_header_name=header_name if header_name else None,
                auth_header_value=header_value,
                param_name=param,
            )
        )
    return tuple(sources)


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
        default_query_count=_env_int("SANDBOX_DEFAULT_QUERY_COUNT", Settings.default_query_count),
        max_query_count=_env_int("SANDBOX_MAX_QUERY_COUNT", Settings.max_query_count),
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
        sources=_parse_sources(),
    )


try:
    SETTINGS = load_settings()
except (ValueError, json.JSONDecodeError) as exc:  # fail fast on bad configuration
    raise RuntimeError(f"invalid sandbox configuration: {exc}") from exc
