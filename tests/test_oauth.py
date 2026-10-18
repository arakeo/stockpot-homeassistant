"""OAuth2 sign-in (Stockpot M17): discovery, PKCE flow, refresh rotation, reauth, revocation."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import logging
import time
from typing import Any
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

import aiohttp
import pytest

from homeassistant.config_entries import SOURCE_USER, ConfigEntryState
from homeassistant.const import CONF_URL, CONF_VERIFY_SSL
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import aiohttp_client
from homeassistant.helpers.config_entry_oauth2_flow import MY_AUTH_CALLBACK_PATH
from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.components.diagnostics import get_diagnostics_for_config_entry
from pytest_homeassistant_custom_component.test_util.aiohttp import AiohttpClientMocker, AiohttpClientMockResponse

from custom_components.stockpot.api import StockpotAuthError
from custom_components.stockpot.const import CONF_AUTH_METHOD, CONF_HOUSEHOLD_ID, DOMAIN, STOCKPOT_URL
from custom_components.stockpot.oauth import (
    StockpotPublicHttpError,
    async_discover,
    implementation_key,
)

from .conftest import (
    ACCESS_TOKEN,
    API,
    AUTHORIZE_URL,
    DISCOVERY,
    DISCOVERY_URL,
    INSTANCE,
    REFRESH_TOKEN,
    REVOKE_URL,
    TOKEN_URL,
    URL,
    authorize as _authorize,
    calls as _calls,
    token_response,
)

HOUSEHOLDS = {"data": [{"id": 7, "name": "Foyer Martin", "role": "owner"}]}
LEGACY_URL = "https://stockpot.example.org"


@pytest.fixture(autouse=True)
async def _oauth_callback(oauth_callback: None) -> None:
    """My Home Assistant redirect and the /auth/external/callback view, for every test."""


async def _start_oauth(hass: HomeAssistant) -> dict[str, Any]:
    """« Ajouter l'intégration → Stockpot »: no form, straight to the sign-in (or an error form)."""
    return await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})


def _discovery_of(url: str) -> dict[str, Any]:
    return {
        **DISCOVERY,
        "issuer": url,
        "authorization_endpoint": f"{url}/oauth/authorize",
        "token_endpoint": f"{url}/oauth/token",
        "revocation_endpoint": f"{url}/oauth/revoke",
    }


async def test_oauth_happy_path(hass: HomeAssistant, hass_client_no_auth, aioclient_mock: AiohttpClientMocker) -> None:
    """No form: discovery at STOCKPOT_URL → PKCE authorize URL → code exchange → entry
    (instance#household), SSL check on and not stored."""
    assert URL == STOCKPOT_URL == "https://stockpot.arakeo.com"
    aioclient_mock.get(DISCOVERY_URL, json=DISCOVERY)
    aioclient_mock.post(TOKEN_URL, json=token_response())
    aioclient_mock.get(f"{API}/households", json=HOUSEHOLDS)

    with patch(
        "custom_components.stockpot.oauth.async_get_clientsession", wraps=aiohttp_client.async_get_clientsession
    ) as session:
        result = await _start_oauth(hass)
    assert result["type"] is FlowResultType.EXTERNAL_STEP
    assert result["step_id"] == "auth"
    assert [str(url) for _, url, _, _ in aioclient_mock.mock_calls] == [DISCOVERY_URL]
    assert session.call_args.kwargs["verify_ssl"] is True
    authorize = urlsplit(result["url"])
    query = {key: values[0] for key, values in parse_qs(authorize.query).items()}
    assert f"{authorize.scheme}://{authorize.netloc}{authorize.path}" == AUTHORIZE_URL
    assert query["response_type"] == "code"
    assert query["client_id"] == "home-assistant"
    assert query["redirect_uri"] == MY_AUTH_CALLBACK_PATH
    assert query["code_challenge_method"] == "S256"
    assert query["scope"] == "ha"

    with patch("custom_components.stockpot.async_setup_entry", return_value=True):
        result = await _authorize(hass, hass_client_no_auth, result)

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "Foyer Martin"
    entry = result["result"]
    assert entry.unique_id == f"{INSTANCE}#7"
    assert entry.data[CONF_URL] == URL
    assert entry.data[CONF_HOUSEHOLD_ID] == 7
    assert entry.data[CONF_AUTH_METHOD] == "oauth"
    assert entry.data["token"]["access_token"] == ACCESS_TOKEN
    assert entry.data["token"]["refresh_token"] == REFRESH_TOKEN
    assert entry.data["revocation_url"] == REVOKE_URL
    assert entry.data["auth_implementation"] == implementation_key(STOCKPOT_URL)
    assert CONF_VERIFY_SSL not in entry.data

    # Public client: no secret; the verifier matches the S256 challenge sent to /oauth/authorize.
    [(form, _)] = _calls(aioclient_mock, "POST", TOKEN_URL)
    assert form["grant_type"] == "authorization_code"
    assert form["code"] == "the-code"
    assert form["client_id"] == "home-assistant"
    assert form["redirect_uri"] == MY_AUTH_CALLBACK_PATH
    assert "client_secret" not in form
    challenge = base64.urlsafe_b64encode(hashlib.sha256(form["code_verifier"].encode()).digest()).rstrip(b"=")
    assert challenge.decode() == query["code_challenge"]
    # The household name is read with the new access token.
    [(_, headers)] = _calls(aioclient_mock, "GET", f"{API}/households")
    assert headers["Authorization"] == f"Bearer {ACCESS_TOKEN}"


