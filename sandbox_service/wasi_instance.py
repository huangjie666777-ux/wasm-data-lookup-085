"""Per-request isolated WASI preview1 instance and `_start` execution."""

from __future__ import annotations

import struct
import threading
import time
from dataclasses import dataclass

from wasmtime import (
    Config,
    Engine,
    ExitTrap,
    FuncType,
    Linker,
    Memory,
    Module,
    Store,
    Trap,
    TrapCode,
    ValType,
    WasiConfig,
    WasmtimeError,
)
from wasmtime import _func as _wasmtime_func

from .models import SANDBOX_MODULE, ResolvedBudget
from .queries import (
    STATUS_BAD_ARG,
    STATUS_BUFFER_TOO_SMALL,
    STATUS_OK,
    STATUS_READ_FAULT,
    QueryAborted,
    QueryBroker,
    QueryControl,
    QueryLimits,
)
from .streams import OutputLimit, StreamCapture

_ERRNO_SUCCESS = 0
_ERRNO_BADF = 8
_ERRNO_FAULT = 21


def _patch_wasmtime_function_slab() -> None:
    """Serialize access to wasmtime's process-global host-function registry.

    wasmtime 49 keeps Python-defined host functions in a plain-list Slab
    (`wasmtime._func.FUNCTIONS`) with no locking. Concurrent
    `Linker.define_func` calls corrupt its free list and raise
    `TypeError: list indices must be integers or slices, not tuple`. We patch
    allocate/deallocate in place with an RLock; this is a narrowly scoped
    workaround for the upstream defect and runs once at import time.
    """
    slab = _wasmtime_func.FUNCTIONS
    if getattr(slab, "_sandbox_patched", False):
        return
    lock = threading.RLock()
    orig_allocate = slab.allocate
    orig_deallocate = slab.deallocate

    def allocate(value):
        with lock:
            return orig_allocate(value)

    def deallocate(idx: int) -> None:
        with lock:
            orig_deallocate(idx)

    slab.allocate = allocate
    slab.deallocate = deallocate
    slab._sandbox_patched = True


_patch_wasmtime_function_slab()


@dataclass
class ExecutionOutcome:
    # status: "exited" | "trapped" | "resource_exhausted"
    status: str
    exit_code: int | None
    stdout: bytes
    stderr: bytes
    reason: str
    truncated_output: bool
    fuel_consumed: int


def new_engine() -> Engine:
    """Each execution gets its own engine, so an epoch bump affects only it."""
    config = Config()
    config.consume_fuel = True
    config.epoch_interruption = True
    return Engine(config)


def _decode_iovecs(memory: Memory, store: Store, base: int, count: int) -> list[tuple[int, int]]:
    iovs: list[tuple[int, int]] = []
    for i in range(count):
        header = bytes(memory.read(store, base + i * 8, base + i * 8 + 8))
        ptr = int.from_bytes(header[0:4], "little")
        length = int.from_bytes(header[4:8], "little")
        iovs.append((ptr, length))
    return iovs


