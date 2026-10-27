"""One-request HTTP transport with a total deadline and scoped cancellation.

Only numeric loopback HTTP or the caller-validated SAFE Unix socket is allowed.
Each network operation receives the *remaining* monotonic allowance; a slow
response cannot reset the total bound by sending another header or body chunk.
No pool, retry, DNS lookup, proxy, redirect, or socket is shared across attempts.
"""

from __future__ import annotations

import socket
import ssl
import threading
import time
from collections.abc import Callable, Iterable, Iterator
from typing import Any

import httpcore
import httpx


class DeadlineBudget:
    def __init__(self, deadline: float, *, clock: Callable[[], float] = time.monotonic):
        self.deadline = deadline
        self.clock = clock
        self._cancelled = threading.Event()
        self._lock = threading.Lock()
        self._streams: list[httpcore.NetworkStream] = []

    @property
    def cancelled(self) -> bool:
        return self._cancelled.is_set()

    def remaining(self, timeout: float | None = None) -> float:
        if self.cancelled:
            raise httpcore.ReadError("LOCAL_REQUEST_CANCELLED")
        remaining = self.deadline - self.clock()
        if remaining <= 0:
            raise httpcore.ReadTimeout("LOCAL_HTTP_DEADLINE_EXCEEDED")
        return remaining if timeout is None else min(timeout, remaining)

    def register(self, stream: httpcore.NetworkStream) -> None:
        with self._lock:
            self._streams.append(stream)
            cancelled = self.cancelled
        if cancelled:
            self.cancel()
            self.remaining()

    def cancel(self) -> None:
        """Interrupt only this request's sockets; server-side outcome is unknown."""
        self._cancelled.set()
        with self._lock:
            streams = list(self._streams)
        for stream in streams:
            try:
                sock = stream.get_extra_info("socket")
                if sock is not None:
                    sock.shutdown(socket.SHUT_RDWR)
            except (OSError, AttributeError):
                pass
            try:
                stream.close()
            except OSError:
                pass


class _DeadlineStream(httpcore.NetworkStream):
    def __init__(self, stream: httpcore.NetworkStream, budget: DeadlineBudget):
        self.stream = stream
        self.budget = budget
        budget.register(stream)

    def read(self, max_bytes: int, timeout: float | None = None) -> bytes:
        data = self.stream.read(max_bytes, timeout=self.budget.remaining(timeout))
        self.budget.remaining()
        return data

    def write(self, buffer: bytes, timeout: float | None = None) -> None:
        self.stream.write(buffer, timeout=self.budget.remaining(timeout))
        self.budget.remaining()

    def close(self) -> None:
        self.stream.close()

    def start_tls(self, ssl_context: ssl.SSLContext, server_hostname: str | None = None,
                  timeout: float | None = None) -> httpcore.NetworkStream:
        raise httpcore.UnsupportedProtocol("LOCAL_HTTP_TLS_ROUTE_DENIED")

    def get_extra_info(self, info: str) -> Any:
        return self.stream.get_extra_info(info)


class _DeadlineBackend(httpcore.NetworkBackend):
    def __init__(self, budget: DeadlineBudget, *, uds: str | None = None):
        self.budget = budget
        self.uds = uds
        self.backend = httpcore.SyncBackend()

    def connect_tcp(self, host: str, port: int, timeout: float | None = None,
                    local_address: str | None = None, socket_options: Any = None) -> httpcore.NetworkStream:
        if self.uds is not None or host != "127.0.0.1" or not 1 <= port <= 65535:
            raise httpcore.ConnectError("LOCAL_HTTP_ROUTE_DENIED")
        stream = self.backend.connect_tcp(
            host, port, timeout=self.budget.remaining(timeout),
            local_address=local_address, socket_options=socket_options,
        )
        return _DeadlineStream(stream, self.budget)

    def connect_unix_socket(self, path: str, timeout: float | None = None,
                            socket_options: Any = None) -> httpcore.NetworkStream:
        if self.uds is None or path != self.uds:
            raise httpcore.ConnectError("LOCAL_HTTP_ROUTE_DENIED")
        stream = self.backend.connect_unix_socket(
            path, timeout=self.budget.remaining(timeout), socket_options=socket_options,
        )
        return _DeadlineStream(stream, self.budget)


def _httpx_error(error: Exception) -> Exception:
    for source, target in (
        (httpcore.ConnectTimeout, httpx.ConnectTimeout),
        (httpcore.ReadTimeout, httpx.ReadTimeout),
        (httpcore.WriteTimeout, httpx.WriteTimeout),
        (httpcore.PoolTimeout, httpx.PoolTimeout),
        (httpcore.ConnectError, httpx.ConnectError),
        (httpcore.ReadError, httpx.ReadError),
        (httpcore.WriteError, httpx.WriteError),
        (httpcore.ProtocolError, httpx.ProtocolError),
        (httpcore.UnsupportedProtocol, httpx.UnsupportedProtocol),
    ):
        if isinstance(error, source):
            return target("BOUNDED_LOCAL_HTTP_FAILED")
    return error


class _ResponseStream(httpx.SyncByteStream):
    def __init__(self, response: httpcore.Response, pool: httpcore.ConnectionPool,
                 budget: DeadlineBudget):
        self.response = response
        self.pool = pool
        self.budget = budget
        self.closed = False

    def __iter__(self) -> Iterator[bytes]:
        try:
            assert isinstance(self.response.stream, Iterable)
            for chunk in self.response.stream:
                self.budget.remaining()
                yield chunk
            self.budget.remaining()
        except Exception as exc:
            raise _httpx_error(exc) from None
        finally:
            self.close()

    def close(self) -> None:
        if not self.closed:
            self.closed = True
            try:
                self.response.close()
            finally:
                self.pool.close()


class DeadlineHTTPTransport(httpx.BaseTransport):
    def __init__(self, budget: DeadlineBudget, *, uds: str | None = None):
        self.budget = budget
        self.uds = uds
        self._used = False
        self._pool: httpcore.ConnectionPool | None = None

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        if (self._used or request.url.scheme != "http"
                or request.url.host != ("safe-episode" if self.uds else "127.0.0.1")):
            raise httpx.TransportError("LOCAL_HTTP_ROUTE_OR_REPLAY_DENIED")
        self._used = True
        assert isinstance(request.stream, httpx.SyncByteStream)
        pool = httpcore.ConnectionPool(
            max_connections=1, max_keepalive_connections=0, retries=0, uds=self.uds,
            network_backend=_DeadlineBackend(self.budget, uds=self.uds),
        )
        self._pool = pool
        try:
            self.budget.remaining()
            response = pool.handle_request(httpcore.Request(
                method=request.method,
                url=httpcore.URL(scheme=request.url.raw_scheme, host=request.url.raw_host,
                                 port=request.url.port, target=request.url.raw_path),
                headers=request.headers.raw, content=request.stream, extensions=request.extensions,
            ))
            self.budget.remaining()
            return httpx.Response(
                status_code=response.status, headers=response.headers,
                stream=_ResponseStream(response, pool, self.budget), extensions=response.extensions,
            )
        except Exception as exc:
            pool.close()
            raise _httpx_error(exc) from None
        except BaseException:
            pool.close()
            raise

    def close(self) -> None:
        if self._pool is not None:
            self._pool.close()
