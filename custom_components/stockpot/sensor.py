"""Sensors: items to buy, low stock, expiring products, next meal, Stockpot health."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .coordinator import StockpotConfigEntry, StockpotData
from .entity import StockpotEntity

PARALLEL_UPDATES = 0


@dataclass(frozen=True, kw_only=True)
class StockpotSensorDescription(SensorEntityDescription):
    """A Stockpot sensor."""

    value: Callable[[StockpotData], Any]
    attributes: Callable[[StockpotData], dict[str, Any]] = lambda _: {}
    exists: Callable[[StockpotData], bool] = lambda _: True


def _shopping(data: StockpotData) -> dict[str, Any]:
    return data.summary["shopping"]


def _next_meal_attributes(data: StockpotData) -> dict[str, Any]:
    meal = data.summary.get("next_meal")
    if not meal:
        return {}
    return {
        "entry_id": meal["id"],
        "date": meal["date"],
        "meal_type": meal["meal_type"]["code"],
        "starts_at": meal["starts_at"],
        "recipe_id": meal["recipe"]["id"] if meal.get("recipe") else None,
        "servings": float(meal["servings"]),
        "present": [m["first_name"] for m in meal.get("members", [])],
    }


def _health(data: StockpotData) -> dict[str, Any] | None:
    """Health block of the summary (absent before Stockpot M14)."""
    health = data.summary.get("health")
    return health if isinstance(health, dict) else None


def _has_health(data: StockpotData) -> bool:
    return _health(data) is not None


def _health_value(key: str, cast: Callable[[Any], Any]) -> Callable[[StockpotData], Any]:
    def value(data: StockpotData) -> Any:
        raw = (_health(data) or {}).get(key)
        return None if raw is None else cast(raw)

    return value


def _llm_cost_attributes(data: StockpotData) -> dict[str, Any]:
    health = _health(data) or {}
    cap = health.get("llm_cap_usd")
    return {
        "cap_usd": None if cap is None else float(cap),
        "blocked": bool(health.get("llm_blocked")),
    }


SENSORS: tuple[StockpotSensorDescription, ...] = (
    StockpotSensorDescription(
        key="to_buy",
        translation_key="to_buy",
        value=lambda d: _shopping(d)["to_buy"],
        attributes=lambda d: {
            "open": _shopping(d)["open"],
            "in_cart": _shopping(d)["in_cart"],
            "ordered": _shopping(d)["ordered"],
            "low_stock": _shopping(d)["low_stock"],
            "next_order_day": _shopping(d)["next_order_day"],
            "stale": d.stale,
        },
    ),
    StockpotSensorDescription(
        key="low_stock",
        translation_key="low_stock",
        value=lambda d: d.summary["stock_counts"]["low_stock"],
        attributes=lambda d: {"products": d.summary["low_stock"]},
    ),
    StockpotSensorDescription(
        key="expiring",
        translation_key="expiring",
        value=lambda d: d.summary["stock_counts"]["expiring"],
        attributes=lambda d: {
            "products": d.summary["expiring"],
            "expired": d.summary["stock_counts"]["expired"],
            "horizon_days": d.summary["household"]["expiry_alert_days"],
        },
    ),
    StockpotSensorDescription(
        key="next_meal",
        translation_key="next_meal",
        value=lambda d: (d.summary.get("next_meal") or {}).get("title") or None,
        attributes=_next_meal_attributes,
    ),
    # Stockpot health (M14). Older versions send no `health` block: these sensors are unavailable.
    StockpotSensorDescription(
        key="health",
        translation_key="health",
        device_class=SensorDeviceClass.ENUM,
        options=["ok", "degraded"],
        entity_category=EntityCategory.DIAGNOSTIC,
        value=_health_value("status", str),
        exists=_has_health,
    ),
    StockpotSensorDescription(
        key="failed_jobs",
        translation_key="failed_jobs",
        state_class=SensorStateClass.MEASUREMENT,
        entity_category=EntityCategory.DIAGNOSTIC,
        value=_health_value("failed_jobs_24h", int),
        exists=_has_health,
    ),
    StockpotSensorDescription(
        key="llm_cost_month",
        translation_key="llm_cost_month",
        device_class=SensorDeviceClass.MONETARY,
        native_unit_of_measurement="USD",
        # Monetary sensors accept `total` (not `measurement`); the sum restarts each calendar month.
        state_class=SensorStateClass.TOTAL,
        suggested_display_precision=2,
        value=_health_value("llm_cost_month_usd", float),
        attributes=_llm_cost_attributes,
        exists=_has_health,
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: StockpotConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Add the sensors of a household."""
    coordinator = entry.runtime_data.coordinator
    async_add_entities(StockpotSensor(coordinator, d) for d in SENSORS)


class StockpotSensor(StockpotEntity, SensorEntity):
    """A summary sensor (unit translated: « articles » / « items »)."""

    # Product lists (≤ 50, bounded by Stockpot) are not written to the recorder.
    _unrecorded_attributes = frozenset({"products"})

    entity_description: StockpotSensorDescription

    def __init__(self, coordinator, description: StockpotSensorDescription) -> None:
        """Create the sensor."""
        super().__init__(coordinator, description.key)
        self.entity_description = description

    @property
    def available(self) -> bool:
        """Unavailable when the Stockpot version does not send the sensor's data."""
        return super().available and self.entity_description.exists(self.coordinator.data)

    @property
    def native_value(self) -> Any:
        """Value from the last poll."""
        return self.entity_description.value(self.coordinator.data)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Lists and details from the last poll."""
        return self.entity_description.attributes(self.coordinator.data)
