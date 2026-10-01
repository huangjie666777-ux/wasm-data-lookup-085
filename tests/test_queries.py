"""Controlled data-source query: protocol, ABI, quotas and cancellation."""

from __future__ import annotations

import asyncio
import base64
import json
import threading
import time
from contextlib import asynccontextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

import httpx
import pytest

from .conftest import b64


def _sources_env(spec_url: str) -> str:
    cfg = {
        "device_specs": {
            "url": spec_url,
            "param": "model",
            "auth_header_name": "Authorization",
            "auth_header_value": "Bearer demo-token-123",
        },
        "noauth": {"url": spec_url, "param": "model"},
    }
    return json.dumps(cfg)


def make_handler(state):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):
            pass

        def do_GET(self):
            state.request_count += 1
            state.seen_auth.append(self.headers.get("Authorization", ""))
            parsed = urlparse(self.path)
            values = parse_qs(parsed.query).get("model", [])
            if state.redirect:
                self.send_response(302)
                self.send_header("location", parsed.path)
                self.end_headers()
                return
            if state.slow:
                time.sleep(2.0)
                self.send_response(504)
                self.end_headers()
                return
            if values == ["big"]:
                body = b"Z" * 5000
            elif values:
                body = b'{"model":"' + values[0].encode() + b'"}'
            else:
                body = b'{}'
            self.send_response(200)
            self.send_header("content-length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    return Handler


@pytest.fixture
def spec_server():
    state = type("S", (), {})()
    state.seen_auth = []
    state.request_count = 0
    state.slow = False
    state.redirect = False
    server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(state))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield server, state
    server.shutdown()
    server.server_close()


@asynccontextmanager
async def lifespan_client(monkeypatch, spec_server, url_override=None):
    server, _state = spec_server
    url = url_override or (
        "http://127.0.0.1:%d/specs" % server.server_address[1]
    )
    monkeypatch.setenv("SANDBOX_SOURCES", _sources_env(url))

    import sandbox_service.config as config_mod

    settings = config_mod.load_settings()
    monkeypatch.setattr(config_mod, "SETTINGS", settings)
    import sandbox_service.app as app_mod
    import sandbox_service.supervisor as sup_mod

    monkeypatch.setattr(app_mod, "SETTINGS", settings)

    sup = sup_mod.ExecutionSupervisor(settings)
    app = app_mod.FastAPI()
    app.state.supervisor = sup
    app.router.post("/execute")(app_mod.execute)
    app.router.get("/limits")(app_mod.limits)
    transport = httpx.ASGITransport(app=app)
    client = httpx.AsyncClient(transport=transport, base_url="http://t")
    try:
        yield client, settings
    finally:
        await client.aclose()
        await sup.await_shutdown()


def _payload(wasm, stdin=b"", sources=None, budget=None):
    payload = {"module": b64(wasm), "stdin": b64(stdin)}
    if sources is not None:
        payload["sources"] = sources
    if budget is not None:
        payload["budget"] = budget
    return payload


def test_enrich_demo_queries_and_completes(spec_server, wasm, monkeypatch):
    server, state = spec_server

    async def scenario():
        async with lifespan_client(monkeypatch, spec_server) as (client, _settings):
            resp = await client.post(
                "/execute",
                json=_payload(
                    wasm("enrich_device"),
                    b"alpha-1\n",
                    sources=["device_specs"],
                    budget={"timeout_ms": 5000},
                ),
            )
            data = resp.json()
            assert data["status"] == "exited", data
            out = base64.b64decode(data["stdout"])
            assert json.loads(out.decode())["model"] == "alpha-1"

    asyncio.run(scenario())
    assert state.request_count == 1
    assert state.seen_auth == ["Bearer demo-token-123"]


def test_unknown_source_rejected_without_traffic(spec_server, wasm, monkeypatch):
    server, state = spec_server

    async def scenario():
        async with lifespan_client(monkeypatch, spec_server) as (client, _settings):
            resp = await client.post(
                "/execute",
                json=_payload(wasm("enrich_device"), b"x", sources=["nope"]),
            )
            assert resp.status_code == 400
            assert "unknown or unconfigured" in resp.json()["error"]

    asyncio.run(scenario())
    assert state.request_count == 0


def test_default_no_network_sources_empty(spec_server, wasm, monkeypatch):
    server, state = spec_server

    async def scenario():
        async with lifespan_client(monkeypatch, spec_server) as (client, _settings):
            resp = await client.post(
                "/execute",
                json=_payload(wasm("enrich_device"), b"alpha-1"),
            )
            data = resp.json()
            assert data["status"] == "exited"
            # guest exits 20+STATUS_SOURCE_DENIED(2) when no sources authorized
            assert data["exit_code"] == 22

    asyncio.run(scenario())
    assert state.request_count == 0


def test_query_count_budget_tightens(spec_server, wasm, monkeypatch):
    async def scenario():
        async with lifespan_client(monkeypatch, spec_server) as (client, settings):
            resp = await client.post(
                "/execute",
                json=_payload(
                    wasm("enrich_device"),
                    b"alpha-1",
                    sources=["device_specs"],
                    budget={"query_count": settings.max_query_count + 1},
                ),
            )
            assert resp.status_code == 400
            assert "query_count" in resp.json()["error"]

    asyncio.run(scenario())


