"""Stockpot: shopping lists, meal planning, stock alerts and actions in Home Assistant."""

from __future__ import annotations

import logging

from homeassistant.const import CONF_TOKEN, CONF_URL, CONF_VERIFY_SSL, Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.typing import ConfigType

from .api import StockpotClient, StockpotError
from .const import CONF_HOUSEHOLD_ID, DOMAIN
from .coordinator import StockpotConfigEntry, StockpotCoordinator, StockpotRuntimeData
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
    """API client of a config entry (shared aiohttp session, SSL check as configured)."""
    session = async_get_clientsession(hass, verify_ssl=data.get(CONF_VERIFY_SSL, True))
    return StockpotClient(session, data[CONF_URL], data[CONF_TOKEN], data.get(CONF_HOUSEHOLD_ID))


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Register the integration's actions (once, for every entry)."""
    async_setup_services(hass)
    return True


async def async_setup_entry(hass: HomeAssistant, entry: StockpotConfigEntry) -> bool:
    """Set up a Stockpot household."""
    client = client_for(hass, dict(entry.data))
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
    """Integration removed: ask Stockpot to stop sending the webhook (best effort)."""
    try:
        await client_for(hass, dict(entry.data)).unregister_webhook()
    except StockpotError as err:
        _LOGGER.debug("Could not remove the Stockpot webhook: %s", err)
