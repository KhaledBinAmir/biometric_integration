# Copyright (c) 2026, Khaled Bin Amir
# SPDX-License-Identifier: MIT

from __future__ import annotations
from abc import ABC, abstractmethod
from werkzeug.wrappers import Request, Response


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

    @staticmethod
    def text(body: str, status: int = 200) -> Response:
        return Response(body, mimetype="text/plain", status=status)

    @staticmethod
    def binary(body: bytes, status: int = 200, headers: dict = None) -> Response:
        return Response(
            body,
            status=status,
            headers=headers or {},
            content_type="application/octet-stream",
        )
