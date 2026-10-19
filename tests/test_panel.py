"""Stockpot panel in the sidebar (0.12.0, Stockpot M18): registration and removal, options,
websocket code command (admin check, OAuth session), manual-token entries, JS module."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from homeassistant.components.frontend import DATA_PANELS
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import issue_registry as ir
from homeassistant.setup import async_setup_component
from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.test_util.aiohttp import AiohttpClientMocker

from custom_components.stockpot.const import DOMAIN

from .conftest import ACCESS_TOKEN, API, INSTANCE, URL, calls

PANEL_CODE = "C" * 43
MODULE = Path(__file__).parent.parent / "custom_components" / "stockpot" / "frontend" / "stockpot-panel.js"


async def _setup(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.LOADED


def _panels(hass: HomeAssistant) -> dict[str, Any]:
    return {path: panel for path, panel in hass.data.get(DATA_PANELS, {}).items() if panel.component_name == "custom"}


async def test_oauth_entry_registers_the_panel_and_unload_removes_it(
    hass: HomeAssistant, mock_api: AiohttpClientMocker, oauth_config_entry: MockConfigEntry
) -> None:
    """« Stockpot » in the sidebar: custom element, no HA iframe, mdi:pot-steam, entry and origin."""
    await _setup(hass, oauth_config_entry)

    panel = _panels(hass)["stockpot"]
    assert panel.sidebar_title == "Stockpot"
    assert panel.sidebar_icon == "mdi:pot-steam"
    assert panel.require_admin is False
    custom = panel.config["_panel_custom"]
    assert custom["name"] == "stockpot-panel"
    assert custom["embed_iframe"] is False
    assert custom["module_url"].startswith("/stockpot_static/stockpot-panel.js?v=0.13.0")
    assert panel.config["entry_id"] == oauth_config_entry.entry_id
    assert panel.config["url"] == URL
    assert panel.config["origin"] == URL

    assert await hass.config_entries.async_unload(oauth_config_entry.entry_id)
    await hass.async_block_till_done()
    assert _panels(hass) == {}


async def test_a_second_entry_gets_its_own_panel_named_after_its_household(
    hass: HomeAssistant, mock_api: AiohttpClientMocker, oauth_config_entry: MockConfigEntry
) -> None:
    """Several households: `stockpot`, then `stockpot_<id>`; titles carry the household."""
    second = MockConfigEntry(
        domain=DOMAIN,
        title="Chalet",
        unique_id=f"{INSTANCE}#8",
        data={**oauth_config_entry.data},
    )
    oauth_config_entry.add_to_hass(hass)
    second.add_to_hass(hass)
    assert await hass.config_entries.async_setup(oauth_config_entry.entry_id)
    await hass.async_block_till_done()

    panels = _panels(hass)
    assert set(panels) == {"stockpot", f"stockpot_{second.entry_id[:8].lower()}"}
    assert {p.sidebar_title for p in panels.values()} == {"Stockpot · Foyer Martin", "Stockpot · Chalet"}


async def test_options_turn_the_panel_off_or_reserve_it_to_administrators(
    hass: HomeAssistant, mock_api: AiohttpClientMocker, oauth_config_entry: MockConfigEntry
) -> None:
    """Options flow: defaults on / off; saving reloads the entry and its panel."""
    await _setup(hass, oauth_config_entry)

    result = await hass.config_entries.options.async_init(oauth_config_entry.entry_id)
    assert result["type"] is FlowResultType.FORM
    defaults = {key.schema: key.default() for key in result["data_schema"].schema}
    assert defaults == {"panel_enabled": True, "panel_admin_only": False}
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"panel_enabled": True, "panel_admin_only": True}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()
    assert _panels(hass)["stockpot"].require_admin is True

    result = await hass.config_entries.options.async_init(oauth_config_entry.entry_id)
    await hass.config_entries.options.async_configure(
        result["flow_id"], {"panel_enabled": False, "panel_admin_only": False}
    )
    await hass.async_block_till_done()
    assert oauth_config_entry.state is ConfigEntryState.LOADED
    assert _panels(hass) == {}


async def test_manual_token_entry_has_no_panel_but_a_repair_issue(
    hass: HomeAssistant, mock_api: AiohttpClientMocker, config_entry: MockConfigEntry
) -> None:
    """A manual-token entry cannot open the panel: an issue suggests signing in with Stockpot;
    turning the panel off in the options removes the issue."""
    await _setup(hass, config_entry)

    assert _panels(hass) == {}
    registry = ir.async_get(hass)
    issue = registry.async_get_issue(DOMAIN, f"panel_requires_oauth_{config_entry.entry_id}")
    assert issue is not None
    assert issue.translation_key == "panel_requires_oauth"
    assert issue.translation_placeholders == {"title": "Foyer Martin"}

    result = await hass.config_entries.options.async_init(config_entry.entry_id)
    await hass.config_entries.options.async_configure(
        result["flow_id"], {"panel_enabled": False, "panel_admin_only": False}
    )
    await hass.async_block_till_done()
    assert registry.async_get_issue(DOMAIN, f"panel_requires_oauth_{config_entry.entry_id}") is None


@pytest.fixture
async def ws_ready(hass: HomeAssistant) -> None:
    """The websocket API (a dependency of the real frontend)."""
    assert await async_setup_component(hass, "websocket_api", {})


async def test_code_command_uses_the_entry_oauth_session(
    hass: HomeAssistant,
    mock_api: AiohttpClientMocker,
    oauth_config_entry: MockConfigEntry,
    ws_ready: None,
    hass_ws_client,
) -> None:
    """`stockpot/panel/code`: POST /ha/panel-codes with the entry's OAuth access token and household."""
    mock_api.post(f"{API}/ha/panel-codes", json={"code": PANEL_CODE, "expires_in": 60})
    await _setup(hass, oauth_config_entry)
    client = await hass_ws_client(hass)

    await client.send_json_auto_id({"type": "stockpot/panel/code", "entry_id": oauth_config_entry.entry_id})
    response = await client.receive_json()

    assert response["success"] is True
    assert response["result"] == {"code": PANEL_CODE, "expires_in": 60, "origin": URL}
    [(_, headers)] = calls(mock_api, "POST", f"{API}/ha/panel-codes")
    assert headers["Authorization"] == f"Bearer {ACCESS_TOKEN}"
    assert headers["X-Household"] == "7"


