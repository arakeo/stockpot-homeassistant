"""Calendars: « Repas » (every meal) and one per meal type of the household.

Stockpot meal types have no time: events use the meal times sent by Stockpot with the
summary (`meal_types[].start_time` / `end_time`, e.g. dinner 19:30–21:00).
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta, tzinfo
from typing import Any

from homeassistant.components.calendar import CalendarEntity, CalendarEvent
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.translation import async_get_translations
from homeassistant.util import dt as dt_util

from .api import StockpotError
from .const import DOMAIN
from .coordinator import StockpotConfigEntry, StockpotCoordinator
from .entity import StockpotEntity

PARALLEL_UPDATES = 0
MAX_DAYS = 31


async def async_setup_entry(
    hass: HomeAssistant,
    entry: StockpotConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """« Repas » + one calendar per active meal type (types activated later are added,
    deactivated ones removed)."""
    coordinator = entry.runtime_data.coordinator
    translations = await async_get_translations(hass, hass.config.language, "selector", {DOMAIN})
    prefix = f"component.{DOMAIN}.selector.calendar_description.options"
    words = {
        "servings": translations.get(f"{prefix}.servings", "{servings}"),
        "decimal_separator": translations.get(f"{prefix}.decimal_separator", "."),
    }
    registry = er.async_get(hass)
    known: set[str] = set()

    @callback
    def sync_meal_types() -> None:
        active = {t["code"] for t in coordinator.data.summary["meal_types"] if t["active"]}
        new: list[StockpotCalendar] = []
        if "all" not in known:
            known.add("all")
            new.append(StockpotCalendar(coordinator, None, words))
        for code in sorted(active - known):
            known.add(code)
            new.append(StockpotCalendar(coordinator, code, words))
        if new:
            async_add_entities(new)
        for code in known - active - {"all"}:
            known.discard(code)
            entity_id = registry.async_get_entity_id("calendar", DOMAIN, f"{entry.entry_id}_meals_{code}")
            if entity_id:
                registry.async_remove(entity_id)

    sync_meal_types()
    entry.async_on_unload(coordinator.async_add_listener(sync_meal_types))


def _at(day: date, hhmm: str, zone: tzinfo) -> datetime:
    hours, minutes = (int(x) for x in hhmm.split(":"))
    return datetime.combine(day, time(hours, minutes), tzinfo=zone)


def _description(servings: str | float, members: list[dict[str, Any]], words: dict[str, str]) -> str:
    count = f"{float(servings):g}".replace(".", words["decimal_separator"])
    text = words["servings"].replace("{servings}", count)
    names = [m["first_name"] for m in members]
    return f"{text} · {', '.join(names)}" if names else text


class StockpotCalendar(StockpotEntity, CalendarEntity):
    """Planned meals (all, or of one meal type)."""

    def __init__(self, coordinator: StockpotCoordinator, meal_code: str | None, words: dict[str, str]) -> None:
        """Calendar of `meal_code` (None = every meal)."""
        super().__init__(coordinator, f"meals_{meal_code or 'all'}")
        self._meal_code = meal_code
        self._words = words
        meal_type = coordinator.data.meal_type(meal_code) if meal_code else None
        if meal_type and meal_type.get("name"):
            self._attr_name = meal_type["name"]
        else:
            self._attr_translation_key = f"meals_{meal_code}" if meal_code else "meals"

    def _wanted(self, code: str) -> bool:
        return self._meal_code is None or code == self._meal_code

    @property
    def event(self) -> CalendarEvent | None:
        """Current or next meal (planning of the next days, from the summary)."""
        now = dt_util.now()
        for meal in self.coordinator.data.summary.get("meals", []):
            if not self._wanted(meal["meal_type"]["code"]):
                continue
            end = dt_util.parse_datetime(meal["ends_at"])
            if end is None or end <= now:
                continue
            return CalendarEvent(
                start=dt_util.parse_datetime(meal["starts_at"]) or end,
                end=end,
                summary=meal["title"],
                description=_description(meal["servings"], meal.get("members", []), self._words),
                uid=str(meal["id"]),
            )
        return None

    async def async_get_events(
        self, hass: HomeAssistant, start_date: datetime, end_date: datetime
    ) -> list[CalendarEvent]:
        """Meals between two dates (GET /meal-plan, 31 days per call)."""
        types = {t["id"]: t for t in self.coordinator.data.summary["meal_types"]}
        # Meal times are in Stockpot's time zone (HA may run in another one).
        zone = (
            dt_util.get_time_zone(self.coordinator.data.summary.get("timezone") or "")
            or dt_util.get_default_time_zone()
        )
        events: list[CalendarEvent] = []
        day = start_date.astimezone(zone).date()
        last = end_date.astimezone(zone).date()
        while day <= last:
            days = min(MAX_DAYS, (last - day).days + 1)
            try:
                week = await self.coordinator.client.meal_plan(day.isoformat(), days)
            except StockpotError as err:
                raise HomeAssistantError(
                    translation_domain=DOMAIN,
                    translation_key="api_error",
                    translation_placeholders={"error": str(err)},
                ) from err
            for entry in week["data"]:
                meal_type = types.get(entry["meal_type_id"])
                if meal_type is None or not self._wanted(meal_type["code"]):
                    continue
                entry_day = date.fromisoformat(entry["date"])
                start = _at(entry_day, meal_type["start_time"], zone)
                end = _at(entry_day, meal_type["end_time"], zone)
                if end <= start_date or start >= end_date:
                    continue
                recipe = entry.get("recipe")
                events.append(
                    CalendarEvent(
                        start=start,
                        end=end,
                        summary=recipe["title"] if recipe else (entry.get("note") or ""),
                        description=_description(entry["servings"], entry.get("members", []), self._words),
                        uid=str(entry["id"]),
                    )
                )
            day += timedelta(days=days)
        return sorted(events, key=lambda e: e.start_datetime_local)