async def test_oauth_duplicate_is_aborted(
    hass: HomeAssistant, hass_client_no_auth, aioclient_mock: AiohttpClientMocker, config_entry: MockConfigEntry
) -> None:
    """Same instance and household already configured (here with a manual token) → abort; the
    access just created (its own Stockpot grant) is revoked with its refresh token."""
    config_entry.add_to_hass(hass)
    aioclient_mock.get(DISCOVERY_URL, json=DISCOVERY)
    aioclient_mock.post(TOKEN_URL, json=token_response())
    aioclient_mock.get(f"{API}/households", json=HOUSEHOLDS)
    aioclient_mock.post(REVOKE_URL, json={})

    result = await _authorize(hass, hass_client_no_auth, await _start_oauth(hass))

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"
    [(form, _)] = _calls(aioclient_mock, "POST", REVOKE_URL)
    assert form == {"token": REFRESH_TOKEN, "token_type_hint": "refresh_token", "client_id": "home-assistant"}


async def test_oauth_two_factor_required_after_sign_in(
    hass: HomeAssistant, hass_client_no_auth, aioclient_mock: AiohttpClientMocker
) -> None:
    """Household with mandatory 2FA past the grace period: abort, the new access is revoked."""
    aioclient_mock.get(DISCOVERY_URL, json=DISCOVERY)
    aioclient_mock.post(TOKEN_URL, json=token_response())
    aioclient_mock.get(f"{API}/households", status=403, json={"code": "two_factor_required"})
    aioclient_mock.post(REVOKE_URL, json={})

    result = await _authorize(hass, hass_client_no_auth, await _start_oauth(hass))

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "two_factor_required"
    assert len(_calls(aioclient_mock, "POST", REVOKE_URL)) == 1
    assert hass.config_entries.async_entries(DOMAIN) == []


@pytest.mark.parametrize(
    ("mock", "error"),
    [
        ({"status": 404, "text": "Not found"}, "not_stockpot"),
        ({"text": "<html>Another site</html>"}, "not_stockpot"),
        ({"json": {"issuer": URL, "token_endpoint": TOKEN_URL}}, "not_stockpot"),
        ({"json": {**DISCOVERY, "authorization_endpoint": "javascript:alert(1)"}}, "not_stockpot"),
        # A Stockpot before M17: only the Claude connector's scope.
        ({"json": {**DISCOVERY, "scopes_supported": ["mcp:use"]}}, "oauth_unsupported"),
        ({"exc": TimeoutError()}, "cannot_connect"),
        # Transient: maintenance, overload.
        ({"status": 503, "text": "maintenance"}, "cannot_connect"),
        ({"status": 500, "json": {"message": "Server Error"}}, "cannot_connect"),
        ({"status": 429, "json": {"message": "Too Many Attempts."}}, "cannot_connect"),
    ],
)
async def test_oauth_discovery_errors(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, mock: dict[str, Any], error: str
) -> None:
    """Discovery failure: a form without any field tells why; submitting it tries again."""
    aioclient_mock.get(DISCOVERY_URL, **mock)
    result = await _start_oauth(hass)
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "user"
    assert result["errors"] == {"base": error}
    assert result["data_schema"] is None
    assert result["description_placeholders"]["url"] == STOCKPOT_URL

    aioclient_mock.clear_requests()
    aioclient_mock.get(DISCOVERY_URL, json=DISCOVERY)
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    assert result["type"] is FlowResultType.EXTERNAL_STEP


