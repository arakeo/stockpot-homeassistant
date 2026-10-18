"""Stockpot: shopping lists, meal planning, stock alerts and actions in Home Assistant."""

from __future__ import annotations

import asyncio
import logging

from homeassistant.const import CONF_TOKEN, CONF_URL, CONF_VERIFY_SSL, Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers import config_entry_oauth2_flow, config_validation as cv
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.typing import ConfigType

from .api import StockpotClient, StockpotError
from .const import CONF_HOUSEHOLD_ID, DOMAIN
from .coordinator import StockpotConfigEntry, StockpotCoordinator, StockpotRuntimeData
from .oauth import (
    CONF_OAUTH_TOKEN,
    REMOVAL_TIMEOUT,
    async_revoke,
    entry_implementation,
    is_oauth_entry,
    oauth_token_provider,
)
from .services import async_setup_services
from .webhook import async_setup_webhook, async_unload_webhook

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[Platform] = [
    Platform.BINARY_SENSOR,
    Platform.CALENDAR,
    Platform.SENSOR,
    Platform.TODO,
]

CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)


def client_for(hass: HomeAssistant, data: dict) -> StockpotClient:
    """API client of a manual-token entry (shared aiohttp session, SSL check as configured)."""
    session = async_get_clientsession(hass, verify_ssl=data.get(CONF_VERIFY_SSL, True))
    return StockpotClient(session, data[CONF_URL], data[CONF_TOKEN], data.get(CONF_HOUSEHOLD_ID))


def oauth_client_for(hass: HomeAssistant, entry: StockpotConfigEntry) -> StockpotClient:
    """API client of an OAuth entry: the implementation is rebuilt from the entry (its own
    Stockpot instance) and the access token comes from `OAuth2Session` (refresh, rotation)."""
    implementation = entry_implementation(hass, entry)
    oauth_session = config_entry_oauth2_flow.OAuth2Session(hass, entry, implementation)
    session = async_get_clientsession(hass, verify_ssl=entry.data.get(CONF_VERIFY_SSL, True))
    return StockpotClient(
        session,
        entry.data[CONF_URL],
        "",
        entry.data.get(CONF_HOUSEHOLD_ID),
        token_provider=oauth_token_provider(hass, entry, oauth_session),
    )


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Register the integration's actions (once, for every entry)."""
    async_setup_services(hass)
    return True


async def async_setup_entry(hass: HomeAssistant, entry: StockpotConfigEntry) -> bool:
    """Set up a Stockpot household."""
    client = oauth_client_for(hass, entry) if is_oauth_entry(entry.data) else client_for(hass, dict(entry.data))
    coordinator = StockpotCoordinator(hass, entry, client)
    await coordinator.async_config_entry_first_refresh()
    entry.runtime_data = StockpotRuntimeData(client=client, coordinator=coordinator)

    await async_setup_webhook(hass, entry)
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: StockpotConfigEntry) -> bool:
    """Unload a Stockpot household."""
    async_unload_webhook(hass, entry)
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)


async def async_remove_entry(hass: HomeAssistant, entry: StockpotConfigEntry) -> None:
    """Integration removed: ask Stockpot to stop sending the webhook (best effort).

    OAuth entry: revoke the refresh token (RFC 7009), which ends the whole Stockpot access,
    its tokens and the webhook it registered."""
    data = dict(entry.data)
    if is_oauth_entry(data):
        token = data.get(CONF_OAUTH_TOKEN) or {}
        if await async_revoke(hass, data, token.get("refresh_token"), time_limit=REMOVAL_TIMEOUT):
            return
        # Revocation impossible or failed: at least stop the webhook, with the stored access
        # token (still valid if it has not expired).
        data = {**data, CONF_TOKEN: str(token.get("access_token") or "")}
        if not data[CONF_TOKEN]:
            return
    try:
        async with asyncio.timeout(REMOVAL_TIMEOUT):
            await client_for(hass, data).unregister_webhook()
    except (StockpotError, TimeoutError) as err:
        _LOGGER.debug("Could not remove the Stockpot webhook: %s", err.__class__.__name__)
