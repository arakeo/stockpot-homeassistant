"""Manual-token entries (0.9 / 0.10) since 0.11.0: they still load; reauthentication and
reconfigure offer the OAuth sign-in at the entry's own URL and convert the entry; the token
form remains only for a Stockpot without Home Assistant sign-in. New entries: test_oauth.py."""

from __future__ import annotations

import json
from pathlib import Path
import re
from typing import Any
from unittest.mock import patch

import pytest

from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import CONF_TOKEN, CONF_URL, CONF_VERIFY_SSL
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import aiohttp_client
from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.test_util.aiohttp import AiohttpClientMocker

from custom_components.stockpot.const import CONF_HOUSEHOLD_ID, DOMAIN
from custom_components.stockpot.oauth import implementation_key

from .conftest import (
    ACCESS_TOKEN,
    API,
    DISCOVERY,
    DISCOVERY_URL,
    INSTANCE,
    REFRESH_TOKEN,
    REVOKE_URL,
    TOKEN,
    TOKEN_URL,
    URL,
    authorize,
    calls,
    token_response,
)

HOUSEHOLDS = {"data": [{"id": 7, "name": "Foyer Martin", "role": "owner"}]}
COMPONENT = Path(__file__).parents[1] / "custom_components" / DOMAIN


@pytest.fixture(autouse=True)
async def _oauth_callback(oauth_callback: None) -> None:
    """My Home Assistant redirect and the /auth/external/callback view."""


async def _choose(hass: HomeAssistant, result: dict[str, Any], option: str) -> dict[str, Any]:
    """Pick an option of the manual entry's menu (reauth or reconfigure)."""
    assert result["type"] is FlowResultType.MENU
    return await hass.config_entries.flow.async_configure(result["flow_id"], {"next_step_id": option})


async def test_legacy_manual_entry_loads(
    hass: HomeAssistant, mock_api: AiohttpClientMocker, config_entry: MockConfigEntry
) -> None:
    """A manual-token entry is set up with its token, no OAuth request."""
    config_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()

    assert config_entry.state is ConfigEntryState.LOADED
    assert config_entry.data[CONF_TOKEN] == TOKEN
    [(_, headers)] = calls(mock_api, "GET", f"{API}/ha/summary")
    assert headers["Authorization"] == f"Bearer {TOKEN}"
    assert calls(mock_api, "GET", DISCOVERY_URL) == []
    assert calls(mock_api, "POST", TOKEN_URL) == []


async def test_manual_reauth_converts_to_oauth(
    hass: HomeAssistant, hass_client_no_auth, aioclient_mock: AiohttpClientMocker, config_entry: MockConfigEntry
) -> None:
    """Reauth of a manual entry: sign in at its URL, same instance and household → OAuth entry,
    SSL setting kept, nothing revoked."""
    config_entry.add_to_hass(hass)
    aioclient_mock.get(DISCOVERY_URL, json=DISCOVERY)
    aioclient_mock.post(TOKEN_URL, json=token_response())
    aioclient_mock.get(f"{API}/households", json=HOUSEHOLDS)
    aioclient_mock.post(REVOKE_URL, json={})

    result = await config_entry.start_reauth_flow(hass)
    assert result["type"] is FlowResultType.MENU
    assert result["step_id"] == "reauth_manual"
    assert result["menu_options"] == ["reauth_oauth", "reauth_confirm"]
    assert result["description_placeholders"]["url"] == URL
    result = await _choose(hass, result, "reauth_oauth")
    assert result["type"] is FlowResultType.EXTERNAL_STEP  # no extra confirmation
    with patch("custom_components.stockpot.async_setup_entry", return_value=True):
        result = await authorize(hass, hass_client_no_auth, result)

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reauth_successful"
    assert config_entry.unique_id == f"{INSTANCE}#7"
    data = config_entry.data
    assert data["auth_method"] == "oauth"
    assert data["auth_implementation"] == implementation_key(URL)
    assert data["token"]["access_token"] == ACCESS_TOKEN
    assert data["token"]["refresh_token"] == REFRESH_TOKEN
    assert data[CONF_URL] == URL
    assert data[CONF_VERIFY_SSL] is True
    assert data[CONF_HOUSEHOLD_ID] == 7
    assert calls(aioclient_mock, "POST", REVOKE_URL) == []


