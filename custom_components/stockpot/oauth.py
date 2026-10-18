"""OAuth2 sign-in with Stockpot (Stockpot M17, `docs/oauth-home-assistant.md`).

Public client `home-assistant`, Authorization Code + PKCE S256, scope `ha`. The endpoints come
from the RFC 8414 discovery document of the instance the user typed (shared with the Claude
connector: only the three endpoints below are read, extra fields are ignored). Refresh tokens
rotate; Home Assistant's `OAuth2Session` merges and persists each new pair in the entry.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
import hashlib
import ipaddress
import json
import logging
from typing import Any, cast
from urllib.parse import urlsplit

import aiohttp

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_URL, CONF_VERIFY_SSL
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import (
    OAuth2TokenRequestBaseError,
    OAuth2TokenRequestConnectionError,
    OAuth2TokenRequestError,
    OAuth2TokenRequestReauthError,
    OAuth2TokenRequestTransientError,
)
from homeassistant.helpers import config_entry_oauth2_flow
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import StockpotAuthError, StockpotConnectionError, StockpotError, StockpotSslError, TokenProvider
from .const import (
    CONF_AUTHORIZE_URL,
    CONF_REVOCATION_URL,
    CONF_TOKEN_URL,
    DOMAIN,
    OAUTH_CLIENT_ID,
    OAUTH_DISCOVERY_PATH,
    OAUTH_SCOPE,
    REQUEST_TIMEOUT,
)

_LOGGER = logging.getLogger(__name__)

CONF_AUTH_IMPLEMENTATION = "auth_implementation"
CONF_OAUTH_TOKEN = "token"


class StockpotNotOAuthError(StockpotError):
    """The URL does not serve a Stockpot authorization server document."""


class StockpotOAuthUnsupportedError(StockpotError):
    """A Stockpot without Home Assistant sign-in (before M17): use a manual token."""


class StockpotInsecureEndpointError(StockpotError):
    """An https Stockpot advertising an http endpoint (scheme downgrade)."""


class StockpotPublicHttpError(StockpotError):
    """Plain http to a host that is not loopback or private."""


# Removal: never hold up the deletion of the entry for long.
REMOVAL_TIMEOUT = 5
# Refresh: retried with identical data when no HTTP status was received (network error,
# timeout). Worst case 3 × 6 s + 2 s + 3 s = 23 s, inside Stockpot's 30 s same-pair window.
REFRESH_ATTEMPT_TIMEOUT = 6
REFRESH_RETRY_DELAYS: tuple[float, ...] = (2, 3)
PRIVATE_DNS_SUFFIXES = (".local", ".lan", ".home.arpa", ".internal", ".localdomain")


@dataclass(frozen=True)
class OAuthEndpoints:
    """What the integration reads from the discovery document."""

    authorize_url: str
    token_url: str
    revocation_url: str | None

    def as_data(self) -> dict[str, str | None]:
        """Entry data keys."""
        return {
            CONF_AUTHORIZE_URL: self.authorize_url,
            CONF_TOKEN_URL: self.token_url,
            CONF_REVOCATION_URL: self.revocation_url,
        }


def is_oauth_entry(data: Any) -> bool:
    """An entry created by the OAuth sign-in (manual token entries have no implementation)."""
    return CONF_AUTH_IMPLEMENTATION in data


def _http_url(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    parts = urlsplit(value)
    if parts.scheme not in ("http", "https") or not parts.netloc:
        return None
    return value


def _origin(url: str) -> str:
    parts = urlsplit(url)
    return f"{parts.scheme}://{parts.netloc}".lower()


def is_private_host(host: str | None) -> bool:
    """Loopback / private address, `localhost`, a single-label name or a LAN-only DNS suffix
    (.local, .lan, .home.arpa, .internal, .localdomain): where plain http is tolerated."""
    if not host:
        return False
    host = host.lower().rstrip(".")
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return host == "localhost" or "." not in host or host.endswith(PRIVATE_DNS_SUFFIXES)
    return address.is_loopback or address.is_private


def check_transport(url: str, endpoints: OAuthEndpoints) -> None:
    """Refuse a scheme downgrade and plain http to a public host; warn on another origin."""
    typed = urlsplit(url)
    for name, endpoint in (
        ("authorization", endpoints.authorize_url),
        ("token", endpoints.token_url),
        ("revocation", endpoints.revocation_url),
    ):
        if endpoint is None:
            continue
        parts = urlsplit(endpoint)
        if typed.scheme == "https" and parts.scheme == "http":
            raise StockpotInsecureEndpointError(f"http {name} endpoint for an https Stockpot")
        if parts.scheme == "http" and not is_private_host(parts.hostname):
            raise StockpotPublicHttpError(f"http {name} endpoint on a public host")
        if _origin(endpoint) != _origin(url):
            # Valid (LAN URL typed, public APP_URL), but worth knowing when debugging.
            _LOGGER.warning(
                "Stockpot %s endpoint is on %s, not on the typed address %s", name, _origin(endpoint), _origin(url)
            )


async def async_discover(hass: HomeAssistant, url: str, verify_ssl: bool) -> OAuthEndpoints:
    """Read the authorization server metadata of a Stockpot instance."""
    parts = urlsplit(url)
    if parts.scheme == "http" and not is_private_host(parts.hostname):
        raise StockpotPublicHttpError("http to a public host")
    session = async_get_clientsession(hass, verify_ssl=verify_ssl)
    try:
        async with asyncio.timeout(REQUEST_TIMEOUT):
            async with session.get(
                url.rstrip("/") + OAUTH_DISCOVERY_PATH,
                headers={"Accept": "application/json"},
                allow_redirects=False,
            ) as response:
                if response.status != 200:
                    raise StockpotNotOAuthError(f"Discovery answered {response.status}")
                try:
                    document = await response.json(content_type=None)
                except (ValueError, aiohttp.ContentTypeError) as err:
                    raise StockpotNotOAuthError("Discovery is not JSON") from err
    except aiohttp.ClientSSLError as err:
        raise StockpotSslError(str(err)) from err
    except (aiohttp.ClientError, TimeoutError) as err:
        raise StockpotConnectionError(str(err) or err.__class__.__name__) from err

    if not isinstance(document, dict):
        raise StockpotNotOAuthError("Discovery is not an object")
    authorize_url = _http_url(document.get("authorization_endpoint"))
    token_url = _http_url(document.get("token_endpoint"))
    if authorize_url is None or token_url is None:
        raise StockpotNotOAuthError("Discovery without authorization or token endpoint")
    # Superset shared with the Claude connector: only refuse what proves there is no `ha` client.
    scopes = document.get("scopes_supported")
    if isinstance(scopes, list) and OAUTH_SCOPE not in scopes:
        raise StockpotOAuthUnsupportedError("No `ha` scope")
    methods = document.get("code_challenge_methods_supported")
    if isinstance(methods, list) and "S256" not in methods:
        raise StockpotOAuthUnsupportedError("No PKCE S256")
    endpoints = OAuthEndpoints(authorize_url, token_url, _http_url(document.get("revocation_endpoint")))
    check_transport(url, endpoints)
    issuer = document.get("issuer")
    if isinstance(issuer, str) and issuer.rstrip("/").lower() != url.rstrip("/").lower():
        # Stockpot's issuer is its APP_URL; a LAN address typed in HA is legitimate.
        _LOGGER.info("Stockpot issuer %s differs from the typed address %s", _origin(issuer), _origin(url))
    return endpoints


def implementation_key(url: str) -> str:
    """Name of the implementation of a Stockpot instance (the entry's `auth_implementation`)."""
    return f"{DOMAIN}_{hashlib.sha256(url.encode()).hexdigest()[:16]}"


class StockpotOAuth2Implementation(config_entry_oauth2_flow.LocalOAuth2ImplementationWithPkce):
    """PKCE public client of one Stockpot instance; honours the entry's SSL check."""

    def __init__(self, hass: HomeAssistant, url: str, verify_ssl: bool, endpoints: OAuthEndpoints) -> None:
        """Create the implementation (a new PKCE verifier each time)."""
        super().__init__(
            hass,
            implementation_key(url),
            OAUTH_CLIENT_ID,
            authorize_url=endpoints.authorize_url,
            token_url=endpoints.token_url,
        )
        self._url = url
        self._verify_ssl = verify_ssl

    @property
    def name(self) -> str:
        """Shown if Home Assistant ever lists the implementations."""
        return f"Stockpot ({urlsplit(self._url).netloc})"

    @property
    def service_domain(self) -> str:
        """Errors are reported for the integration, not for the per-instance key."""
        return DOMAIN

    @property
    def extra_authorize_data(self) -> dict:
        """PKCE challenge + the single `ha` scope."""
        return {"scope": OAUTH_SCOPE, **super().extra_authorize_data}

    async def _token_request(self, data: dict) -> dict:
        """POST /oauth/token (same mapping as Home Assistant's, with the entry's SSL check).

        4xx (`invalid_grant`…) → reauthentication; 429 / 5xx → transient; no answer → connection.
        A refresh that got no HTTP status is retried with the same data: Stockpot answers a
        rotated refresh token presented again within 30 s with the same pair. A code exchange
        is never retried here. Only the OAuth error code is logged, never a token.
        """
        data["client_id"] = self.client_id
        if data.get("grant_type") != "refresh_token":
            return await self._token_post(data, REQUEST_TIMEOUT)
        for delay in (*REFRESH_RETRY_DELAYS, None):
            try:
                return await self._token_post(data, REFRESH_ATTEMPT_TIMEOUT)
            except OAuth2TokenRequestConnectionError as err:
                if delay is None or not _no_answer(err.__cause__):
                    raise
                _LOGGER.debug("Stockpot token refresh got no answer, retrying in %s s", delay)
                await asyncio.sleep(delay)
        raise AssertionError("unreachable")  # pragma: no cover

    async def _token_post(self, data: dict, time_limit: float) -> dict:
        session = async_get_clientsession(self.hass, verify_ssl=self._verify_ssl)
        try:
            async with (
                asyncio.timeout(time_limit),
                session.post(self.token_url, data=data, allow_redirects=False) as response,
            ):
                if response.status >= 400:
                    _LOGGER.debug(
                        "Stockpot token request failed (%s): %s",
                        response.status,
                        await _oauth_error(response),
                    )
                response.raise_for_status()
                try:
                    payload = await response.json(content_type=None)
                except (ValueError, aiohttp.ContentTypeError) as err:
                    raise OAuth2TokenRequestConnectionError(domain=DOMAIN) from err
                if not isinstance(payload, dict):
                    raise OAuth2TokenRequestConnectionError(domain=DOMAIN)
                return cast(dict, payload)
        except OAuth2TokenRequestConnectionError:
            raise  # unusable body (a ClientError too): not a network failure
        except aiohttp.ClientResponseError as err:
            kwargs: dict[str, Any] = {
                "request_info": err.request_info,
                "history": err.history or (),
                "status": err.status,
                "message": err.message,
                "headers": err.headers,
                "domain": DOMAIN,
            }
            if err.status == 429 or err.status >= 500:
                raise OAuth2TokenRequestTransientError(**kwargs) from err
            if 400 <= err.status < 500:
                raise OAuth2TokenRequestReauthError(**kwargs) from err
            raise OAuth2TokenRequestError(**kwargs) from err
        except (aiohttp.ClientError, TimeoutError) as err:
            raise OAuth2TokenRequestConnectionError(domain=DOMAIN) from err


def _no_answer(cause: BaseException | None) -> bool:
    """Network error or timeout without any HTTP status (a certificate error is not retried)."""
    if isinstance(cause, (aiohttp.ClientResponseError, aiohttp.ClientSSLError, OAuth2TokenRequestBaseError)):
        return False
    return isinstance(cause, (aiohttp.ClientError, TimeoutError))


async def _oauth_error(response: aiohttp.ClientResponse) -> str:
    try:
        payload = json.loads(await response.text())
    except aiohttp.ClientError, ValueError:
        return "unreadable error"
    if isinstance(payload, dict):
        return str(payload.get("error") or "unknown error")
    return "unknown error"


def endpoints_from_data(data: Any) -> OAuthEndpoints:
    """Endpoints persisted in the entry at sign-in."""
    return OAuthEndpoints(data[CONF_AUTHORIZE_URL], data[CONF_TOKEN_URL], data.get(CONF_REVOCATION_URL))


def entry_implementation(hass: HomeAssistant, entry: ConfigEntry) -> StockpotOAuth2Implementation:
    """Rebuild the entry's implementation from its own data (URL, endpoints, SSL check).

    Handed straight to `OAuth2Session`: not registered with the OAuth2 helper, whose registry
    is keyed by implementation and would let two entries of one instance share an SSL check.
    """
    return StockpotOAuth2Implementation(
        hass, entry.data[CONF_URL], entry.data.get(CONF_VERIFY_SSL, True), endpoints_from_data(entry.data)
    )


def oauth_token_provider(
    hass: HomeAssistant, entry: ConfigEntry, session: config_entry_oauth2_flow.OAuth2Session
) -> TokenProvider:
    """Valid access token of an OAuth entry, refreshed (and persisted) by `OAuth2Session`.

    `invalid_grant` (and any other 4xx) on refresh: `OAuth2Session` starts the reauth flow and
    the client sees a StockpotAuthError. Given the token Stockpot refused, it is renewed first
    unless a concurrent request already did.
    """

    async def provide(refused: str | None) -> str:
        if refused is not None and session.token.get("access_token") == refused:
            hass.config_entries.async_update_entry(
                entry, data={**entry.data, CONF_OAUTH_TOKEN: {**session.token, "expires_at": 0}}
            )
        try:
            await session.async_ensure_token_valid()
        except OAuth2TokenRequestReauthError as err:
            raise StockpotAuthError("invalid_grant") from err
        except OAuth2TokenRequestBaseError as err:
            raise StockpotConnectionError("Token refresh failed") from err
        return str(session.token["access_token"])

    return provide


async def async_revoke(
    hass: HomeAssistant,
    data: Any,
    token: str | None,
    token_type_hint: str = "refresh_token",
    time_limit: float = REQUEST_TIMEOUT,
) -> bool:
    """RFC 7009 revocation, best effort. A refresh token revokes the whole Stockpot access
    (all its tokens and the webhook it registered); an access token only itself."""
    revocation_url = data.get(CONF_REVOCATION_URL)
    if not revocation_url or not token:
        return False
    session = async_get_clientsession(hass, verify_ssl=data.get(CONF_VERIFY_SSL, True))
    try:
        async with asyncio.timeout(time_limit):
            async with session.post(
                revocation_url,
                data={"token": token, "token_type_hint": token_type_hint, "client_id": OAUTH_CLIENT_ID},
                allow_redirects=False,
            ) as response:
                if response.status >= 400:
                    error = await _oauth_error(response)
                    _LOGGER.warning("Stockpot refused the revocation (%s): %s", response.status, error)
                    return False
    except (aiohttp.ClientError, TimeoutError) as err:
        _LOGGER.warning("Could not revoke the Stockpot access: %s", err.__class__.__name__)
        return False
    return True
