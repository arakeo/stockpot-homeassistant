"""Stockpot → Home Assistant webhook: immediate refresh on change (Stockpot ADR 0010).

At set-up the integration registers a HA webhook and gives its URL to Stockpot
(`PUT /households/{id}/ha-webhook`, owner's `ha` token); Stockpot answers with an HMAC
secret, kept in the config entry. Each notification is signed:
`X-Stockpot-Signature: sha256=HMAC-SHA256(secret, "<X-Stockpot-Timestamp>.<raw body>")`.
A bad signature, a timestamp more than 5 minutes away or another household is refused (401);
a valid one triggers a coordinator refresh. Without a reachable HA URL, an allow-listed
host on the Stockpot side or owner rights, the integration simply keeps polling.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import time

from aiohttp import web

from homeassistant.components import webhook
from homeassistant.const import CONF_WEBHOOK_ID
from homeassistant.core import HomeAssistant
from homeassistant.helpers.network import NoURLAvailableError

from .api import StockpotError
from .const import (
    CONF_HOUSEHOLD_ID,
    CONF_WEBHOOK_SECRET,
    DOMAIN,
    SIGNATURE_HEADER,
    SIGNATURE_TOLERANCE,
    TIMESTAMP_HEADER,
)
from .coordinator import StockpotConfigEntry

_LOGGER = logging.getLogger(__name__)


def verify_signature(secret: str, timestamp: str | None, body: bytes, signature: str | None, now: float) -> bool:
    """Constant-time check of the signature and of the replay window."""
    if not secret or not timestamp or not signature:
        return False
    try:
        sent_at = int(timestamp)
    except ValueError:
        return False
    if abs(now - sent_at) > SIGNATURE_TOLERANCE:
        return False
    expected = "sha256=" + hmac.new(secret.encode(), timestamp.encode() + b"." + body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature)


async def async_setup_webhook(hass: HomeAssistant, entry: StockpotConfigEntry) -> None:
    """Register the HA webhook and hand its URL to Stockpot (best effort)."""
    runtime = entry.runtime_data
    webhook_id = entry.data.get(CONF_WEBHOOK_ID) or webhook.async_generate_id()
    if entry.data.get(CONF_WEBHOOK_ID) != webhook_id:
        hass.config_entries.async_update_entry(entry, data={**entry.data, CONF_WEBHOOK_ID: webhook_id})

    webhook.async_register(
        hass,
        DOMAIN,
        f"Stockpot ({entry.title})",
        webhook_id,
        _handler(entry),
        allowed_methods=["POST"],
    )
    runtime.webhook_id = webhook_id

    try:
        url = webhook.async_generate_url(hass, webhook_id, allow_internal=True, prefer_external=False)
    except NoURLAvailableError:
        _LOGGER.info("No Home Assistant URL configured: Stockpot will be polled only")
        return

    try:
        secret = await runtime.client.register_webhook(url)
    except StockpotError as err:
        _LOGGER.info("Stockpot did not accept the webhook (%s): polling every 60 s only", err)
        hass.config_entries.async_update_entry(entry, data={**entry.data, CONF_WEBHOOK_SECRET: None})
        return
    hass.config_entries.async_update_entry(entry, data={**entry.data, CONF_WEBHOOK_SECRET: secret})


def async_unload_webhook(hass: HomeAssistant, entry: StockpotConfigEntry) -> None:
    """Unregister the HA webhook."""
    if entry.runtime_data.webhook_id:
        webhook.async_unregister(hass, entry.runtime_data.webhook_id)


def _handler(entry: StockpotConfigEntry):
    async def handle(hass: HomeAssistant, webhook_id: str, request: web.Request) -> web.Response:
        body = await request.read()
        secret = entry.data.get(CONF_WEBHOOK_SECRET) or ""
        if not verify_signature(
            secret,
            request.headers.get(TIMESTAMP_HEADER),
            body,
            request.headers.get(SIGNATURE_HEADER),
            time.time(),
        ):
            _LOGGER.warning("Stockpot webhook refused: invalid signature or timestamp")
            return web.Response(status=401)
        try:
            payload = json.loads(body)
        except ValueError:
            return web.Response(status=400)
        if not isinstance(payload, dict):
            return web.Response(status=400)
        if payload.get("household_id") != entry.data.get(CONF_HOUSEHOLD_ID):
            return web.Response(status=401)
        await entry.runtime_data.coordinator.async_request_refresh()
        return web.Response(status=200)

    return handle
