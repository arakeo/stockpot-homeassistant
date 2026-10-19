"""Actions: consume_product (EAN / product), add_stock, add_to_shopping_list, plan_meal, cook_meal, generate."""

from __future__ import annotations

from unittest.mock import patch

import pytest
import voluptuous as vol

from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.test_util.aiohttp import AiohttpClientMocker

from custom_components.stockpot.api import StockpotConnectionError
from custom_components.stockpot.const import DOMAIN

from .conftest import API, SUMMARY, shopping_list

MOVEMENT = {
    "data": {"id": 55, "product_id": 3, "quantity": "-250.000", "shortfall": "0.000"},
    "product_stock": {"quantity": "250.000"},
}
BARCODE = {
    "data": {
        "ean": "3033490004743",
        "pack_quantity": "250.000",
        "pack_unit": {"id": 1, "code": "g"},
        "product": {
            "id": 3,
            "name": "Beurre",
            "base_unit": {"id": 1, "code": "g"},
            "stock_tracked": True,
            "archived": False,
        },
    }
}


# Stockpot ≥ M20a: the EAN names its article (0.13.0 sends its id).
BARCODE_ARTICLE = {
    "data": {
        **BARCODE["data"],
        "article": {
            "id": 31,
            "ean": "3033490004743",
            "name": None,
            "brand": "Président",
            "pack_quantity": "250.000",
            "pack_unit_id": 1,
            "archived": False,
        },
    }
}


def _calls(mock: AiohttpClientMocker, method: str, path: str) -> list[tuple]:
    return [c for c in mock.mock_calls if c[0].lower() == method.lower() and str(c[1]).startswith(API + path)]


async def test_consume_product_by_barcode(
    hass: HomeAssistant, setup_integration: MockConfigEntry, mock_api: AiohttpClientMocker
) -> None:
    """An EAN is resolved to its product; the quantity is a number of packs; a fresh Idempotency-Key is sent."""
    mock_api.get(f"{API}/ha/barcodes/3033490004743", json=BARCODE)
    mock_api.post(f"{API}/stock/consume", status=201, json=MOVEMENT)

    response = await hass.services.async_call(
        DOMAIN, "consume_product", {"ean": "3033490004743", "quantity": 2}, blocking=True, return_response=True
    )

    (call,) = _calls(mock_api, "post", "/stock/consume")
    assert call[2] == {"product_id": 3, "quantity": "500", "unit_id": 1}
    assert call[3]["Idempotency-Key"].startswith("ha-")
    assert call[3]["X-Household"] == "7"
    assert response == {
        "movement_id": 55,
        "product_id": 3,
        "product_name": "Beurre",
        "quantity": "-250.000",
        "shortfall": "0.000",
        "stock_quantity": "250.000",
    }


async def test_barcode_actions_send_the_article(
    hass: HomeAssistant, setup_integration: MockConfigEntry, mock_api: AiohttpClientMocker
) -> None:
    """0.13.0 (Stockpot M20b): consume / add_stock by EAN send the article of the EAN with its product;
    add_to_shopping_list makes it the line's preferred article, unless it is archived."""
    mock_api.get(f"{API}/ha/barcodes/3033490004743", json=BARCODE_ARTICLE)
    mock_api.post(f"{API}/stock/consume", status=201, json=MOVEMENT)
    mock_api.post(f"{API}/stock/purchase", status=201, json=MOVEMENT)
    mock_api.post(f"{API}/shopping-lists/1/items", status=201, json={"data": {"id": 111, "name": "Beurre"}})

    await hass.services.async_call(DOMAIN, "consume_product", {"ean": "3033490004743"}, blocking=True)
    await hass.services.async_call(DOMAIN, "add_stock", {"ean": "3033490004743", "quantity": 2}, blocking=True)
    await hass.services.async_call(DOMAIN, "add_to_shopping_list", {"ean": "3033490004743"}, blocking=True)

    (consume,) = _calls(mock_api, "post", "/stock/consume")
    assert consume[2] == {"product_id": 3, "article_id": 31, "quantity": "250", "unit_id": 1}
    (purchase,) = _calls(mock_api, "post", "/stock/purchase")
    assert purchase[2] == {"product_id": 3, "article_id": 31, "quantity": "500", "unit_id": 1}
    (item,) = _calls(mock_api, "post", "/shopping-lists/1/items")
    assert item[2] == {"product_id": 3, "article_id": 31}


