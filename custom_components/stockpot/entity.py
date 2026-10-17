"""Base entity of the Stockpot integration."""

from __future__ import annotations

from homeassistant.const import CONF_URL
from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import StockpotCoordinator


class StockpotEntity(CoordinatorEntity[StockpotCoordinator]):
    """An entity of a Stockpot household (one device per household)."""

    _attr_has_entity_name = True

    def __init__(self, coordinator: StockpotCoordinator, key: str) -> None:
        """Entity `key` of the household."""
        super().__init__(coordinator)
        entry = coordinator.config_entry
        self._attr_unique_id = f"{entry.entry_id}_{key}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            name=entry.title,
            manufacturer="Stockpot",
            entry_type=DeviceEntryType.SERVICE,
            configuration_url=entry.data[CONF_URL],
        )
