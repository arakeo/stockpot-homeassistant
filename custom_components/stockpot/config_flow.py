"""Config flow of the Stockpot integration.

Since 0.11.0, « Ajouter l'intégration → Stockpot » goes straight to the OAuth2 sign-in
(Authorization Code + PKCE through Home Assistant's `config_entry_oauth2_flow`) against
`STOCKPOT_URL`, with the SSL check always on: nothing to type. The household is chosen on the
Stockpot consent screen and returned with the tokens.

Entries created before 0.11.0 keep their own URL and SSL setting. A manual-token entry (0.9 /
0.10) is offered, on reauthentication and reconfigure, a menu: the sign-in against its own URL
(the entry becomes an OAuth entry on success) or a new token (reconfigure: also URL and SSL
check, as in 0.10).
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
from homeassistant.helpers.selector import TextSelector, TextSelectorConfig, TextSelectorType

from .api import (
    StockpotApiError,
    StockpotAuthError,
    StockpotClient,
    StockpotConnectionError,
    StockpotError,
    StockpotSslError,
)
from .const import AUTH_METHOD_OAUTH, CONF_AUTH_METHOD, CONF_HOUSEHOLD_ID, DOMAIN, STOCKPOT_URL
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

# Errors after which a manual-token entry falls back to the token form on reauthentication:
# its Stockpot does not offer the Home Assistant sign-in.
NO_SIGN_IN_ERRORS = ("oauth_unsupported", "not_stockpot")

# A manual entry's API token (CONF_TOKEN) and an OAuth entry's token dict (CONF_OAUTH_TOKEN)
# share the entry key `token`: converting a manual entry to OAuth replaces the former.


def _unique_id(instance_id: str, household_id: int) -> str:
    """Stable across URL changes: Stockpot instance id + household."""
    return f"{instance_id}#{household_id}"


class StockpotConfigFlow(config_entry_oauth2_flow.AbstractOAuth2FlowHandler, domain=DOMAIN):
    """Add a Stockpot household."""

    DOMAIN = DOMAIN
    VERSION = 1

    def __init__(self) -> None:
        """Start a flow."""
        super().__init__()
        self._data: dict[str, Any] = {}
        self._verify_ssl = True
        self._instance_id: str = ""
        self._my_warned = False

    @property
    def logger(self) -> logging.Logger:
        """Logger of the OAuth2 helper."""
        return _LOGGER

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Straight to the sign-in on Stockpot. A form (no field) is shown only when the sign-in
        cannot start: it tells why, and submitting it tries again."""
        errors = await self._async_start_oauth(STOCKPOT_URL, True)
        if not errors:
            return await self.async_step_auth()
        return self.async_show_form(
            step_id="user",
            description_placeholders={"url": STOCKPOT_URL},
            errors=errors,
        )

    # --- OAuth -------------------------------------------------------------------

    async def _async_start_oauth(self, url: str, verify_ssl: bool, *, discover_first: bool = False) -> dict[str, str]:
        """Discover the endpoints and prepare the PKCE implementation of this instance.

        `discover_first` (manual-token entries): the My Home Assistant warning only once the
        instance proved it offers the sign-in, so a pre-M17 Stockpot is not shown it."""
        if not discover_first and (warning := self._my_warning()):
            return warning
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
            # Only reachable from a pre-0.11 entry with a plain-http LAN URL (reauth, reconfigure).
            return {"base": "http_public_host"}
        if discover_first and (warning := self._my_warning()):
            return warning
        self._verify_ssl = verify_ssl
        self._data = {CONF_URL: url, **endpoints.as_data()}
        if not verify_ssl:
            # Kept only for a pre-0.11 entry that disabled the check; new entries omit the key.
            self._data[CONF_VERIFY_SSL] = False
        # A fresh implementation per flow: its own PKCE verifier.
        self.flow_impl = StockpotOAuth2Implementation(self.hass, url, verify_ssl, endpoints)
        return {}

    def _my_warning(self) -> dict[str, str]:
        """Without My Home Assistant, Stockpot must allow https://<ha>/auth/external/callback
        (STOCKPOT_OAUTH_HA_REDIRECT_HOSTS): warn once, a second submit goes on."""
        if "my" in self.hass.config.components or self._my_warned:
            return {}
        self._my_warned = True
        return {"base": "my_required"}

    async def async_oauth_create_entry(self, data: dict) -> ConfigFlowResult:
        """Tokens received: unique id from the `instance_id` / `household_id` extensions."""
        token: dict[str, Any] = data[CONF_OAUTH_TOKEN]
        session = async_get_clientsession(self.hass, verify_ssl=self._verify_ssl)
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
            # Any abort here (already configured, flow in progress, other instance or
            # household) leaves the access just created unused: revoke it.
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
            # Webhook: Stockpot keeps a single one per household (last registration wins); the
            # reload registers it again with this access and a new secret.
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

    # --- reauthentication ----------------------------------------------------------

    async def async_step_reauth(self, entry_data: Mapping[str, Any]) -> ConfigFlowResult:
        """Access lost. OAuth entry: sign in again. Manual-token entry (pre-0.11): choose between
        the sign-in (converts the entry) and a new token (if this Home Assistant cannot finish
        the sign-in: no My Home Assistant, host not allowed by Stockpot)."""
        if is_oauth_entry(entry_data):
            return await self.async_step_reauth_oauth()
        return await self.async_step_reauth_manual()

    async def async_step_reauth_manual(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Manual-token entry: sign in with Stockpot or paste a new token."""
        return self.async_show_menu(
            step_id="reauth_manual",
            menu_options=["reauth_oauth", "reauth_confirm"],
            description_placeholders={"url": self._get_reauth_entry().data[CONF_URL]},
        )

    async def async_step_reauth_oauth(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Sign in with Stockpot again, at the entry's own URL (same instance and household).

        A manual-token entry becomes an OAuth entry on success (its token is not revoked); if
        its Stockpot has no Home Assistant sign-in, the token form is shown instead."""
        return await self._async_sign_in_again(self._get_reauth_entry(), "reauth_oauth", user_input)

    async def async_step_reauth_confirm(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Manual-token entry: a new token."""
        entry = self._get_reauth_entry()
        errors: dict[str, str] = {}
        if user_input is not None:
            data = {**entry.data, CONF_TOKEN: user_input[CONF_TOKEN]}
            errors = await self._validate(data, entry.data[CONF_HOUSEHOLD_ID])
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

    async def _async_sign_in_again(
        self, entry: ConfigEntry, step_id: str, user_input: dict[str, Any] | None
    ) -> ConfigFlowResult:
        """OAuth at the entry's URL with its SSL setting. An OAuth entry confirms first; a manual
        entry got here from the menu and starts at once. Errors: a form that retries."""
        manual = not is_oauth_entry(entry.data)
        errors: dict[str, str] = {}
        if user_input is not None or manual:
            errors = await self._async_start_oauth(
                entry.data[CONF_URL], entry.data.get(CONF_VERIFY_SSL, True), discover_first=manual
            )
            if not errors:
                return await self.async_step_auth()
            if manual and self.source == SOURCE_REAUTH and errors["base"] in NO_SIGN_IN_ERRORS:
                return await self.async_step_reauth_confirm()
        return self.async_show_form(
            step_id=step_id,
            description_placeholders={"url": entry.data[CONF_URL]},
            errors=errors,
        )

    async def _validate(self, data: Mapping[str, Any], household_id: int) -> dict[str, str]:
        """Manual token: check it (and read the instance id) and its `ha` ability on the household."""
        session = async_get_clientsession(self.hass, verify_ssl=data.get(CONF_VERIFY_SSL, True))
        client = StockpotClient(session, data[CONF_URL], data[CONF_TOKEN], household_id)
        try:
            self._instance_id = await client.instance_id()
            await client.summary()
        except StockpotSslError:
            return {"base": "ssl_error"}
        except StockpotConnectionError:
            return {"base": "cannot_connect"}
        except StockpotAuthError:
            return {"base": "invalid_auth"}
        except StockpotApiError as err:
            if err.code == "missing_ability":
                return {"base": "missing_ability"}
            if err.code in ("two_factor_required", "household_forbidden"):
                return {"base": err.code}
            _LOGGER.exception("Unexpected Stockpot answer")
            return {"base": "unknown"}
        except StockpotError:
            _LOGGER.exception("Unexpected Stockpot error")
            return {"base": "unknown"}
        return {}

    # --- reconfigure -----------------------------------------------------------------

    async def async_step_reconfigure(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """OAuth entry: sign in again at its URL (not editable). Manual-token entry (pre-0.11):
        the sign-in (converts the entry) or, as in 0.10, URL / token / SSL check."""
        entry = self._get_reconfigure_entry()
        if is_oauth_entry(entry.data):
            return await self.async_step_reconfigure_oauth()
        return await self.async_step_reconfigure_manual()

    async def async_step_reconfigure_manual(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Manual-token entry: sign in with Stockpot, or change URL / token / SSL check."""
        return self.async_show_menu(
            step_id="reconfigure_manual",
            menu_options=["reconfigure_oauth", "reconfigure_token"],
            description_placeholders={"url": self._get_reconfigure_entry().data[CONF_URL]},
        )

    async def async_step_reconfigure_oauth(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Sign in with Stockpot again at the entry's URL (same instance and household)."""
        return await self._async_sign_in_again(self._get_reconfigure_entry(), "reconfigure_oauth", user_input)

    async def async_step_reconfigure_token(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Manual-token entry: change URL, token or SSL check of the same household (as in 0.10)."""
        entry = self._get_reconfigure_entry()
        errors: dict[str, str] = {}
        if user_input is not None:
            data = {**entry.data, **user_input, CONF_URL: user_input[CONF_URL].strip().rstrip("/")}
            household_id = entry.data[CONF_HOUSEHOLD_ID]
            errors = await self._validate(data, household_id)
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
            step_id="reconfigure_token",
            data_schema=self.add_suggested_values_to_schema(
                vol.Schema(
                    {
                        vol.Required(CONF_URL): URL_SELECTOR,
                        vol.Required(CONF_TOKEN): TOKEN_SELECTOR,
                        vol.Required(CONF_VERIFY_SSL, default=True): bool,
                    }
                ),
                {CONF_URL: entry.data[CONF_URL], CONF_VERIFY_SSL: entry.data.get(CONF_VERIFY_SSL, True)},
            ),
            errors=errors,
        )
