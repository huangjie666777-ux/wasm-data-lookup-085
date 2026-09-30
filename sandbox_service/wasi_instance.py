"""Per-request isolated WASI preview1 instance and `_start` execution."""

from __future__ import annotations

import threading
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

from .models import ResolvedBudget
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
