"""DataUpdateCoordinator for Skola24."""

from __future__ import annotations

import logging
from datetime import timedelta

import aiohttp
from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .api import Skola24Api, Skola24AuthError, Skola24ApiError, lesson_to_datetimes
from .const import (
    CONF_HOST,
    CONF_PASSWORD,
    CONF_SELECTION_TYPE,
    CONF_SELECTION_VALUE,
    CONF_UNIT_GUID,
    CONF_USERNAME,
    UPDATE_INTERVAL_HOURS,
    WEEKS_FUTURE,
    WEEKS_PAST,
    DOMAIN,
)

_LOGGER = logging.getLogger(__name__)


class Skola24Coordinator(DataUpdateCoordinator):
    """
    Fetches timetable data from Skola24 and makes it available to entities.

    `data` after a successful update is a list of CalendarEvent-ready dicts:
        {
            "summary": str,
            "location": str,
            "start": datetime (aware, Europe/Stockholm),
            "end": datetime (aware, Europe/Stockholm),
        }
    """

    def __init__(
        self,
        hass: HomeAssistant,
        session: aiohttp.ClientSession,
        config: dict,
    ) -> None:
        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            update_interval=timedelta(hours=UPDATE_INTERVAL_HOURS),
        )
        self._api = Skola24Api(session, config[CONF_HOST])
        self._username = config[CONF_USERNAME]
        self._password = config[CONF_PASSWORD]
        self._selection_type = config[CONF_SELECTION_TYPE]
        self._selection_value = config[CONF_SELECTION_VALUE]
        self._unit_guid: str | None = config.get(CONF_UNIT_GUID)
        self._session_ok = False

    # ------------------------------------------------------------------

    async def _ensure_authenticated(self) -> None:
        """
        Re-login if the session is expired or about to expire.

        From DevTools: /api/get/user/info confirms session lives ~1 hour.
        We update every 6 h, so we must re-login proactively.
        The margin is 10 minutes — checked before every timetable fetch.
        """
        need_login = (
            not self._session_ok
            or self._api.session_about_to_expire(margin_minutes=10)
        )
        if need_login:
            expires_at = self._api.session_expires_at
            if expires_at:
                _LOGGER.debug(
                    "Session expires at %s — re-authenticating", expires_at.isoformat()
                )
            else:
                _LOGGER.debug("No valid session — authenticating")
            await self._api.login(self._username, self._password)
            self._session_ok = True
            _LOGGER.debug(
                "Re-authentication successful; new session expires at %s",
                self._api.session_expires_at,
            )

    async def _async_update_data(self) -> list[dict]:
        """Fetch fresh timetable data."""
        try:
            await self._ensure_authenticated()

            # Resolve unit GUID once (cached after first run)
            if not self._unit_guid:
                self._unit_guid = await self._api.get_unit_guid()

            school_year = await self._api.get_school_year()

            raw_lessons = await self._api.fetch_lessons(
                selection_type=self._selection_type,
                selection_raw=self._selection_value,
                unit_guid=self._unit_guid,
                school_year=school_year,
                weeks_past=WEEKS_PAST,
                weeks_future=WEEKS_FUTURE,
            )

            return _parse_lessons(raw_lessons)

        except Skola24AuthError as exc:
            # Force re-login next time
            self._session_ok = False
            raise UpdateFailed(f"Authentication error: {exc}") from exc

        except Skola24ApiError as exc:
            raise UpdateFailed(f"Skola24 API error: {exc}") from exc


def _parse_lessons(raw: list[dict]) -> list[dict]:
    """Convert raw API lesson dicts to clean calendar-event dicts."""
    events: list[dict] = []
    seen: set[str] = set()  # deduplicate

    for lesson in raw:
        start, end = lesson_to_datetimes(lesson)
        if start is None or end is None:
            continue

        texts = lesson.get("texts") or []
        summary = texts[0] if len(texts) > 0 else "Lektion"
        teacher = texts[1] if len(texts) > 1 else ""
        location = texts[2] if len(texts) > 2 else ""

        # Build a deduplication key
        dedup_key = f"{start.isoformat()}|{end.isoformat()}|{summary}"
        if dedup_key in seen:
            continue
        seen.add(dedup_key)

        description_parts = []
        if teacher:
            description_parts.append(f"Lärare: {teacher}")
        if location:
            description_parts.append(f"Sal: {location}")

        events.append(
            {
                "summary": summary,
                "location": location,
                "description": "\n".join(description_parts),
                "start": start,
                "end": end,
            }
        )

    # Sort chronologically
    events.sort(key=lambda e: e["start"])
    return events
