from __future__ import annotations

import base64
import json
from collections.abc import Mapping
from types import TracebackType
from typing import Any, Generic, Literal, TypeVar
from urllib.parse import quote

import httpx
from google.protobuf.message import DecodeError, Message

from . import proto
from ._http import DiadocProtocolError, _HttpClientT, async_send, decode_json, parse_base_url, send

PRODUCTION_URL = "https://diadoc-api.kontur.ru"
STAGING_URL = "https://diadoc-api-staging.kontur.ru"

JSON = "application/json"
BINARY = "application/octet-stream"
TEXT = "text/plain"

_ClientT = TypeVar("_ClientT", bound="_Client")
_AsyncClientT = TypeVar("_AsyncClientT", bound="_AsyncClient")
StructuredBody = Mapping[str, Any] | Message


def _json_default(value: Any) -> Any:
    if isinstance(value, bytes):
        return base64.b64encode(value).decode("ascii")
    if isinstance(value, Mapping):
        return dict(value)
    raise TypeError(f"{type(value).__name__} is not JSON serializable")


class _ClientBase(Generic[_HttpClientT]):
    _http: _HttpClientT

    def __init__(
        self,
        access_token: str | None = None,
        *,
        base_url: str = PRODUCTION_URL,
        developer_key: str | None = None,
        legacy_token: str | None = None,
        response_format: Literal["json", "protobuf"] = "json",
    ) -> None:
        if legacy_token and (not developer_key or access_token):
            raise ValueError("A legacy token requires a developer key and cannot be used with a Bearer token")
        if response_format not in {"json", "protobuf"}:
            raise ValueError("response_format must be 'json' or 'protobuf'")
        self._base_url = parse_base_url(base_url)
        self.access_token = access_token
        self.legacy_token = legacy_token
        self._developer_key = developer_key
        self._response_format = response_format

    def _build_request(
        self,
        method: str,
        path: str,
        *,
        params: Mapping[str, Any] | None = None,
        headers: Mapping[str, str] | None = None,
        content: bytes | str | None = None,
        json: Any = None,
        legacy: bool = False,
        flags: tuple[str, ...] = (),
    ) -> httpx.Request:
        if not path.startswith("/") or path.startswith("//") or "#" in path or "\\" in path:
            raise ValueError("Expected a relative API path starting with '/'")
        if content is not None and json is not None:
            raise ValueError("Pass either content or json")
        url = httpx.URL(f"{self._base_url}{path}")
        if params:
            query_params = {key: value for key, value in params.items() if value is not None and key not in flags}
            if query_params:
                url = url.copy_merge_params(query_params)
            enabled_flags = [quote(key, safe="").encode("ascii") for key in flags if params.get(key)]
            if enabled_flags:
                url = url.copy_with(query=b"&".join(part for part in [url.query, *enabled_flags] if part))
        request_headers = httpx.Headers(headers)
        if "Authorization" not in request_headers:
            authorization = self._authorization(legacy)
            if authorization is not None:
                request_headers["Authorization"] = authorization
        return self._http.build_request(method, url, headers=request_headers, content=content, json=json)

    def _authorization(self, legacy: bool) -> str | None:
        if not legacy and self.access_token:
            return f"Bearer {self.access_token}"
        if self._developer_key:
            header = f"DiadocAuth ddauth_api_client_id={self._developer_key}"
            if not legacy and self.legacy_token:
                header += f",ddauth_token={self.legacy_token}"
            return header
        if legacy:
            raise ValueError("Legacy authentication requires developer_key")
        return None

    def _build_call(
        self,
        method: str,
        path: str,
        *,
        params: Mapping[str, Any] | None = None,
        headers: Mapping[str, str | None] | None = None,
        body: StructuredBody | bytes | str | None = None,
        body_model: str | None = None,
        response_media: str | None = None,
        response_model: str | None = None,
        legacy: bool = False,
        flags: tuple[str, ...] = (),
    ) -> tuple[httpx.Request, str | None]:
        request_headers = {key: value for key, value in (headers or {}).items() if value is not None}
        if response_model is not None:
            response_media = BINARY if self._response_format == "protobuf" else JSON
        if response_media is not None:
            request_headers["Accept"] = response_media

        content: bytes | str | None = None
        if body is not None:
            if isinstance(body, Message):
                if body.DESCRIPTOR.full_name != body_model:
                    raise TypeError(f"Expected {body_model}, got {body.DESCRIPTOR.full_name}")
                request_headers["Content-Type"] = BINARY
                content = body.SerializeToString()
            elif isinstance(body, Mapping):
                request_headers["Content-Type"] = f"{JSON}; charset=utf-8"
                content = json.dumps(body, default=_json_default, ensure_ascii=False, allow_nan=False).encode("utf-8")
            else:
                request_headers["Content-Type"] = f"{TEXT}; charset=utf-8" if isinstance(body, str) else BINARY
                content = body

        request = self._build_request(
            method, path, params=params, headers=request_headers, content=content, legacy=legacy, flags=flags,
        )
        return request, response_media


