from __future__ import annotations

from collections.abc import Iterable
from types import TracebackType
from typing import Generic, TypedDict, cast

import httpx

from ._http import DiadocHTTPError, DiadocProtocolError, _HttpClientT, async_send, decode_json, parse_base_url, send

IDENTITY_URL = "https://identity.kontur.ru"
PRODUCTION_SCOPE = "Diadoc.PublicAPI"
STAGING_SCOPE = "Diadoc.PublicAPI.Staging"
_DEFAULT_SCOPES = ("openid", "offline_access", PRODUCTION_SCOPE)


class _Token(TypedDict):
    access_token: str
    token_type: str
    expires_in: int


class TokenResponse(_Token, total=False):
    refresh_token: str
    id_token: str
    scope: str


class _DeviceAuthorization(TypedDict):
    device_code: str
    user_code: str
    verification_uri: str
    expires_in: int


class DeviceAuthorization(_DeviceAuthorization, total=False):
    verification_uri_complete: str
    interval: int


class OidcError(DiadocHTTPError):
    """An HTTP error from Kontur Identity, with the OAuth error code if present."""

    def __init__(self, response: httpx.Response) -> None:
        super().__init__(response)
        try:
            payload = response.json()
        except ValueError:
            payload = None
        self.error = payload.get("error") if isinstance(payload, dict) else None


class _OidcBase(Generic[_HttpClientT]):
    _http: _HttpClientT

    def __init__(
        self,
        client_id: str,
        client_secret: str,
        *,
        base_url: str = IDENTITY_URL,
        scopes: Iterable[str] = _DEFAULT_SCOPES,
    ) -> None:
        if not client_id or not client_secret:
            raise ValueError("Both client_id and client_secret are required")
        self._client_id = client_id
        self._client_secret = client_secret
        self._base_url = parse_base_url(base_url)
        self._scope = scopes if isinstance(scopes, str) else " ".join(scopes)

    def authorization_url(self, redirect_uri: str, *, state: str, nonce: str | None = None) -> str:
        """Build the browser URL. Verify the returned state in your callback."""
        if not state:
            raise ValueError("state must be a non-empty value tied to the user's session")
        query = {
            "client_id": self._client_id,
            "response_type": "code",
            "scope": self._scope,
            "redirect_uri": redirect_uri,
            "state": state,
        }
        if nonce is not None:
            query["nonce"] = nonce
        return str(httpx.URL(f"{self._base_url}/connect/authorize", params=query))

    def _exchange_code_request(self, code: str, redirect_uri: str) -> httpx.Request:
        return self._build_post("token", grant_type="authorization_code", code=code, redirect_uri=redirect_uri)

    def _refresh_request(self, refresh_token: str) -> httpx.Request:
        return self._build_post("token", grant_type="refresh_token", refresh_token=refresh_token)

    def _device_authorization_request(self) -> httpx.Request:
        return self._build_post("deviceauthorization", scope=self._scope)

    def _poll_device_token_request(self, device_code: str) -> httpx.Request:
        return self._build_post(
            "token", grant_type="urn:ietf:params:oauth:grant-type:device_code", device_code=device_code, scope=self._scope,
        )

    def _build_post(self, endpoint: str, **form: str) -> httpx.Request:
        url = httpx.URL(f"{self._base_url}/connect/{endpoint}")
        data = {"client_id": self._client_id, "client_secret": self._client_secret, **form}
        return self._http.build_request("POST", url, data=data, headers={"Accept": "application/json"})


def _decode_device_authorization(response: httpx.Response) -> DeviceAuthorization:
    data = decode_json(response)
    fields = {"device_code", "user_code", "verification_uri", "expires_in"}
    if not isinstance(data, dict) or not fields.issubset(data):
        raise DiadocProtocolError("Expected a device authorization response", response)
    return cast(DeviceAuthorization, data)


def _decode_token(response: httpx.Response) -> TokenResponse:
    data = decode_json(response)
    if not isinstance(data, dict) or not isinstance(data.get("access_token"), str) or not data["access_token"]:
        raise DiadocProtocolError("Expected an OAuth access token", response)
    return cast(TokenResponse, data)


class OidcClient(_OidcBase[httpx.Client]):
    """Exchange authorization codes, device codes and refresh tokens at Kontur Identity."""

    def __init__(
        self,
        client_id: str,
        client_secret: str,
        *,
        base_url: str = IDENTITY_URL,
        scopes: Iterable[str] = _DEFAULT_SCOPES,
        timeout: float | httpx.Timeout = 30.0,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        super().__init__(client_id, client_secret, base_url=base_url, scopes=scopes)
        self._http = httpx.Client(timeout=timeout, transport=transport)

    def __enter__(self) -> OidcClient:
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

    def exchange_code(self, code: str, redirect_uri: str) -> TokenResponse:
        return _decode_token(self._send(self._exchange_code_request(code, redirect_uri)))

    def refresh(self, refresh_token: str) -> TokenResponse:
        """Exchange a refresh token. Persist the new refresh token returned by Identity."""
        return _decode_token(self._send(self._refresh_request(refresh_token)))

    def start_device_authorization(self) -> DeviceAuthorization:
        return _decode_device_authorization(self._send(self._device_authorization_request()))

    def poll_device_token(self, device_code: str) -> TokenResponse:
        """Check once for device approval. OAuth errors are available as OidcError.error."""
        return _decode_token(self._send(self._poll_device_token_request(device_code)))

    def _send(self, request: httpx.Request) -> httpx.Response:
        return send(self._http, request, OidcError)


class AsyncOidcClient(_OidcBase[httpx.AsyncClient]):
    """Asynchronously exchange authorization codes, device codes and refresh tokens at Kontur Identity."""

    def __init__(
        self,
        client_id: str,
        client_secret: str,
        *,
        base_url: str = IDENTITY_URL,
        scopes: Iterable[str] = _DEFAULT_SCOPES,
        timeout: float | httpx.Timeout = 30.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        super().__init__(client_id, client_secret, base_url=base_url, scopes=scopes)
        self._http = httpx.AsyncClient(timeout=timeout, transport=transport)

    async def __aenter__(self) -> AsyncOidcClient:
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

    async def exchange_code(self, code: str, redirect_uri: str) -> TokenResponse:
        return _decode_token(await self._send(self._exchange_code_request(code, redirect_uri)))

    async def refresh(self, refresh_token: str) -> TokenResponse:
        """Exchange a refresh token. Persist the new refresh token returned by Identity."""
        return _decode_token(await self._send(self._refresh_request(refresh_token)))

    async def start_device_authorization(self) -> DeviceAuthorization:
        return _decode_device_authorization(await self._send(self._device_authorization_request()))

    async def poll_device_token(self, device_code: str) -> TokenResponse:
        """Check once for device approval. OAuth errors are available as OidcError.error."""
        return _decode_token(await self._send(self._poll_device_token_request(device_code)))

    async def _send(self, request: httpx.Request) -> httpx.Response:
        return await async_send(self._http, request, OidcError)