async def test_code_command_checks_admin_when_the_option_requires_it(
    hass: HomeAssistant,
    mock_api: AiohttpClientMocker,
    oauth_config_entry: MockConfigEntry,
    ws_ready: None,
    hass_ws_client,
    hass_read_only_access_token: str,
) -> None:
    """« Réservé aux administrateurs »: a non-admin user gets `unauthorized`, Stockpot is not called."""
    mock_api.post(f"{API}/ha/panel-codes", json={"code": PANEL_CODE, "expires_in": 60})
    oauth_config_entry.add_to_hass(hass)
    hass.config_entries.async_update_entry(
        oauth_config_entry, options={"panel_enabled": True, "panel_admin_only": True}
    )
    assert await hass.config_entries.async_setup(oauth_config_entry.entry_id)
    await hass.async_block_till_done()

    client = await hass_ws_client(hass, hass_read_only_access_token)
    await client.send_json_auto_id({"type": "stockpot/panel/code", "entry_id": oauth_config_entry.entry_id})
    response = await client.receive_json()
    assert response["success"] is False
    assert response["error"]["code"] == "unauthorized"
    assert calls(mock_api, "POST", f"{API}/ha/panel-codes") == []

    # Without the option, any Home Assistant user may open it.
    hass.config_entries.async_update_entry(
        oauth_config_entry, options={"panel_enabled": True, "panel_admin_only": False}
    )
    await client.send_json_auto_id({"type": "stockpot/panel/code", "entry_id": oauth_config_entry.entry_id})
    assert (await client.receive_json())["success"] is True


async def test_code_command_passes_on_panel_disabled_and_refuses_manual_entries(
    hass: HomeAssistant,
    mock_api: AiohttpClientMocker,
    oauth_config_entry: MockConfigEntry,
    config_entry: MockConfigEntry,
    ws_ready: None,
    hass_ws_client,
) -> None:
    """Stockpot's 403 `panel_disabled` reaches the panel as such; a manual entry or an unknown id is refused."""
    mock_api.post(
        f"{API}/ha/panel-codes",
        status=403,
        json={"code": "panel_disabled", "status": 403, "detail": "Le panneau Stockpot est désactivé"},
    )
    await _setup(hass, oauth_config_entry)
    client = await hass_ws_client(hass)

    await client.send_json_auto_id({"type": "stockpot/panel/code", "entry_id": oauth_config_entry.entry_id})
    response = await client.receive_json()
    assert response["success"] is False
    assert response["error"]["code"] == "panel_disabled"

    config_entry.add_to_hass(hass)
    await client.send_json_auto_id({"type": "stockpot/panel/code", "entry_id": config_entry.entry_id})
    assert (await client.receive_json())["error"]["code"] == "panel_requires_oauth"

    await client.send_json_auto_id({"type": "stockpot/panel/code", "entry_id": "inconnu"})
    assert (await client.receive_json())["error"]["code"] == "not_found"


def test_panel_module_is_shipped_and_keeps_to_the_protocol() -> None:
    """The custom element answers the embedded Stockpot only (source + origin), posts codes to its
    origin only, relays the menu and handles `narrow`."""
    source = MODULE.read_text(encoding="utf-8")
    assert 'customElements.define("stockpot-panel"' in source
    assert "event.source !== this._iframe.contentWindow || event.origin !== this._origin" in source
    assert "postMessage(message, origin)" in source
    assert '"stockpot/panel/code"' in source
    assert '"hass-toggle-menu"' in source
    assert 'iframe.setAttribute("allow", "camera; fullscreen")' in source
    assert "/embed" in source
    assert "set narrow(" in source
    assert "panel_disabled" in source


def test_panel_module_sizes_itself_on_the_viewport() -> None:
    """ha-panel-custom gives the element an automatic height: a percentage would collapse the
    iframe to its 150 px default (0.12.1)."""
    from pathlib import Path

    js = (Path(__file__).parents[1] / "custom_components" / "stockpot" / "frontend" / "stockpot-panel.js").read_text(
        encoding="utf-8"
    )
    assert "height: 100dvh" in js
    assert "height: 100vh" in js
