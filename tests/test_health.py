"""Health sensors: Stockpot status, failed jobs, Claude cost of the month (Stockpot M14)."""

from __future__ import annotations

from typing import Any

from homeassistant.components.sensor import ATTR_OPTIONS, ATTR_STATE_CLASS, SensorDeviceClass, SensorStateClass
from homeassistant.const import ATTR_DEVICE_CLASS, ATTR_UNIT_OF_MEASUREMENT, STATE_UNAVAILABLE, EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.test_util.aiohttp import AiohttpClientMocker

from .conftest import API, shopping_list

HEALTH = "sensor.foyer_martin_stockpot_health"
FAILED_JOBS = "sensor.foyer_martin_failed_jobs_24_h"
LLM_COST = "sensor.foyer_martin_claude_cost_this_month"


async def _setup(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, summary: dict[str, Any], config_entry: MockConfigEntry
) -> None:
    aioclient_mock.get(f"{API}/ha/summary", json={"data": summary})
    aioclient_mock.get(f"{API}/shopping-lists/1", json=shopping_list())
    aioclient_mock.put(f"{API}/households/7/ha-webhook", status=403, json={"code": "forbidden"})
    config_entry.add_to_hass(hass)
    await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()


async def test_health_sensors(hass: HomeAssistant, setup_integration: MockConfigEntry) -> None:
    """Values, device classes, units and attributes from the `health` block."""
    health = hass.states.get(HEALTH)
    assert health.state == "ok"
    assert health.attributes[ATTR_DEVICE_CLASS] == SensorDeviceClass.ENUM
    assert health.attributes[ATTR_OPTIONS] == ["ok", "degraded"]

    failed = hass.states.get(FAILED_JOBS)
    assert failed.state == "0"
    assert failed.attributes[ATTR_STATE_CLASS] == SensorStateClass.MEASUREMENT

    cost = hass.states.get(LLM_COST)
    assert float(cost.state) == 1.2345
    assert cost.attributes[ATTR_DEVICE_CLASS] == SensorDeviceClass.MONETARY
    assert cost.attributes[ATTR_UNIT_OF_MEASUREMENT] == "USD"
    assert cost.attributes[ATTR_STATE_CLASS] == SensorStateClass.TOTAL
    assert cost.attributes["cap_usd"] == 20.0
    assert cost.attributes["blocked"] is False

    registry = er.async_get(hass)
    entry_id = setup_integration.entry_id
    assert registry.async_get(HEALTH).unique_id == f"{entry_id}_health"
    assert registry.async_get(HEALTH).entity_category is EntityCategory.DIAGNOSTIC
    assert registry.async_get(FAILED_JOBS).unique_id == f"{entry_id}_failed_jobs"
    assert registry.async_get(FAILED_JOBS).entity_category is EntityCategory.DIAGNOSTIC
    assert registry.async_get(LLM_COST).unique_id == f"{entry_id}_llm_cost_month"
    assert registry.async_get(LLM_COST).entity_category is None


async def test_degraded_and_blocked(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, summary: dict[str, Any], config_entry: MockConfigEntry
) -> None:
    """Degraded instance, failed jobs, Claude blocked, no household cap."""
    summary["health"] = {
        "status": "degraded",
        "failed_jobs_24h": 3,
        "llm_cost_month_usd": 20.5,
        "llm_cap_usd": None,
        "llm_blocked": True,
    }
    await _setup(hass, aioclient_mock, summary, config_entry)

    assert hass.states.get(HEALTH).state == "degraded"
    assert hass.states.get(FAILED_JOBS).state == "3"
    cost = hass.states.get(LLM_COST)
    assert float(cost.state) == 20.5
    assert cost.attributes["cap_usd"] is None
    assert cost.attributes["blocked"] is True


async def test_unavailable_without_health(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, summary: dict[str, Any], config_entry: MockConfigEntry
) -> None:
    """An older Stockpot sends no `health` block: the sensors are unavailable, the rest works."""
    del summary["health"]
    await _setup(hass, aioclient_mock, summary, config_entry)

    for entity_id in (HEALTH, FAILED_JOBS, LLM_COST):
        assert hass.states.get(entity_id).state == STATE_UNAVAILABLE
    assert hass.states.get("sensor.foyer_martin_items_to_buy").state == "2"
