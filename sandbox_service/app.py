"""HTTP API for running third-party wasm data-transformation programs."""

from __future__ import annotations

import base64

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from pydantic import ValidationError

from .config import SETTINGS
from .models import ExecuteRequest, RequestError, ResolvedBudget, resolve_request
from .supervisor import ExecutionSupervisor, LoadShedded, lifespan
from .validation import ModuleInvalid, compile_module
from .wasi_instance import ExecutionOutcome, new_engine

app = FastAPI(
    title="Wasm Execution Sandbox",
    version="1.0.0",
    description="Pure-backend HTTP service that runs isolated WASI preview1 modules.",
    lifespan=lifespan,
)


def _json_error(status_code: int, error: str, detail=None) -> JSONResponse:
    payload = {"status": "input_error", "error": error}
    if detail is not None:
        payload["detail"] = detail
    return JSONResponse(status_code=status_code, content=payload)


@app.exception_handler(RequestError)
async def request_error_handler(_request: Request, exc: RequestError) -> JSONResponse:
    return _json_error(400, str(exc))


@app.exception_handler(ValidationError)
async def validation_error_handler(_request: Request, exc: ValidationError) -> JSONResponse:
    return _json_error(400, "malformed request body", exc.errors())


@app.get("/health")
async def health(request: Request) -> dict:
    supervisor: ExecutionSupervisor = request.app.state.supervisor
    return {
        "status": "ok",
        "active_executions": supervisor.active_count,
        "max_concurrency": SETTINGS.max_concurrency,
    }


@app.get("/limits")
async def limits() -> dict:
    """Report the server-side ceilings; request budgets may only tighten these."""
    return {
        "default_fuel": SETTINGS.default_fuel,
        "max_fuel": SETTINGS.max_fuel,
        "default_timeout_ms": SETTINGS.default_timeout_ms,
        "max_timeout_ms": SETTINGS.max_timeout_ms,
        "default_memory_bytes": SETTINGS.default_memory_bytes,
        "max_memory_bytes": SETTINGS.max_memory_bytes,
        "default_output_bytes": SETTINGS.default_output_bytes,
        "max_output_bytes": SETTINGS.max_output_bytes,
        "default_query_count": SETTINGS.default_query_count,
        "max_query_count": SETTINGS.max_query_count,
        "default_query_response_bytes": SETTINGS.default_query_response_bytes,
        "max_query_response_bytes": SETTINGS.max_query_response_bytes,
        "default_query_total_bytes": SETTINGS.default_query_total_bytes,
        "max_query_total_bytes": SETTINGS.max_query_total_bytes,
        "available_sources": sorted(source.source_id for source in SETTINGS.sources),
        "max_module_bytes": SETTINGS.max_module_bytes,
        "max_stdin_bytes": SETTINGS.max_stdin_bytes,
        "max_concurrency": SETTINGS.max_concurrency,
    }


def _outcome_payload(outcome: ExecutionOutcome, budget: ResolvedBudget) -> dict:
    response = {
        "status": outcome.status,
        "exit_code": outcome.exit_code,
        "stdout": base64.b64encode(outcome.stdout).decode("ascii"),
        "stderr": base64.b64encode(outcome.stderr).decode("ascii"),
        "stdout_truncated": outcome.truncated_output,
        "reason": outcome.reason,
        "fuel_consumed": outcome.fuel_consumed,
        "budget": budget.model_dump(),
    }
    return response


@app.post("/execute")
async def execute(request: Request) -> JSONResponse:
    supervisor: ExecutionSupervisor = request.app.state.supervisor

    try:
        body = await request.json()
    except Exception:
        return _json_error(400, "request body must be JSON")

    try:
        req = ExecuteRequest.model_validate(body)
    except ValidationError as exc:
        return _json_error(400, "malformed request body", exc.errors())

    try:
        module_bytes, stdin_bytes, budget, argv, allowed_sources = resolve_request(req, SETTINGS)
    except RequestError as exc:
        return _json_error(400, str(exc))

    source_map = {
        source.source_id: source
        for source in SETTINGS.sources
        if source.source_id in allowed_sources
    }

    # Static validation + compilation happens before taking a concurrency slot.
    # Compilation failures are input errors and never consume execution slots.
    # A throwaway engine is used; the worker compiles again on its own engine.
    try:
        compile_module(new_engine(), module_bytes)
    except ModuleInvalid as exc:
        return _json_error(400, str(exc))

    if supervisor.active_count >= SETTINGS.max_concurrency:
        return JSONResponse(
            status_code=503,
            content={
                "status": "unavailable",
                "error": "execution capacity full, retry later",
                "active_executions": supervisor.active_count,
                "max_concurrency": SETTINGS.max_concurrency,
            },
        )

    try:
        outcome = await supervisor.execute(
            module_bytes=module_bytes,
            stdin=stdin_bytes,
            budget=budget,
            argv=argv,
            source_map=source_map,
        )
    except LoadShedded:
        # Lost a race for the last slot between the check above and acquisition.
        return JSONResponse(
            status_code=503,
            content={
                "status": "unavailable",
                "error": "execution capacity full, retry later",
            },
        )

    return JSONResponse(status_code=200, content=_outcome_payload(outcome, budget))
