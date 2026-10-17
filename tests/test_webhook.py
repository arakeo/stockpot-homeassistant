"""Webhook: registered with Stockpot at set-up; a valid signature refreshes, an invalid one is refused."""

from __future__ import annotations

import hashlib
import hmac
import json
import time

import pytest

from homeassistant.const import CONF_WEBHOOK_ID
from homeassistant.core import HomeAssistant
from homeassistant.core_config import async_process_ha_core_config
from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.test_util.aiohttp import AiohttpClientMocker

from custom_components.stockpot.const import CONF_WEBHOOK_SECRET
from custom_components.stockpot.webhook import verify_signature

from .conftest import API, SUMMARY, shopping_list

SECRET = "a" * 64


@pytest.fixture
async def with_webhook(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, config_entry: MockConfigEntry
) -> MockConfigEntry:
    """Stockpot accepts the webhook URL and returns a secret."""
    await async_process_ha_core_config(hass, {"internal_url": "http://192.168.1.5:8123"})
    aioclient_mock.get(f"{API}/ha/summary", json={"data": SUMMARY})
    aioclient_mock.get(f"{API}/shopping-lists/1", json=shopping_list())
    aioclient_mock.put(
        f"{API}/households/7/ha-webhook", status=201, json={"data": {"configured": True, "secret": SECRET}}
    )
    config_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()
    return config_entry


def _signed(body: bytes, secret: str = SECRET, timestamp: int | None = None) -> dict[str, str]:
    ts = str(int(time.time()) if timestamp is None else timestamp)
    signature = "sha256=" + hmac.new(secret.encode(), ts.encode() + b"." + body, hashlib.sha256).hexdigest()
    return {"X-Stockpot-Timestamp": ts, "X-Stockpot-Signature": signature, "Content-Type": "application/json"}


def _summary_calls(mock: AiohttpClientMocker) -> int:
    return sum(1 for c in mock.mock_calls if str(c[1]) == f"{API}/ha/summary")


async def test_registered_at_setup(with_webhook: MockConfigEntry, aioclient_mock: AiohttpClientMocker) -> None:
    """The HA webhook URL is sent to Stockpot and the secret kept."""
    webhook_id = with_webhook.data[CONF_WEBHOOK_ID]
    (put,) = [c for c in aioclient_mock.mock_calls if c[0] == "PUT"]
    assert put[2] == {"url": f"http://192.168.1.5:8123/api/webhook/{webhook_id}"}
    assert with_webhook.data[CONF_WEBHOOK_SECRET] == SECRET


async def test_valid_signature_refreshes(
    hass: HomeAssistant, with_webhook: MockConfigEntry, aioclient_mock: AiohttpClientMocker, hass_client_no_auth
) -> None:
    """A signed notification of the household triggers a refresh."""
    client = await hass_client_no_auth()
    body = json.dumps({"event": "stockpot.changed", "household_id": 7}).encode()
    before = _summary_calls(aioclient_mock)

    response = await client.post(f"/api/webhook/{with_webhook.data[CONF_WEBHOOK_ID]}", data=body, headers=_signed(body))
    await hass.async_block_till_done()

    assert response.status == 200
    assert _summary_calls(aioclient_mock) == before + 1


@pytest.mark.parametrize(
    ("headers_for", "payload"),
    [
        (lambda body: _signed(body, secret="b" * 64), {"household_id": 7}),
        (lambda body: _signed(body, timestamp=int(time.time()) - 600), {"household_id": 7}),
        (lambda body: {"Content-Type": "application/json"}, {"household_id": 7}),
        (lambda body: _signed(body + b" "), {"household_id": 7}),
        (lambda body: _signed(body), {"household_id": 8}),
    ],
    ids=["other secret", "old timestamp", "unsigned", "body changed", "other household"],
)
async def test_invalid_signature_is_refused(
    hass: HomeAssistant,
    with_webhook: MockConfigEntry,
    aioclient_mock: AiohttpClientMocker,
    hass_client_no_auth,
    headers_for,
    payload,
) -> None:
    """Refused (401), no refresh."""
    client = await hass_client_no_auth()
    body = json.dumps({"event": "stockpot.changed", **payload}).encode()
    before = _summary_calls(aioclient_mock)

    response = await client.post(
        f"/api/webhook/{with_webhook.data[CONF_WEBHOOK_ID]}", data=body, headers=headers_for(body)
    )
    await hass.async_block_till_done()

    assert response.status == 401
    assert _summary_calls(aioclient_mock) == before


async def test_refused_registration_keeps_polling(hass: HomeAssistant, setup_integration: MockConfigEntry) -> None:
    """Stockpot refuses the host (allow-list): no secret, the integration still works."""
    assert setup_integration.data.get(CONF_WEBHOOK_SECRET) is None
    assert hass.states.get("sensor.foyer_martin_items_to_buy").state == "2"


async def test_non_object_payload_is_rejected(
    hass: HomeAssistant, with_webhook: MockConfigEntry, hass_client_no_auth
) -> None:
    """A signed body that is not a JSON object → 400."""
    client = await hass_client_no_auth()
    body = b"[1, 2]"
    response = await client.post(f"/api/webhook/{with_webhook.data[CONF_WEBHOOK_ID]}", data=body, headers=_signed(body))
    assert response.status == 400


def test_signature_vector_shared_with_stockpot() -> None:
    """Same vector as Stockpot's HaWebhookTest (PHP WebhookSignature::sign)."""
    body = b'{"event":"stockpot.changed","household_id":7}'
    signature = "sha256=c9ac2d8ae6c1fffa82aba61214ae7a8b16cb2995840491daab495c1d7b36565c"
    assert verify_signature("0123456789abcdef0123456789abcdef", "1800000000", body, signature, 1_800_000_000)


def test_verify_signature_helper() -> None:
    """Same algorithm as Stockpot (WebhookSignature::sign)."""
    body = b'{"a":1}'
    headers = _signed(body, timestamp=1_800_000_000)
    assert verify_signature(SECRET, "1800000000", body, headers["X-Stockpot-Signature"], 1_800_000_100)
    assert not verify_signature(SECRET, "1800000000", body, headers["X-Stockpot-Signature"], 1_800_000_301)
    assert not verify_signature(SECRET, "nope", body, headers["X-Stockpot-Signature"], 1_800_000_000)
    assert not verify_signature("", "1800000000", body, headers["X-Stockpot-Signature"], 1_800_000_000)