async def test_converted_entry_loads_with_oauth(
    hass: HomeAssistant, hass_client_no_auth, mock_api: AiohttpClientMocker, config_entry: MockConfigEntry
) -> None:
    """After the conversion the entry is reloaded and uses the OAuth access token."""
    config_entry.add_to_hass(hass)
    mock_api.get(DISCOVERY_URL, json=DISCOVERY)
    mock_api.post(TOKEN_URL, json=token_response())

    result = await _choose(hass, await config_entry.start_reauth_flow(hass), "reauth_oauth")
    result = await authorize(hass, hass_client_no_auth, result)
    await hass.async_block_till_done()

    assert result["reason"] == "reauth_successful"
    assert config_entry.state is ConfigEntryState.LOADED
    assert calls(mock_api, "GET", f"{API}/ha/summary")[-1][1]["Authorization"] == f"Bearer {ACCESS_TOKEN}"


async def test_manual_reauth_with_another_household_is_refused(
    hass: HomeAssistant, hass_client_no_auth, aioclient_mock: AiohttpClientMocker, config_entry: MockConfigEntry
) -> None:
    """Another household on the consent screen: wrong_instance, manual token kept, the access
    just created revoked."""
    config_entry.add_to_hass(hass)
    aioclient_mock.get(DISCOVERY_URL, json=DISCOVERY)
    aioclient_mock.post(TOKEN_URL, json=token_response(household_id=8, refresh="refresh-other"))
    aioclient_mock.get(f"{API}/households", json={"data": [{"id": 8, "name": "Chalet", "role": "owner"}]})
    aioclient_mock.post(REVOKE_URL, json={})

    result = await _choose(hass, await config_entry.start_reauth_flow(hass), "reauth_oauth")
    result = await authorize(hass, hass_client_no_auth, result)

    assert result["reason"] == "wrong_instance"
    assert config_entry.data[CONF_TOKEN] == TOKEN
    assert "auth_implementation" not in config_entry.data
    [(form, _)] = calls(aioclient_mock, "POST", REVOKE_URL)
    assert form["token"] == "refresh-other"


async def test_manual_reauth_falls_back_to_a_token_without_sign_in(
    hass: HomeAssistant, mock_api: AiohttpClientMocker, config_entry: MockConfigEntry
) -> None:
    """A Stockpot without Home Assistant sign-in: the token form, as before 0.11."""
    config_entry.add_to_hass(hass)
    mock_api.get(DISCOVERY_URL, status=404, text="Not found")

    result = await _choose(hass, await config_entry.start_reauth_flow(hass), "reauth_oauth")
    assert result["step_id"] == "reauth_confirm"
    with patch("custom_components.stockpot.async_setup_entry", return_value=True):
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {CONF_TOKEN: "13|stkp_new"})

    assert result["reason"] == "reauth_successful"
    assert config_entry.data[CONF_TOKEN] == "13|stkp_new"
    assert "auth_implementation" not in config_entry.data


async def test_manual_reauth_token_errors(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, config_entry: MockConfigEntry
) -> None:
    """« Coller un nouveau jeton »: no OAuth request; invalid token and missing `ha` ability
    are reported."""
    config_entry.add_to_hass(hass)
    aioclient_mock.get(f"{API}/me", status=401, json={"code": "unauthenticated"})

    result = await _choose(hass, await config_entry.start_reauth_flow(hass), "reauth_confirm")
    assert result["step_id"] == "reauth_confirm"
    assert calls(aioclient_mock, "GET", DISCOVERY_URL) == []
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {CONF_TOKEN: "bad"})
    assert result["errors"] == {"base": "invalid_auth"}

    aioclient_mock.clear_requests()
    aioclient_mock.get(f"{API}/me", json={"data": {}, "meta": {"instance_id": INSTANCE}})
    aioclient_mock.get(f"{API}/ha/summary", status=403, json={"code": "missing_ability", "abilities": ["ha"]})
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {CONF_TOKEN: "read-only"})
    assert result["errors"] == {"base": "missing_ability"}
    assert config_entry.data[CONF_TOKEN] == TOKEN


