"""Binary sensors: « Commande Drive à passer », « Commande à récupérer »."""

from __future__ import annotations

from typing import Any

from homeassistant.components.binary_sensor import BinarySensorEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .coordinator import StockpotConfigEntry
from .entity import StockpotEntity

PARALLEL_UPDATES = 0


async def async_setup_entry(
    hass: HomeAssistant,
    entry: StockpotConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Add the binary sensors of a household."""
    coordinator = entry.runtime_data.coordinator
    async_add_entities([DriveOrderDue(coordinator), OrderToCollect(coordinator)])


class DriveOrderDue(StockpotEntity, BinarySensorEntity):
    """On when the main list has lines to buy and today is the household's Drive day."""

    _attr_translation_key = "drive_order_due"

    def __init__(self, coordinator) -> None:
        """Create the binary sensor."""
        super().__init__(coordinator, "drive_order_due")

    @property
    def is_on(self) -> bool:
        """Order to place today."""
        return bool(self.coordinator.data.summary["shopping"]["drive_order_due"])

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Next Drive day and lines still to buy."""
        shopping = self.coordinator.data.summary["shopping"]
        return {"next_order_day": shopping["next_order_day"], "open": shopping["open"]}


class OrderToCollect(StockpotEntity, BinarySensorEntity):
    """On while a placed Drive order waits to be collected (its reception not yet validated).

    The pickup time is the one the person chose on the retailer's site, read on the confirmed
    order (Stockpot never chooses it). Older Stockpot versions without `orders`: off.
    """

    _attr_translation_key = "order_to_collect"

    def __init__(self, coordinator) -> None:
        """Create the binary sensor."""
        super().__init__(coordinator, "order_to_collect")

    def _orders(self) -> dict[str, Any]:
        return self.coordinator.data.summary.get("orders") or {}

    @property
    def is_on(self) -> bool:
        """An order waits for its pickup."""
        return int(self._orders().get("awaiting_pickup") or 0) > 0

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Next order to collect: number, pickup time, lines, total."""
        orders = self._orders()
        nxt = orders.get("next") or {}
        return {
            "awaiting_pickup": int(orders.get("awaiting_pickup") or 0),
            "pickup_due": bool(orders.get("pickup_due")),
            "order_id": nxt.get("id"),
            "order_number": nxt.get("order_number"),
            "pickup_at": nxt.get("pickup_at"),
            "pickup_until": nxt.get("pickup_until"),
            "lines_count": nxt.get("lines_count"),
            "total": nxt.get("total"),
        }
