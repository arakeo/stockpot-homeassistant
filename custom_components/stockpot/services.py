"""Actions of the Stockpot integration.

Every stock / cooking action sends a fresh Idempotency-Key: Stockpot never applies the same
action twice. With several Stockpot households, `config_entry_id` chooses one.
"""

from __future__ import annotations

from typing import Any

import voluptuous as vol

from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import (
    HomeAssistant,
    ServiceCall,
    ServiceResponse,
    SupportsResponse,
    callback,
)
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import config_validation as cv

from .api import StockpotApiError, StockpotError
from .const import (
    ATTR_CONFIG_ENTRY_ID,
    DOMAIN,
    MEAL_TYPES,
    SERVICE_ADD_STOCK,
    SERVICE_ADD_TO_SHOPPING_LIST,
    SERVICE_CONSUME_PRODUCT,
    SERVICE_COOK_MEAL,
    SERVICE_GENERATE_SHOPPING_LIST,
    SERVICE_PLAN_MEAL,
)
from .coordinator import StockpotConfigEntry

_ENTRY = {vol.Optional(ATTR_CONFIG_ENTRY_ID): cv.string}
# EAN-8 / UPC-A / EAN-13 / GTIN-14 digits only (Stockpot checks the check digit).
EAN = vol.All(cv.string, vol.Match(r"^\d{8,14}$"))
_QUANTITY = vol.All(vol.Coerce(float), vol.Range(min=0.001, max=999999999))

_PRODUCT_REF = {
    **_ENTRY,
    vol.Exclusive("ean", "product"): EAN,
    vol.Exclusive("product_id", "product"): cv.positive_int,
    vol.Optional("quantity"): _QUANTITY,
    vol.Optional("unit_id"): cv.positive_int,
}

CONSUME_SCHEMA = vol.Schema({**_PRODUCT_REF, vol.Optional("location_id"): cv.positive_int})
ADD_STOCK_SCHEMA = vol.Schema(
    {**_PRODUCT_REF, vol.Optional("location_id"): cv.positive_int, vol.Optional("best_before"): cv.date}
)
ADD_TO_LIST_SCHEMA = vol.Schema(
    {
        **_ENTRY,
        vol.Exclusive("ean", "item"): EAN,
        vol.Exclusive("product_id", "item"): cv.positive_int,
        vol.Exclusive("label", "item"): vol.All(cv.string, vol.Length(min=1, max=160)),
        vol.Optional("quantity"): _QUANTITY,
        vol.Optional("unit_id"): cv.positive_int,
        vol.Optional("note"): vol.All(cv.string, vol.Length(max=200)),
        vol.Optional("list_id"): cv.positive_int,
    }
)
PLAN_MEAL_SCHEMA = vol.Schema(
    {
        **_ENTRY,
        vol.Required("date"): cv.date,
        vol.Required("meal_type"): vol.In(MEAL_TYPES),
        vol.Exclusive("recipe_id", "meal"): cv.positive_int,
        vol.Exclusive("note", "meal"): vol.All(cv.string, vol.Length(min=1, max=200)),
        vol.Optional("servings"): vol.All(vol.Coerce(float), vol.Range(min=0.5, max=99)),
    }
)
COOK_MEAL_SCHEMA = vol.Schema(
    {
        **_ENTRY,
        vol.Optional("entry_id"): cv.positive_int,
        vol.Optional("date"): cv.date,
        vol.Optional("meal_type"): vol.In(MEAL_TYPES),
    }
)
GENERATE_SCHEMA = vol.Schema({**_ENTRY, vol.Optional("until"): cv.date})


def _entry(hass: HomeAssistant, call: ServiceCall) -> StockpotConfigEntry:
    entries: list[StockpotConfigEntry] = [
        e for e in hass.config_entries.async_entries(DOMAIN) if e.state is ConfigEntryState.LOADED
    ]
    wanted = call.data.get(ATTR_CONFIG_ENTRY_ID)
    if wanted:
        entries = [e for e in entries if e.entry_id == wanted]
    if not entries:
        raise ServiceValidationError(translation_domain=DOMAIN, translation_key="no_entry")
    if len(entries) > 1:
        raise ServiceValidationError(translation_domain=DOMAIN, translation_key="several_entries")
    return entries[0]


