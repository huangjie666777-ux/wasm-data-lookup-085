#!/usr/bin/env python3
"""Compile the example WAT sources into .wasm files using wasmtime itself.

Usage: .venv/bin/python examples/build_examples.py
"""

from __future__ import annotations

from pathlib import Path

from wasmtime import wat2wasm

HERE = Path(__file__).resolve().parent

EXAMPLES = (
    "uppercase",
    "busy_loop",
    "memory_grower",
    "spam_output",
    "exits_nonzero",
    "stderr_msg",
    "traps",
    "device_enricher",
)


def build(name: str) -> Path:
    wat_path = HERE / f"{name}.wat"
    wasm_path = HERE / f"{name}.wasm"
    wasm = bytes(wat2wasm(wat_path.read_text(encoding="utf-8")))
    wasm_path.write_bytes(wasm)
    return wasm_path


def main() -> int:
    for module_name in EXAMPLES:
        path = build(module_name)
        print(f"built {path} ({path.stat().st_size} bytes)", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
