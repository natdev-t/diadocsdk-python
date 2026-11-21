"""Python SDK for the Kontur.Diadoc API."""

from . import proto
from ._client import PRODUCTION_URL, STAGING_URL
from ._http import DiadocError, DiadocHTTPError, DiadocProtocolError, DiadocTransportError
from ._methods import DiadocClient
from .oidc import IDENTITY_URL, PRODUCTION_SCOPE, STAGING_SCOPE, OidcClient, OidcError

__all__ = [
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
    "STAGING_SCOPE",
    "STAGING_URL",
    "proto",
]