def run_module(
    engine: Engine,
    module: Module,
    stdin_bytes: bytes,
    budget: ResolvedBudget,
    argv: list[str],
    source_map: dict | None = None,
    query_control: QueryControl | None = None,
    deadline_monotonic: float | None = None,
    query_grace_seconds: float = 0.25,
) -> ExecutionOutcome:
    """Instantiate a fresh isolated instance and run its `_start`.

    The WASI environment carries no host env vars and preopens no host
    directories or sockets. Only the guest's own stdin/stdout/stderr are wired,
    and all three are in-memory and bounded.
    """
    capture = StreamCapture(budget.output_bytes)
    stdin_view = bytearray(stdin_bytes)
    stdin_pos = {"pos": 0}

    store = Store(engine)
    store.set_fuel(budget.fuel)
    store.set_epoch_deadline(1)
    store.set_limits(
        memory_size=budget.memory_bytes,
        table_elements=100_000,
        instances=32,
        tables=32,
        memories=1,
    )

    wasi = WasiConfig()
    wasi.argv = argv
    wasi.env = []
    # No stdin_file/preopen_dir/inherit_*: no host file/network access.
    store.set_wasi(wasi)

    linker = Linker(engine)
    linker.allow_shadowing = True
    linker.define_wasi()
    _bind_stream_funcs(linker, store, capture, stdin_view, stdin_pos)

    broker: QueryBroker | None = None
    if source_map is not None and query_control is not None:
        broker = QueryBroker(
            sources=source_map,
            limits=QueryLimits(
                max_count=budget.query_count,
                max_response_bytes=budget.query_response_bytes,
                max_total_bytes=budget.query_total_bytes,
            ),
            control=query_control,
            deadline_monotonic=(
                deadline_monotonic
                if deadline_monotonic is not None
                else time.monotonic() + budget.timeout_ms / 1000.0
            ),
            grace_seconds=query_grace_seconds,
        )
    _bind_query_funcs(linker, store, broker)

    try:
        instance = linker.instantiate(store, module)
    except WasmtimeError as exc:
        return ExecutionOutcome(
            status="resource_exhausted",
            exit_code=None,
            stdout=capture.stdout,
            stderr=capture.stderr,
            reason=f"instantiation rejected by resource limits: {exc}",
            truncated_output=False,
            fuel_consumed=budget.fuel - store.get_fuel(),
        )
    start = instance.exports(store)["_start"]

    reason = ""
    status = "exited"
    exit_code: int | None = 0
    try:
        start(store)
    except ExitTrap as exc:
        status = "exited"
        exit_code = exc.code
    except Trap as exc:
        exit_code = None
        if exc.trap_code is TrapCode.OUT_OF_FUEL:
            status = "resource_exhausted"
            reason = "instruction fuel exhausted"
        elif exc.trap_code is TrapCode.INTERRUPT:
            status = "resource_exhausted"
            reason = getattr(engine, "_interrupt_reason", "wall-clock timeout")
        else:
            status = "trapped"
            reason = _trap_message(exc)
    except WasmtimeError as exc:
        if capture.truncated:
            status = "resource_exhausted"
            reason = "combined output byte limit exceeded"
        else:
            status = "trapped"
            reason = str(exc)
    except QueryAborted as exc:
        status = "resource_exhausted"
        exit_code = None
        reason = str(exc)

    if status == "trapped" and capture.truncated:
        status = "resource_exhausted"
        reason = "combined output byte limit exceeded"

    return ExecutionOutcome(
        status=status,
        exit_code=exit_code,
        stdout=capture.stdout,
        stderr=capture.stderr,
        reason=reason,
        truncated_output=capture.truncated,
        fuel_consumed=budget.fuel - store.get_fuel(),
    )


def _trap_message(exc: Trap) -> str:
    code = exc.trap_code
    label = code.name.lower() if code is not None else "trap"
    return f"guest trapped: {label}"


def _bind_stream_funcs(
    linker: Linker,
    store: Store,
    capture: StreamCapture,
    stdin_view: bytearray,
    stdin_pos: dict,
) -> None:
    i32 = ValType.i32()
    io_type = FuncType([i32, i32, i32, i32], [i32])

    def get_memory(caller) -> Memory:
        memory = caller.get("memory")
        if not isinstance(memory, Memory):
            raise WasmtimeError("guest has no exported linear memory named 'memory'")
        return memory

    def fd_write(caller, fd: int, iovs: int, iovs_len: int, nwritten: int) -> int:
        if fd == 1:
            sink = capture.write_stdout
        elif fd == 2:
            sink = capture.write_stderr
        else:
            return _ERRNO_BADF
        memory = get_memory(caller)
        total = 0
        limited = False
        try:
            for ptr, length in _decode_iovecs(memory, store, iovs, iovs_len):
                chunk = bytes(memory.read(store, ptr, ptr + length))
                total += sink(chunk)
        except OutputLimit:
            limited = True
        except (WasmtimeError, IndexError, ValueError):
            return _ERRNO_FAULT
        memory.write(store, bytearray(total.to_bytes(4, "little")), nwritten)
        if limited:
            raise Trap("combined output byte limit exceeded")
        return _ERRNO_SUCCESS

    def fd_read(caller, fd: int, iovs: int, iovs_len: int, nread: int) -> int:
        if fd != 0:
            return _ERRNO_BADF
        memory = get_memory(caller)
        total = 0
        try:
            for ptr, length in _decode_iovecs(memory, store, iovs, iovs_len):
                pos = stdin_pos["pos"]
                chunk = bytes(stdin_view[pos:pos + length])
                stdin_pos["pos"] = pos + len(chunk)
                memory.write(store, bytearray(chunk), ptr)
                total += len(chunk)
                if len(chunk) < length:
                    break
        except (WasmtimeError, IndexError, ValueError):
            return _ERRNO_FAULT
        memory.write(store, bytearray(total.to_bytes(4, "little")), nread)
        return _ERRNO_SUCCESS

    linker.define_func("wasi_snapshot_preview1", "fd_write", io_type, fd_write, access_caller=True)
    linker.define_func("wasi_snapshot_preview1", "fd_read", io_type, fd_read, access_caller=True)


def _pack_result(status: int, value: int) -> int:
    """Pack (u32 status, u32 value) into the low 64 bits of an i64."""
    packed = ((status & 0xFFFFFFFF) << 32) | (value & 0xFFFFFFFF)
    return struct.unpack("<q", struct.pack("<Q", packed))[0]


