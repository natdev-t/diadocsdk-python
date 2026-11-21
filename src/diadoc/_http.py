from __future__ import annotations

from typing import Any

import httpx


class DiadocError(Exception):
    """Base class for errors reported by the SDK."""


class DiadocHTTPError(DiadocError):
    """The server returned an unsuccessful HTTP status."""

    def __init__(self, response: httpx.Response) -> None:
        self.response = response
        self.status_code = response.status_code
        self.error_code = response.headers.get("X-Diadoc-ErrorCode")
        self.retry_after = response.headers.get("Retry-After")
        message = f"HTTP {self.status_code} for {response.request.method} {response.request.url.path}"
        if self.error_code:
            message += f" ({self.error_code})"
        super().__init__(message)


class DiadocTransportError(DiadocError):
    """The HTTP request could not be completed."""


class DiadocProtocolError(DiadocError):
    """The server response could not be decoded as expected."""

    def __init__(self, message: str, response: httpx.Response) -> None:
        self.response = response
        super().__init__(message)


def parse_base_url(value: str) -> httpx.URL:
    url = httpx.URL(value.rstrip("/"))
    if url.scheme not in {"http", "https"} or not url.host or url.userinfo or url.query or url.fragment:
        raise ValueError("Expected an HTTP(S) base URL without credentials, query or fragment")
    return url


def send(client: httpx.Client, method: str, url: httpx.URL, **kwargs: Any) -> httpx.Response:
    try:
        response = client.request(method, url, **kwargs)
    except httpx.RequestError as error:
        raise DiadocTransportError(f"{type(error).__name__} for {method} {url.path}") from error
    if not response.is_success:
        raise DiadocHTTPError(response)
    return response


def decode_json(response: httpx.Response) -> Any:
    try:
        return response.json()
    except ValueError as error:
        raise DiadocProtocolError("Expected a JSON response", response) from error
