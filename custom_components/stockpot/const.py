"""Constants of the Stockpot integration."""

from __future__ import annotations

from datetime import timedelta
from typing import Final

DOMAIN: Final = "stockpot"

CONF_HOUSEHOLD_ID: Final = "household_id"
CONF_WEBHOOK_SECRET: Final = "webhook_secret"
CONF_AUTH_METHOD: Final = "auth_method"
CONF_AUTHORIZE_URL: Final = "authorize_url"
CONF_TOKEN_URL: Final = "token_url"
CONF_REVOCATION_URL: Final = "revocation_url"

AUTH_METHOD_OAUTH: Final = "oauth"

# The Stockpot instance new entries connect to (0.11.0: nothing to type, SSL always verified).
# Entries created before 0.11.0 keep their own URL and SSL setting.
STOCKPOT_URL: Final = "https://stockpot.arakeo.com"

# OAuth2 sign-in (Stockpot M17): public client, PKCE S256, single scope.
OAUTH_CLIENT_ID: Final = "home-assistant"
OAUTH_SCOPE: Final = "ha"
OAUTH_DISCOVERY_PATH: Final = "/.well-known/oauth-authorization-server"

UPDATE_INTERVAL: Final = timedelta(seconds=60)
REQUEST_TIMEOUT: Final = 15

# Webhook signature (Stockpot ADR 0010): sha256=HMAC(secret, "<timestamp>.<body>").
SIGNATURE_HEADER: Final = "X-Stockpot-Signature"
TIMESTAMP_HEADER: Final = "X-Stockpot-Timestamp"
SIGNATURE_TOLERANCE: Final = 300

MEAL_TYPES: Final = ("breakfast", "lunch", "snack", "dinner")

ATTR_CONFIG_ENTRY_ID: Final = "config_entry_id"

SERVICE_CONSUME_PRODUCT: Final = "consume_product"
SERVICE_ADD_STOCK: Final = "add_stock"
SERVICE_ADD_TO_SHOPPING_LIST: Final = "add_to_shopping_list"
SERVICE_PLAN_MEAL: Final = "plan_meal"
SERVICE_COOK_MEAL: Final = "cook_meal"
SERVICE_GENERATE_SHOPPING_LIST: Final = "generate_shopping_list"
