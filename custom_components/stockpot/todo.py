"""To-do lists: one per Stockpot shopping list.

Lines to buy (open, in the cart, ordered) are « needs action »; checking one calls
« Reçu » in Stockpot (status `received`, a product line enters the stock); lines received
today are shown as completed. Add (free text), rename (free text only), note
(description), delete and move are supported. A line « Pas cette fois » is hidden.
"""

from __future__ import annotations

from typing import Any

from homeassistant.components.todo import (
    TodoItem,
    TodoItemStatus,
    TodoListEntity,
    TodoListEntityFeature,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.translation import async_get_translations
from homeassistant.util import dt as dt_util

from .api import StockpotError
from .const import DOMAIN
from .coordinator import StockpotConfigEntry, StockpotCoordinator
from .entity import StockpotEntity

PARALLEL_UPDATES = 1

ACTIVE = ("open", "in_cart", "ordered")


async def async_setup_entry(
    hass: HomeAssistant,
    entry: StockpotConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """One to-do entity per shopping list: lists created later are added, deleted ones removed."""
    coordinator = entry.runtime_data.coordinator
    translations = await async_get_translations(hass, hass.config.language, "selector", {DOMAIN})
    words = {
        key: translations.get(f"component.{DOMAIN}.selector.todo_description.options.{key}", key)
        for key in ("for", "low_stock")
    }
    known: set[int] = set()
    registry = er.async_get(hass)

    @callback
    def sync_lists() -> None:
        current = set(coordinator.data.lists)
        new = [i for i in coordinator.data.lists if i not in known]
        known.update(new)
        if new:
            async_add_entities(StockpotTodoList(coordinator, list_id, words) for list_id in new)
        for gone in known - current:
            known.discard(gone)
            entity_id = registry.async_get_entity_id("todo", DOMAIN, f"{entry.entry_id}_shopping_list_{gone}")
            if entity_id:
                registry.async_remove(entity_id)

    sync_lists()
    entry.async_on_unload(coordinator.async_add_listener(sync_lists))


def _auto_description(item: dict[str, Any], words: dict[str, str]) -> str | None:
    """Quantity and reasons, as Stockpot shows them (read-only part of the description)."""
    parts: list[str] = []
    display = item.get("purchase_display") or item.get("needed_display")
    if item.get("pack_count") and item.get("pack_display"):
        pack = item["pack_display"]
        parts.append(f"{item['pack_count']} × {pack['quantity']} {pack.get('unit') or ''}".strip())
    elif display and display.get("quantity"):
        parts.append(f"{display['quantity']} {display.get('unit') or ''}".strip())
    labels = [r["label"] for r in item.get("reasons", []) if r.get("type") == "recipe" and r.get("label")]
    if labels:
        parts.append(f"{words['for']} {', '.join(dict.fromkeys(labels))}")
    if any(r.get("type") == "low_stock" for r in item.get("reasons", [])):
        parts.append(words["low_stock"])
    return " · ".join(parts) or None


def _description(item: dict[str, Any], words: dict[str, str]) -> str | None:
    note = item.get("note")
    return note if note else _auto_description(item, words)


class StockpotTodoList(StockpotEntity, TodoListEntity):
    """A Stockpot shopping list."""

    _attr_supported_features = (
        TodoListEntityFeature.CREATE_TODO_ITEM
        | TodoListEntityFeature.UPDATE_TODO_ITEM
        | TodoListEntityFeature.DELETE_TODO_ITEM
        | TodoListEntityFeature.MOVE_TODO_ITEM
        | TodoListEntityFeature.SET_DESCRIPTION_ON_ITEM
    )

    def __init__(self, coordinator: StockpotCoordinator, list_id: int, words: dict[str, str]) -> None:
        """Create the to-do list of `list_id`."""
        super().__init__(coordinator, f"shopping_list_{list_id}")
        self._list_id = list_id
        self._words = words
        if self._list.get("is_main"):
            self._attr_translation_key = "main_shopping_list"
        else:
            self._attr_name = self._list["name"]

    @callback
    def _handle_coordinator_update(self) -> None:
        """A secondary list renamed in Stockpot is renamed here too."""
        if not self._list.get("is_main") and self._list.get("name"):
            self._attr_name = self._list["name"]
        super()._handle_coordinator_update()

    @property
    def _list(self) -> dict[str, Any]:
        return self.coordinator.data.lists.get(self._list_id, {})

    @property
    def available(self) -> bool:
        """Unavailable once the list is deleted in Stockpot."""
        return super().available and self._list_id in self.coordinator.data.lists

    def _items(self) -> list[dict[str, Any]]:
        return [i for i in self._list.get("items", []) if i["status"] in (*ACTIVE, "received")]

    def _item(self, uid: str) -> dict[str, Any]:
        for item in self._list.get("items", []):
            if str(item["id"]) == uid:
                return item
        raise ServiceValidationError(
            translation_domain=DOMAIN, translation_key="item_not_found", translation_placeholders={"uid": uid}
        )

    @property
    def todo_items(self) -> list[TodoItem]:
        """Lines in Stockpot's order (aisle, manual position, name)."""
        items = []
        for item in self._items():
            received = item["status"] == "received"
            completed = dt_util.parse_datetime(item["received_at"]) if received and item.get("received_at") else None
            items.append(
                TodoItem(
                    uid=str(item["id"]),
                    summary=item["name"],
                    status=TodoItemStatus.COMPLETED if received else TodoItemStatus.NEEDS_ACTION,
                    description=_description(item, self._words),
                    completed=completed,
                )
            )
        return items

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """`stale`: Stockpot has changes not rebuilt yet in this (main) list."""
        return {"list_id": self._list_id, "stale": bool(self._list.get("stale"))}

    async def _call(self, coro) -> Any:
        try:
            return await coro
        except StockpotError as err:
            raise HomeAssistantError(
                translation_domain=DOMAIN, translation_key="api_error", translation_placeholders={"error": str(err)}
            ) from err

    async def async_create_todo_item(self, item: TodoItem) -> None:
        """Add a free text line (« Ajouter » in Stockpot, merged with an open one)."""
        payload: dict[str, Any] = {"label": (item.summary or "").strip()}
        if item.description:
            payload["note"] = item.description
        await self._call(self.coordinator.client.add_item(self._list_id, payload))
        await self.coordinator.async_request_refresh()

    async def async_update_todo_item(self, item: TodoItem) -> None:
        """Check (« Reçu »), rename a free text line, change the note."""
        current = self._item(str(item.uid))
        client = self.coordinator.client
        received = current["status"] == "received"

        if item.status == TodoItemStatus.NEEDS_ACTION and received:
            raise ServiceValidationError(translation_domain=DOMAIN, translation_key="already_received")

        changes: dict[str, Any] = {}
        if item.summary is not None and item.summary.strip() != current["name"]:
            if current.get("product"):
                raise ServiceValidationError(translation_domain=DOMAIN, translation_key="product_rename")
            changes["label"] = item.summary.strip()
        if item.description is not None and item.description != _description(current, self._words):
            changes["note"] = item.description or None
        if changes and not received:
            await self._call(client.update_item(current["id"], changes))

        if item.status == TodoItemStatus.COMPLETED and not received:
            product = current.get("product")
            add_to_stock = bool(product and product.get("stock_tracked") and not product.get("archived"))
            await self._call(client.receive_item(current["id"], add_to_stock))
        await self.coordinator.async_request_refresh()

    async def async_delete_todo_items(self, uids: list[str]) -> None:
        """Remove lines (an automatic line becomes « Pas cette fois » in Stockpot)."""
        for uid in uids:
            await self._call(self.coordinator.client.delete_item(int(self._item(uid)["id"])))
        await self.coordinator.async_request_refresh()

    async def async_move_todo_item(self, uid: str, previous_uid: str | None = None) -> None:
        """Move a line after another one (it joins that line's aisle)."""
        item = self._item(uid)
        after = int(self._item(previous_uid)["id"]) if previous_uid else None
        await self._call(self.coordinator.client.move_item(int(item["id"]), after))
        await self.coordinator.async_request_refresh()