async def test_manual_reconfigure_converts_to_oauth(
    hass: HomeAssistant, hass_client_no_auth, aioclient_mock: AiohttpClientMocker
) -> None:
    """Reconfigure menu → sign-in at the entry's own URL, SSL setting kept."""
    custom = "https://stockpot.example.org"
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id=f"{INSTANCE}#7",
        data={CONF_URL: custom, CONF_TOKEN: TOKEN, CONF_VERIFY_SSL: False, CONF_HOUSEHOLD_ID: 7},
    )
    entry.add_to_hass(hass)
    aioclient_mock.get(
        f"{custom}/.well-known/oauth-authorization-server",
        json={
            **DISCOVERY,
            "authorization_endpoint": f"{custom}/oauth/authorize",
            "token_endpoint": f"{custom}/oauth/token",
            "revocation_endpoint": f"{custom}/oauth/revoke",
        },
    )
    aioclient_mock.post(f"{custom}/oauth/token", json=token_response())
    aioclient_mock.get(f"{custom}/api/v1/households", json=HOUSEHOLDS)

    result = await entry.start_reconfigure_flow(hass)
    assert result["step_id"] == "reconfigure_manual"
    assert result["menu_options"] == ["reconfigure_oauth", "reconfigure_token"]
    result = await _choose(hass, result, "reconfigure_oauth")
    with patch("custom_components.stockpot.async_setup_entry", return_value=True):
        result = await authorize(hass, hass_client_no_auth, result)

    assert result["reason"] == "reconfigure_successful"
    assert entry.data[CONF_URL] == custom
    assert entry.data[CONF_VERIFY_SSL] is False
    assert entry.data["token_url"] == f"{custom}/oauth/token"
    assert entry.data["token"]["access_token"] == ACCESS_TOKEN


async def test_duplicate_of_a_manual_entry_is_aborted(
    hass: HomeAssistant, hass_client_no_auth, aioclient_mock: AiohttpClientMocker, config_entry: MockConfigEntry
) -> None:
    """Adding the household of an existing manual entry: already_configured, new access revoked."""
    config_entry.add_to_hass(hass)
    aioclient_mock.get(DISCOVERY_URL, json=DISCOVERY)
    aioclient_mock.post(TOKEN_URL, json=token_response())
    aioclient_mock.get(f"{API}/households", json=HOUSEHOLDS)
    aioclient_mock.post(REVOKE_URL, json={})

    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": "user"})
    result = await authorize(hass, hass_client_no_auth, result)

    assert result["reason"] == "already_configured"
    assert config_entry.data[CONF_TOKEN] == TOKEN
    assert len(calls(aioclient_mock, "POST", REVOKE_URL)) == 1


async def test_manual_reauth_new_token_from_the_menu(
    hass: HomeAssistant, mock_api: AiohttpClientMocker, config_entry: MockConfigEntry
) -> None:
    """A Home Assistant that cannot finish the sign-in pastes a token, as before 0.11."""
    config_entry.add_to_hass(hass)
    result = await _choose(hass, await config_entry.start_reauth_flow(hass), "reauth_confirm")
    with patch("custom_components.stockpot.async_setup_entry", return_value=True):
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {CONF_TOKEN: "13|stkp_new"})
    assert result["reason"] == "reauth_successful"
    assert config_entry.data[CONF_TOKEN] == "13|stkp_new"
    assert calls(mock_api, "GET", DISCOVERY_URL) == []


async def test_manual_reauth_without_my_home_assistant(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, config_entry: MockConfigEntry, current_request_with_host
) -> None:
    """No `my`: discovery first, so a Stockpot without sign-in goes to the token form with no
    warning; one with the sign-in gets my_required once, then submitting goes on."""
    hass.config.components.remove("my")
    config_entry.add_to_hass(hass)
    aioclient_mock.get(DISCOVERY_URL, json={**DISCOVERY, "scopes_supported": ["mcp:use"]})
    result = await _choose(hass, await config_entry.start_reauth_flow(hass), "reauth_oauth")
    assert result["step_id"] == "reauth_confirm"
    assert result["errors"] == {}

    aioclient_mock.clear_requests()
    aioclient_mock.get(DISCOVERY_URL, json=DISCOVERY)
    result = await _choose(hass, await config_entry.start_reauth_flow(hass), "reauth_oauth")
    assert result["step_id"] == "reauth_oauth"
    assert result["errors"] == {"base": "my_required"}
    assert len(calls(aioclient_mock, "GET", DISCOVERY_URL)) == 1
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    assert result["type"] is FlowResultType.EXTERNAL_STEP


