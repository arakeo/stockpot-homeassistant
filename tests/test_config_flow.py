"""Config flow: success, invalid token, SSL, missing `ha` ability, households, reauth, reconfigure."""

from __future__ import annotations

from unittest.mock import patch

from homeassistant.config_entries import SOURCE_USER
from homeassistant.const import CONF_TOKEN, CONF_URL, CONF_VERIFY_SSL
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import aiohttp_client
from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.test_util.aiohttp import AiohttpClientMocker

from custom_components.stockpot.api import StockpotSslError
from custom_components.stockpot.const import CONF_HOUSEHOLD_ID, DOMAIN

from .conftest import API, INSTANCE, TOKEN, URL

USER_INPUT = {CONF_URL: URL + "/", CONF_TOKEN: TOKEN, CONF_VERIFY_SSL: True}


async def _start(hass: HomeAssistant):
    return await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})


async def test_success_with_one_household(hass: HomeAssistant, mock_api: AiohttpClientMocker) -> None:
    """One household: created at once, URL normalised, token kept."""
    result = await _start(hass)
    assert result["type"] is FlowResultType.FORM

    with patch("custom_components.stockpot.async_setup_entry", return_value=True):
        result = await hass.config_entries.flow.async_configure(result["flow_id"], USER_INPUT)

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "Foyer Martin"
    assert result["data"] == {CONF_URL: URL, CONF_TOKEN: TOKEN, CONF_VERIFY_SSL: True, CONF_HOUSEHOLD_ID: 7}
    assert result["result"].unique_id == f"{INSTANCE}#7"
    # The token is sent as a Bearer header, the household check goes to /ha/summary.
    calls = {str(url): headers for _, url, _, headers in mock_api.mock_calls}
    assert calls[f"{API}/households"]["Authorization"] == f"Bearer {TOKEN}"
    assert calls[f"{API}/ha/summary"]["X-Household"] == "7"


async def test_invalid_token(hass: HomeAssistant, aioclient_mock: AiohttpClientMocker) -> None:
    """401 → invalid_auth."""
    aioclient_mock.get(f"{API}/me", status=401, json={"code": "unauthenticated"})
    result = await _start(hass)
    result = await hass.config_entries.flow.async_configure(result["flow_id"], USER_INPUT)
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "invalid_auth"}


async def test_missing_ha_ability(hass: HomeAssistant, aioclient_mock: AiohttpClientMocker) -> None:
    """A read token without `ha`: refused on /ha/summary."""
    aioclient_mock.get(f"{API}/me", json={"data": {}, "meta": {"instance_id": INSTANCE}})
    aioclient_mock.get(f"{API}/households", json={"data": [{"id": 7, "name": "Foyer Martin"}]})
    aioclient_mock.get(f"{API}/ha/summary", status=403, json={"code": "missing_ability", "abilities": ["ha"]})
    result = await _start(hass)
    result = await hass.config_entries.flow.async_configure(result["flow_id"], USER_INPUT)
    assert result["errors"] == {"base": "missing_ability"}


async def test_ssl_error_then_without_ssl_check(hass: HomeAssistant, mock_api: AiohttpClientMocker) -> None:
    """A refused certificate is reported; unchecking the SSL check is kept in the entry."""
    result = await _start(hass)
    with patch(
        "custom_components.stockpot.config_flow.StockpotClient.instance_id", side_effect=StockpotSslError("cert")
    ):
        result = await hass.config_entries.flow.async_configure(result["flow_id"], USER_INPUT)
    assert result["errors"] == {"base": "ssl_error"}

    with (
        patch(
            "custom_components.stockpot.config_flow.async_get_clientsession",
            wraps=aiohttp_client.async_get_clientsession,
        ) as session,
        patch("custom_components.stockpot.async_setup_entry", return_value=True),
    ):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {**USER_INPUT, CONF_VERIFY_SSL: False}
        )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_VERIFY_SSL] is False
    assert session.call_args.kwargs["verify_ssl"] is False


