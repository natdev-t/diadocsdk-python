"""Python SDK for the Kontur.Diadoc API."""

from . import proto
from ._client import PRODUCTION_URL, STAGING_URL
from ._http import DiadocError, DiadocHTTPError, DiadocProtocolError, DiadocTransportError
from ._methods import AsyncDiadocClient, DiadocClient
from .oidc import (
    IDENTITY_URL,
    PRODUCTION_SCOPE,
    STAGING_SCOPE,
    AsyncOidcClient,
    AsyncRefreshTokenAuth,
    OidcClient,
    OidcError,
    RefreshTokenAuth,
)

__all__ = [
    "AsyncDiadocClient",
    "AsyncOidcClient",
    "AsyncRefreshTokenAuth",
    "DiadocClient",
    "DiadocError",
    "DiadocHTTPError",
    "DiadocProtocolError",
    "DiadocTransportError",
    "IDENTITY_URL",
    "OidcClient",
    "OidcError",
    "PRODUCTION_SCOPE",
    "PRODUCTION_URL",
    "RefreshTokenAuth",
    "STAGING_SCOPE",
    "STAGING_URL",
    "proto",
]
