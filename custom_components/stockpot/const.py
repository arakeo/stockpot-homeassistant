"""Constants of the Stockpot integration."""

from __future__ import annotations

from datetime import timedelta
from typing import Final

DOMAIN: Final = "stockpot"

CONF_HOUSEHOLD_ID: Final = "household_id"
CONF_WEBHOOK_SECRET: Final = "webhook_secret"

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
