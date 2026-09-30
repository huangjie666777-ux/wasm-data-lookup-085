"""Concurrency cap, immediate load shedding, and isolation tests."""

from __future__ import annotations

import asyncio
import base64
from contextlib import asynccontextmanager

import httpx

from .conftest import b64
from sandbox_service.config import SETTINGS


@asynccontextmanager
async def lifespan_client():
    from sandbox_service.app import app

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as client:
        # Drive FastAPI lifespan so the supervisor exists and shutdown runs.
        started = asyncio.Event()
        scope = {"type": "lifespan"}

        async def receive():
            if not started.is_set():
                started.set()
                return {"type": "lifespan.startup"}
            await asyncio.Event().wait()
            return {"type": "lifespan.shutdown"}

        sent: list[dict] = []

        async def send(message: dict):
            sent.append(message)

        task = asyncio.create_task(app(scope, receive, send))
        await started.wait()
        await asyncio.sleep(0)
        try:
            yield client
        finally:
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass


async def _post_async(client: httpx.AsyncClient, wasm_bytes: bytes, budget=None):
    payload = {"module": b64(wasm_bytes), "stdin": b64(b"")}
    if budget:
        payload["budget"] = budget
    return await client.post("/execute", json=payload)


def test_full_load_rejected_immediately(wasm):
    """More than max_concurrency long jobs in flight => 503 without queueing."""
    busy = wasm("busy_loop")

    async def scenario():
        async with lifespan_client() as client:
            tasks = [
                asyncio.create_task(
                    _post_async(client, busy, budget={"timeout_ms": 800, "fuel": 10_000_000_000})
                )
                for _ in range(SETTINGS.max_concurrency + 3)
            ]
            responses = await asyncio.gather(*tasks, return_exceptions=False)
            return responses

    responses = asyncio.run(scenario())
    statuses = [r.status_code for r in responses]
    assert 503 in statuses
    # Every long-running request that started eventually returns a bounded result.
    assert all(s in (200, 503) for s in statuses)
    bodies = [r.json() for r in responses if r.status_code == 200]
    assert all(b["status"] == "resource_exhausted" for b in bodies)


def test_isolation_between_requests(wasm):
    """Two concurrent transforms must not share memory or captured output."""
    module = wasm("uppercase")

    async def scenario():
        async with lifespan_client() as client:
            async def one(data: bytes):
                payload = {"module": b64(module), "stdin": b64(data)}
                return await client.post("/execute", json=payload)

            results = await asyncio.gather(one(b"aaaa"), one(b"bbbb"), one(b"cccc"))
            return results

    responses = asyncio.run(scenario())
    outs = {base64.b64decode(r.json()["stdout"]) for r in responses}
    assert outs == {b"AAAA", b"BBBB", b"CCCC"}


def test_client_disconnect_releases_slot(wasm):
    """Cancelling a waiting request must not leak an execution slot."""
    busy = wasm("busy_loop")

    async def scenario():
        async with lifespan_client() as client:
            # Fill all slots with long-running jobs.
            fill = [
                asyncio.create_task(
                    _post_async(client, busy, budget={"timeout_ms": 2_000, "fuel": 10_000_000_000})
                )
                for _ in range(SETTINGS.max_concurrency)
            ]
            await asyncio.sleep(0.15)
            health = await client.get("/health")
            assert health.json()["active_executions"] == SETTINGS.max_concurrency

            # Cancel everything (client disconnect).
            for task in fill:
                task.cancel()
            await asyncio.gather(*fill, return_exceptions=True)

            # Give the supervisor a brief moment to reap slots via epochs.
            for _ in range(50):
                await asyncio.sleep(0.05)
                health = await client.get("/health")
                if health.json()["active_executions"] == 0:
                    break
            assert health.json()["active_executions"] == 0

    asyncio.run(scenario())
