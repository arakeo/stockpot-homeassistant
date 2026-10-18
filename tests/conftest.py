"""Fixtures of the Stockpot integration tests (Stockpot API mocked with aioclient_mock)."""

from __future__ import annotations

from copy import deepcopy
import time
from typing import Any
from urllib.parse import parse_qs, urlsplit

import pytest

from homeassistant.const import CONF_TOKEN, CONF_URL, CONF_VERIFY_SSL
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.setup import async_setup_component
from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.test_util.aiohttp import AiohttpClientMocker

from custom_components.stockpot.const import (
    CONF_AUTH_METHOD,
    CONF_AUTHORIZE_URL,
    CONF_HOUSEHOLD_ID,
    CONF_REVOCATION_URL,
    CONF_TOKEN_URL,
    DOMAIN,
    STOCKPOT_URL,
)
from custom_components.stockpot.oauth import implementation_key

# Since 0.11.0 new entries always use the fixed Stockpot URL.
URL = STOCKPOT_URL
API = f"{URL}/api/v1"
TOKEN = "12|stkp_secret"
INSTANCE = "9b2f4c1e-0000-4000-8000-000000000001"

# OAuth (Stockpot M17): discovery document shared with the Claude connector (extra fields).
DISCOVERY_URL = f"{URL}/.well-known/oauth-authorization-server"
AUTHORIZE_URL = f"{URL}/oauth/authorize"
TOKEN_URL = f"{URL}/oauth/token"
REVOKE_URL = f"{URL}/oauth/revoke"
DISCOVERY: dict[str, Any] = {
    "issuer": URL,
    "authorization_endpoint": AUTHORIZE_URL,
    "token_endpoint": TOKEN_URL,
    "revocation_endpoint": REVOKE_URL,
    "registration_endpoint": f"{URL}/oauth/register",
    "response_types_supported": ["code"],
    "grant_types_supported": ["authorization_code", "refresh_token"],
    "code_challenge_methods_supported": ["S256"],
    "token_endpoint_auth_methods_supported": ["none", "client_secret_basic", "client_secret_post"],
    "scopes_supported": ["mcp:use", "ha"],
}
ACCESS_TOKEN = "31|stkp_oauth_access"
REFRESH_TOKEN = "refresh-secret-1"

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
    "orders": {
        "awaiting_pickup": 1,
        "pickup_due": False,
        "next": {
            "id": 12,
            "order_number": "0012345999",
            "ordered_at": "2026-10-13T18:40:00+02:00",
            "pickup_at": "2026-10-14T10:00:00+02:00",
            "pickup_until": "2026-10-14T11:00:00+02:00",
            "lines_count": 14,
            "total": "63.82",
        },
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
    "health": {
        "status": "ok",
        "failed_jobs_24h": 0,
        "llm_cost_month_usd": 1.2345,
        "llm_cap_usd": 20.0,
        "llm_blocked": False,
    },
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


def token_response(access: str = ACCESS_TOKEN, refresh: str = REFRESH_TOKEN, **extra: Any) -> dict[str, Any]:
    """POST /oauth/token answer, with the `household_id` / `instance_id` extensions."""
    return {
        "access_token": access,
        "token_type": "Bearer",
        "expires_in": 3600,
        "refresh_token": refresh,
        "scope": "ha",
        "household_id": 7,
        "instance_id": INSTANCE,
        **extra,
    }


@pytest.fixture
def oauth_config_entry() -> MockConfigEntry:
    """A household connected with the OAuth sign-in (valid access token)."""
    return MockConfigEntry(
        domain=DOMAIN,
        title="Foyer Martin",
        unique_id=f"{INSTANCE}#7",
        data={
            CONF_URL: URL,
            CONF_VERIFY_SSL: True,
            CONF_AUTHORIZE_URL: AUTHORIZE_URL,
            CONF_TOKEN_URL: TOKEN_URL,
            CONF_REVOCATION_URL: REVOKE_URL,
            CONF_AUTH_METHOD: "oauth",
            CONF_HOUSEHOLD_ID: 7,
            "auth_implementation": implementation_key(URL),
            "token": {**token_response(), "expires_at": time.time() + 3600},
        },
    )


@pytest.fixture
async def oauth_callback(hass: HomeAssistant) -> None:
    """My Home Assistant redirect (default_config) and the /auth/external/callback view."""
    hass.config.components.add("my")
    assert await async_setup_component(hass, "auth", {})


def calls(aioclient_mock: AiohttpClientMocker, method: str, url: str) -> list[tuple[Any, Any]]:
    """(data, headers) of the mocked requests to that URL."""
    return [(data, headers) for m, u, data, headers in aioclient_mock.mock_calls if m == method and str(u) == url]


async def authorize(
    hass: HomeAssistant, hass_client_no_auth, result: dict[str, Any], answer: str = "code=the-code"
) -> dict[str, Any]:
    """The browser comes back from Stockpot's consent screen with a code; the flow resumes."""
    assert result["type"] is FlowResultType.EXTERNAL_STEP
    state = parse_qs(urlsplit(result["url"]).query)["state"][0]
    client = await hass_client_no_auth()
    response = await client.get(f"/auth/external/callback?{answer}&state={state}")
    assert response.status == 200
    return await hass.config_entries.flow.async_configure(result["flow_id"])