async def test_archived_article_is_not_the_preferred_one(
    hass: HomeAssistant, setup_integration: MockConfigEntry, mock_api: AiohttpClientMocker
) -> None:
    """An archived article is still consumed by its id, but never set as a line's preferred article."""
    article = {**BARCODE_ARTICLE["data"]["article"], "archived": True}
    archived = {"data": {**BARCODE_ARTICLE["data"], "article": article}}
    mock_api.get(f"{API}/ha/barcodes/3033490004743", json=archived)
    mock_api.post(f"{API}/shopping-lists/1/items", status=201, json={"data": {"id": 112, "name": "Beurre"}})
    await hass.services.async_call(DOMAIN, "add_to_shopping_list", {"ean": "3033490004743"}, blocking=True)
    (item,) = _calls(mock_api, "post", "/shopping-lists/1/items")
    assert item[2] == {"product_id": 3}


async def test_consume_product_by_id(
    hass: HomeAssistant, setup_integration: MockConfigEntry, mock_api: AiohttpClientMocker
) -> None:
    """With a product id the quantity is required (base unit, or the given unit)."""
    mock_api.post(f"{API}/stock/consume", status=201, json=MOVEMENT)
    with pytest.raises(ServiceValidationError):
        await hass.services.async_call(DOMAIN, "consume_product", {"product_id": 3}, blocking=True)

    await hass.services.async_call(
        DOMAIN, "consume_product", {"product_id": 3, "quantity": 0.5, "unit_id": 2}, blocking=True
    )
    (call,) = _calls(mock_api, "post", "/stock/consume")
    assert call[2] == {"product_id": 3, "quantity": "0.5", "unit_id": 2}


async def test_unknown_barcode_is_an_error(
    hass: HomeAssistant, setup_integration: MockConfigEntry, mock_api: AiohttpClientMocker
) -> None:
    """404 from Stockpot → HomeAssistantError, no stock call."""
    mock_api.get(f"{API}/ha/barcodes/3000000000014", status=404, json={"code": "not_found", "title": "Introuvable"})
    with pytest.raises(HomeAssistantError):
        await hass.services.async_call(DOMAIN, "consume_product", {"ean": "3000000000014"}, blocking=True)
    assert _calls(mock_api, "post", "/stock/") == []


async def test_add_stock(
    hass: HomeAssistant, setup_integration: MockConfigEntry, mock_api: AiohttpClientMocker
) -> None:
    """A purchase with a best-before date."""
    mock_api.get(f"{API}/ha/barcodes/3033490004743", json=BARCODE)
    mock_api.post(f"{API}/stock/purchase", status=201, json=MOVEMENT)
    await hass.services.async_call(
        DOMAIN, "add_stock", {"ean": "3033490004743", "best_before": "2026-11-01"}, blocking=True
    )
    (call,) = _calls(mock_api, "post", "/stock/purchase")
    assert call[2] == {"product_id": 3, "quantity": "250", "unit_id": 1, "best_before": "2026-11-01"}


async def test_add_to_shopping_list(
    hass: HomeAssistant, setup_integration: MockConfigEntry, mock_api: AiohttpClientMocker
) -> None:
    """Default list = the main one."""
    mock_api.post(f"{API}/shopping-lists/1/items", status=201, json={"data": {"id": 110, "name": "Pain"}})
    response = await hass.services.async_call(
        DOMAIN, "add_to_shopping_list", {"label": "Pain", "quantity": 2}, blocking=True, return_response=True
    )
    (call,) = _calls(mock_api, "post", "/shopping-lists/1/items")
    assert call[2] == {"label": "Pain", "quantity": "2"}
    assert response == {"item_id": 110, "list_id": 1, "name": "Pain"}