def _bind_query_funcs(linker: Linker, store: Store, broker: QueryBroker | None) -> None:
    """Bind the controlled data-source query ABI under `sandbox_queries`.

    query_fetch(src_ptr, src_len, key_ptr, key_len, buf_ptr, buf_len) -> i64
        packed (status, value): STATUS_OK  -> value = body length, complete
        body was copied into the guest buffer;
        STATUS_BUFFER_TOO_SMALL -> value = retrieval token for query_read;
        other failures -> value = 0, no bytes copied.

    query_read(token, offset, buf_ptr, buf_len) -> i64
        STATUS_OK with value = bytes copied into the buffer; an offset past
        the end copies zero bytes and releases the cache slot.
    """
    i32 = ValType.i32()
    i64 = ValType.i64()
    fetch_type = FuncType([i32] * 6, [i64])
    read_type = FuncType([i32] * 4, [i64])

    def get_memory(caller) -> Memory:
        memory = caller.get("memory")
        if not isinstance(memory, Memory):
            raise WasmtimeError("guest has no exported linear memory named 'memory'")
        return memory

    def guest_bytes(memory: Memory, ptr: int, length: int) -> bytes | None:
        # Guest pointers are unsigned 32-bit; wasmtime may hand us the wrapped
        # (negative) Python value for values over 2 GiB.
        if length < 0:
            return None
        ptr &= 0xFFFFFFFF
        try:
            if ptr + length > memory.data_len(store):
                return None
            data = bytes(memory.read(store, ptr, ptr + length))
        except (WasmtimeError, IndexError, ValueError):
            return None
        if len(data) != length:
            return None
        return data

    def guest_write(memory: Memory, ptr: int, data: bytes) -> bool:
        try:
            memory.write(store, bytearray(data), ptr & 0xFFFFFFFF)
            return True
        except (WasmtimeError, IndexError, ValueError):
            return False

    def query_fetch(
        caller,
        src_ptr: int,
        src_len: int,
        key_ptr: int,
        key_len: int,
        buf_ptr: int,
        buf_len: int,
    ) -> int:
        if broker is None:
            return _pack_result(STATUS_BAD_ARG, 0)
        memory = get_memory(caller)
        if src_len > 64 or key_len > 4096 or buf_len < 0:
            return _pack_result(STATUS_BAD_ARG, 0)
        raw_source = guest_bytes(memory, src_ptr, src_len)
        raw_key = guest_bytes(memory, key_ptr, key_len)
        if raw_source is None or raw_key is None:
            return _pack_result(STATUS_READ_FAULT, 0)
        if not raw_source:
            return _pack_result(STATUS_BAD_ARG, 0)
        # Validate the destination buffer range before any outbound request so
        # an illegal pointer never triggers network traffic.
        if buf_len and guest_bytes(memory, buf_ptr, buf_len) is None:
            return _pack_result(STATUS_READ_FAULT, 0)
        try:
            source_id = raw_source.decode("utf-8")
            key = raw_key.decode("utf-8")
        except UnicodeDecodeError:
            return _pack_result(STATUS_BAD_ARG, 0)

        status, body = broker.fetch(source_id, key)
        if status != STATUS_OK or body is None:
            return _pack_result(status, 0)
        if len(body) > buf_len:
            token = broker.store(body)
            return _pack_result(STATUS_BUFFER_TOO_SMALL, token)
        if len(body) and not guest_write(memory, buf_ptr, body):
            return _pack_result(STATUS_READ_FAULT, 0)
        return _pack_result(STATUS_OK, len(body))

    def query_read(caller, token: int, offset: int, buf_ptr: int, buf_len: int) -> int:
        if broker is None or token < 0 or offset < 0 or buf_len < 0:
            return _pack_result(STATUS_BAD_ARG, 0)
        memory = get_memory(caller)
        body = broker.cached(token)
        if body is None:
            return _pack_result(STATUS_BAD_ARG, 0)
        if buf_len and guest_bytes(memory, buf_ptr, buf_len) is None:
            return _pack_result(STATUS_READ_FAULT, 0)
        remaining = body[offset:]
        chunk = remaining[:buf_len]
        if len(chunk) and not guest_write(memory, buf_ptr, chunk):
            return _pack_result(STATUS_READ_FAULT, 0)
        if offset + len(chunk) >= len(body):
            broker.release(token)
        return _pack_result(STATUS_OK, len(chunk))

    linker.define_func(
        SANDBOX_MODULE, "query_fetch", fetch_type, query_fetch, access_caller=True
    )
    linker.define_func(
        SANDBOX_MODULE, "query_read", read_type, query_read, access_caller=True
    )
