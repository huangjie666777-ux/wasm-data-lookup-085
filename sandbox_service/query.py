"""Controlled data-source query bridge.

One QueryState exists per execution and is never reused across executions, so
query allow-lists, counters and response bodies are fully isolated.

Protocol (host module "sandbox"; see models.SANDBOX_FUNCS):

* query_fetch(src_ptr, src_len, key_ptr, key_len, id_out) -> status code
* query_body(resp_id, dst_ptr, dst_len, copied_out) -> status code

The guest supplies only a server-configured source id and a UTF-8 business key.
The host encodes the key as the single query parameter "key" on the source's
fixed URL and issues GET; the guest cannot influence method, path, headers or
redirects. Credentials live only in server configuration and are never
returned to the guest, error messages or logs.

The full response body is downloaded once (no length-probing second request)
and retained only in this per-execution state; the guest then copies it into
its own memory. A body that does not fit the guest buffer is never reported as
success, and the retained copy is the authoritative complete payload.
"""

from __future__ import annotations

import threading

import httpx

from .config import SourceConfig
from .control import ExecutionControl
from .models import (
    QUERY_ERR_BAD_CAPACITY,
    QUERY_ERR_BAD_KEY,
    QUERY_ERR_BAD_POINTER,
    QUERY_ERR_BAD_SOURCE,
    QUERY_ERR_BAD_UTF8,
    QUERY_ERR_FAILED,
    QUERY_ERR_RESPONSE_TOO_LARGE,
    QUERY_ERR_TOO_MANY,
    QUERY_OK,
)

PARAM_NAME = "key"


class QueryAborted(Exception):
    """Raised inside a worker when the execution was cancelled/timed out."""


class QueryResponseTooLarge(Exception):
    """Body exceeded the per-response or per-execution byte ceiling."""


class QueryState:
    def __init__(
        self,
        allowed: dict[str, SourceConfig],
        max_queries: int,
        max_response_bytes: int,
        max_total_bytes: int,
        control: ExecutionControl,
    ) -> None:
        self._allowed = dict(allowed)
        self._max_queries = max_queries
        self._max_response_bytes = max_response_bytes
        self._max_total_bytes = max_total_bytes
        self._control = control
        self._lock = threading.Lock()
        self._used = 0
        self._total_bytes = 0
        self._responses: dict[int, bytes] = {}
        self._next_id = 1

    # -- guest memory helpers ------------------------------------------------

    @staticmethod
    def _bounds(memory, store, ptr: int, length: int) -> bool:
        if ptr < 0 or length < 0:
            return False
        if ptr > memory.data_len(store):
            return False
        return ptr + length <= memory.data_len(store)

    def _read_guest_string(self, memory, store, ptr: int, length: int, bad_utf8: int) -> tuple[int, str] | None:
        if not self._bounds(memory, store, ptr, length):
            return QUERY_ERR_BAD_POINTER, ""
        raw = bytes(memory.read(store, ptr, ptr + length))
        try:
            text = raw.decode("utf-8", errors="strict")
        except UnicodeDecodeError:
            return bad_utf8, ""
        return QUERY_OK, text

    # -- host calls ----------------------------------------------------------

    def fetch(self, caller, memory, src_ptr: int, src_len: int, key_ptr: int, key_len: int, id_out: int) -> int:
        parsed = self._read_guest_string(memory, caller, src_ptr, src_len, QUERY_ERR_BAD_SOURCE)
        if parsed is None or parsed[0] != QUERY_OK:
            return parsed[0] if parsed is not None else QUERY_ERR_BAD_POINTER
        _, source_id = parsed

        parsed = self._read_guest_string(memory, caller, key_ptr, key_len, QUERY_ERR_BAD_UTF8)
        if parsed is None or parsed[0] != QUERY_OK:
            return parsed[0] if parsed is not None else QUERY_ERR_BAD_POINTER
        _, key = parsed

        # Output pointer must hold one i32, checked before any network use.
        if not self._bounds(memory, caller, id_out, 4):
            return QUERY_ERR_BAD_POINTER
        if not key:
            return QUERY_ERR_BAD_KEY

        with self._lock:
            source = self._allowed.get(source_id)
            if source is None:
                # Unknown or not authorized THIS execution: no request made.
                return QUERY_ERR_BAD_SOURCE
            if self._used >= self._max_queries:
                return QUERY_ERR_TOO_MANY
            response_budget = min(
                self._max_response_bytes,
                self._max_total_bytes - self._total_bytes,
            )
            if response_budget <= 0:
                return QUERY_ERR_RESPONSE_TOO_LARGE
            # Reserve the outbound slot before the network round trip so a
            # timed-out/cancelled query still consumes its quota exactly once.
            self._used += 1

        try:
            body = self._http_get(source, key, response_budget)
        except QueryAborted:
            raise
        except QueryResponseTooLarge:
            return QUERY_ERR_RESPONSE_TOO_LARGE
        except Exception:
            # Never leak URL/header/network detail to the guest or logs.
            return QUERY_ERR_FAILED

        with self._lock:
            self._total_bytes += len(body)
            response_id = self._next_id
            self._next_id += 1
            self._responses[response_id] = body

        memory.write(caller, response_id.to_bytes(4, "little"), id_out)
        return QUERY_OK

    def body(self, caller, memory, response_id: int, dst_ptr: int, dst_len: int, copied_out: int) -> int:
        if not self._bounds(memory, caller, copied_out, 4):
            return QUERY_ERR_BAD_POINTER
        if dst_len < 0 or not self._bounds(memory, caller, dst_ptr, dst_len):
            return QUERY_ERR_BAD_POINTER
        with self._lock:
            payload = self._responses.get(response_id)
        if payload is None:
            return QUERY_ERR_BAD_CAPACITY
        if len(payload) > dst_len:
            # Complete body required: never return a truncated success.
            memory.write(caller, (0).to_bytes(4, "little"), copied_out)
            return QUERY_ERR_BAD_CAPACITY
        memory.write(caller, bytearray(payload), dst_ptr)
        memory.write(caller, len(payload).to_bytes(4, "little"), copied_out)
        return QUERY_OK

    # -- network -------------------------------------------------------------

    def _http_get(self, source: SourceConfig, key: str, response_budget: int) -> bytes:
        control = self._control
        remaining = control.remaining_seconds()
        if remaining <= 0:
            raise QueryAborted()

        url = httpx.URL(source.url).copy_merge_params(httpx.QueryParams({PARAM_NAME: key}))
        # Dedicated client per call: close() aborts the connection immediately
        # on timeout/cancellation/shutdown. No redirects, no env proxies, no
        # extra headers beyond the operator-configured (possibly secret) ones.
        client = httpx.Client(
            timeout=httpx.Timeout(remaining),
            follow_redirects=False,
            trust_env=False,
        )
        request = client.build_request("GET", url, headers=dict(source.headers))

        def abort() -> None:
            try:
                client.close()
            except Exception:
                pass

        control.set_abort_hook(abort)
        try:
            response = client.send(request, stream=True)
            try:
                if response.status_code != 200:
                    raise RuntimeError("upstream status error")
                chunks: list[bytes] = []
                total = 0
                for chunk in response.iter_bytes():
                    if control.is_stopped():
                        raise QueryAborted()
                    total += len(chunk)
                    if total > response_budget or total > self._max_response_bytes:
                        raise QueryResponseTooLarge("response byte ceiling exceeded")
                    chunks.append(chunk)
                return b"".join(chunks)
            finally:
                response.close()
        finally:
            control.clear_abort_hook()
            client.close()
