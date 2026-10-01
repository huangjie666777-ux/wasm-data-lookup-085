"""Controlled data-source query: protocol, budgets, isolation and safety."""

from __future__ import annotations

import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from sandbox_service import config as config_mod
from sandbox_service import models as models_mod
from sandbox_service.config import SETTINGS, SourceConfig

from .conftest import b64


class _Handler(BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802
        q = self.path
        if q.startswith("/spec?") and getattr(self.server, "echo_spec", True):
            key = q.split("key=", 1)[-1]
            body = key.encode()
        elif q.startswith("/big?"):
            body = b"Z" * self.server.big_size
        elif q.startswith("/slow?"):
            time.sleep(3.0)
            body = b"late"
        elif q.startswith("/redir?"):
            self.send_response(302)
            self.send_header("Location", "/spec?key=ZZ")
            self.end_headers()
            return
        elif q.startswith("/secret?"):
            if self.headers.get("X-Api-Key") != "topsecret":
                self.send_error(401)
                return
            body = b"needs-auth"
        else:
            self.send_error(404)
            return
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        return


@pytest.fixture
def source_server():
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    server.request_count = 0
    server.big_size = 100_000
    orig_handle_one = server.RequestHandlerClass

    class Counting(orig_handle_one):
        def do_GET(self_inner):
            server.request_count += 1
            return super().do_GET()

    server.RequestHandlerClass = Counting
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()


def _make_settings(server, spec_path="/spec", spec_headers=None, extra=()):
    port = server.server_address[1]
    base = f"http://127.0.0.1:{port}"
    sources = [SourceConfig(id="specs", url=f"{base}{spec_path}",
                            headers=dict(spec_headers or {}))]
    sources.extend(extra)
    settings = object.__new__(type(SETTINGS))
    object.__setattr__(settings, "__dict__", {**SETTINGS.__dict__,
                                            "sources": tuple(sources)})
    return settings


def _install(monkeypatch, settings):
    monkeypatch.setattr(config_mod, "SETTINGS", settings)
    import sandbox_service.app as app_mod
    monkeypatch.setattr(app_mod, "SETTINGS", settings)
    from fastapi.testclient import TestClient
    return TestClient(app_mod.app)


@pytest.fixture
def configured_client(source_server, monkeypatch):
    settings = _make_settings(source_server)
    with _install(monkeypatch, settings) as client:
        yield client


def _client_for(source_server, monkeypatch, **kw):
    settings = _make_settings(source_server, **kw)
    return _install(monkeypatch, settings)


ENRICHER = "device_enricher"


def _execute(client, wasm_bytes, stdin=b"", budget=None, sources=None):
    payload = {"module": b64(wasm_bytes), "stdin": b64(stdin)}
    if budget is not None:
        payload["budget"] = budget
    if sources is not None:
        payload["allowed_sources"] = sources
    return client.post("/execute", json=payload)


def test_enrichment_completes_records(configured_client, wasm):
    client = configured_client
    resp = _execute(client, wasm(ENRICHER), b"TH-100\n", sources=["specs"])
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "exited"
    out = __import__("base64").b64decode(data["stdout"])
    assert out == b"TH-100\tTH-100\n"


def test_unknown_source_rejected_before_network(configured_client, source_server, wasm):
    client = configured_client
    resp = _execute(client, wasm(ENRICHER), b"TH-100\n", sources=["nope"])
    assert resp.status_code == 400
    assert "unknown data source" in resp.json()["error"]
    assert source_server.request_count == 0


def test_unauthorized_source_no_request(configured_client, source_server, wasm):
    client = configured_client
    resp = _execute(client, wasm(ENRICHER), b"TH-100\n", sources=[])
    import base64
    assert b"QUERY_FAILED:1" in base64.b64decode(resp.json()["stdout"])
    assert source_server.request_count == 0


def test_query_budget_can_only_tighten(configured_client, wasm):
    client = configured_client
    resp = _execute(
        client, wasm(ENRICHER), b"TH-100\n",
        budget={"max_queries": SETTINGS.max_max_queries + 1}, sources=["specs"],
    )
    assert resp.status_code == 400
    assert "max_queries" in resp.json()["error"]


def test_max_queries_enforced(configured_client, wasm):
    client = configured_client
    resp = _execute(
        client, wasm(ENRICHER), b"TH-100\nTH-200\n",
        budget={"max_queries": 1, "timeout_ms": 5000}, sources=["specs"],
    )
    import base64
    out = base64.b64decode(resp.json()["stdout"])
    assert b"QUERY_FAILED:6" in out


def test_single_response_byte_cap(source_server, monkeypatch, wasm):
    import base64
    with _client_for(source_server, monkeypatch, spec_path="/big") as client:
        resp = _execute(
            client, wasm(ENRICHER), b"TH-100\n",
            budget={"query_response_bytes": 100, "timeout_ms": 5000},
            sources=["specs"],
        )
        out = base64.b64decode(resp.json()["stdout"])
        assert b"QUERY_FAILED:7" in out

def test_body_buffer_too_small_is_distinct():
    from wasmtime import Engine, Linker, Module, Store, WasiConfig
    from sandbox_service.control import ExecutionControl
    from sandbox_service.models import QUERY_ERR_BAD_CAPACITY, QUERY_OK
    from sandbox_service.query import QueryState

    engine = Engine()
    module = Module(engine, '(module (memory (export "memory") 1))')
    store = Store(engine)
    store.set_wasi(WasiConfig())
    memory = Linker(engine).instantiate(store, module).exports(store)["memory"]
    state = QueryState({}, 1, 1000, 1000, ExecutionControl(5))
    state._responses[1] = b"abcdef"
    assert state.body(store, memory, 1, 100, 3, 200) == QUERY_ERR_BAD_CAPACITY
    assert state.body(store, memory, 1, 100, 6, 200) == QUERY_OK


WAT_BAD_PTR = r'''
(module
  (import "wasi_snapshot_preview1" "proc_exit" (func $e (param i32)))
  (import "sandbox" "query_fetch"
    (func $f (param i32 i32 i32 i32 i32) (result i32)))
  (memory (export "memory") 1)
  (data (i32.const 100) "specs")
  (func (export "_start")
    i32.const 100 i32.const 5
    i32.const 0x70000000 i32.const 3 i32.const 4
    call $f call $e))
'''

WAT_BAD_UTF8 = r'''
(module
  (import "wasi_snapshot_preview1" "proc_exit" (func $e (param i32)))
  (import "sandbox" "query_fetch"
    (func $f (param i32 i32 i32 i32 i32) (result i32)))
  (memory (export "memory") 1)
  (data (i32.const 100) "specs")
  (data (i32.const 200) "\ff")
  (func (export "_start")
    i32.const 100 i32.const 5
    i32.const 200 i32.const 1 i32.const 4
    call $f call $e))
'''


def test_bad_pointers_do_not_crash(configured_client, source_server):
    from wasmtime import wat2wasm
    client = configured_client
    resp = _execute(client, bytes(wat2wasm(WAT_BAD_PTR)), sources=["specs"])
    assert resp.status_code == 200
    assert resp.json()["exit_code"] == 3
    assert source_server.request_count == 0


def test_non_utf8_key_rejected_distinct_code(configured_client, source_server):
    from wasmtime import wat2wasm
    client = configured_client
    resp = _execute(client, bytes(wat2wasm(WAT_BAD_UTF8)), sources=["specs"])
    assert resp.json()["exit_code"] == 4
    assert source_server.request_count == 0


def test_redirects_not_followed(source_server, monkeypatch, wasm):
    import base64
    with _client_for(source_server, monkeypatch, spec_path="/redir") as client:
        resp = _execute(
            client, wasm(ENRICHER), b"TH-100\n",
            budget={"timeout_ms": 5000}, sources=["specs"],
        )
        assert b"QUERY_FAILED:5" in base64.b64decode(resp.json()["stdout"])

def test_query_waits_count_against_wallclock_budget(source_server, monkeypatch, wasm):
    with _client_for(source_server, monkeypatch, spec_path="/slow") as client:
        start = time.monotonic()
        resp = _execute(
            client, wasm(ENRICHER), b"TH-100\n",
            budget={"timeout_ms": 400, "fuel": 10_000_000_000},
            sources=["specs"],
        )
        elapsed = time.monotonic() - start
        data = resp.json()
        assert data["status"] == "resource_exhausted"
        assert "timeout" in data["reason"]
        assert elapsed < 2.0

def test_auth_header_used_but_never_exposed(source_server, monkeypatch, wasm):
    import base64
    with _client_for(source_server, monkeypatch, spec_path="/secret",
                     spec_headers={"X-Api-Key": "topsecret"}) as client:
        resp = _execute(
            client, wasm(ENRICHER), b"secret\n",
            budget={"timeout_ms": 5000}, sources=["specs"],
        )
        out = base64.b64decode(resp.json()["stdout"])
        assert b"\tneeds-auth\n" in out
        assert b"topsecret" not in out

def test_wrong_import_signature_rejected(configured_client):
    from wasmtime import wat2wasm
    client = configured_client
    wat = ('(module (import "sandbox" "query_fetch"'
           ' (func (param i64 i32 i32 i32 i32) (result i32)))'
           ' (func (export "_start")))')
    resp = _execute(client, bytes(wat2wasm(wat)), sources=["specs"])
    assert resp.status_code == 400
    assert "wrong signature" in resp.json()["error"]


def test_disconnect_during_query_aborts(source_server, monkeypatch, wasm):
    """A real disconnect must abort a query blocked on a slow upstream."""
    import asyncio
    import httpx

    with _client_for(source_server, monkeypatch, spec_path="/slow") as client:
        transport = httpx.ASGITransport(app=client.app)
        payload = {
            "module": b64(wasm(ENRICHER)),
            "stdin": b64(b"TH-100\n"),
            "allowed_sources": ["specs"],
            "budget": {"timeout_ms": 5000, "fuel": 10_000_000_000},
        }

        async def scenario():
            async with httpx.AsyncClient(
                transport=transport, base_url="http://t"
            ) as ac:
                task = asyncio.create_task(ac.post("/execute", json=payload))
                await asyncio.sleep(0.3)
                task.cancel()
                with pytest.raises((asyncio.CancelledError, httpx.HTTPError)):
                    await task

        asyncio.run(scenario())

        deadline = time.monotonic() + 3.0
        while time.monotonic() < deadline:
            health = client.get("/health").json()
            if health["active_executions"] == 0:
                break
            time.sleep(0.05)
        assert client.get("/health").json()["active_executions"] == 0


def test_cumulative_total_byte_cap_across_queries(configured_client, wasm):
    import base64
    client = configured_client
    # Echoed key body: "TH-100" is 7 bytes; total cap 8 leaves 1 byte for the
    # second query, so it must fail with RESPONSE_TOO_LARGE (7), not succeed.
    resp = _execute(
        client, wasm(ENRICHER), b"TH-100\nTH-200\n",
        budget={
            "query_total_bytes": 8,
            "query_response_bytes": 1024,
            "max_queries": 4,
            "timeout_ms": 5000,
        },
        sources=["specs"],
    )
    out = base64.b64decode(resp.json()["stdout"])
    assert b"TH-100\tTH-100\n" in out
    assert b"TH-200\tQUERY_FAILED:7\n" in out