async def test_oauth_discovery_tolerates_a_minimal_document(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    """Only the two endpoints are required (no scopes, methods or revocation listed)."""
    aioclient_mock.get(
        DISCOVERY_URL, json={"authorization_endpoint": AUTHORIZE_URL, "token_endpoint": TOKEN_URL, "extra": [1]}
    )
    result = await _start_oauth(hass)
    assert result["type"] is FlowResultType.EXTERNAL_STEP


async def test_refresh_persists_the_rotated_refresh_token(
    hass: HomeAssistant, mock_api: AiohttpClientMocker, oauth_config_entry: MockConfigEntry
) -> None:
    """Expired access token: refreshed before the first request, the new pair is stored."""
    oauth_config_entry.add_to_hass(hass)
    hass.config_entries.async_update_entry(
        oauth_config_entry,
        data={**oauth_config_entry.data, "token": {**oauth_config_entry.data["token"], "expires_at": time.time() - 10}},
    )
    mock_api.post(TOKEN_URL, json=token_response(access="32|stkp_new", refresh="refresh-secret-2"))

    assert await hass.config_entries.async_setup(oauth_config_entry.entry_id)
    await hass.async_block_till_done()

    assert oauth_config_entry.state is ConfigEntryState.LOADED
    token = oauth_config_entry.data["token"]
    assert token["access_token"] == "32|stkp_new"
    assert token["refresh_token"] == "refresh-secret-2"
    assert token["expires_at"] > time.time() + 3000
    [(form, _)] = _calls(mock_api, "POST", TOKEN_URL)
    assert form == {"grant_type": "refresh_token", "refresh_token": REFRESH_TOKEN, "client_id": "home-assistant"}
    [(_, headers)] = _calls(mock_api, "GET", f"{API}/ha/summary")
    assert headers["Authorization"] == "Bearer 32|stkp_new"
    assert headers["X-Household"] == "7"


async def test_invalid_grant_starts_reauth(
    hass: HomeAssistant, mock_api: AiohttpClientMocker, oauth_config_entry: MockConfigEntry
) -> None:
    """Refresh refused (revoked, idle, replay, user removed) → reauthentication flow."""
    oauth_config_entry.add_to_hass(hass)
    hass.config_entries.async_update_entry(
        oauth_config_entry,
        data={**oauth_config_entry.data, "token": {**oauth_config_entry.data["token"], "expires_at": 0}},
    )
    mock_api.post(TOKEN_URL, status=400, json={"error": "invalid_grant", "error_description": "Revoked."})

    await hass.config_entries.async_setup(oauth_config_entry.entry_id)
    await hass.async_block_till_done()

    assert oauth_config_entry.state is ConfigEntryState.SETUP_ERROR
    flows = hass.config_entries.flow.async_progress_by_handler(DOMAIN)
    assert flows and flows[0]["context"]["source"] == "reauth"
    assert flows[0]["step_id"] == "reauth_oauth"
    assert _calls(mock_api, "GET", f"{API}/ha/summary") == []


async def test_401_is_retried_once_after_a_refresh_then_reauth(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, oauth_config_entry: MockConfigEntry
) -> None:
    """Access token refused before its expiry: one refresh and one retry, then reauthentication."""
    aioclient_mock.get(f"{API}/ha/summary", status=401, json={"code": "unauthenticated"})
    aioclient_mock.post(TOKEN_URL, json=token_response(access="32|stkp_new", refresh="refresh-secret-2"))
    oauth_config_entry.add_to_hass(hass)

    await hass.config_entries.async_setup(oauth_config_entry.entry_id)
    await hass.async_block_till_done()

    assert len(_calls(aioclient_mock, "POST", TOKEN_URL)) == 1
    sent = [headers["Authorization"] for _, headers in _calls(aioclient_mock, "GET", f"{API}/ha/summary")]
    assert sent == [f"Bearer {ACCESS_TOKEN}", "Bearer 32|stkp_new"]
    assert oauth_config_entry.data["token"]["refresh_token"] == "refresh-secret-2"
    assert oauth_config_entry.state is ConfigEntryState.SETUP_ERROR
    flows = hass.config_entries.flow.async_progress_by_handler(DOMAIN)
    assert flows and flows[0]["context"]["source"] == "reauth"


async def test_reauth_success_updates_the_entry(
    hass: HomeAssistant, hass_client_no_auth, aioclient_mock: AiohttpClientMocker, oauth_config_entry: MockConfigEntry
) -> None:
    """Sign in again: same instance and household → new tokens stored, entry reloaded, and the
    previous refresh token revoked (each code exchange is its own Stockpot grant)."""
    oauth_config_entry.add_to_hass(hass)
    aioclient_mock.get(DISCOVERY_URL, json=DISCOVERY)
    aioclient_mock.post(TOKEN_URL, json=token_response(access="40|stkp_again", refresh="refresh-secret-9"))
    aioclient_mock.get(f"{API}/households", json=HOUSEHOLDS)
    aioclient_mock.post(REVOKE_URL, json={})

    result = await oauth_config_entry.start_reauth_flow(hass)
    assert result["step_id"] == "reauth_oauth"
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    with patch("custom_components.stockpot.async_setup_entry", return_value=True):
        result = await _authorize(hass, hass_client_no_auth, result)

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reauth_successful"
    assert oauth_config_entry.data["token"]["access_token"] == "40|stkp_again"
    assert oauth_config_entry.data["token"]["refresh_token"] == "refresh-secret-9"
    assert oauth_config_entry.unique_id == f"{INSTANCE}#7"
    [(form, _)] = _calls(aioclient_mock, "POST", REVOKE_URL)
    assert form == {"token": REFRESH_TOKEN, "token_type_hint": "refresh_token", "client_id": "home-assistant"}


async def test_reauth_survives_a_failed_revocation_of_the_previous_access(
    hass: HomeAssistant, hass_client_no_auth, aioclient_mock: AiohttpClientMocker, oauth_config_entry: MockConfigEntry
) -> None:
    """Revoking the previous grant is best effort: the new tokens are kept anyway."""
    oauth_config_entry.add_to_hass(hass)
    aioclient_mock.get(DISCOVERY_URL, json=DISCOVERY)
    aioclient_mock.post(TOKEN_URL, json=token_response(access="40|stkp_again", refresh="refresh-secret-9"))
    aioclient_mock.get(f"{API}/households", json=HOUSEHOLDS)
    aioclient_mock.post(REVOKE_URL, exc=TimeoutError())

    result = await oauth_config_entry.start_reauth_flow(hass)
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    with patch("custom_components.stockpot.async_setup_entry", return_value=True):
        result = await _authorize(hass, hass_client_no_auth, result)

    assert result["reason"] == "reauth_successful"
    assert oauth_config_entry.data["token"]["refresh_token"] == "refresh-secret-9"


@pytest.mark.parametrize(
    ("mock", "error"),
    [
        ({"status": 404, "text": "Not found"}, "not_stockpot"),
        ({"json": {**DISCOVERY, "scopes_supported": ["mcp:use"]}}, "oauth_unsupported"),
    ],
)
async def test_oauth_entry_reauth_never_offers_a_token(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    oauth_config_entry: MockConfigEntry,
    mock: dict[str, Any],
    error: str,
) -> None:
    """An OAuth entry whose Stockpot no longer offers the sign-in: the error on the sign-in
    form, no menu and no token form."""
    oauth_config_entry.add_to_hass(hass)
    aioclient_mock.get(DISCOVERY_URL, **mock)
    result = await oauth_config_entry.start_reauth_flow(hass)
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "reauth_oauth"
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "reauth_oauth"
    assert result["errors"] == {"base": error}


async def test_reauth_with_another_household_is_refused(
    hass: HomeAssistant, hass_client_no_auth, aioclient_mock: AiohttpClientMocker, oauth_config_entry: MockConfigEntry
) -> None:
    """Another household chosen on the consent screen → wrong_instance, entry untouched."""
    oauth_config_entry.add_to_hass(hass)
    aioclient_mock.get(DISCOVERY_URL, json=DISCOVERY)
    aioclient_mock.post(TOKEN_URL, json=token_response(access="41|stkp_other", refresh="refresh-other", household_id=8))
    aioclient_mock.get(f"{API}/households", json={"data": [{"id": 8, "name": "Chalet", "role": "owner"}]})
    aioclient_mock.post(REVOKE_URL, json={})

    result = await oauth_config_entry.start_reauth_flow(hass)
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    result = await _authorize(hass, hass_client_no_auth, result)

    assert result["reason"] == "wrong_instance"
    assert oauth_config_entry.data["token"]["access_token"] == ACCESS_TOKEN
    [(form, _)] = _calls(aioclient_mock, "POST", REVOKE_URL)
    assert form["token"] == "refresh-other"  # the access just created, not the entry's
    assert form["token_type_hint"] == "refresh_token"


async def test_reconfigure_oauth_entry(
    hass: HomeAssistant, hass_client_no_auth, aioclient_mock: AiohttpClientMocker, oauth_config_entry: MockConfigEntry
) -> None:
    """Reconfigure only signs in again at the entry's URL (no field): new tokens stored, the
    previous access revoked."""
    oauth_config_entry.add_to_hass(hass)
    aioclient_mock.get(DISCOVERY_URL, json=DISCOVERY)
    aioclient_mock.post(TOKEN_URL, json=token_response(access="42|stkp_again", refresh="refresh-again"))
    aioclient_mock.get(f"{API}/households", json=HOUSEHOLDS)
    aioclient_mock.post(REVOKE_URL, json={})

    result = await oauth_config_entry.start_reconfigure_flow(hass)
    assert result["step_id"] == "reconfigure_oauth"
    assert result["data_schema"] is None
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    with patch("custom_components.stockpot.async_setup_entry", return_value=True):
        result = await _authorize(hass, hass_client_no_auth, result)

    assert result["reason"] == "reconfigure_successful"
    assert oauth_config_entry.data[CONF_URL] == URL
    assert oauth_config_entry.data["token"]["access_token"] == "42|stkp_again"
    [(form, _)] = _calls(aioclient_mock, "POST", REVOKE_URL)
    assert form["token"] == REFRESH_TOKEN


async def test_legacy_oauth_entry_with_custom_url_and_no_ssl_check(
    hass: HomeAssistant,
    hass_client_no_auth,
    aioclient_mock: AiohttpClientMocker,
    oauth_config_entry: MockConfigEntry,
    summary: dict[str, Any],
) -> None:
    """A 0.10.0 OAuth entry on another URL with the SSL check disabled: it loads and refreshes
    against its own URL without the check, and its reauth signs in there, settings kept."""
    legacy = _discovery_of(LEGACY_URL)
    api = f"{LEGACY_URL}/api/v1"
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="Foyer Martin",
        unique_id=f"{INSTANCE}#7",
        data={
            **oauth_config_entry.data,
            CONF_URL: LEGACY_URL,
            CONF_VERIFY_SSL: False,
            "authorize_url": legacy["authorization_endpoint"],
            "token_url": legacy["token_endpoint"],
            "revocation_url": legacy["revocation_endpoint"],
            "auth_implementation": implementation_key(LEGACY_URL),
            "token": {**oauth_config_entry.data["token"], "expires_at": time.time() - 10},
        },
    )
    aioclient_mock.post(legacy["token_endpoint"], json=token_response(access="32|stkp_new", refresh="r-2"))
    aioclient_mock.get(f"{api}/me", json={"data": {"id": 1}, "meta": {"instance_id": INSTANCE}})
    aioclient_mock.get(f"{api}/ha/summary", json={"data": summary})
    aioclient_mock.get(f"{api}/shopping-lists/1", json={"data": {"id": 1, "name": "Courses", "items": []}})
    aioclient_mock.put(f"{api}/households/7/ha-webhook", status=403, json={"code": "forbidden"})
    with (
        patch(
            "custom_components.stockpot.oauth.async_get_clientsession", wraps=aiohttp_client.async_get_clientsession
        ) as oauth_session,
        patch(
            "custom_components.stockpot.async_get_clientsession", wraps=aiohttp_client.async_get_clientsession
        ) as api_session,
    ):
        await _setup(hass, entry)

    assert entry.state is ConfigEntryState.LOADED
    assert entry.data["token"]["refresh_token"] == "r-2"
    assert entry.data[CONF_URL] == LEGACY_URL
    assert entry.data[CONF_VERIFY_SSL] is False
    assert {call.kwargs["verify_ssl"] for call in oauth_session.call_args_list} == {False}
    assert {call.kwargs["verify_ssl"] for call in api_session.call_args_list} == {False}
    assert _calls(aioclient_mock, "POST", TOKEN_URL) == []
    assert len(_calls(aioclient_mock, "GET", f"{api}/ha/summary")) == 1

    # Reauth: discovery at its own URL, still without the SSL check; settings kept.
    aioclient_mock.get(f"{LEGACY_URL}/.well-known/oauth-authorization-server", json=legacy)
    aioclient_mock.get(f"{api}/households", json=HOUSEHOLDS)
    aioclient_mock.post(legacy["revocation_endpoint"], json={})
    aioclient_mock.post(legacy["token_endpoint"], json=token_response(access="33|stkp_re", refresh="r-3"))
    result = await entry.start_reauth_flow(hass)
    with patch(
        "custom_components.stockpot.oauth.async_get_clientsession", wraps=aiohttp_client.async_get_clientsession
    ) as session:
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    assert result["type"] is FlowResultType.EXTERNAL_STEP
    assert result["url"].startswith(legacy["authorization_endpoint"])
    assert session.call_args.kwargs["verify_ssl"] is False
    with patch("custom_components.stockpot.async_setup_entry", return_value=True):
        result = await _authorize(hass, hass_client_no_auth, result)
    assert result["reason"] == "reauth_successful"
    assert entry.data[CONF_URL] == LEGACY_URL
    assert entry.data[CONF_VERIFY_SSL] is False
    assert entry.data["auth_implementation"] == implementation_key(LEGACY_URL)


