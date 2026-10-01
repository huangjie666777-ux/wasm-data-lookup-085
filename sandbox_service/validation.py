"""Static validation of uploaded wasm modules before any execution."""

from __future__ import annotations

from wasmtime import Engine, Module, WasmtimeError

from .models import ALLOWED_WASI_IMPORTS, SANDBOX_FUNCS, SANDBOX_MODULE, WASI_MODULE


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

    for imp in module.imports:
        extern_type = imp.type
        if type(extern_type).__name__ != "FuncType":
            raise ModuleInvalid(
                f"import '{imp.module}::{imp.name}' must be a function"
            )
        if imp.module == WASI_MODULE:
            if imp.name is None or imp.name not in ALLOWED_WASI_IMPORTS:
                raise ModuleInvalid(f"non-permitted import '{imp.module}::{imp.name}'")
            continue
        if imp.module == SANDBOX_MODULE:
            if imp.name is None or imp.name not in SANDBOX_FUNCS:
                raise ModuleInvalid(f"non-permitted import '{imp.module}::{imp.name}'")
            expected_params, expected_results = SANDBOX_FUNCS[imp.name]
            if (
                list(extern_type.params) != list(expected_params)
                or list(extern_type.results) != list(expected_results)
            ):
                raise ModuleInvalid(
                    f"import '{imp.module}::{imp.name}' has wrong signature"
                )
            continue
        raise ModuleInvalid(
            "import from non-permitted module "
            f"'{imp.module}::{'?' if imp.name is None else imp.name}'"
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
