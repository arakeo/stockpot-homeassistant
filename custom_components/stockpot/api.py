"""Client of the Stockpot API (/api/v1), as used by the Home Assistant integration.

Authentication (Bearer): a personal access token with the `ha` ability, or an OAuth access
token (Stockpot M17), which behaves exactly like a household-bound `ha` token. With OAuth, a
token provider returns a valid access token before each request (refreshed by Home Assistant's
`OAuth2Session`). The household is chosen with the `X-Household` header. Errors follow
RFC 7807 (`code` member).
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import quote
import uuid

import aiohttp

from .const import REQUEST_TIMEOUT

IDEMPOTENT_RETRIES = 2
RETRY_DELAY = 1.0


class StockpotError(Exception):
    """Base error of the Stockpot client."""


class StockpotConnectionError(StockpotError):
    """Stockpot cannot be reached (network, timeout, invalid response)."""


class StockpotSslError(StockpotConnectionError):
    """The TLS certificate of Stockpot was refused."""


class StockpotAuthError(StockpotError):
    """The token is invalid, revoked or expired (401)."""


@dataclass
class StockpotApiError(StockpotError):
    """Stockpot answered with a problem (4xx / 5xx)."""

    status: int
    code: str
    detail: str | None = None
    errors: dict[str, list[str]] = field(default_factory=dict)
    abilities: list[str] = field(default_factory=list)

    def __str__(self) -> str:
        """Readable message (detail, or first field error)."""
        if self.errors:
            first = next(iter(self.errors.values()))
            if first:
                return first[0]
        return self.detail or self.code


type TokenProvider = Callable[[str | None], Awaitable[str]]
"""Returns a valid access token; given the token Stockpot just refused (401), renews it first."""


def new_idempotency_key() -> str:
    """One key per action: Stockpot replays a retried request instead of applying it twice."""
    return f"ha-{uuid.uuid4()}"


class StockpotClient:
    """Thin async client of the Stockpot API."""

    def __init__(
        self,
        session: aiohttp.ClientSession,
        url: str,
        token: str,
        household_id: int | None = None,
        *,
        token_provider: TokenProvider | None = None,
    ) -> None:
        """Create the client (`url` = origin of the Stockpot instance).

        `token_provider` (OAuth entries) replaces the fixed `token`."""
        self._session = session
        self._base = url.rstrip("/") + "/api/v1"
        self._token = token
        self._token_provider = token_provider
        self.household_id = household_id

    async def _request(
        self,
        method: str,
        path: str,
        *,
        json: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
        idempotency_key: str | None = None,
        household: bool = True,
    ) -> Any:
        """One request; with an Idempotency-Key, a timeout / network error / 5xx is retried
        (at most IDEMPOTENT_RETRIES times) with the SAME key: Stockpot replays the action
        instead of applying it twice."""
        attempt = 0
        refused: str | None = None
        while True:
            token = await self._access_token(refused)
            try:
                return await self._send(method, path, json, params, idempotency_key, household, token)
            except StockpotAuthError:
                # OAuth: an access token refused before its expiry (revoked, clock skew) is
                # renewed once; a second 401 means the access itself is gone → reauth.
                if self._token_provider is None or refused is not None:
                    raise
                refused = token
                continue
            except StockpotSslError:
                raise
            except (StockpotConnectionError, StockpotApiError) as err:
                retryable = isinstance(err, StockpotConnectionError) or err.status >= 500
                if idempotency_key is None or not retryable or attempt >= IDEMPOTENT_RETRIES:
                    raise
                attempt += 1
                await asyncio.sleep(RETRY_DELAY * attempt)

    async def _access_token(self, refused: str | None) -> str:
        """Token of the next request (fixed, or from the OAuth token provider)."""
        if self._token_provider is not None:
            return await self._token_provider(refused)
        return self._token

    async def _send(
        self,
        method: str,
        path: str,
        json: dict[str, Any] | None,
        params: dict[str, Any] | None,
        idempotency_key: str | None,
        household: bool,
        token: str,
    ) -> Any:
        headers = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
        }
        if household and self.household_id is not None:
            headers["X-Household"] = str(self.household_id)
        if idempotency_key is not None:
            headers["Idempotency-Key"] = idempotency_key
        try:
            async with asyncio.timeout(REQUEST_TIMEOUT):
                async with self._session.request(
                    method,
                    self._base + path,
                    json=json,
                    params=params,
                    headers=headers,
                    allow_redirects=False,
                ) as response:
                    if response.status == 204:
                        return None
                    try:
                        payload = await response.json(content_type=None)
                    except (ValueError, aiohttp.ContentTypeError) as err:
                        raise StockpotConnectionError(f"Invalid response ({response.status})") from err
                    if response.status == 401:
                        raise StockpotAuthError(_detail(payload))
                    if response.status >= 400:
                        raise _problem(response.status, payload)
                    return payload
        except aiohttp.ClientSSLError as err:
            raise StockpotSslError(str(err)) from err
        except (aiohttp.ClientError, TimeoutError) as err:
            raise StockpotConnectionError(str(err) or err.__class__.__name__) from err

    # --- account -----------------------------------------------------------------

    async def instance_id(self) -> str:
        """Stable id of the Stockpot instance (same behind a new URL)."""
        return str((await self._request("GET", "/me", household=False))["meta"]["instance_id"])

    async def households(self) -> list[dict[str, Any]]:
        """Households of the token's user (with `role`)."""
        return (await self._request("GET", "/households", household=False))["data"]

    # --- reads -------------------------------------------------------------------

    async def summary(self) -> dict[str, Any]:
        """What the coordinator polls (`ha` ability)."""
        return (await self._request("GET", "/ha/summary"))["data"]

    async def shopping_list(self, list_id: int, received_since: str | None = None) -> dict[str, Any]:
        """A list with its lines (+ lines received since that day)."""
        params = {"received_since": received_since} if received_since else None
        return (await self._request("GET", f"/shopping-lists/{list_id}", params=params))["data"]

    async def meal_plan(self, start: str, days: int) -> dict[str, Any]:
        """Planning of [start, start + days[ (days ≤ 31)."""
        return await self._request("GET", "/meal-plan", params={"from": start, "days": days})

    async def barcode(self, ean: str) -> dict[str, Any]:
        """Product and pack of an EAN of the household."""
        return (await self._request("GET", f"/ha/barcodes/{quote(ean, safe='')}"))["data"]

    # --- shopping ----------------------------------------------------------------

    async def add_item(self, list_id: int, payload: dict[str, Any]) -> dict[str, Any]:
        """Add a line (product or free text)."""
        return (await self._request("POST", f"/shopping-lists/{list_id}/items", json=payload))["data"]

    async def update_item(self, item_id: int, payload: dict[str, Any]) -> dict[str, Any]:
        """Change a line (label, note, status…)."""
        return (await self._request("PATCH", f"/shopping-list-items/{item_id}", json=payload))["data"]

    async def delete_item(self, item_id: int) -> None:
        """Remove a line."""
        await self._request("DELETE", f"/shopping-list-items/{item_id}")

    async def receive_item(self, item_id: int, add_to_stock: bool) -> dict[str, Any]:
        """« Reçu »: the line leaves the list (and enters the stock for a product)."""
        return await self._request(
            "POST",
            f"/shopping-list-items/{item_id}/receive",
            json={"add_to_stock": add_to_stock},
            idempotency_key=new_idempotency_key(),
        )

    async def move_item(self, item_id: int, after_id: int | None) -> dict[str, Any]:
        """Move a line after another one (None = top)."""
        return (
            await self._request(
                "POST",
                f"/shopping-list-items/{item_id}/move",
                json={"after_id": after_id},
            )
        )["data"]

    async def regenerate(self, list_id: int, until: str | None = None) -> dict[str, Any]:
        """Rebuild the main list now (optionally for another period end)."""
        payload = {"until": until} if until else {}
        return (await self._request("POST", f"/shopping-lists/{list_id}/regenerate", json=payload))["data"]

    # --- stock -------------------------------------------------------------------

    async def stock_action(self, action: str, payload: dict[str, Any]) -> dict[str, Any]:
        """POST /stock/{purchase|consume} with a fresh Idempotency-Key."""
        return await self._request(
            "POST",
            f"/stock/{action}",
            json=payload,
            idempotency_key=new_idempotency_key(),
        )

    # --- planning ----------------------------------------------------------------

    async def plan_meal(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Create a planned meal."""
        return (await self._request("POST", "/meal-plan-entries", json=payload))["data"]

    async def cook_meal(self, entry_id: int) -> dict[str, Any]:
        """« Marquer comme cuisiné » (stock decremented by Stockpot)."""
        return await self._request(
            "POST",
            f"/meal-plan-entries/{entry_id}/cook",
            json={},
            idempotency_key=new_idempotency_key(),
        )

    # --- panel -------------------------------------------------------------------

    async def panel_code(self) -> dict[str, Any]:
        """A single-use code for the Stockpot panel (Stockpot M18): `{code, expires_in}`.

        Needs an OAuth access token of a grant whose panel is enabled (403 `panel_disabled` /
        `panel_requires_oauth` otherwise)."""
        return await self._request("POST", "/ha/panel-codes")

    # --- webhook -----------------------------------------------------------------

    async def register_webhook(self, url: str) -> str:
        """Register the HA webhook of the household; returns the HMAC secret (shown once)."""
        response = await self._request(
            "PUT",
            f"/households/{self.household_id}/ha-webhook",
            json={"url": url},
            household=False,
        )
        return response["data"]["secret"]

    async def unregister_webhook(self) -> None:
        """Remove the HA webhook of the household."""
        await self._request("DELETE", f"/households/{self.household_id}/ha-webhook", household=False)


def _detail(payload: Any) -> str:
    if isinstance(payload, dict):
        return str(payload.get("detail") or payload.get("title") or payload.get("code") or "")
    return ""


def _problem(status: int, payload: Any) -> StockpotApiError:
    if not isinstance(payload, dict):
        return StockpotApiError(status, "unknown")
    errors = payload.get("errors")
    abilities = payload.get("abilities")
    return StockpotApiError(
        status=status,
        code=str(payload.get("code") or "unknown"),
        detail=payload.get("detail") or payload.get("title"),
        errors=errors if isinstance(errors, dict) else {},
        abilities=abilities if isinstance(abilities, list) else [],
    )