async def test_removal_revokes_the_refresh_token(
    hass: HomeAssistant, mock_api: AiohttpClientMocker, oauth_config_entry: MockConfigEntry
) -> None:
    """RFC 7009 on removal: the refresh token ends the whole access (and its webhook)."""
    mock_api.post(REVOKE_URL, json={})
    oauth_config_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(oauth_config_entry.entry_id)
    await hass.async_block_till_done()

    await hass.config_entries.async_remove(oauth_config_entry.entry_id)
    await hass.async_block_till_done()

    [(form, _)] = _calls(mock_api, "POST", REVOKE_URL)
    assert form == {"token": REFRESH_TOKEN, "token_type_hint": "refresh_token", "client_id": "home-assistant"}
    # The OAuth access token is not used to delete the webhook: revoking does it on Stockpot's side.
    assert _calls(mock_api, "DELETE", f"{API}/households/7/ha-webhook") == []


async def test_failed_revocation_is_logged_without_secrets(
    hass: HomeAssistant,
    mock_api: AiohttpClientMocker,
    oauth_config_entry: MockConfigEntry,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Best effort: an error is logged (status, OAuth error code), never a token."""
    mock_api.post(REVOKE_URL, status=500, json={"error": "server_error", "echo": REFRESH_TOKEN})
    oauth_config_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(oauth_config_entry.entry_id)
    await hass.async_block_till_done()

    with caplog.at_level(logging.DEBUG):
        await hass.config_entries.async_remove(oauth_config_entry.entry_id)
        await hass.async_block_till_done()

    assert hass.config_entries.async_get_entry(oauth_config_entry.entry_id) is None
    assert "Stockpot refused the revocation (500): server_error" in caplog.text
    assert REFRESH_TOKEN not in caplog.text
    assert ACCESS_TOKEN not in caplog.text


async def test_oauth_diagnostics_redact_the_tokens(
    hass: HomeAssistant, hass_client, mock_api: AiohttpClientMocker, oauth_config_entry: MockConfigEntry
) -> None:
    """Access and refresh tokens redacted; expiry and extensions kept."""
    oauth_config_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(oauth_config_entry.entry_id)
    await hass.async_block_till_done()

    diagnostics = await get_diagnostics_for_config_entry(hass, hass_client, oauth_config_entry)

    assert diagnostics["auth_method"] == "oauth"
    token = diagnostics["entry"]["token"]
    assert token["access_token"] == "**REDACTED**"
    assert token["refresh_token"] == "**REDACTED**"
    assert token["household_id"] == 7
    assert "expires_at" in token
    assert ACCESS_TOKEN not in str(diagnostics)
    assert REFRESH_TOKEN not in str(diagnostics)


def _expire(hass: HomeAssistant, entry: MockConfigEntry, **data: Any) -> None:
    """Access token expired (refresh due at the next request)."""
    if hass.config_entries.async_get_entry(entry.entry_id) is None:
        entry.add_to_hass(hass)
    hass.config_entries.async_update_entry(
        entry, data={**entry.data, **data, "token": {**entry.data["token"], "expires_at": time.time() - 10}}
    )


async def _setup(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    if hass.config_entries.async_get_entry(entry.entry_id) is None:
        entry.add_to_hass(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()


# --- server dependency guards -------------------------------------------------------


async def test_reauth_with_the_same_refresh_token_does_not_revoke(
    hass: HomeAssistant, hass_client_no_auth, aioclient_mock: AiohttpClientMocker, oauth_config_entry: MockConfigEntry
) -> None:
    """A server answering with the refresh token already stored: never revoke what is in use."""
    oauth_config_entry.add_to_hass(hass)
    aioclient_mock.get(DISCOVERY_URL, json=DISCOVERY)
    aioclient_mock.post(TOKEN_URL, json=token_response(access="40|stkp_again"))  # same REFRESH_TOKEN
    aioclient_mock.get(f"{API}/households", json=HOUSEHOLDS)
    aioclient_mock.post(REVOKE_URL, json={})

    result = await oauth_config_entry.start_reauth_flow(hass)
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    with patch("custom_components.stockpot.async_setup_entry", return_value=True):
        result = await _authorize(hass, hass_client_no_auth, result)

    assert result["reason"] == "reauth_successful"
    assert oauth_config_entry.data["token"]["access_token"] == "40|stkp_again"
    assert _calls(aioclient_mock, "POST", REVOKE_URL) == []


# --- refresh --------------------------------------------------------------------------


async def test_refresh_without_answer_is_retried_with_the_same_data(
    hass: HomeAssistant, mock_api: AiohttpClientMocker, oauth_config_entry: MockConfigEntry
) -> None:
    """No HTTP status (network error): the same refresh request again (30 s same-pair window)."""
    attempts: list[Any] = []

    async def flaky(method, url, data):
        attempts.append(dict(data))
        if len(attempts) == 1:
            raise aiohttp.ClientConnectionError("reset")
        return AiohttpClientMockResponse(method, url, json=token_response(access="32|stkp_new", refresh="r-2"))

    mock_api.post(TOKEN_URL, side_effect=flaky)
    _expire(hass, oauth_config_entry)
    with patch("custom_components.stockpot.oauth.REFRESH_RETRY_DELAYS", (0, 0)):
        await _setup(hass, oauth_config_entry)

    assert oauth_config_entry.state is ConfigEntryState.LOADED
    assert len(attempts) == 2
    assert attempts[0] == attempts[1]
    assert attempts[0]["refresh_token"] == REFRESH_TOKEN
    assert oauth_config_entry.data["token"]["refresh_token"] == "r-2"


@pytest.mark.parametrize(
    ("mock", "posts"),
    [
        ({"status": 500, "json": {"error": "server_error"}}, 1),
        ({"status": 503, "text": "maintenance"}, 1),
        ({"status": 429, "json": {"error": "slow_down"}}, 1),
        ({"exc": TimeoutError()}, 3),
        ({"json": ["not", "an", "object"]}, 1),
    ],
)
async def test_transient_refresh_failure_retries_setup_not_reauth(
    hass: HomeAssistant,
    mock_api: AiohttpClientMocker,
    oauth_config_entry: MockConfigEntry,
    mock: dict[str, Any],
    posts: int,
) -> None:
    """5xx, 429, timeout or an unusable body: setup retried later, no reauthentication; only
    a missing answer is retried in place (at most 3 attempts)."""
    mock_api.post(TOKEN_URL, **mock)
    _expire(hass, oauth_config_entry)
    with patch("custom_components.stockpot.oauth.REFRESH_RETRY_DELAYS", (0, 0)):
        await _setup(hass, oauth_config_entry)

    assert oauth_config_entry.state is ConfigEntryState.SETUP_RETRY
    assert hass.config_entries.flow.async_progress_by_handler(DOMAIN) == []
    assert len(_calls(mock_api, "POST", TOKEN_URL)) == posts
    assert oauth_config_entry.data["token"]["refresh_token"] == REFRESH_TOKEN


async def test_invalid_grant_is_not_retried(
    hass: HomeAssistant, mock_api: AiohttpClientMocker, oauth_config_entry: MockConfigEntry
) -> None:
    """4xx on refresh: one request, then reauthentication."""
    mock_api.post(TOKEN_URL, status=400, json={"error": "invalid_grant"})
    _expire(hass, oauth_config_entry)
    await _setup(hass, oauth_config_entry)
    assert len(_calls(mock_api, "POST", TOKEN_URL)) == 1
    assert oauth_config_entry.state is ConfigEntryState.SETUP_ERROR


async def test_concurrent_401s_refresh_once(
    hass: HomeAssistant, mock_api: AiohttpClientMocker, oauth_config_entry: MockConfigEntry
) -> None:
    """Two requests refused at once: a single refresh, both retried with the new token, and
    the rotated refresh token is the one kept."""
    mock_api.post(TOKEN_URL, json=token_response(access="32|stkp_new", refresh="r-2"))
    await _setup(hass, oauth_config_entry)
    client = oauth_config_entry.runtime_data.client
    sent: list[str] = []

    async def send(method, path, json, params, key, household, token):
        sent.append(token)
        await asyncio.sleep(0)
        if token == ACCESS_TOKEN:
            raise StockpotAuthError("expired")
        return {"data": {"ok": True}}

    with patch.object(client, "_send", side_effect=send):
        results = await asyncio.gather(client.summary(), client.summary())

    assert results == [{"ok": True}, {"ok": True}]
    assert len(_calls(mock_api, "POST", TOKEN_URL)) == 1
    assert sent.count("32|stkp_new") == 2
    assert oauth_config_entry.data["token"]["refresh_token"] == "r-2"


async def test_token_requests_honour_each_entry_ssl_check(
    hass: HomeAssistant, mock_api: AiohttpClientMocker, oauth_config_entry: MockConfigEntry
) -> None:
    """Two households of the same instance, one with the SSL check disabled: each refresh
    uses its own entry's setting."""
    other = MockConfigEntry(
        domain=DOMAIN, title="Chalet", unique_id=f"{INSTANCE}#8", data={**oauth_config_entry.data, CONF_HOUSEHOLD_ID: 8}
    )
    mock_api.post(TOKEN_URL, json=token_response(access="32|stkp_new", refresh="r-2"))
    mock_api.put(f"{API}/households/8/ha-webhook", status=403, json={"code": "forbidden"})
    with patch(
        "custom_components.stockpot.oauth.async_get_clientsession", wraps=aiohttp_client.async_get_clientsession
    ) as session:
        for entry, verify in ((oauth_config_entry, True), (other, False)):
            _expire(hass, entry, verify_ssl=verify)
            assert await hass.config_entries.async_setup(entry.entry_id)
            await hass.async_block_till_done()

    assert [call.kwargs["verify_ssl"] for call in session.call_args_list] == [True, False]
    assert other.data[CONF_VERIFY_SSL] is False


# --- flow outcomes -----------------------------------------------------------------


async def test_code_exchange_refused(
    hass: HomeAssistant, hass_client_no_auth, aioclient_mock: AiohttpClientMocker
) -> None:
    """400 invalid_grant on the code exchange (expired code, PKCE) → oauth_unauthorized."""
    aioclient_mock.get(DISCOVERY_URL, json=DISCOVERY)
    aioclient_mock.post(TOKEN_URL, status=400, json={"error": "invalid_grant"})
    result = await _authorize(hass, hass_client_no_auth, await _start_oauth(hass))
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "oauth_unauthorized"
    assert len(_calls(aioclient_mock, "POST", TOKEN_URL)) == 1


async def test_consent_denied(hass: HomeAssistant, hass_client_no_auth, aioclient_mock: AiohttpClientMocker) -> None:
    """« Refuser » on Stockpot's consent screen → user_rejected_authorize, no token request."""
    aioclient_mock.get(DISCOVERY_URL, json=DISCOVERY)
    result = await _authorize(hass, hass_client_no_auth, await _start_oauth(hass), answer="error=access_denied")
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "user_rejected_authorize"
    assert _calls(aioclient_mock, "POST", TOKEN_URL) == []


async def test_flow_in_progress_aborts_and_revokes(
    hass: HomeAssistant, hass_client_no_auth, aioclient_mock: AiohttpClientMocker
) -> None:
    """Another flow already holds this household: already_in_progress, the new access revoked."""
    aioclient_mock.get(DISCOVERY_URL, json=DISCOVERY)
    await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER, "unique_id": f"{INSTANCE}#7"})
    aioclient_mock.post(TOKEN_URL, json=token_response())
    aioclient_mock.get(f"{API}/households", json=HOUSEHOLDS)
    aioclient_mock.post(REVOKE_URL, json={})

    result = await _authorize(hass, hass_client_no_auth, await _start_oauth(hass))

    assert result["reason"] == "already_in_progress"
    [(form, _)] = _calls(aioclient_mock, "POST", REVOKE_URL)
    assert form["token"] == REFRESH_TOKEN


