"""To-do list: read, add, check (→ « Reçu »), rename, delete, move."""

from __future__ import annotations

from copy import deepcopy

import pytest

from homeassistant.components.todo import DOMAIN as TODO_DOMAIN
from homeassistant.const import ATTR_ENTITY_ID
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ServiceValidationError
from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.test_util.aiohttp import AiohttpClientMocker

from .conftest import API, ITEMS, SUMMARY, shopping_list

ENTITY = "todo.foyer_martin_shopping_list"


def _calls(mock: AiohttpClientMocker, method: str, path: str) -> list[tuple]:
    return [c for c in mock.mock_calls if c[0].lower() == method.lower() and str(c[1]).startswith(API + path)]


async def test_items(hass: HomeAssistant, setup_integration: MockConfigEntry) -> None:
    """Lines to buy in Stockpot's order, quantity and reasons as description, skipped hidden."""
    result = await hass.services.async_call(
        TODO_DOMAIN, "get_items", {ATTR_ENTITY_ID: ENTITY}, blocking=True, return_response=True
    )
    items = result[ENTITY]["items"]
    assert [(i["uid"], i["summary"], i["status"]) for i in items] == [
        ("101", "Beurre", "needs_action"),
        ("102", "Pain", "needs_action"),
    ]
    assert items[0]["description"] == "2 × 250 g · For: Pâte brisée"


async def test_add_item(hass: HomeAssistant, setup_integration: MockConfigEntry, mock_api: AiohttpClientMocker) -> None:
    """Adding creates a free text line in the list."""
    mock_api.post(f"{API}/shopping-lists/1/items", status=201, json={"data": {"id": 104, "name": "Lait"}})
    await hass.services.async_call(TODO_DOMAIN, "add_item", {ATTR_ENTITY_ID: ENTITY, "item": "Lait"}, blocking=True)
    (call,) = _calls(mock_api, "post", "/shopping-lists/1/items")
    assert call[2] == {"label": "Lait"}


async def test_check_product_line_receives_it_into_stock(
    hass: HomeAssistant, setup_integration: MockConfigEntry, mock_api: AiohttpClientMocker
) -> None:
    """Checking calls « Reçu » with a fresh Idempotency-Key; a tracked product enters the stock."""
    mock_api.post(
        f"{API}/shopping-list-items/101/receive", status=201, json={"data": {"id": 101}, "meta": {"movement_id": 9}}
    )
    mock_api.post(
        f"{API}/shopping-list-items/102/receive", status=201, json={"data": {"id": 102}, "meta": {"movement_id": None}}
    )

    await hass.services.async_call(
        TODO_DOMAIN, "update_item", {ATTR_ENTITY_ID: ENTITY, "item": "Beurre", "status": "completed"}, blocking=True
    )
    await hass.services.async_call(
        TODO_DOMAIN, "update_item", {ATTR_ENTITY_ID: ENTITY, "item": "102", "status": "completed"}, blocking=True
    )

    (butter,) = _calls(mock_api, "post", "/shopping-list-items/101/receive")
    assert butter[2] == {"add_to_stock": True}
    assert butter[3]["Idempotency-Key"].startswith("ha-")
    (bread,) = _calls(mock_api, "post", "/shopping-list-items/102/receive")
    assert bread[2] == {"add_to_stock": False}
    assert bread[3]["Idempotency-Key"] != butter[3]["Idempotency-Key"]


async def test_rename_and_note(
    hass: HomeAssistant, setup_integration: MockConfigEntry, mock_api: AiohttpClientMocker
) -> None:
    """A free text line is renamed; a product line is not; the description is the note."""
    mock_api.patch(f"{API}/shopping-list-items/102", json={"data": {"id": 102}})
    await hass.services.async_call(
        TODO_DOMAIN,
        "update_item",
        {ATTR_ENTITY_ID: ENTITY, "item": "Pain", "rename": "Pain complet", "description": "chez Paul"},
        blocking=True,
    )
    (call,) = _calls(mock_api, "patch", "/shopping-list-items/102")
    assert call[2] == {"label": "Pain complet", "note": "chez Paul"}

    with pytest.raises(ServiceValidationError):
        await hass.services.async_call(
            TODO_DOMAIN, "update_item", {ATTR_ENTITY_ID: ENTITY, "item": "Beurre", "rename": "Margarine"}, blocking=True
        )


async def test_received_line_cannot_be_unchecked(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    mock_api: AiohttpClientMocker,
    config_entry: MockConfigEntry,
) -> None:
    """A line received today is completed; unchecking it is refused (it is in the stock)."""
    received = [{**ITEMS[0], "status": "received", "received_at": "2026-10-14T10:00:00+02:00"}]
    aioclient_mock.clear_requests()
    aioclient_mock.get(f"{API}/ha/summary", json={"data": deepcopy(SUMMARY)})
    aioclient_mock.get(f"{API}/shopping-lists/1", json=shopping_list(received))
    aioclient_mock.put(f"{API}/households/7/ha-webhook", status=403, json={"code": "forbidden"})
    config_entry.add_to_hass(hass)
    await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()

    result = await hass.services.async_call(
        TODO_DOMAIN, "get_items", {ATTR_ENTITY_ID: ENTITY}, blocking=True, return_response=True
    )
    assert result[ENTITY]["items"][0]["status"] == "completed"
    with pytest.raises(ServiceValidationError):
        await hass.services.async_call(
            TODO_DOMAIN, "update_item", {ATTR_ENTITY_ID: ENTITY, "item": "101", "status": "needs_action"}, blocking=True
        )


async def test_delete_and_move(
    hass: HomeAssistant, setup_integration: MockConfigEntry, mock_api: AiohttpClientMocker, hass_ws_client
) -> None:
    """Delete removes the line; move sends the previous line (or None for the top)."""
    mock_api.delete(f"{API}/shopping-list-items/102", status=204)
    mock_api.post(f"{API}/shopping-list-items/102/move", json={"data": {"id": 102}})

    client = await hass_ws_client(hass)
    await client.send_json_auto_id({"type": "todo/item/move", "entity_id": ENTITY, "uid": "102"})
    assert (await client.receive_json())["success"]
    (move,) = _calls(mock_api, "post", "/shopping-list-items/102/move")
    assert move[2] == {"after_id": None}

    await hass.services.async_call(
        TODO_DOMAIN, "remove_item", {ATTR_ENTITY_ID: ENTITY, "item": ["Pain"]}, blocking=True
    )
    assert len(_calls(mock_api, "delete", "/shopping-list-items/102")) == 1
