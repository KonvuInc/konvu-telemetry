"""Outbound HTTP shared by the provider-limit and product-analytics clients."""

from __future__ import annotations

from typing import Protocol, cast
from urllib.request import HTTPRedirectHandler, Request, build_opener


class HTTPResponse(Protocol):
    headers: object
    status: int

    def read(self, size: int = -1) -> bytes: ...

    def __enter__(self) -> HTTPResponse: ...

    def __exit__(self, *args: object) -> None: ...


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args: object, **kwargs: object) -> None:
        return None


def open_without_redirects(request: Request, timeout: float) -> HTTPResponse:
    """Open one request; any redirect raises before a body reaches another URL."""
    return cast(
        HTTPResponse, build_opener(_NoRedirect()).open(request, timeout=timeout)
    )
