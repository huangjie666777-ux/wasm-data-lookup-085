"""HTTP protocol models and request-level budget validation."""

from __future__ import annotations

import base64
import binascii
from typing import Optional

from pydantic import BaseModel, Field

from .config import Settings

WASI_MODULE = "wasi_snapshot_preview1"
SANDBOX_MODULE = "sandbox_queries"

# Host-provided controlled data-source query ABI (v1).
ALLOWED_SANDBOX_QUERY_IMPORTS = frozenset({"query_fetch", "query_read"})

# Canonical WASI preview1 function set. Only imports from this module with
# these exact function names are permitted.
ALLOWED_WASI_IMPORTS = frozenset(
    {
        "args_get",
        "args_sizes_get",
        "environ_get",
        "environ_sizes_get",
        "clock_res_get",
        "clock_time_get",
        "fd_advise",
        "fd_allocate",
        "fd_close",
        "fd_datasync",
        "fd_fdstat_get",
        "fd_fdstat_set_flags",
        "fd_fdstat_set_rights",
        "fd_filestat_get",
        "fd_filestat_set_size",
        "fd_filestat_set_times",
        "fd_pread",
        "fd_prestat_dir_name",
        "fd_prestat_get",
        "fd_pwrite",
        "fd_read",
        "fd_readdir",
        "fd_renumber",
        "fd_seek",
        "fd_sync",
        "fd_tell",
        "fd_write",
        "path_create_directory",
        "path_filestat_get",
        "path_filestat_set_times",
        "path_link",
        "path_open",
        "path_readlink",
        "path_remove_directory",
        "path_rename",
        "path_symlink",
        "path_unlink_file",
        "poll_oneoff",
        "proc_exit",
        "proc_raise",
        "random_get",
        "sched_yield",
        "sock_accept",
        "sock_recv",
        "sock_send",
        "sock_shutdown",
    }
)


class Budget(BaseModel):
    # All fields optional: omitted values fall back to server defaults.
    # Values may only tighten (reduce) the server-side maximums.
    fuel: Optional[int] = Field(default=None, ge=1)
    timeout_ms: Optional[int] = Field(default=None, ge=1)
    memory_bytes: Optional[int] = Field(default=None, ge=1, alias="memory_bytes")
    output_bytes: Optional[int] = Field(default=None, ge=0)

    # Controlled data-source query limits; all optional and only tightenable.
    query_count: Optional[int] = Field(default=None, ge=0)
    query_response_bytes: Optional[int] = Field(default=None, ge=1)
    query_total_bytes: Optional[int] = Field(default=None, ge=0)

    model_config = {"populate_by_name": True}


class ExecuteRequest(BaseModel):
    # Base64-encoded wasm module bytes and stdin. Standard base64 is accepted
    # (with or without padding); raw JSON unicode is not used for binary data.
    module: str = Field(min_length=1)
    stdin: str = Field(default="")
    budget: Budget = Field(default_factory=Budget)
    argv: list[str] = Field(default_factory=list, max_length=64)
    # Server-side source ids this execution may query. The guest never supplies
    # URLs or credentials; an unknown/unauthorized id is rejected before run.
    sources: list[str] = Field(default_factory=list, max_length=16)


class RequestError(ValueError):
    """Input validation failure reported with HTTP 400."""


class ResolvedBudget(BaseModel):
    fuel: int
    timeout_ms: int
    memory_bytes: int
    output_bytes: int
    query_count: int
    query_response_bytes: int
    query_total_bytes: int


def _b64decode(field_name: str, value: str, max_bytes: int) -> bytes:
    try:
        raw = base64.b64decode(value, validate=True)
    except (binascii.Error, ValueError):
        raise RequestError(f"{field_name} must be valid base64")
    if len(raw) > max_bytes:
        raise RequestError(
            f"{field_name} decodes to {len(raw)} bytes, limit is {max_bytes}"
        )
    return raw


def resolve_request(
    req: ExecuteRequest, settings: Settings
) -> tuple[bytes, bytes, ResolvedBudget, list[str], frozenset[str]]:
    module_bytes = _b64decode("module", req.module, settings.max_module_bytes)
    stdin_bytes = _b64decode("stdin", req.stdin, settings.max_stdin_bytes)

    budget = req.budget

    fuel = budget.fuel if budget.fuel is not None else settings.default_fuel
    if fuel > settings.max_fuel:
        raise RequestError(f"budget.fuel may not exceed {settings.max_fuel}")

    timeout_ms = budget.timeout_ms if budget.timeout_ms is not None else settings.default_timeout_ms
    if timeout_ms > settings.max_timeout_ms:
        raise RequestError(f"budget.timeout_ms may not exceed {settings.max_timeout_ms}")

    memory_bytes = (
        budget.memory_bytes if budget.memory_bytes is not None else settings.default_memory_bytes
    )
    if memory_bytes > settings.max_memory_bytes:
        raise RequestError(f"budget.memory_bytes may not exceed {settings.max_memory_bytes}")

    output_bytes = (
        budget.output_bytes if budget.output_bytes is not None else settings.default_output_bytes
    )
    if output_bytes > settings.max_output_bytes:
        raise RequestError(f"budget.output_bytes may not exceed {settings.max_output_bytes}")

    query_count = (
        budget.query_count if budget.query_count is not None else settings.default_query_count
    )
    if query_count > settings.max_query_count:
        raise RequestError(f"budget.query_count may not exceed {settings.max_query_count}")

    query_response_bytes = (
        budget.query_response_bytes
        if budget.query_response_bytes is not None
        else settings.default_query_response_bytes
    )
    if query_response_bytes > settings.max_query_response_bytes:
        raise RequestError(
            f"budget.query_response_bytes may not exceed {settings.max_query_response_bytes}"
        )

    query_total_bytes = (
        budget.query_total_bytes
        if budget.query_total_bytes is not None
        else settings.default_query_total_bytes
    )
    if query_total_bytes > settings.max_query_total_bytes:
        raise RequestError(
            f"budget.query_total_bytes may not exceed {settings.max_query_total_bytes}"
        )

    # Authorize requested sources against the server-side configuration. No
    # source id means default-off networking; an unknown id is an input error
    # and must never trigger any request.
    configured = {source.source_id for source in settings.sources}
    allowed: set[str] = set()
    for source_id in req.sources:
        if source_id not in configured:
            raise RequestError(f"unknown or unconfigured data source: {source_id}")
        allowed.add(source_id)

    argv = ["program", *req.argv]
    return module_bytes, stdin_bytes, ResolvedBudget(
        fuel=fuel,
        timeout_ms=timeout_ms,
        memory_bytes=memory_bytes,
        output_bytes=output_bytes,
        query_count=query_count,
        query_response_bytes=query_response_bytes,
        query_total_bytes=query_total_bytes,
    ), argv, frozenset(allowed)
