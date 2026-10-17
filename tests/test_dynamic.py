"""Entities follow Stockpot: new / renamed / deleted lists, meal types (de)activated."""

from __future__ import annotations

from copy import deepcopy
from typing import Any

from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.test_util.aiohttp import AiohttpClientMocker

from .conftest import API, INSTANCE, SUMMARY, shopping_list


def _register(mock: AiohttpClientMocker, summary: dict[str, Any], lists: dict[int, dict[str, Any]]) -> None:
    mock.clear_requests()
    mock.get(f"{API}/me", json={"data": {}, "meta": {"instance_id": INSTANCE}})
    mock.get(f"{API}/ha/summary", json={"data": summary})
    for list_id, payload in lists.items():
        mock.get(f"{API}/shopping-lists/{list_id}", json={"data": payload})
    mock.put(f"{API}/households/7/ha-webhook", status=403, json={"code": "forbidden"})


async def test_lists_and_meal_types_follow_stockpot(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, config_entry: MockConfigEntry
) -> None:
    """A new list appears, a renamed one is renamed, a deleted one disappears; same for meal types."""
    summary = deepcopy(SUMMARY)
    _register(aioclient_mock, summary, {1: shopping_list()["data"]})
    config_entry.add_to_hass(hass)
    await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()
    coordinator = config_entry.runtime_data.coordinator
    assert hass.states.get("calendar.foyer_martin_snack") is None

    pharmacy = {"id": 2, "name": "Pharmacie", "is_main": False, "stale": False, "items": []}
    summary["shopping"]["lists"].append({"id": 2, "name": "Pharmacie", "is_main": False, "counts": {}, "stale": False})
    summary["meal_types"][2]["active"] = True
    _register(aioclient_mock, summary, {1: shopping_list()["data"], 2: pharmacy})
    await coordinator.async_refresh()
    await hass.async_block_till_done()
    assert hass.states.get("todo.foyer_martin_pharmacie") is not None
    assert hass.states.get("calendar.foyer_martin_snack") is not None

    _register(aioclient_mock, summary, {1: shopping_list()["data"], 2: {**pharmacy, "name": "Parapharmacie"}})
    await coordinator.async_refresh()
    await hass.async_block_till_done()
    assert hass.states.get("todo.foyer_martin_pharmacie").attributes["friendly_name"] == "Foyer Martin Parapharmacie"

    summary["shopping"]["lists"].pop()
    summary["meal_types"][2]["active"] = False
    _register(aioclient_mock, summary, {1: shopping_list()["data"]})
    await coordinator.async_refresh()
    await hass.async_block_till_done()
    assert hass.states.get("todo.foyer_martin_pharmacie") is None
    assert hass.states.get("calendar.foyer_martin_snack") is None