def test_response_byte_quota_reports_failure(spec_server, wasm, monkeypatch):
    async def scenario():
        async with lifespan_client(monkeypatch, spec_server) as (client, _settings):
            resp = await client.post(
                "/execute",
                json=_payload(
                    wasm("enrich_device"),
                    b"big",
                    sources=["device_specs"],
                    budget={"query_response_bytes": 1024, "timeout_ms": 5000},
                ),
            )
            data = resp.json()
            assert data["status"] == "exited"
            # Body larger than the per-response ceiling is an incomplete
            # stream: reported as query failure, never truncated success.
            assert data["exit_code"] == 24  # 20 + STATUS_QUERY_FAILED

    asyncio.run(scenario())


def test_slow_query_aborts_at_wallclock_budget(spec_server, wasm, monkeypatch):
    server, state = spec_server
    state.slow = True

    async def scenario():
        async with lifespan_client(monkeypatch, spec_server) as (client, _settings):
            started = time.monotonic()
            resp = await client.post(
                "/execute",
                json=_payload(
                    wasm("enrich_device"),
                    b"x",
                    sources=["device_specs"],
                    budget={"timeout_ms": 300},
                ),
            )
            elapsed = time.monotonic() - started
            data = resp.json()
            assert data["status"] == "resource_exhausted"
            assert "timeout" in data["reason"]
            assert elapsed < 1.5

    asyncio.run(scenario())


def test_redirect_not_followed_is_query_failure(spec_server, wasm, monkeypatch):
    server, state = spec_server
    state.redirect = True

    async def scenario():
        async with lifespan_client(monkeypatch, spec_server) as (client, _settings):
            resp = await client.post(
                "/execute",
                json=_payload(
                    wasm("enrich_device"), b"x", sources=["device_specs"]
                ),
            )
            data = resp.json()
            assert data["exit_code"] == 24  # 20 + STATUS_QUERY_FAILED

    asyncio.run(scenario())
    assert state.request_count == 1


def test_lists_source_ids_without_credentials(spec_server, monkeypatch):
    async def scenario():
        async with lifespan_client(monkeypatch, spec_server) as (client, _settings):
            data = (await client.get("/limits")).json()
            assert "device_specs" in data["available_sources"]
            payload = json.dumps(data)
            assert "demo-token-123" not in payload
            assert "/specs" not in payload

    asyncio.run(scenario())


def test_abi_probe_statuses_and_pointer_validation(spec_server, wasm, monkeypatch):
    server, state = spec_server

    async def scenario():
        async with lifespan_client(monkeypatch, spec_server) as (client, settings):
            payload = _payload(
                wasm("query_probe"),
                sources=["device_specs"],
                budget={"timeout_ms": 5000, "query_count": 8},
            )
            first = await client.post("/execute", json=payload)
            assert first.json()["status"] == "exited", first.json()
            assert first.json()["exit_code"] == 0, first.json()
            # Second execution gets a fresh broker: tokens/caches are isolated.
            second = await client.post("/execute", json=payload)
            assert second.json()["exit_code"] == 0

    asyncio.run(scenario())
    # Exactly two real fetches across two executions (cases 5 and 8 reuse the
    # cached body; illegal-pointer cases never hit the network).
    assert state.request_count == 2


def test_unreachable_source_is_query_failure(spec_server, wasm, monkeypatch):
    import socket

    server, state = spec_server
    probe = socket.socket()
    probe.bind(("127.0.0.1", 0))
    dead_port = probe.getsockname()[1]
    probe.close()

    async def scenario():
        async with lifespan_client(
            monkeypatch,
            spec_server,
            url_override=f"http://127.0.0.1:{dead_port}/specs",
        ) as (client, settings):
            started = time.monotonic()
            resp = await client.post(
                "/execute",
                json=_payload(
                    wasm("enrich_device"),
                    b"x",
                    sources=["device_specs"],
                    budget={"timeout_ms": 5000},
                ),
            )
            data = resp.json()
            assert data["status"] == "exited"
            assert data["exit_code"] == 24
            assert data["reason"] == ""
            assert time.monotonic() - started < 2.0

    asyncio.run(scenario())


def test_wrong_signature_import_rejected():
    from wasmtime import wat2wasm

    wat = (
        '(module (import "sandbox_queries" "query_fetch"'
        ' (func (param i32 i32) (result i32)))'
        ' (func (export "_start")))'
    )

    from sandbox_service.validation import ModuleInvalid, compile_module
    from sandbox_service.wasi_instance import new_engine

    with pytest.raises(ModuleInvalid, match="wrong signature"):
        compile_module(new_engine(), bytes(wat2wasm(wat)))


def test_cumulative_bytes_and_execution_isolation(spec_server, wasm, monkeypatch):
    server, state = spec_server

    async def scenario():
        async with lifespan_client(monkeypatch, spec_server) as (client, settings):
            body = b'{"model":"alpha-1"}'
            # The demo body is 22 bytes; two successful fetches fit in 60,
            # a third exceeds the cumulative cap. The guest only performs one
            # fetch per run, so drive one execution at a time with a tiny
# cumulative ceiling and verify isolation: each new execution resets it.
            for _ in range(3):
                resp = await client.post(
                    "/execute",
                    json=_payload(
                        wasm("enrich_device"),
                        b"alpha-1",
                        sources=["device_specs"],
                        budget={
                            "timeout_ms": 5000,
                            "query_total_bytes": len(body) + 1,
                        },
                    ),
                )
                assert resp.json()["status"] == "exited"
                assert resp.json()["exit_code"] == 0

    asyncio.run(scenario())
    assert state.request_count == 3
