"""Data coordinator of the Stockpot integration (60 s polling + webhook refresh)."""

from __future__ import annotations

from dataclasses import dataclass, field
import logging
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from .api import StockpotApiError, StockpotAuthError, StockpotClient, StockpotError
from .const import DOMAIN, UPDATE_INTERVAL

_LOGGER = logging.getLogger(__name__)

AUTH_FAILURES = ("missing_ability", "household_forbidden", "two_factor_required")

type StockpotConfigEntry = ConfigEntry[StockpotRuntimeData]


@dataclass
class StockpotData:
    """One poll: the summary and every shopping list with its lines."""

    summary: dict[str, Any]
    lists: dict[int, dict[str, Any]] = field(default_factory=dict)

    @property
    def stale(self) -> bool:
        """The main list has changes Stockpot has not rebuilt yet (ADR 0009)."""
        return bool(self.summary.get("shopping", {}).get("stale"))

    def meal_type(self, code: str) -> dict[str, Any] | None:
        """Meal type of the household by code."""
        for meal_type in self.summary.get("meal_types", []):
            if meal_type["code"] == code:
                return meal_type
        return None


@dataclass
class StockpotRuntimeData:
    """Runtime data stored on the config entry."""

    client: StockpotClient
    coordinator: StockpotCoordinator
    webhook_id: str | None = None


class StockpotCoordinator(DataUpdateCoordinator[StockpotData]):
    """Polls /ha/summary and the shopping lists."""

    config_entry: StockpotConfigEntry

    def __init__(self, hass: HomeAssistant, entry: StockpotConfigEntry, client: StockpotClient) -> None:
        """Create the coordinator."""
        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name=DOMAIN,
            update_interval=UPDATE_INTERVAL,
        )
        self.client = client

    async def _async_update_data(self) -> StockpotData:
        try:
            summary = await self.client.summary()
            today = dt_util.now().date().isoformat()
            lists: dict[int, dict[str, Any]] = {}
            for shopping_list in summary["shopping"]["lists"]:
                detail = await self.client.shopping_list(shopping_list["id"], received_since=today)
                lists[detail["id"]] = detail
        except StockpotAuthError as err:
            raise ConfigEntryAuthFailed(translation_domain=DOMAIN, translation_key="invalid_auth") from err
        except StockpotApiError as err:
            # Token refused for this household: retrying every 60 s would never succeed.
            if err.status == 403 and err.code in AUTH_FAILURES:
                raise ConfigEntryAuthFailed(translation_domain=DOMAIN, translation_key=err.code) from err
            raise UpdateFailed(
                translation_domain=DOMAIN,
                translation_key="api_error",
                translation_placeholders={"error": str(err)},
            ) from err
        except StockpotError as err:
            raise UpdateFailed(
                translation_domain=DOMAIN,
                translation_key="cannot_connect",
                translation_placeholders={"error": str(err)},
            ) from err
        return StockpotData(summary=summary, lists=lists)