async def test_plan_and_cook_meal(
    hass: HomeAssistant, setup_integration: MockConfigEntry, mock_api: AiohttpClientMocker
) -> None:
    """plan_meal resolves the meal type; cook_meal finds the meal of that day and sends a key."""
    mock_api.post(f"{API}/meal-plan-entries", status=201, json={"data": {"id": 41, "servings": "4.0"}})
    mock_api.get(
        f"{API}/meal-plan",
        json={"data": [{"id": 41, "date": "2026-10-15", "meal_type_id": 4, "cooked_at": None}], "meta": {}},
    )
    mock_api.post(
        f"{API}/meal-plan-entries/41/cook",
        status=201,
        json={"data": {"id": 41}, "cook": {"decremented_count": 3, "blocked_count": 0, "shortfall_count": 1}},
    )

    await hass.services.async_call(
        DOMAIN, "plan_meal", {"date": "2026-10-15", "meal_type": "dinner", "recipe_id": 9, "servings": 4}, blocking=True
    )
    (plan,) = _calls(mock_api, "post", "/meal-plan-entries")
    assert plan[2] == {"date": "2026-10-15", "meal_type_id": 4, "recipe_id": 9, "servings": "4.0"}

    with pytest.raises(ServiceValidationError):
        await hass.services.async_call(
            DOMAIN, "plan_meal", {"date": "2026-10-15", "meal_type": "snack", "note": "x"}, blocking=True
        )

    response = await hass.services.async_call(
        DOMAIN, "cook_meal", {"date": "2026-10-15", "meal_type": "dinner"}, blocking=True, return_response=True
    )
    (cook,) = _calls(mock_api, "post", "/meal-plan-entries/41/cook")
    assert cook[3]["Idempotency-Key"].startswith("ha-")
    assert response == {"entry_id": 41, "decremented_count": 3, "blocked_count": 0, "shortfall_count": 1}


async def test_generate_shopping_list(
    hass: HomeAssistant, setup_integration: MockConfigEntry, mock_api: AiohttpClientMocker
) -> None:
    """Rebuild the main list now."""
    mock_api.post(
        f"{API}/shopping-lists/1/regenerate",
        json={"data": {"id": 1, "counts": {"total": 5}, "period": {"until": "2026-10-20"}}},
    )
    response = await hass.services.async_call(DOMAIN, "generate_shopping_list", {}, blocking=True, return_response=True)
    assert response == {"list_id": 1, "to_buy": 5, "until": "2026-10-20"}


async def test_malformed_barcode_is_refused_before_any_call(
    hass: HomeAssistant, setup_integration: MockConfigEntry, mock_api: AiohttpClientMocker
) -> None:
    """Only 8 to 14 digits: no path injection towards Stockpot."""
    for ean in ("../me", "12345", "3033490004743?x=1"):
        with pytest.raises((vol.Invalid, ServiceValidationError)):
            await hass.services.async_call(DOMAIN, "consume_product", {"ean": ean}, blocking=True)
    assert [c for c in mock_api.mock_calls if "/ha/barcodes" in str(c[1])] == []


async def test_a_failed_action_is_retried_with_the_same_key(
    hass: HomeAssistant, setup_integration: MockConfigEntry
) -> None:
    """Timeout then success: the same Idempotency-Key is sent again (Stockpot replays)."""
    calls: list[str | None] = []

    async def send(method, path, json, params, key, household, token):
        if key is None:  # coordinator refresh after the action
            return {"data": SUMMARY} if path == "/ha/summary" else shopping_list()
        calls.append(key)
        if len(calls) == 1:
            raise StockpotConnectionError("timeout")
        return MOVEMENT

    with (
        patch("custom_components.stockpot.api.RETRY_DELAY", 0),
        patch.object(setup_integration.runtime_data.client, "_send", side_effect=send),
    ):
        await hass.services.async_call(DOMAIN, "consume_product", {"product_id": 3, "quantity": 1}, blocking=True)

    assert len(calls) == 2
    assert calls[0] is not None and calls[0] == calls[1]