def _decode_response(
    response: httpx.Response, response_media: str | None, response_model: str | None,
) -> Any:
    if response.status_code == 204 or (not response.content and "Retry-After" in response.headers):
        return None
    if response_media is None:
        return None
    if response_media == JSON:
        return decode_json(response)
    if response_media == TEXT:
        return response.text
    if response_model is not None:
        message = proto.get_message(response_model).FromString
        try:
            return message(response.content)
        except DecodeError as error:
            raise DiadocProtocolError(f"Expected a Protobuf {response_model} response", response) from error
    return response.content


class _Client(_ClientBase[httpx.Client]):
    def __init__(
        self,
        access_token: str | None = None,
        *,
        base_url: str = PRODUCTION_URL,
        developer_key: str | None = None,
        legacy_token: str | None = None,
        response_format: Literal["json", "protobuf"] = "json",
        timeout: float | httpx.Timeout = 30.0,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        super().__init__(
            access_token, base_url=base_url, developer_key=developer_key, legacy_token=legacy_token,
            response_format=response_format,
        )
        self._http = httpx.Client(timeout=timeout, transport=transport)

    def __enter__(self: _ClientT) -> _ClientT:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.close()

    def close(self) -> None:
        self._http.close()

    def request(
        self,
        method: str,
        path: str,
        *,
        params: Mapping[str, Any] | None = None,
        headers: Mapping[str, str] | None = None,
        content: bytes | str | None = None,
        json: Any = None,
    ) -> httpx.Response:
        """Call an API path and return its HTTP response without decoding the body."""
        request = self._build_request(method, path, params=params, headers=headers, content=content, json=json)
        return send(self._http, request)

    def _call(
        self, method: str, path: str, *, response_model: str | None = None, raw: bool = False, **kwargs: Any,
    ) -> Any:
        request, media = self._build_call(method, path, response_model=response_model, **kwargs)
        response = send(self._http, request)
        return response if raw else _decode_response(response, media, response_model)


class _AsyncClient(_ClientBase[httpx.AsyncClient]):
    def __init__(
        self,
        access_token: str | None = None,
        *,
        base_url: str = PRODUCTION_URL,
        developer_key: str | None = None,
        legacy_token: str | None = None,
        response_format: Literal["json", "protobuf"] = "json",
        timeout: float | httpx.Timeout = 30.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        super().__init__(
            access_token, base_url=base_url, developer_key=developer_key, legacy_token=legacy_token,
            response_format=response_format,
        )
        self._http = httpx.AsyncClient(timeout=timeout, transport=transport)

    async def __aenter__(self: _AsyncClientT) -> _AsyncClientT:
        await self._http.__aenter__()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        if not self._http.is_closed:
            await self._http.__aexit__(exc_type, exc_value, traceback)

    async def aclose(self) -> None:
        """Close the HTTP client and its connections."""
        await self._http.aclose()

    async def request(
        self,
        method: str,
        path: str,
        *,
        params: Mapping[str, Any] | None = None,
        headers: Mapping[str, str] | None = None,
        content: bytes | str | None = None,
        json: Any = None,
    ) -> httpx.Response:
        """Call an API path and return its HTTP response without decoding the body."""
        request = self._build_request(method, path, params=params, headers=headers, content=content, json=json)
        return await async_send(self._http, request)

    async def _call(
        self, method: str, path: str, *, response_model: str | None = None, raw: bool = False, **kwargs: Any,
    ) -> Any:
        request, media = self._build_call(method, path, response_model=response_model, **kwargs)
        response = await async_send(self._http, request)
        return response if raw else _decode_response(response, media, response_model)
