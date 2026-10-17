"""Binary sensor: « Commande Drive à passer » (lines to buy and Drive day reached)."""

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
    """Add the binary sensor of a household."""
    async_add_entities([DriveOrderDue(entry.runtime_data.coordinator)])


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