async def test_my_required_warns_once(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, current_request_with_host: None
) -> None:
    """Without My Home Assistant: my_required first; submitting again goes on with
    https://<ha>/auth/external/callback (allowed by STOCKPOT_OAUTH_HA_REDIRECT_HOSTS)."""
    hass.config.components.remove("my")
    aioclient_mock.get(DISCOVERY_URL, json=DISCOVERY)
    result = await _start_oauth(hass)
    assert result["errors"] == {"base": "my_required"}
    assert _calls(aioclient_mock, "GET", DISCOVERY_URL) == []

    assert result["data_schema"] is None

    result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    assert result["type"] is FlowResultType.EXTERNAL_STEP
    redirect = parse_qs(urlsplit(result["url"]).query)["redirect_uri"][0]
    assert redirect == "https://example.com/auth/external/callback"


# --- discovery transport ---------------------------------------------------------------


async def test_discovery_redirect_is_not_followed(hass: HomeAssistant, aioclient_mock: AiohttpClientMocker) -> None:
    """A 3xx on discovery is refused, not followed."""
    aioclient_mock.get(DISCOVERY_URL, status=302, headers={"Location": "https://evil.test/meta"})
    aioclient_mock.get("https://evil.test/meta", json=DISCOVERY)
    result = await _start_oauth(hass)
    assert result["errors"] == {"base": "not_stockpot"}
    assert _calls(aioclient_mock, "GET", "https://evil.test/meta") == []


