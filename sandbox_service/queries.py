"""Controlled, per-execution data-source query broker.

The guest hands over only a configured source id and a UTF-8 business key.
The host performs exactly one fixed GET per query: the key is encoded as a
single query parameter; the guest cannot influence the path, method, headers
or redirects. Bodies are streamed under per-response and cumulative byte
quotas; an over-limit or incomplete body is a failure, never a truncated
success. Bodies that do not fit the guest's buffer are cached for one
execution and retrieved through `query_read`, so length probing never causes
a second outbound request.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass

import httpx

# Host ABI status codes (high 32 bits of the packed i64 result).
STATUS_OK = 0
STATUS_BAD_ARG = 1
STATUS_SOURCE_DENIED = 2
STATUS_BUFFER_TOO_SMALL = 3
STATUS_QUERY_FAILED = 4
STATUS_QUOTA_EXCEEDED = 5
STATUS_READ_FAULT = 6


class QueryAborted(Exception):
    """Raised when the supervisor cancels the in-flight query (timeout/close)."""


@dataclass
class QueryLimits:
    max_count: int
    max_response_bytes: int
    max_total_bytes: int


class QueryControl:
    """Cancellation handle shared between the supervisor and the broker.

    The watchdog aborts an in-flight HTTP request from its own thread by
    closing the client, so a blocked host call cannot outlive the wall-clock
    budget, a client disconnect or server shutdown.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._clients: set[httpx.Client] = set()
        self._aborted = threading.Event()

    def register(self, client: httpx.Client) -> None:
        with self._lock:
            if self._aborted.is_set():
                raise QueryAborted("query aborted")
            self._clients.add(client)

    def unregister(self, client: httpx.Client) -> None:
        with self._lock:
            self._clients.discard(client)

    def abort_all(self, reason: str = "query aborted") -> None:
        with self._lock:
            if self._aborted.is_set():
                return
            self._aborted.set()
            self.reason = reason
            clients = list(self._clients)
        # Closing from another thread interrupts the blocked request thread
        # with a network error. Never invoked with credentials in the reason.
        for client in clients:
            try:
                client.close()
            except Exception:
                pass

    def check(self) -> None:
        if self._aborted.is_set():
            raise QueryAborted(getattr(self, "reason", "query aborted"))


class QueryBroker:
    """Per-execution broker: quota accounting and cached response bodies."""

    def __init__(
        self,
        sources: dict,
        limits: QueryLimits,
        control: QueryControl,
        deadline_monotonic: float,
        grace_seconds: float,
    ) -> None:
        # sources: dict[str, ResourceSource] configured AND authorized for
        # this execution only.
        self._sources = sources
        self._limits = limits
        self._control = control
        self._deadline = deadline_monotonic
        self._grace = grace_seconds
        self._used_count = 0
        self._used_total = 0
        self._cache: dict[int, bytes] = {}
        self._next_token = 0

    def fetch(self, source_id: str, key: str) -> tuple[int, bytes | None]:
        """Perform one controlled GET.

        Returns (status, body): on STATUS_OK body is the complete bytes;
        otherwise body is None.
        """
        self._control.check()
        if source_id not in self._sources:
            return STATUS_SOURCE_DENIED, None
        if self._used_count >= self._limits.max_count:
            return STATUS_QUOTA_EXCEEDED, None

        source = self._sources[source_id]
        remaining = self._deadline - time.monotonic()
        if remaining <= 0:
            self._control.abort_all("wall-clock deadline reached")
            raise QueryAborted("wall-clock deadline reached")

        # Count the attempt now; quota is reclaimed nowhere on failure because
        # an outbound attempt actually happened (the slot is not reusable).
        self._used_count += 1
        body, ok = self._http_get(source, key, remaining + self._grace)
        if not ok:
            return STATUS_QUERY_FAILED, None

        # Cumulative accounting happens only on fully accepted bodies.
        if self._used_total + len(body) > self._limits.max_total_bytes:
            return STATUS_QUOTA_EXCEEDED, None
        self._used_total += len(body)
        return STATUS_OK, body

    def store(self, body: bytes) -> int:
        """Cache a complete body for buffer-too-small retrieval."""
        token = self._next_token
        self._next_token += 1
        self._cache[token] = body
        return token

    def cached(self, token: int) -> bytes | None:
        return self._cache.get(token)

    def release(self, token: int) -> None:
        self._cache.pop(token, None)

    def _http_get(self, source, key: str, timeout_seconds: float) -> tuple[bytes | None, bool]:
        """Fixed-address GET; never follows redirects; streams under the cap."""
        headers = {}
        if source.auth_header_name is not None:
            headers[source.auth_header_name] = source.auth_header_value
        # httpx timeouts bound every blocking phase so the host call cannot
        # hang past the wall-clock budget.
        timeout = httpx.Timeout(
            max(timeout_seconds, 0.001),
            connect=min(timeout_seconds, 2.0),
        )
        client = httpx.Client(timeout=timeout, follow_redirects=False, headers=headers)
        self._control.register(client)
        try:
            request = client.build_request(
                "GET", source.url, params={source.param_name: key}
            )
            try:
                response = client.send(request, stream=True)
            except QueryAborted:
                raise
            except Exception:
                # Connect resets, DNS failures, timeouts and tls errors are
                # plain query failures - unless the supervisor already asked
                # us to abort (watchdog/client disconnect/shutdown).
                self._control.check()
                return None, False
            try:
                if response.status_code != 200:
                    return None, False
                chunks: list[bytes] = []
                received = 0
                try:
                    for chunk in response.iter_bytes(chunk_size=16 * 1024):
                        self._control.check()
                        received += len(chunk)
                        if received > self._limits.max_response_bytes:
                            return None, False
                        chunks.append(chunk)
                except QueryAborted:
                    raise
                except Exception:
                    self._control.check()
                    return None, False
                # A network drop mid-body raises above; additionally reject
                # ambiguous short reads where content-length promised more.
                expected = response.headers.get("content-length")
                if expected is not None:
                    try:
                        if int(expected) != received:
                            return None, False
                    except ValueError:
                        pass
                return b"".join(chunks), True
            finally:
                response.close()
        finally:
            self._control.unregister(client)
            client.close()
