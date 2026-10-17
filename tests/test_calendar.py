"""Calendars: events of a period from /meal-plan, filtered by meal type, at the meal times."""

from __future__ import annotations

from datetime import datetime

from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.test_util.aiohttp import AiohttpClientMocker

from .conftest import API

WEEK = {
    "data": [
        {
            "id": 40,
            "date": "2026-10-14",
            "meal_type_id": 4,
            "recipe": {"id": 9, "title": "Hachis parmentier"},
            "note": None,
            "servings": "2.5",
            "members": [{"id": 1, "first_name": "Camille"}],
            "cooked_at": None,
        },
        {
            "id": 41,
            "date": "2026-10-15",
            "meal_type_id": 2,
            "recipe": None,
            "note": "Restes",
            "servings": "2.0",
            "members": [],
            "cooked_at": None,
        },
    ],
    "meta": {},
}


async def test_events(hass: HomeAssistant, setup_integration: MockConfigEntry, mock_api: AiohttpClientMocker) -> None:
    """« Repas » shows every meal; « Dîner » only the dinners."""
    await hass.config.async_set_time_zone("Europe/Paris")
    mock_api.get(f"{API}/meal-plan", json=WEEK)
    period = {"start_date_time": "2026-10-14T00:00:00+02:00", "end_date_time": "2026-10-16T00:00:00+02:00"}

    result = await hass.services.async_call(
        "calendar",
        "get_events",
        {"entity_id": ["calendar.foyer_martin_meals", "calendar.foyer_martin_dinner"], **period},
        blocking=True,
        return_response=True,
    )

    meals = result["calendar.foyer_martin_meals"]["events"]
    assert [(e["summary"], e["start"], e["end"]) for e in meals] == [
        ("Hachis parmentier", "2026-10-14T19:30:00+02:00", "2026-10-14T21:00:00+02:00"),
        ("Restes", "2026-10-15T12:00:00+02:00", "2026-10-15T13:30:00+02:00"),
    ]
    assert meals[0]["description"] == "2.5 servings · Camille"
    assert [e["summary"] for e in result["calendar.foyer_martin_dinner"]["events"]] == ["Hachis parmentier"]


async def test_events_use_stockpot_time_zone(
    hass: HomeAssistant, setup_integration: MockConfigEntry, mock_api: AiohttpClientMocker
) -> None:
    """HA in New York, Stockpot in Paris: a 19:30 dinner is 19:30 Paris time (13:30 in New York)."""
    await hass.config.async_set_time_zone("America/New_York")
    mock_api.get(f"{API}/meal-plan", json=WEEK)
    period = {"start_date_time": "2026-10-13T18:00:00-04:00", "end_date_time": "2026-10-15T18:00:00-04:00"}

    result = await hass.services.async_call(
        "calendar",
        "get_events",
        {"entity_id": "calendar.foyer_martin_dinner", **period},
        blocking=True,
        return_response=True,
    )

    (event,) = result["calendar.foyer_martin_dinner"]["events"]
    assert datetime.fromisoformat(event["start"]) == datetime.fromisoformat("2026-10-14T13:30:00-04:00")
    assert datetime.fromisoformat(event["end"]) == datetime.fromisoformat("2026-10-14T15:00:00-04:00")