def _decimal(value: float) -> str:
    """Stockpot quantities: plain decimal string, 3 decimals at most."""
    return f"{value:.3f}".rstrip("0").rstrip(".")


async def _api(coro) -> Any:
    try:
        return await coro
    except StockpotApiError as err:
        raise HomeAssistantError(
            translation_domain=DOMAIN,
            translation_key="api_error",
            translation_placeholders={"error": str(err)},
        ) from err
    except StockpotError as err:
        raise HomeAssistantError(
            translation_domain=DOMAIN,
            translation_key="cannot_connect",
            translation_placeholders={"error": str(err)},
        ) from err


async def _product_quantity(entry: StockpotConfigEntry, data: dict[str, Any]) -> dict[str, Any]:
    """`{product_id, quantity, unit_id?}` from an EAN (quantity = packs) or a product id."""
    client = entry.runtime_data.client
    if "ean" in data:
        barcode = await _api(client.barcode(data["ean"]))
        packs = data.get("quantity", 1)
        if barcode.get("pack_quantity") and barcode.get("pack_unit"):
            return {
                "product_id": barcode["product"]["id"],
                "quantity": _decimal(packs * float(barcode["pack_quantity"])),
                "unit_id": barcode["pack_unit"]["id"],
                "_name": barcode["product"]["name"],
            }
        result = {
            "product_id": barcode["product"]["id"],
            "quantity": _decimal(packs),
            "_name": barcode["product"]["name"],
        }
    elif "product_id" in data:
        if "quantity" not in data:
            raise ServiceValidationError(translation_domain=DOMAIN, translation_key="quantity_required")
        result = {"product_id": data["product_id"], "quantity": _decimal(data["quantity"])}
    else:
        raise ServiceValidationError(translation_domain=DOMAIN, translation_key="product_required")
    if "unit_id" in data:
        result["unit_id"] = data["unit_id"]
    return result


def _movement_response(response: dict[str, Any], name: str | None) -> dict[str, Any]:
    movement = response["data"]
    return {
        "movement_id": movement["id"],
        "product_id": movement["product_id"],
        "product_name": name,
        "quantity": movement["quantity"],
        "shortfall": movement.get("shortfall"),
        "stock_quantity": response.get("product_stock", {}).get("quantity"),
    }