@pytest.mark.parametrize("mock", [{"status": 503, "text": "maintenance"}, {"status": 429}, {"exc": TimeoutError()}])
async def test_manual_reauth_transient_error_does_not_fall_back(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, config_entry: MockConfigEntry, mock: dict[str, Any]
) -> None:
    """5xx, 429 or timeout on discovery: cannot_connect on the sign-in form (retry), never the
    token form."""
    config_entry.add_to_hass(hass)
    aioclient_mock.get(DISCOVERY_URL, **mock)
    result = await _choose(hass, await config_entry.start_reauth_flow(hass), "reauth_oauth")
    assert result["step_id"] == "reauth_oauth"
    assert result["errors"] == {"base": "cannot_connect"}

    aioclient_mock.clear_requests()
    aioclient_mock.get(DISCOVERY_URL, json=DISCOVERY)
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    assert result["type"] is FlowResultType.EXTERNAL_STEP


async def test_manual_reauth_keeps_a_disabled_ssl_check(
    hass: HomeAssistant, hass_client_no_auth, aioclient_mock: AiohttpClientMocker
) -> None:
    """A manual entry with `verify_ssl=False`: discovery and code exchange without the check,
    and the converted entry keeps it."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id=f"{INSTANCE}#7",
        data={CONF_URL: URL, CONF_TOKEN: TOKEN, CONF_VERIFY_SSL: False, CONF_HOUSEHOLD_ID: 7},
    )
    entry.add_to_hass(hass)
    aioclient_mock.get(DISCOVERY_URL, json=DISCOVERY)
    aioclient_mock.post(TOKEN_URL, json=token_response())
    aioclient_mock.get(f"{API}/households", json=HOUSEHOLDS)
    with (
        patch(
            "custom_components.stockpot.oauth.async_get_clientsession", wraps=aiohttp_client.async_get_clientsession
        ) as oauth_session,
        patch(
            "custom_components.stockpot.config_flow.async_get_clientsession",
            wraps=aiohttp_client.async_get_clientsession,
        ) as flow_session,
        patch("custom_components.stockpot.async_setup_entry", return_value=True),
    ):
        result = await _choose(hass, await entry.start_reauth_flow(hass), "reauth_oauth")
        result = await authorize(hass, hass_client_no_auth, result)

    assert result["reason"] == "reauth_successful"
    assert {call.kwargs["verify_ssl"] for call in oauth_session.call_args_list} == {False}
    assert {call.kwargs["verify_ssl"] for call in flow_session.call_args_list} == {False}
    assert entry.data[CONF_VERIFY_SSL] is False
    assert "auth_implementation" in entry.data


@pytest.mark.parametrize(
    ("token_mock", "households_mock", "reason"),
    [
        ({"status": 400, "json": {"error": "invalid_grant"}}, None, "oauth_unauthorized"),
        ({"json": token_response()}, {"exc": TimeoutError()}, "cannot_connect"),
        ({"json": token_response()}, {"status": 500, "json": {"code": "server_error"}}, "unknown"),
    ],
)
async def test_failed_conversion_leaves_the_manual_entry_untouched(
    hass: HomeAssistant,
    hass_client_no_auth,
    aioclient_mock: AiohttpClientMocker,
    config_entry: MockConfigEntry,
    token_mock: dict[str, Any],
    households_mock: dict[str, Any] | None,
    reason: str,
) -> None:
    """Code exchange refused or /households failing: abort, the entry stays a manual entry."""
    config_entry.add_to_hass(hass)
    before = dict(config_entry.data)
    aioclient_mock.get(DISCOVERY_URL, json=DISCOVERY)
    aioclient_mock.post(TOKEN_URL, **token_mock)
    if households_mock is not None:
        aioclient_mock.get(f"{API}/households", **households_mock)
    aioclient_mock.post(REVOKE_URL, json={})

    result = await _choose(hass, await config_entry.start_reauth_flow(hass), "reauth_oauth")
    result = await authorize(hass, hass_client_no_auth, result)

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == reason
    assert dict(config_entry.data) == before


async def test_manual_reconfigure_token_url_and_ssl(
    hass: HomeAssistant, mock_api: AiohttpClientMocker, config_entry: MockConfigEntry
) -> None:
    """Reconfigure menu → token: a new URL of the same instance is accepted (as in 0.10);
    another instance is refused."""
    config_entry.add_to_hass(hass)
    new_api = "https://stockpot.example.org/api/v1"
    mock_api.get(f"{new_api}/me", json={"data": {}, "meta": {"instance_id": INSTANCE}})
    mock_api.get(f"{new_api}/ha/summary", json={"data": {}})
    other_api = "https://other.test/api/v1"
    mock_api.get(f"{other_api}/me", json={"data": {}, "meta": {"instance_id": "another-instance"}})
    mock_api.get(f"{other_api}/ha/summary", json={"data": {}})

    result = await _choose(hass, await config_entry.start_reconfigure_flow(hass), "reconfigure_token")
    assert result["step_id"] == "reconfigure_token"
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_URL: "https://other.test", CONF_TOKEN: TOKEN, CONF_VERIFY_SSL: True}
    )
    assert result["reason"] == "wrong_instance"

    result = await _choose(hass, await config_entry.start_reconfigure_flow(hass), "reconfigure_token")
    with patch("custom_components.stockpot.async_setup_entry", return_value=True):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {CONF_URL: "https://stockpot.example.org/", CONF_TOKEN: "14|stkp_other", CONF_VERIFY_SSL: False},
        )
    assert result["reason"] == "reconfigure_successful"
    assert config_entry.data[CONF_URL] == "https://stockpot.example.org"
    assert config_entry.data[CONF_TOKEN] == "14|stkp_other"
    assert config_entry.data[CONF_VERIFY_SSL] is False
    assert "auth_implementation" not in config_entry.data


# --- strings ---------------------------------------------------------------------------

STEPS = {
    "user",
    "reauth_manual",
    "reauth_oauth",
    "reauth_confirm",
    "reconfigure_manual",
    "reconfigure_oauth",
    "reconfigure_token",
}
MENUS = {
    "reauth_manual": {"reauth_oauth", "reauth_confirm"},
    "reconfigure_manual": {"reconfigure_oauth", "reconfigure_token"},
}
# Raised by this flow, by `async_update_reload_and_abort` and by Home Assistant's OAuth2 helper.
ABORTS = {
    "already_configured",
    "already_in_progress",
    "reauth_successful",
    "reconfigure_successful",
    "wrong_instance",
    "cannot_connect",
    "two_factor_required",
    "unknown",
    "oauth_unauthorized",
    "user_rejected_authorize",
    "oauth_error",
    "oauth_failed",
    "oauth_timeout",
    "authorize_url_timeout",
    "no_url_available",
}
# `reconfigure` remains the entry point but shows no form of its own any more.
REMOVED_STEPS = ("token", "household", "reconfigure")
REMOVED_KEYS = ("no_household", "AUTH_METHOD_TOKEN", "AUTH_METHOD_SELECTOR", "_start_schema", "_user_schema")


def _load(name: str) -> dict:
    return json.loads((COMPONENT / name).read_text(encoding="utf-8"))


def test_flow_strings_match_the_flow() -> None:
    """Removed steps and keys are gone from the code and every translation; each step, menu
    option, error and abort of the flow has a string, and nothing more."""
    source = (COMPONENT / "config_flow.py").read_text(encoding="utf-8")
    for step in STEPS:
        assert f'"{step}"' in source, step
    # `init`: the options flow (the Stockpot panel, 0.12.0), checked below.
    assert {f"{step}" for step in re.findall(r'step_id="(\w+)"', source)} <= STEPS | {"init"}
    # Literal errors, plus the API codes passed through as `{"base": err.code}`.
    errors = set(re.findall(r'"base": "(\w+)"', source)) | {"two_factor_required", "household_forbidden"}
    assert set(re.findall(r'reason="(\w+)"', source)) <= ABORTS
    for name in ("strings.json", "translations/en.json", "translations/fr.json"):
        assert set(_load(name)["options"]["step"]) == {"init"}, name
        config = _load(name)["config"]
        assert set(config["step"]) == STEPS, name
        for menu, options in MENUS.items():
            assert set(config["step"][menu]["menu_options"]) == options, name
        assert set(config["error"]) == errors, name
        assert set(config["abort"]) == ABORTS, name
        assert "auth_method" not in _load(name)["selector"], name
    for path in COMPONENT.rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        for key in REMOVED_KEYS:
            assert key not in text, f"{key} in {path.name}"
        for step in REMOVED_STEPS:
            assert f'step_id="{step}"' not in text, f"{step} in {path.name}"
        for step in ("token", "household"):
            assert f"async_step_{step}(" not in text, f"{step} in {path.name}"
    assert _load("strings.json") == _load("translations/en.json")
