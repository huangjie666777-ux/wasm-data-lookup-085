"""End-to-end HTTP tests covering status classes and resource limits."""

from __future__ import annotations

import base64

from .conftest import b64


def _execute(client, wasm_bytes, stdin=b"", budget=None, argv=None):
    payload = {"module": b64(wasm_bytes), "stdin": b64(stdin)}
    if budget is not None:
        payload["budget"] = budget
    if argv is not None:
        payload["argv"] = argv
    return client.post("/execute", json=payload)


def _out(resp):
    data = resp.json()
    return (
        data,
        base64.b64decode(data["stdout"]),
        base64.b64decode(data["stderr"]),
    )


def test_uppercase_transform(client, wasm):
    resp = _execute(client, wasm("uppercase"), b"Hello, Wasm!\n")
    assert resp.status_code == 200
    data, out, err = _out(resp)
    assert data["status"] == "exited"
    assert data["exit_code"] == 0
    assert out == b"HELLO, WASM!\n"
    assert err == b""
    assert data["stdout_truncated"] is False


def test_nonzero_exit_code(client, wasm):
    resp = _execute(client, wasm("exits_nonzero"))
    data, _out_b, _err = _out(resp)
    assert resp.status_code == 200
    assert data["status"] == "exited"
    assert data["exit_code"] == 7



def test_wallclock_timeout_on_infinite_loop(client, wasm):
    resp = _execute(
        client,
        wasm("busy_loop"),
        budget={"timeout_ms": 200, "fuel": 10_000_000_000},
    )
    data, _, _ = _out(resp)
    assert resp.status_code == 200
    assert data["status"] == "resource_exhausted"
    assert "timeout" in data["reason"]



def test_fuel_exhaustion(client, wasm):
    resp = _execute(client, wasm("busy_loop"), budget={"fuel": 1_000, "timeout_ms": 5_000})
    data, _, _ = _out(resp)
    assert data["status"] == "resource_exhausted"
    assert "fuel" in data["reason"]


def test_memory_growth_capped(client, wasm):
    resp = _execute(
        client,
        wasm("memory_grower"),
        budget={"memory_bytes": 256 * 1024, "timeout_ms": 5_000, "fuel": 10_000_000},
    )
    data, _, _ = _out(resp)
    # memory.grow returns -1; the module then exits the loop and calls
    # proc_exit(0), so a clean exit is equally acceptable as an exhaustion.
    assert data["status"] in {"exited", "resource_exhausted"}


def test_output_byte_cap_truncates_and_terminates(client, wasm):
    resp = _execute(
        client,
        wasm("spam_output"),
        budget={"output_bytes": 27, "timeout_ms": 5_000, "fuel": 10_000_000_000},
    )
    data, out, _ = _out(resp)
    assert data["status"] == "resource_exhausted"
    assert "output" in data["reason"]
    assert data["stdout_truncated"] is True
    assert len(out) == 27
    assert out == b"X" * 27


def test_invalid_base64_rejected(client, wasm):
    resp = client.post("/execute", json={"module": "!!!not base64!!!"})
    assert resp.status_code == 400
    assert resp.json()["status"] == "input_error"


def test_budget_cannot_exceed_server_max(client, wasm):
    resp = _execute(client, wasm("busy_loop"), budget={"timeout_ms": 10_000_000})
    assert resp.status_code == 400
    assert "timeout_ms" in resp.json()["error"]


def test_illegal_module_is_input_error(client):
    resp = _execute(client, b"\x00asm-bogus")
    assert resp.status_code == 400
    assert "invalid wasm module" in resp.json()["error"]


def test_module_with_non_wasi_import_rejected(client):
    # Imports a function from an arbitrary module name.
    wat = (
    '(module (import "env" "evil" (func)) (func (export "_start") call 0))'
    )
    from wasmtime import wat2wasm

    resp = _execute(client, bytes(wat2wasm(wat)))
    assert resp.status_code == 400
    assert "non-permitted module" in resp.json()["error"]


def test_missing_start_rejected(client):
    wat = '(module)'
    from wasmtime import wat2wasm

    resp = _execute(client, bytes(wat2wasm(wat)))
    assert resp.status_code == 400
    assert "_start" in resp.json()["error"]


def test_health_and_limits(client):
    h = client.get("/health")
    assert h.status_code == 200
    assert h.json()["status"] == "ok"
    l = client.get("/limits")
    assert l.status_code == 200
    assert l.json()["max_concurrency"] >= 1


def test_stdin_isolation(client, wasm):
    # The uppercase program reflects transformed stdin; ensure supplied bytes
    # are read rather than the host's stdin.
    resp = _execute(client, wasm("uppercase"), b"abc")
    _, out, _ = _out(resp)
    assert out == b"ABC"


def test_guest_trap_reported(client, wasm):
    resp = _execute(client, wasm("traps"))
    data, _, _ = _out(resp)
    assert data["status"] == "trapped"
    assert data["exit_code"] is None
    assert "unreachable" in data["reason"]


def test_stderr_captured(client, wasm):
    resp = _execute(client, wasm("stderr_msg"))
    data, _, err = _out(resp)
    assert data["status"] == "exited"
    assert err == b"warning from wasm\n"
