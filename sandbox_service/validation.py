"""Static validation of uploaded wasm modules before any execution."""

from __future__ import annotations

from wasmtime import Engine, Module, WasmtimeError

from .models import ALLOWED_SANDBOX_QUERY_IMPORTS, ALLOWED_WASI_IMPORTS, SANDBOX_MODULE, WASI_MODULE


class ModuleInvalid(Exception):
    """Raised when the uploaded bytes are not a legal, permitted module."""


def compile_module(engine: Engine, wasm_bytes: bytes) -> Module:
    """Validate + compile the module, rejecting illegal or unsupported modules.

    Only modules that:
      * are valid WebAssembly, and
      * import exclusively functions from `wasi_snapshot_preview1` with names
        from the preview1 allow-list, and
      * export a `_start` function

    are accepted. No host directories, environment, or network are offered.
    Imports of anything else (including other wasm modules) fail validation.
    """
    try:
        module = Module(engine, wasm_bytes)
    except WasmtimeError as exc:
        raise ModuleInvalid(f"invalid wasm module: {exc}") from exc

    allowed_names = {
        WASI_MODULE: ALLOWED_WASI_IMPORTS,
        SANDBOX_MODULE: ALLOWED_SANDBOX_QUERY_IMPORTS,
    }
    for imp in module.imports:
        allowed = allowed_names.get(imp.module)
        if allowed is None:
            raise ModuleInvalid(
                f"import from non-permitted module "
                f"'{imp.module}::{'?' if imp.name is None else imp.name}'"
            )
        if imp.name is None or imp.name not in allowed:
            raise ModuleInvalid(f"non-permitted import '{imp.module}::{imp.name}'")
        extern_type = imp.type
        if type(extern_type).__name__ != "FuncType":
            raise ModuleInvalid(f"import '{imp.module}::{imp.name}' must be a function")

    # Verify the exact ABI of the optional sandbox query imports.
    names = {imp.name for imp in module.imports if imp.module == SANDBOX_MODULE}
    query_expectations = {
        # (source_ptr, source_len, key_ptr, key_len, buf_ptr, buf_len)
        "query_fetch": (["i32"] * 6, ["i64"]),
        # (token, offset, buf_ptr, buf_len)
        "query_read": (["i32", "i32", "i32", "i32"], ["i64"]),
    }
    for imp in module.imports:
        if imp.module != SANDBOX_MODULE:
            continue
        params, results = query_expectations[imp.name]
        got_params = [str(p) for p in imp.type.params]
        got_results = [str(r) for r in imp.type.results]
        if got_params != params or got_results != results:
            raise ModuleInvalid(
                f"import '{imp.module}::{imp.name}' has the wrong signature; "
                f"expected ({', '.join(params)}) -> ({', '.join(results)})"
            )

    exports = {ex.name: ex for ex in module.exports}
    start = exports.get("_start")
    if start is None:
        raise ModuleInvalid("module must export a `_start` function")
    if type(start.type).__name__ != "FuncType":
        raise ModuleInvalid("export `_start` must be a function")
    if start.type.params or start.type.results:
        raise ModuleInvalid("export `_start` must take no parameters and return no results")

    return module
