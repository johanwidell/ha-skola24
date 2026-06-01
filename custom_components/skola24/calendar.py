"""Skola24 Calendar entity."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from homeassistant.components.calendar import CalendarEntity, CalendarEvent
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from . import Skola24ConfigEntry
from .const import DOMAIN
from .coordinator import Skola24Coordinator


async def async_setup_entry(
    hass: HomeAssistant,
    entry: Skola24ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the Skola24 calendar entity from a config entry."""
    coordinator: Skola24Coordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities([Skola24CalendarEntity(coordinator, entry)], update_before_add=True)


class Skola24CalendarEntity(CoordinatorEntity[Skola24Coordinator], CalendarEntity):
    """A calendar entity that shows the Skola24 timetable."""

    _attr_has_entity_name = True
    _attr_name = "Schema"

    def __init__(
        self,
        coordinator: Skola24Coordinator,
        entry: Skola24ConfigEntry,
    ) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{entry.entry_id}_calendar"
        self._attr_device_info = {
            "identifiers": {(DOMAIN, entry.entry_id)},
            "name": entry.title,
            "manufacturer": "Skola24",
            "model": "Timetable",
        }

    @property
    def event(self) -> CalendarEvent | None:
        """Return the current or next upcoming event."""
        if not self.coordinator.data:
            return None
        now = datetime.now(tz=self.coordinator.data[0]["start"].tzinfo)
        for ev in self.coordinator.data:
            if ev["end"] >= now:
                return _to_calendar_event(ev)
        return None

    async def async_get_events(
        self,
        hass: HomeAssistant,
        start_date: datetime,
        end_date: datetime,
    ) -> list[CalendarEvent]:
        """Return events in the requested time window."""
        if not self.coordinator.data:
            return []
        return [
            _to_calendar_event(ev)
            for ev in self.coordinator.data
            if ev["start"] < end_date and ev["end"] > start_date
        ]

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Expose extra attributes for debugging / Lovelace cards."""
        if not self.coordinator.data:
            return {}
        return {
            "event_count": len(self.coordinator.data),
            "next_events": [
                {
                    "summary": ev["summary"],
                    "start": ev["start"].isoformat(),
                    "end": ev["end"].isoformat(),
                    "location": ev.get("location", ""),
                }
                for ev in self.coordinator.data[:5]
                if ev["end"] >= datetime.now(tz=ev["start"].tzinfo)
            ],
        }


def _to_calendar_event(ev: dict) -> CalendarEvent:
    return CalendarEvent(
        start=ev["start"],
        end=ev["end"],
        summary=ev["summary"],
        location=ev.get("location") or None,
        description=ev.get("description") or None,
    )
