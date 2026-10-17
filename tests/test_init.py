"""Set-up: entities created from the summary, stale flag, diagnostics, auth failure, unload."""

from __future__ import annotations

from typing import Any

import pytest

from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.components.diagnostics import get_diagnostics_for_config_entry
from pytest_homeassistant_custom_component.test_util.aiohttp import AiohttpClientMocker

from custom_components.stockpot.const import DOMAIN

from .conftest import API, TOKEN, shopping_list


async def test_entities_are_created(hass: HomeAssistant, setup_integration: MockConfigEntry) -> None:
    """Sensors, binary sensor, to-do list and calendars of the household."""
    assert setup_integration.state is ConfigEntryState.LOADED

    to_buy = hass.states.get("sensor.foyer_martin_items_to_buy")
    assert to_buy.state == "2"
    assert to_buy.attributes["stale"] is False
    assert to_buy.attributes["next_order_day"] == "2026-10-14"

    low = hass.states.get("sensor.foyer_martin_low_stock")
    assert low.state == "1"
    assert low.attributes["products"][0]["name"] == "Lait"

    expiring = hass.states.get("sensor.foyer_martin_expiring_soon")
    assert expiring.state == "1"
    assert expiring.attributes["products"][0]["expires_on"] == "2026-10-15"

    next_meal = hass.states.get("sensor.foyer_martin_next_meal")
    assert next_meal.state == "Hachis parmentier"
    assert next_meal.attributes["servings"] == 2.5
    assert next_meal.attributes["present"] == ["Camille", "Léana"]

    assert hass.states.get("binary_sensor.foyer_martin_drive_order_to_place").state == "on"

    to_collect = hass.states.get("binary_sensor.foyer_martin_drive_order_to_collect")
    assert to_collect.state == "on"
    assert to_collect.attributes["order_number"] == "0012345999"
    assert to_collect.attributes["pickup_at"] == "2026-10-14T10:00:00+02:00"
    assert to_collect.attributes["lines_count"] == 14

    todo = hass.states.get("todo.foyer_martin_shopping_list")
    assert todo.state == "2"  # skipped line hidden

    assert hass.states.get("calendar.foyer_martin_meals") is not None
    assert hass.states.get("calendar.foyer_martin_dinner") is not None
    assert hass.states.get("calendar.foyer_martin_lunch") is not None
    # Inactive meal types get no calendar.
    assert hass.states.get("calendar.foyer_martin_breakfast") is None

    registry = er.async_get(hass)
    assert len(er.async_entries_for_config_entry(registry, setup_integration.entry_id)) == 10


async def test_stale_list_is_reported(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, summary: dict[str, Any], config_entry: MockConfigEntry
) -> None:
    """An `ha` token never rebuilds the list: `stale` is exposed as an attribute."""
    summary["shopping"]["stale"] = True
    aioclient_mock.get(f"{API}/ha/summary", json={"data": summary})
    aioclient_mock.get(f"{API}/shopping-lists/1", json={"data": {**shopping_list()["data"], "stale": True}})
    aioclient_mock.put(f"{API}/households/7/ha-webhook", status=403, json={"code": "forbidden"})
    config_entry.add_to_hass(hass)
    await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()
    assert hass.states.get("sensor.foyer_martin_items_to_buy").attributes["stale"] is True
    assert hass.states.get("todo.foyer_martin_shopping_list").attributes["stale"] is True


async def test_order_to_collect_off_without_orders(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, summary: dict[str, Any], config_entry: MockConfigEntry
) -> None:
    """No order waiting (or an older Stockpot without `orders`): the binary sensor is off."""
    del summary["orders"]
    aioclient_mock.get(f"{API}/ha/summary", json={"data": summary})
    aioclient_mock.get(f"{API}/shopping-lists/1", json=shopping_list())
    aioclient_mock.put(f"{API}/households/7/ha-webhook", status=403, json={"code": "forbidden"})
    config_entry.add_to_hass(hass)
    await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()
    state = hass.states.get("binary_sensor.foyer_martin_drive_order_to_collect")
    assert state.state == "off"
    assert state.attributes["awaiting_pickup"] == 0
    assert state.attributes["order_number"] is None


async def test_diagnostics_hide_the_token(hass: HomeAssistant, hass_client, setup_integration: MockConfigEntry) -> None:
    """Token and webhook secrets are redacted."""
    diagnostics = await get_diagnostics_for_config_entry(hass, hass_client, setup_integration)
    assert diagnostics["entry"]["token"] == "**REDACTED**"
    assert TOKEN not in str(diagnostics)
    assert diagnostics["summary"]["household"]["id"] == 7
    assert diagnostics["lists"]["1"]["items"] == 3


async def test_invalid_token_starts_reauth(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, config_entry: MockConfigEntry
) -> None:
    """401 at set-up → reauthentication flow."""
    aioclient_mock.get(f"{API}/ha/summary", status=401, json={"code": "unauthenticated"})
    config_entry.add_to_hass(hass)
    await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()
    assert config_entry.state is ConfigEntryState.SETUP_ERROR
    flows = hass.config_entries.flow.async_progress_by_handler(DOMAIN)
    assert flows and flows[0]["context"]["source"] == "reauth"


async def test_unavailable_stockpot_retries(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, config_entry: MockConfigEntry
) -> None:
    """Network error at set-up → retried later."""
    aioclient_mock.get(f"{API}/ha/summary", exc=TimeoutError())
    config_entry.add_to_hass(hass)
    await hass.config_entries.async_setup(config_entry.entry_id)
    assert config_entry.state is ConfigEntryState.SETUP_RETRY


async def test_unload(hass: HomeAssistant, setup_integration: MockConfigEntry) -> None:
    """The entry unloads cleanly."""
    assert await hass.config_entries.async_unload(setup_integration.entry_id)
    assert setup_integration.state is ConfigEntryState.NOT_LOADED


@pytest.mark.parametrize("code", ["household_forbidden", "two_factor_required", "missing_ability"])
async def test_refused_household_starts_reauth(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, config_entry: MockConfigEntry, code: str
) -> None:
    """403 for this household (token bound elsewhere, 2FA, ability) → reauthentication, not a retry loop."""
    aioclient_mock.get(f"{API}/ha/summary", status=403, json={"code": code})
    config_entry.add_to_hass(hass)
    await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()
    assert config_entry.state is ConfigEntryState.SETUP_ERROR
    flows = hass.config_entries.flow.async_progress_by_handler(DOMAIN)
    assert flows and flows[0]["context"]["source"] == "reauth"