async def test_cannot_connect(hass: HomeAssistant, aioclient_mock: AiohttpClientMocker) -> None:
    """Network error → cannot_connect."""
    aioclient_mock.get(f"{API}/me", exc=TimeoutError())
    result = await _start(hass)
    result = await hass.config_entries.flow.async_configure(result["flow_id"], USER_INPUT)
    assert result["errors"] == {"base": "cannot_connect"}


async def test_choose_household(hass: HomeAssistant, aioclient_mock: AiohttpClientMocker) -> None:
    """Several households (unbound read token): the user chooses one."""
    aioclient_mock.get(f"{API}/me", json={"data": {}, "meta": {"instance_id": INSTANCE}})
    aioclient_mock.get(
        f"{API}/households", json={"data": [{"id": 7, "name": "Foyer Martin"}, {"id": 8, "name": "Chalet"}]}
    )
    aioclient_mock.get(f"{API}/ha/summary", json={"data": {}})
    result = await _start(hass)
    result = await hass.config_entries.flow.async_configure(result["flow_id"], USER_INPUT)
    assert result["step_id"] == "household"
    with patch("custom_components.stockpot.async_setup_entry", return_value=True):
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {CONF_HOUSEHOLD_ID: "8"})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "Chalet"
    assert result["data"][CONF_HOUSEHOLD_ID] == 8


async def test_already_configured(
    hass: HomeAssistant, mock_api: AiohttpClientMocker, config_entry: MockConfigEntry
) -> None:
    """The same household of the same instance cannot be added twice."""
    config_entry.add_to_hass(hass)
    result = await _start(hass)
    result = await hass.config_entries.flow.async_configure(result["flow_id"], USER_INPUT)
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"


async def test_reauth(hass: HomeAssistant, mock_api: AiohttpClientMocker, config_entry: MockConfigEntry) -> None:
    """A new token replaces the old one."""
    config_entry.add_to_hass(hass)
    result = await config_entry.start_reauth_flow(hass)
    assert result["step_id"] == "reauth_confirm"
    with patch("custom_components.stockpot.async_setup_entry", return_value=True):
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {CONF_TOKEN: "13|stkp_new"})
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reauth_successful"
    assert config_entry.data[CONF_TOKEN] == "13|stkp_new"


async def test_reconfigure(hass: HomeAssistant, mock_api: AiohttpClientMocker, config_entry: MockConfigEntry) -> None:
    """A new URL of the same instance is accepted; another instance is refused."""
    config_entry.add_to_hass(hass)
    new_api = "https://stockpot.example.org/api/v1"
    mock_api.get(f"{new_api}/me", json={"data": {}, "meta": {"instance_id": INSTANCE}})
    mock_api.get(f"{new_api}/ha/summary", json={"data": {}})
    other_api = "https://other.test/api/v1"
    mock_api.get(f"{other_api}/me", json={"data": {}, "meta": {"instance_id": "another-instance"}})
    mock_api.get(f"{other_api}/ha/summary", json={"data": {}})

    result = await config_entry.start_reconfigure_flow(hass)
    assert result["step_id"] == "reconfigure"
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_URL: "https://other.test", CONF_TOKEN: TOKEN, CONF_VERIFY_SSL: True}
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "wrong_instance"

    result = await config_entry.start_reconfigure_flow(hass)
    with patch("custom_components.stockpot.async_setup_entry", return_value=True):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {CONF_URL: "https://stockpot.example.org/", CONF_TOKEN: "14|stkp_other", CONF_VERIFY_SSL: False},
        )
    assert result["reason"] == "reconfigure_successful"
    assert config_entry.data[CONF_URL] == "https://stockpot.example.org"
    assert config_entry.data[CONF_TOKEN] == "14|stkp_other"
    assert config_entry.data[CONF_VERIFY_SSL] is False
    assert config_entry.unique_id == f"{INSTANCE}#7"
