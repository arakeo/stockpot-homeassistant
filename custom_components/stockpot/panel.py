"""Stockpot panel in the Home Assistant sidebar (0.12.0, Stockpot M18, ADR 0021).

The panel is a custom element (`frontend/stockpot-panel.js`) holding an iframe of
`<Stockpot URL>/embed`. The embedded Stockpot asks its parent for a single-use code; the element
gets one through the websocket command `stockpot/panel/code`, which calls
`POST /api/v1/ha/panel-codes` with the entry's OAuth access (no sign-in inside Home Assistant), and
posts it to the iframe for Stockpot's origin only. Actions done in the panel are attributed to the
Stockpot account that connected the integration.

- One panel per OAuth entry with « Afficher le panneau » on: `stockpot`, then `stockpot_<id>` for
  further entries; « Stockpot », suffixed with the household when there are several entries.
- « Réservé aux administrateurs »: `require_admin` of the panel, checked again by the command.
- Manual-token entries (before 0.11.0) never get a panel: Stockpot only opens it for OAuth
  connections. A repair issue suggests signing in with Stockpot (Reconfigure).
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import voluptuous as vol

from homeassistant.components import frontend, panel_custom, websocket_api
from homeassistant.components.http import StaticPathConfig
from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import CONF_URL
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import issue_registry as ir
from homeassistant.loader import async_get_integration

from .api import StockpotApiError, StockpotAuthError, StockpotConnectionError, StockpotError
from .const import (
    CONF_PANEL_ADMIN_ONLY,
    CONF_PANEL_ENABLED,
    DEFAULT_PANEL_ADMIN_ONLY,
    DEFAULT_PANEL_ENABLED,
    DOMAIN,
    PANEL_COMPONENT,
    PANEL_ICON,
    PANEL_MODULE,
    PANEL_STATIC_URL,
    PANEL_TITLE,
    PANEL_URL_PATH,
    WS_PANEL_CODE,
)
from .coordinator import StockpotConfigEntry
from .oauth import is_oauth_entry

_LOGGER = logging.getLogger(__name__)

FRONTEND_DIR = Path(__file__).parent / "frontend"
DATA_PANELS = f"{DOMAIN}_panels"
DATA_RESOURCES = f"{DOMAIN}_panel_resources"
# Answers of Stockpot passed on to the panel as they are (it shows a message for each).
FORWARDED_ERRORS = ("panel_disabled", "panel_requires_oauth", "two_factor_required", "email_unverified")


def panel_enabled(entry: StockpotConfigEntry) -> bool:
    """Option « Afficher le panneau » (on by default)."""
    return bool(entry.options.get(CONF_PANEL_ENABLED, DEFAULT_PANEL_ENABLED))


def panel_admin_only(entry: StockpotConfigEntry) -> bool:
    """Option « Réservé aux administrateurs » (off by default)."""
    return bool(entry.options.get(CONF_PANEL_ADMIN_ONLY, DEFAULT_PANEL_ADMIN_ONLY))


def stockpot_origin(url: str) -> str:
    """Origin (scheme://host[:port]) of the Stockpot instance: the only one the panel talks to."""
    parts = urlsplit(url)
    return f"{parts.scheme}://{parts.netloc}"


def manual_issue_id(entry: StockpotConfigEntry) -> str:
    """Repair issue of a manual-token entry that cannot have the panel."""
    return f"panel_requires_oauth_{entry.entry_id}"


async def async_setup_panel_resources(hass: HomeAssistant) -> None:
    """Once per Home Assistant run: the panel's JS module and the websocket command."""
    if hass.data.get(DATA_RESOURCES):
        return
    await hass.http.async_register_static_paths(
        [StaticPathConfig(PANEL_STATIC_URL, str(FRONTEND_DIR), cache_headers=False)]
    )
    websocket_api.async_register_command(hass, ws_panel_code)
    # The module URL carries the version: a new release is never served from the browser cache.
    hass.data[DATA_RESOURCES] = str((await async_get_integration(hass, DOMAIN)).version or "0")


async def async_setup_entry_panel(hass: HomeAssistant, entry: StockpotConfigEntry) -> None:
    """Register the entry's panel (OAuth entry, option on), or the repair issue of a manual entry."""
    if not is_oauth_entry(entry.data):
        if panel_enabled(entry):
            ir.async_create_issue(
                hass,
                DOMAIN,
                manual_issue_id(entry),
                is_fixable=False,
                severity=ir.IssueSeverity.WARNING,
                translation_key="panel_requires_oauth",
                translation_placeholders={"title": entry.title},
            )
        else:
            ir.async_delete_issue(hass, DOMAIN, manual_issue_id(entry))
        return
    ir.async_delete_issue(hass, DOMAIN, manual_issue_id(entry))
    if not panel_enabled(entry):
        return

    await async_setup_panel_resources(hass)
    panels: dict[str, str] = hass.data.setdefault(DATA_PANELS, {})
    taken = set(panels.values())
    url_path = PANEL_URL_PATH if PANEL_URL_PATH not in taken else f"{PANEL_URL_PATH}_{entry.entry_id[:8].lower()}"
    several = len(hass.config_entries.async_entries(DOMAIN)) > 1
    version = hass.data[DATA_RESOURCES]
    panel_custom_config: dict[str, Any] = {
        "entry_id": entry.entry_id,
        "url": entry.data[CONF_URL].rstrip("/"),
        "origin": stockpot_origin(entry.data[CONF_URL]),
    }
    await panel_custom.async_register_panel(
        hass,
        frontend_url_path=url_path,
        webcomponent_name=PANEL_COMPONENT,
        sidebar_title=f"{PANEL_TITLE} · {entry.title}" if several else PANEL_TITLE,
        sidebar_icon=PANEL_ICON,
        module_url=f"{PANEL_STATIC_URL}/{PANEL_MODULE}?v={version}",
        embed_iframe=False,
        require_admin=panel_admin_only(entry),
        config=panel_custom_config,
    )
    panels[entry.entry_id] = url_path


@callback
def async_unload_entry_panel(hass: HomeAssistant, entry: StockpotConfigEntry) -> None:
    """Remove the entry's panel (unload, reload after an option change, removal)."""
    url_path = hass.data.get(DATA_PANELS, {}).pop(entry.entry_id, None)
    if url_path is not None:
        frontend.async_remove_panel(hass, url_path, warn_if_unknown=False)


@callback
def async_remove_entry_panel(hass: HomeAssistant, entry: StockpotConfigEntry) -> None:
    """Entry removed: its repair issue goes too."""
    ir.async_delete_issue(hass, DOMAIN, manual_issue_id(entry))


@websocket_api.websocket_command({vol.Required("type"): WS_PANEL_CODE, vol.Required("entry_id"): str})
@websocket_api.async_response
async def ws_panel_code(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]) -> None:
    """A single-use Stockpot panel code for this entry, asked with the entry's OAuth access."""
    entry: StockpotConfigEntry | None = hass.config_entries.async_get_entry(msg["entry_id"])
    if entry is None or entry.domain != DOMAIN:
        connection.send_error(msg["id"], websocket_api.ERR_NOT_FOUND, "Unknown Stockpot entry")
        return
    if panel_admin_only(entry) and not connection.user.is_admin:
        connection.send_error(
            msg["id"], websocket_api.ERR_UNAUTHORIZED, "The Stockpot panel is reserved to administrators"
        )
        return
    if not is_oauth_entry(entry.data):
        connection.send_error(msg["id"], "panel_requires_oauth", "Sign in with Stockpot to use the panel")
        return
    if not panel_enabled(entry) or entry.state is not ConfigEntryState.LOADED:
        connection.send_error(msg["id"], "panel_unavailable", "The Stockpot panel is not available")
        return
    try:
        result = await entry.runtime_data.client.panel_code()
    except StockpotApiError as err:
        code = err.code if err.code in FORWARDED_ERRORS else "stockpot_error"
        connection.send_error(msg["id"], code, str(err))
        return
    except StockpotAuthError:
        connection.send_error(msg["id"], "reauth_required", "Sign in with Stockpot again")
        return
    except StockpotConnectionError:
        connection.send_error(msg["id"], "cannot_connect", "Stockpot cannot be reached")
        return
    except StockpotError as err:
        _LOGGER.debug("Stockpot panel code failed: %s", err.__class__.__name__)
        connection.send_error(msg["id"], "stockpot_error", "Unexpected Stockpot error")
        return
    connection.send_result(
        msg["id"],
        {
            "code": str(result["code"]),
            "expires_in": int(result.get("expires_in", 60)),
            "origin": stockpot_origin(entry.data[CONF_URL]),
        },
    )