@callback
def async_setup_services(hass: HomeAssistant) -> None:
    """Register the actions."""

    async def consume_product(call: ServiceCall) -> ServiceResponse:
        entry = _entry(hass, call)
        payload = await _product_quantity(entry, dict(call.data))
        name = payload.pop("_name", None)
        if "location_id" in call.data:
            payload["location_id"] = call.data["location_id"]
        response = await _api(entry.runtime_data.client.stock_action("consume", payload))
        await entry.runtime_data.coordinator.async_request_refresh()
        return _movement_response(response, name)

    async def add_stock(call: ServiceCall) -> ServiceResponse:
        entry = _entry(hass, call)
        payload = await _product_quantity(entry, dict(call.data))
        name = payload.pop("_name", None)
        if "location_id" in call.data:
            payload["location_id"] = call.data["location_id"]
        if "best_before" in call.data:
            payload["best_before"] = call.data["best_before"].isoformat()
        response = await _api(entry.runtime_data.client.stock_action("purchase", payload))
        await entry.runtime_data.coordinator.async_request_refresh()
        return _movement_response(response, name)

    async def add_to_shopping_list(call: ServiceCall) -> ServiceResponse:
        entry = _entry(hass, call)
        runtime = entry.runtime_data
        list_id = call.data.get("list_id") or runtime.coordinator.data.summary["shopping"]["main_list_id"]
        payload: dict[str, Any] = {}
        if "ean" in call.data:
            payload["product_id"] = (await _api(runtime.client.barcode(call.data["ean"])))["product"]["id"]
        elif "product_id" in call.data:
            payload["product_id"] = call.data["product_id"]
        elif "label" in call.data:
            payload["label"] = call.data["label"]
        else:
            raise ServiceValidationError(translation_domain=DOMAIN, translation_key="item_required")
        if "quantity" in call.data:
            payload["quantity"] = _decimal(call.data["quantity"])
        for key in ("unit_id", "note"):
            if key in call.data:
                payload[key] = call.data[key]
        item = await _api(runtime.client.add_item(list_id, payload))
        await runtime.coordinator.async_request_refresh()
        return {"item_id": item["id"], "list_id": list_id, "name": item["name"]}

    async def plan_meal(call: ServiceCall) -> ServiceResponse:
        entry = _entry(hass, call)
        runtime = entry.runtime_data
        meal_type = runtime.coordinator.data.meal_type(call.data["meal_type"])
        if meal_type is None or not meal_type["active"]:
            raise ServiceValidationError(translation_domain=DOMAIN, translation_key="meal_type_inactive")
        if "recipe_id" not in call.data and "note" not in call.data:
            raise ServiceValidationError(translation_domain=DOMAIN, translation_key="meal_required")
        payload: dict[str, Any] = {"date": call.data["date"].isoformat(), "meal_type_id": meal_type["id"]}
        for key in ("recipe_id", "note"):
            if key in call.data:
                payload[key] = call.data[key]
        if "servings" in call.data:
            payload["servings"] = f"{round(call.data['servings'] * 2) / 2:.1f}"
        meal = await _api(runtime.client.plan_meal(payload))
        await runtime.coordinator.async_request_refresh()
        return {"entry_id": meal["id"], "servings": meal["servings"]}

    async def cook_meal(call: ServiceCall) -> ServiceResponse:
        entry = _entry(hass, call)
        runtime = entry.runtime_data
        entry_id = call.data.get("entry_id")
        if entry_id is None:
            if "date" not in call.data or "meal_type" not in call.data:
                raise ServiceValidationError(translation_domain=DOMAIN, translation_key="meal_reference_required")
            entry_id = await _find_meal(runtime, call.data["date"].isoformat(), call.data["meal_type"])
        response = await _api(runtime.client.cook_meal(entry_id))
        await runtime.coordinator.async_request_refresh()
        cook = response.get("cook", {})
        return {
            "entry_id": entry_id,
            "decremented_count": cook.get("decremented_count"),
            "blocked_count": cook.get("blocked_count"),
            "shortfall_count": cook.get("shortfall_count"),
        }

    async def generate_shopping_list(call: ServiceCall) -> ServiceResponse:
        entry = _entry(hass, call)
        runtime = entry.runtime_data
        list_id = runtime.coordinator.data.summary["shopping"]["main_list_id"]
        until = call.data["until"].isoformat() if "until" in call.data else None
        result = await _api(runtime.client.regenerate(list_id, until))
        await runtime.coordinator.async_request_refresh()
        return {
            "list_id": list_id,
            "to_buy": result["counts"]["total"],
            "until": (result.get("period") or {}).get("until"),
        }

    for name, handler, schema in (
        (SERVICE_CONSUME_PRODUCT, consume_product, CONSUME_SCHEMA),
        (SERVICE_ADD_STOCK, add_stock, ADD_STOCK_SCHEMA),
        (SERVICE_ADD_TO_SHOPPING_LIST, add_to_shopping_list, ADD_TO_LIST_SCHEMA),
        (SERVICE_PLAN_MEAL, plan_meal, PLAN_MEAL_SCHEMA),
        (SERVICE_COOK_MEAL, cook_meal, COOK_MEAL_SCHEMA),
        (SERVICE_GENERATE_SHOPPING_LIST, generate_shopping_list, GENERATE_SCHEMA),
    ):
        hass.services.async_register(DOMAIN, name, handler, schema=schema, supports_response=SupportsResponse.OPTIONAL)


async def _find_meal(runtime, day: str, meal_code: str) -> int:
    """The planned, not yet cooked meal of that day and meal type."""
    meal_type = runtime.coordinator.data.meal_type(meal_code)
    if meal_type is None:
        raise ServiceValidationError(translation_domain=DOMAIN, translation_key="meal_not_found")
    week = await _api(runtime.client.meal_plan(day, 1))
    for meal in week["data"]:
        if meal["meal_type_id"] == meal_type["id"] and meal["date"] == day and not meal.get("cooked_at"):
            return int(meal["id"])
    raise ServiceValidationError(translation_domain=DOMAIN, translation_key="meal_not_found")
