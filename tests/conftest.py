"""Fixtures of the Stockpot integration tests (Stockpot API mocked with aioclient_mock)."""

from __future__ import annotations

from copy import deepcopy
from typing import Any

import pytest

from homeassistant.const import CONF_TOKEN, CONF_URL, CONF_VERIFY_SSL
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.test_util.aiohttp import AiohttpClientMocker

from custom_components.stockpot.const import CONF_HOUSEHOLD_ID, DOMAIN

URL = "https://stockpot.test"
API = f"{URL}/api/v1"
TOKEN = "12|stkp_secret"
INSTANCE = "9b2f4c1e-0000-4000-8000-000000000001"

SUMMARY: dict[str, Any] = {
    "household": {"id": 7, "name": "Foyer Martin", "drive_order_weekday": 3, "expiry_alert_days": 3},
    "shopping": {
        "main_list_id": 1,
        "to_buy": 2,
        "open": 2,
        "in_cart": 0,
        "ordered": 0,
        "low_stock": 1,
        "stale": False,
        "next_order_day": "2026-10-14",
        "drive_order_due": True,
        "lists": [
            {"id": 1, "name": "Courses", "is_main": True, "counts": {"total": 2}, "stale": False},
        ],
    },
    "low_stock": [{"product_id": 5, "name": "Lait", "quantity": "0.000", "min_stock": "2.000", "unit": "pièce"}],
    "expiring": [
        {
            "product_id": 6,
            "name": "Yaourt",
            "location_id": 1,
            "quantity": "4.000",
            "unit": "pièce",
            "expires_on": "2026-10-15",
            "days_left": 1,
            "status": "soon",
        }
    ],
    "next_meal": {
        "id": 40,
        "date": "2026-10-14",
        "meal_type": {"id": 4, "code": "dinner", "name": None},
        "recipe": {"id": 9, "title": "Hachis parmentier"},
        "note": None,
        "title": "Hachis parmentier",
        "servings": "2.5",
        "members": [{"id": 1, "first_name": "Camille"}, {"id": 2, "first_name": "Léana"}],
        "cooked_at": None,
        "starts_at": "2026-10-14T19:30:00+02:00",
        "ends_at": "2026-10-14T21:00:00+02:00",
    },
    "meals": [],
    "meal_types": [
        {
            "id": 1,
            "code": "breakfast",
            "name": None,
            "position": 0,
            "active": False,
            "start_time": "07:30",
            "end_time": "08:30",
        },
        {
            "id": 2,
            "code": "lunch",
            "name": None,
            "position": 1,
            "active": True,
            "start_time": "12:00",
            "end_time": "13:30",
        },
        {
            "id": 3,
            "code": "snack",
            "name": None,
            "position": 2,
            "active": False,
            "start_time": "16:00",
            "end_time": "16:30",
        },
        {
            "id": 4,
            "code": "dinner",
            "name": None,
            "position": 3,
            "active": True,
            "start_time": "19:30",
            "end_time": "21:00",
        },
    ],
    "stock_counts": {"low_stock": 1, "expiring": 1, "expired": 0},
    "timezone": "Europe/Paris",
    "generated_at": "2026-10-14T09:00:00+02:00",
}
SUMMARY["meals"] = [SUMMARY["next_meal"]]

ITEMS: list[dict[str, Any]] = [
    {
        "id": 101,
        "name": "Beurre",
        "label": None,
        "status": "open",
        "product": {"id": 3, "name": "Beurre", "stock_tracked": True, "archived": False},
        "pack_count": 2,
        "pack_display": {"quantity": "250", "unit": "g"},
        "purchase_display": {"quantity": "500", "unit": "g"},
        "needed_display": {"quantity": "300", "unit": "g"},
        "reasons": [{"type": "recipe", "label": "Pâte brisée"}],
        "note": None,
        "received_at": None,
    },
    {
        "id": 102,
        "name": "Pain",
        "label": "Pain",
        "status": "open",
        "product": None,
        "pack_count": None,
        "pack_display": None,
        "purchase_display": None,
        "needed_display": None,
        "reasons": [{"type": "manual", "label": None}],
        "note": None,
        "received_at": None,
    },
    {
        "id": 103,
        "name": "Bougies",
        "label": "Bougies",
        "status": "skipped",
        "product": None,
        "reasons": [],
        "note": None,
        "received_at": None,
    },
]


def shopping_list(items: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    """GET /shopping-lists/1 payload."""
    return {
        "data": {
            "id": 1,
            "name": "Courses",
            "is_main": True,
            "stale": False,
            "items": deepcopy(ITEMS if items is None else items),
        },
        "meta": {"stale": False},
    }


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations: None) -> None:
    """Load custom_components/stockpot."""


@pytest.fixture
def summary() -> dict[str, Any]:
    """A copy of the summary payload."""
    return deepcopy(SUMMARY)


@pytest.fixture
def mock_api(aioclient_mock: AiohttpClientMocker, summary: dict[str, Any]) -> AiohttpClientMocker:
    """The read endpoints the coordinator polls, and a refused webhook registration."""
    aioclient_mock.get(f"{API}/me", json={"data": {"id": 1}, "meta": {"instance_id": INSTANCE}})
    aioclient_mock.get(f"{API}/ha/summary", json={"data": summary})
    aioclient_mock.get(f"{API}/shopping-lists/1", json=shopping_list())
    aioclient_mock.get(f"{API}/households", json={"data": [{"id": 7, "name": "Foyer Martin", "role": "owner"}]})
    aioclient_mock.put(
        f"{API}/households/7/ha-webhook",
        status=422,
        json={"code": "validation_failed", "errors": {"url": ["Hôte non autorisé"]}},
    )
    return aioclient_mock


@pytest.fixture
def config_entry() -> MockConfigEntry:
    """A configured household."""
    return MockConfigEntry(
        domain=DOMAIN,
        title="Foyer Martin",
        unique_id=f"{INSTANCE}#7",
        data={CONF_URL: URL, CONF_TOKEN: TOKEN, CONF_VERIFY_SSL: True, CONF_HOUSEHOLD_ID: 7},
    )


@pytest.fixture
async def setup_integration(
    hass: HomeAssistant, mock_api: AiohttpClientMocker, config_entry: MockConfigEntry
) -> MockConfigEntry:
    """The integration set up with the mocked API."""
    config_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()
    return config_entry
