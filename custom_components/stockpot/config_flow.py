"""Config flow of the Stockpot integration: URL, `ha` token, SSL check, household."""

from __future__ import annotations

from collections.abc import Mapping
import logging
from typing import Any

import voluptuous as vol

from homeassistant.config_entries import ConfigFlow, ConfigFlowResult
from homeassistant.const import CONF_TOKEN, CONF_URL, CONF_VERIFY_SSL
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.selector import (
    SelectOptionDict,
    SelectSelector,
    SelectSelectorConfig,
    TextSelector,
    TextSelectorConfig,
    TextSelectorType,
)

from .api import (
    StockpotApiError,
    StockpotAuthError,
    StockpotClient,
    StockpotConnectionError,
    StockpotError,
    StockpotSslError,
)
from .const import CONF_HOUSEHOLD_ID, DOMAIN

_LOGGER = logging.getLogger(__name__)

TOKEN_SELECTOR = TextSelector(TextSelectorConfig(type=TextSelectorType.PASSWORD))


def _user_schema(defaults: Mapping[str, Any]) -> vol.Schema:
    return vol.Schema(
        {
            vol.Required(CONF_URL, default=defaults.get(CONF_URL, "")): TextSelector(
                TextSelectorConfig(type=TextSelectorType.URL)
            ),
            vol.Required(CONF_TOKEN): TOKEN_SELECTOR,
            vol.Required(CONF_VERIFY_SSL, default=defaults.get(CONF_VERIFY_SSL, True)): bool,
        }
    )


def _unique_id(instance_id: str, household_id: int) -> str:
    """Stable across URL changes: Stockpot instance id + household."""
    return f"{instance_id}#{household_id}"


def _normalize_url(url: str) -> str:
    return url.strip().rstrip("/")


class StockpotConfigFlow(ConfigFlow, domain=DOMAIN):
    """Add a Stockpot household."""

    VERSION = 1

    def __init__(self) -> None:
        """Start a flow."""
        self._data: dict[str, Any] = {}
        self._households: list[dict[str, Any]] = []
        self._instance_id: str = ""

    async def _validate(
        self, data: Mapping[str, Any], household_id: int | None = None
    ) -> tuple[dict[str, str], list[dict[str, Any]]]:
        """Check URL, token and SSL (and read the instance id); with a household, check the
        `ha` ability on it. An `ha` token is bound to one household by Stockpot."""
        session = async_get_clientsession(self.hass, verify_ssl=data[CONF_VERIFY_SSL])
        client = StockpotClient(session, data[CONF_URL], data[CONF_TOKEN], household_id)
        households: list[dict[str, Any]] = []
        try:
            self._instance_id = await client.instance_id()
            if household_id is None:
                households = await client.households()
                if not households:
                    return {"base": "no_household"}, []
            else:
                await client.summary()
        except StockpotSslError:
            return {"base": "ssl_error"}, []
        except StockpotConnectionError:
            return {"base": "cannot_connect"}, []
        except StockpotAuthError:
            return {"base": "invalid_auth"}, []
        except StockpotApiError as err:
            if err.code == "missing_ability":
                return {"base": "missing_ability"}, []
            if err.code in ("two_factor_required", "household_forbidden"):
                return {"base": err.code}, []
            _LOGGER.exception("Unexpected Stockpot answer")
            return {"base": "unknown"}, []
        except StockpotError:
            _LOGGER.exception("Unexpected Stockpot error")
            return {"base": "unknown"}, []
        return {}, households

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """URL, token, SSL."""
        errors: dict[str, str] = {}
        if user_input is not None:
            data = {**user_input, CONF_URL: _normalize_url(user_input[CONF_URL])}
            errors, households = await self._validate(data)
            if not errors:
                self._data = data
                self._households = households
                if len(households) == 1:
                    return await self.async_step_household({CONF_HOUSEHOLD_ID: str(households[0]["id"])})
                return await self.async_step_household()
        return self.async_show_form(
            step_id="user",
            data_schema=self.add_suggested_values_to_schema(_user_schema(user_input or {}), user_input or {}),
            errors=errors,
        )

    async def async_step_household(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Choose the household (asked only when the user has several)."""
        errors: dict[str, str] = {}
        if user_input is not None:
            household_id = int(user_input[CONF_HOUSEHOLD_ID])
            household = next((h for h in self._households if h["id"] == household_id), None)
            if household is None:
                errors["base"] = "household_forbidden"
            else:
                await self.async_set_unique_id(_unique_id(self._instance_id, household_id))
                self._abort_if_unique_id_configured()
                errors, _ = await self._validate(self._data, household_id)
                if not errors:
                    return self.async_create_entry(
                        title=household["name"],
                        data={**self._data, CONF_HOUSEHOLD_ID: household_id},
                    )
        options = [SelectOptionDict(value=str(h["id"]), label=h["name"]) for h in self._households]
        return self.async_show_form(
            step_id="household",
            data_schema=vol.Schema(
                {vol.Required(CONF_HOUSEHOLD_ID): SelectSelector(SelectSelectorConfig(options=options))}
            ),
            errors=errors,
        )

    async def async_step_reauth(self, entry_data: Mapping[str, Any]) -> ConfigFlowResult:
        """The token was revoked, expired or lost the `ha` ability."""
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Ask for a new token."""
        entry = self._get_reauth_entry()
        errors: dict[str, str] = {}
        if user_input is not None:
            data = {**entry.data, CONF_TOKEN: user_input[CONF_TOKEN]}
            errors, _ = await self._validate(data, entry.data[CONF_HOUSEHOLD_ID])
            if not errors:
                await self.async_set_unique_id(_unique_id(self._instance_id, entry.data[CONF_HOUSEHOLD_ID]))
                self._abort_if_unique_id_mismatch(reason="wrong_instance")
                return self.async_update_reload_and_abort(entry, data_updates={CONF_TOKEN: user_input[CONF_TOKEN]})
        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=vol.Schema({vol.Required(CONF_TOKEN): TOKEN_SELECTOR}),
            description_placeholders={"url": entry.data[CONF_URL]},
            errors=errors,
        )

    async def async_step_reconfigure(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Change URL, token or SSL check of the same household."""
        entry = self._get_reconfigure_entry()
        errors: dict[str, str] = {}
        if user_input is not None:
            data = {
                **entry.data,
                **user_input,
                CONF_URL: _normalize_url(user_input[CONF_URL]),
            }
            household_id = entry.data[CONF_HOUSEHOLD_ID]
            errors, _ = await self._validate(data, household_id)
            if not errors:
                # Same instance (stable id), possibly behind a new URL.
                await self.async_set_unique_id(_unique_id(self._instance_id, household_id))
                self._abort_if_unique_id_mismatch(reason="wrong_instance")
                return self.async_update_reload_and_abort(
                    entry,
                    data_updates={
                        CONF_URL: data[CONF_URL],
                        CONF_TOKEN: data[CONF_TOKEN],
                        CONF_VERIFY_SSL: data[CONF_VERIFY_SSL],
                    },
                )
        return self.async_show_form(
            step_id="reconfigure",
            data_schema=self.add_suggested_values_to_schema(
                _user_schema(entry.data),
                {CONF_URL: entry.data[CONF_URL], CONF_VERIFY_SSL: entry.data[CONF_VERIFY_SSL]},
            ),
            errors=errors,
        )