@pytest.mark.parametrize(
    ("field", "value", "error"),
    [
        ("token_endpoint", "http://stockpot.arakeo.com/oauth/token", "insecure_endpoint"),
        ("revocation_endpoint", "http://stockpot.arakeo.com/oauth/revoke", "insecure_endpoint"),
        ("authorization_endpoint", "http://stockpot.arakeo.com/oauth/authorize", "insecure_endpoint"),
    ],
)
async def test_discovery_scheme_downgrade_is_refused(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, field: str, value: str, error: str
) -> None:
    """https Stockpot advertising an http endpoint: refused."""
    aioclient_mock.get(DISCOVERY_URL, json={**DISCOVERY, field: value})
    result = await _start_oauth(hass)
    assert result["errors"] == {"base": error}


async def test_http_only_for_private_hosts(hass: HomeAssistant, aioclient_mock: AiohttpClientMocker) -> None:
    """Plain http (pre-0.11 entries' own URL): refused for a public host (no request sent),
    accepted on the LAN."""
    with pytest.raises(StockpotPublicHttpError):
        await async_discover(hass, "http://stockpot.example.com", True)
    assert aioclient_mock.call_count == 0

    lan = "http://192.168.1.20:8080"
    aioclient_mock.get(f"{lan}/.well-known/oauth-authorization-server", json=_discovery_of(lan))
    endpoints = await async_discover(hass, lan, True)
    assert endpoints.token_url == f"{lan}/oauth/token"


