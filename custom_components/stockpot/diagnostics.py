"""Diagnostics of the Stockpot integration (tokens, webhook id and secret redacted)."""

from __future__ import annotations

from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.const import CONF_TOKEN, CONF_WEBHOOK_ID
from homeassistant.core import HomeAssistant

from .const import CONF_WEBHOOK_SECRET
from .coordinator import StockpotConfigEntry
from .oauth import is_oauth_entry

TO_REDACT = {CONF_TOKEN, CONF_WEBHOOK_ID, CONF_WEBHOOK_SECRET}
# OAuth entry: `token` is the token response; keep its expiry and extensions, hide the secrets.
TO_REDACT_OAUTH = {"access_token", "refresh_token", CONF_WEBHOOK_ID, CONF_WEBHOOK_SECRET}


async def async_get_config_entry_diagnostics(hass: HomeAssistant, entry: StockpotConfigEntry) -> dict[str, Any]:
    """Configuration and last poll."""
    coordinator = entry.runtime_data.coordinator
    data = coordinator.data
    oauth = is_oauth_entry(entry.data)
    return {
        "entry": async_redact_data(dict(entry.data), TO_REDACT_OAUTH if oauth else TO_REDACT),
        "auth_method": "oauth" if oauth else "token",
        "webhook_registered": bool(entry.data.get(CONF_WEBHOOK_SECRET)),
        "last_update_success": coordinator.last_update_success,
        "summary": data.summary if data else None,
        "lists": {
            list_id: {
                "name": shopping_list.get("name"),
                "is_main": shopping_list.get("is_main"),
                "stale": shopping_list.get("stale"),
                "items": len(shopping_list.get("items", [])),
            }
            for list_id, shopping_list in (data.lists.items() if data else [])
        },
    }
