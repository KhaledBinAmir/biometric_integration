# Copyright (c) 2026, Khaled Bin Amir
# SPDX-License-Identifier: MIT

from __future__ import annotations
from abc import ABC, abstractmethod
from werkzeug.wrappers import Request, Response

_LOGGED_HEADERS = {"host", "user-agent", "content-type", "content-length", "x-forwarded-for"}


class AbstractDeviceAdapter(ABC):
    """Base class for all biometric device brand adapters."""

    def __init__(self, request: Request):
        self.request = request
        self.path = request.path          # full path with leading /
        self.method = request.method
        self.raw_body = request.get_data(cache=True)  # cache=True: safe to read multiple times

    @abstractmethod
    def dispatch(self) -> Response:
        """Route the request and return a werkzeug Response."""
        ...

    def raw_dump(self, response_body: str = None) -> str:
        """Format the full request (and optionally response) for logging.

        Captures method, path+query, relevant headers, and body.
        Appends the response body when provided so both sides are visible.
        """
        qs = self.request.query_string.decode("utf-8", errors="replace")
        url_line = f"{self.method} {self.path}" + (f"?{qs}" if qs else "")

        header_lines = "\n".join(
            f"{k}: {v}"
            for k, v in self.request.headers
            if k.lower() in _LOGGED_HEADERS
        )

        body = self.raw_body.decode("utf-8", errors="replace").strip()

        parts = [f"→ {url_line}"]
        if header_lines:
            parts.append(header_lines)
        if body:
            parts.append(f"\n{body}")

        if response_body is not None:
            parts.append(f"\n← {response_body.strip()}")

        return "\n".join(parts)

    @staticmethod
    def text(body: str, status: int = 200, headers: dict = None) -> Response:
        r = Response(body, mimetype="text/plain", status=status)
        if headers:
            for k, v in headers.items():
                r.headers[k] = v
        return r

    @staticmethod
    def binary(body: bytes, status: int = 200, headers: dict = None) -> Response:
        return Response(
            body,
            status=status,
            headers=headers or {},
            content_type="application/octet-stream",
        )