async def test_http_endpoint_on_a_public_host_is_refused(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    """LAN URL, but an http endpoint on a public host: refused."""
    lan = "http://stockpot.lan"
    aioclient_mock.get(
        f"{lan}/.well-known/oauth-authorization-server",
        json={**DISCOVERY, "token_endpoint": "http://stockpot.example.com/oauth/token"},
    )
    with pytest.raises(StockpotPublicHttpError):
        await async_discover(hass, lan, True)


async def test_legacy_http_lan_entry_reauth(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, oauth_config_entry: MockConfigEntry
) -> None:
    """A pre-0.11 entry on a plain-http LAN address can still sign in again there; an http
    endpoint on a public host is reported."""
    lan = "http://stockpot.lan"
    entry = MockConfigEntry(domain=DOMAIN, unique_id=f"{INSTANCE}#7", data={**oauth_config_entry.data, CONF_URL: lan})
    entry.add_to_hass(hass)
    aioclient_mock.get(
        f"{lan}/.well-known/oauth-authorization-server",
        json={**_discovery_of(lan), "token_endpoint": "http://stockpot.example.com/oauth/token"},
    )
    result = await entry.start_reauth_flow(hass)
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    assert result["errors"] == {"base": "http_public_host"}

    aioclient_mock.clear_requests()
    aioclient_mock.get(f"{lan}/.well-known/oauth-authorization-server", json=_discovery_of(lan))
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    assert result["type"] is FlowResultType.EXTERNAL_STEP
    assert result["url"].startswith(f"{lan}/oauth/authorize")


async def test_other_origin_and_issuer_are_logged(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, caplog: pytest.LogCaptureFixture
) -> None:
    """LAN address of a pre-0.11 entry, public APP_URL: accepted, with a warning naming the
    origins only."""
    lan = "https://192.168.1.20"
    aioclient_mock.get(f"{lan}/.well-known/oauth-authorization-server", json=DISCOVERY)
    with caplog.at_level(logging.INFO):
        await async_discover(hass, lan, True)
    assert f"Stockpot token endpoint is on {URL}, not on the typed address" in caplog.text
    assert f"Stockpot issuer {URL} differs" in caplog.text


# --- removal ---------------------------------------------------------------------------


async def test_revoke_timeout_on_removal_falls_back_to_the_webhook(
    hass: HomeAssistant, mock_api: AiohttpClientMocker, oauth_config_entry: MockConfigEntry
) -> None:
    """Revocation timed out: the webhook is removed with the stored access token instead."""
    mock_api.post(REVOKE_URL, exc=TimeoutError())
    mock_api.delete(f"{API}/households/7/ha-webhook", status=204)
    await _setup(hass, oauth_config_entry)

    await hass.config_entries.async_remove(oauth_config_entry.entry_id)
    await hass.async_block_till_done()

    assert len(_calls(mock_api, "POST", REVOKE_URL)) == 1
    [(_, headers)] = _calls(mock_api, "DELETE", f"{API}/households/7/ha-webhook")
    assert headers["Authorization"] == f"Bearer {ACCESS_TOKEN}"
    assert hass.config_entries.async_get_entry(oauth_config_entry.entry_id) is None
