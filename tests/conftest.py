"""Shared fixtures and helpers for the sandbox test suite."""

from __future__ import annotations

import base64
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
EXAMPLES = REPO_ROOT / "examples"


def b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


@pytest.fixture
def wasm():
    def load(name: str) -> bytes:
        path = EXAMPLES / f"{name}.wasm"
        if not path.exists():
            pytest.skip(f"example {name}.wasm not built; run examples/build_examples.py")
        return path.read_bytes()

    return load


@pytest.fixture
def client():
    from fastapi.testclient import TestClient

    from sandbox_service.app import app

    with TestClient(app) as c:
        yield c
