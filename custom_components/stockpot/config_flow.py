"""Config flow of the Stockpot integration.

First step: Stockpot URL, SSL check and the sign-in method:
- « Se connecter avec Stockpot » (recommended): OAuth2 Authorization Code + PKCE through
  Home Assistant's `config_entry_oauth2_flow`, endpoints discovered on the instance (RFC 8414);
  the household is chosen on the Stockpot consent screen and returned with the tokens;
- « Jeton manuel »: an `ha` token pasted by the user, then the household (unchanged since 0.9).
"""

from __future__ import annotations

from collections.abc import Mapping
import logging
from typing import Any

import voluptuous as vol

from homeassistant.config_entries import SOURCE_REAUTH, SOURCE_RECONFIGURE, ConfigEntry, ConfigFlowResult
from homeassistant.const import CONF_TOKEN, CONF_URL, CONF_VERIFY_SSL
from homeassistant.data_entry_flow import AbortFlow
from homeassistant.helpers import config_entry_oauth2_flow
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.selector import (
    SelectOptionDict,
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
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
from .const import AUTH_METHOD_OAUTH, AUTH_METHOD_TOKEN, CONF_AUTH_METHOD, CONF_HOUSEHOLD_ID, DOMAIN
from .oauth import (
    CONF_AUTH_IMPLEMENTATION,
    CONF_OAUTH_TOKEN,
    StockpotInsecureEndpointError,
    StockpotNotOAuthError,
    StockpotOAuth2Implementation,
    StockpotOAuthUnsupportedError,
    StockpotPublicHttpError,
    async_discover,
    async_revoke,
    is_oauth_entry,
)

_LOGGER = logging.getLogger(__name__)

TOKEN_SELECTOR = TextSelector(TextSelectorConfig(type=TextSelectorType.PASSWORD))


URL_SELECTOR = TextSelector(TextSelectorConfig(type=TextSelectorType.URL))
AUTH_METHOD_SELECTOR = SelectSelector(
    SelectSelectorConfig(
        options=[AUTH_METHOD_OAUTH, AUTH_METHOD_TOKEN],
        mode=SelectSelectorMode.LIST,
        translation_key=CONF_AUTH_METHOD,
    )
)


def _start_schema(defaults: Mapping[str, Any]) -> vol.Schema:
    return vol.Schema(
        {
            vol.Required(CONF_URL, default=defaults.get(CONF_URL, "")): URL_SELECTOR,
            vol.Required(CONF_VERIFY_SSL, default=defaults.get(CONF_VERIFY_SSL, True)): bool,
            vol.Required(CONF_AUTH_METHOD, default=defaults.get(CONF_AUTH_METHOD, AUTH_METHOD_OAUTH)): (
                AUTH_METHOD_SELECTOR
            ),
        }
    )


def _oauth_server_schema(defaults: Mapping[str, Any]) -> vol.Schema:
    return vol.Schema(
        {
            vol.Required(CONF_URL, default=defaults.get(CONF_URL, "")): URL_SELECTOR,
            vol.Required(CONF_VERIFY_SSL, default=defaults.get(CONF_VERIFY_SSL, True)): bool,
        }
    )


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


class StockpotConfigFlow(config_entry_oauth2_flow.AbstractOAuth2FlowHandler, domain=DOMAIN):
    """Add a Stockpot household."""

    DOMAIN = DOMAIN
    VERSION = 1

    def __init__(self) -> None:
        """Start a flow."""
        super().__init__()
        self._data: dict[str, Any] = {}
        self._households: list[dict[str, Any]] = []
        self._instance_id: str = ""
        self._my_warned = False

    @property
    def logger(self) -> logging.Logger:
        """Logger of the OAuth2 helper."""
        return _LOGGER

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
        """URL, SSL check and sign-in method."""
        errors: dict[str, str] = {}
        if user_input is not None:
            url = _normalize_url(user_input[CONF_URL])
            verify_ssl = user_input[CONF_VERIFY_SSL]
            if user_input[CONF_AUTH_METHOD] == AUTH_METHOD_TOKEN:
                self._data = {CONF_URL: url, CONF_VERIFY_SSL: verify_ssl}
                return await self.async_step_token()
            errors = await self._async_start_oauth(url, verify_ssl)
            if not errors:
                return await self.async_step_auth()
        return self.async_show_form(
            step_id="user",
            data_schema=_start_schema(user_input or {}),
            errors=errors,
        )

    async def async_step_token(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Manual `ha` token (URL and SSL check already given)."""
        errors: dict[str, str] = {}
        if user_input is not None:
            data = {**self._data, CONF_TOKEN: user_input[CONF_TOKEN]}
            errors, households = await self._validate(data)
            if errors.get("base") in ("ssl_error", "cannot_connect"):
                # URL or SSL check to fix: back to the first step, with what was typed.
                return self.async_show_form(
                    step_id="user",
                    data_schema=_start_schema({**self._data, CONF_AUTH_METHOD: AUTH_METHOD_TOKEN}),
                    errors=errors,
                )
            if not errors:
                self._data = data
                self._households = households
                if len(households) == 1:
                    return await self.async_step_household({CONF_HOUSEHOLD_ID: str(households[0]["id"])})
                return await self.async_step_household()
        return self.async_show_form(
            step_id="token",
            data_schema=vol.Schema({vol.Required(CONF_TOKEN): TOKEN_SELECTOR}),
            description_placeholders={"url": self._data[CONF_URL]},
            errors=errors,
        )

    # --- OAuth -------------------------------------------------------------------

    async def _async_start_oauth(self, url: str, verify_ssl: bool) -> dict[str, str]:
        """Discover the endpoints and prepare the PKCE implementation of this instance."""
        if "my" not in self.hass.config.components and not self._my_warned:
            # Without My Home Assistant, Stockpot must allow https://<ha>/auth/external/callback
            # (STOCKPOT_OAUTH_HA_REDIRECT_HOSTS): warn once, a second submit goes on.
            self._my_warned = True
            return {"base": "my_required"}
        try:
            endpoints = await async_discover(self.hass, url, verify_ssl)
        except StockpotSslError:
            return {"base": "ssl_error"}
        except StockpotConnectionError:
            return {"base": "cannot_connect"}
        except StockpotOAuthUnsupportedError:
            return {"base": "oauth_unsupported"}
        except StockpotNotOAuthError:
            return {"base": "not_stockpot"}
        except StockpotInsecureEndpointError:
            return {"base": "insecure_endpoint"}
        except StockpotPublicHttpError:
            return {"base": "http_public_host"}
        self._data = {CONF_URL: url, CONF_VERIFY_SSL: verify_ssl, **endpoints.as_data()}
        # A fresh implementation per flow: its own PKCE verifier.
        self.flow_impl = StockpotOAuth2Implementation(self.hass, url, verify_ssl, endpoints)
        return {}

    async def async_oauth_create_entry(self, data: dict) -> ConfigFlowResult:
        """Tokens received: unique id from the `instance_id` / `household_id` extensions."""
        token: dict[str, Any] = data[CONF_OAUTH_TOKEN]
        session = async_get_clientsession(self.hass, verify_ssl=self._data[CONF_VERIFY_SSL])
        client = StockpotClient(session, self._data[CONF_URL], str(token["access_token"]))
        reason: str | None = None
        try:
            instance_id = token.get("instance_id") or await client.instance_id()
            households = await client.households()
        except StockpotConnectionError:
            reason = "cannot_connect"
        except StockpotApiError as err:
            reason = "two_factor_required" if err.code == "two_factor_required" else "unknown"
            if reason == "unknown":
                _LOGGER.error("Unexpected Stockpot answer after sign-in: %s %s", err.status, err.code)
        except StockpotError as err:
            _LOGGER.error("Unexpected Stockpot error after sign-in: %s", err.__class__.__name__)
            reason = "unknown"
        else:
            household_id = token.get("household_id")
            if household_id is None and len(households) == 1:
                household_id = households[0]["id"]
            household = next((h for h in households if h["id"] == household_id), None)
            if household is None:
                reason = "unknown"
        if reason is not None:
            await self._async_discard(token)
            return self.async_abort(reason=reason)

        entry_data = {
            **self._data,
            CONF_AUTH_METHOD: AUTH_METHOD_OAUTH,
            CONF_HOUSEHOLD_ID: int(household_id),
            CONF_AUTH_IMPLEMENTATION: data[CONF_AUTH_IMPLEMENTATION],
            CONF_OAUTH_TOKEN: token,
        }
        unique_id = _unique_id(str(instance_id), int(household_id))
        update = self.source in (SOURCE_REAUTH, SOURCE_RECONFIGURE)
        try:
            # Any abort here (configured, ignored, in progress, other instance or household)
            # leaves the access just created unused: revoke it.
            await self.async_set_unique_id(unique_id, raise_on_progress=not update)
            if update:
                self._abort_if_unique_id_mismatch(reason="wrong_instance")
            else:
                self._abort_if_unique_id_configured()
        except AbortFlow:
            await self._async_discard(token)
            raise
        if update:
            entry = self._get_reauth_entry() if self.source == SOURCE_REAUTH else self._get_reconfigure_entry()
            previous = dict(entry.data)
            result = self.async_update_reload_and_abort(entry, data_updates=entry_data)
            await self._async_revoke_previous(entry, previous, token)
            return result
        return self.async_create_entry(title=household["name"], data=entry_data)

    async def _async_discard(self, token: Mapping[str, Any]) -> None:
        """Tokens that will not be used: each code exchange creates its own Stockpot access,
        so revoking its refresh token ends only that access (best effort)."""
        await async_revoke(self.hass, self._data, token.get("refresh_token"))

    async def _async_revoke_previous(
        self, entry: ConfigEntry, previous: Mapping[str, Any], token: Mapping[str, Any]
    ) -> None:
        """Reauth / reconfigure: the entry now holds a new access; revoke the previous one
        (its refresh token, at the previous instance's revocation endpoint) so it does not
        linger in Stockpot > Settings > Home Assistant. Best effort.

        Never revoke what the entry now uses: skipped if Stockpot answered with the same
        refresh token, or if it is the one just stored."""
        if not is_oauth_entry(previous):
            return
        old_refresh = (previous.get(CONF_OAUTH_TOKEN) or {}).get("refresh_token")
        stored = (entry.data.get(CONF_OAUTH_TOKEN) or {}).get("refresh_token")
        if not old_refresh or old_refresh in (token.get("refresh_token"), stored):
            _LOGGER.debug("Previous Stockpot access not revoked: same refresh token as the new one")
            return
        await async_revoke(self.hass, previous, old_refresh)

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
        """The token was revoked, expired or lost the `ha` ability (OAuth: refresh refused)."""
        if is_oauth_entry(entry_data):
            return await self.async_step_reauth_oauth()
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_oauth(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Sign in with Stockpot again (same instance and household)."""
        entry = self._get_reauth_entry()
        errors: dict[str, str] = {}
        if user_input is not None:
            errors = await self._async_start_oauth(entry.data[CONF_URL], entry.data.get(CONF_VERIFY_SSL, True))
            if not errors:
                return await self.async_step_auth()
        return self.async_show_form(
            step_id="reauth_oauth",
            description_placeholders={"url": entry.data[CONF_URL]},
            errors=errors,
        )

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
        if is_oauth_entry(entry.data):
            return await self.async_step_reconfigure_oauth()
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

    async def async_step_reconfigure_oauth(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """OAuth entry: change URL or SSL check, then sign in again (same instance)."""
        entry = self._get_reconfigure_entry()
        errors: dict[str, str] = {}
        if user_input is not None:
            errors = await self._async_start_oauth(_normalize_url(user_input[CONF_URL]), user_input[CONF_VERIFY_SSL])
            if not errors:
                return await self.async_step_auth()
        return self.async_show_form(
            step_id="reconfigure_oauth",
            data_schema=_oauth_server_schema(user_input or entry.data),
            errors=errors,
        )
