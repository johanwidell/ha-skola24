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

    async def _probe_best_unit(
        self, units: list[dict], school_year: str | None
    ) -> str:
        """
        Try each unit and return the GUID of the first one that yields lessons.

        Falls back to the first unit if none return lessons (e.g. summer break).
        """
        from .const import SELECTION_TYPE_CLASS

        if school_year is None:
            try:
                school_year = await self._api.get_school_year()
            except Exception:  # noqa: BLE001
                school_year = ""

        selection_raw = self._selection_value
        for unit in units:
            guid = unit.get("unitGuid", "")
            if not guid:
                continue
            try:
                raw = self._selection_value
                if self._selection_type == SELECTION_TYPE_CLASS:
                    try:
                        raw = await self._api.get_class_guid(guid, self._selection_value)
                    except Exception:  # noqa: BLE001
                        continue   # class not in this unit → skip
                render_key = await self._api.get_render_key()
                encrypted  = await self._api.get_encrypted_signature(raw)
                from .api import EP_RENDER, BASE_URL
                resp = await self._api._post(
                    EP_RENDER,
                    {
                        "renderKey": render_key,
                        "host": self._api._host,
                        "unitGuid": guid,
                        "startDate": None, "endDate": None,
                        "scheduleDay": 0, "blackAndWhite": False,
                        "width": 1223, "height": 550,
                        "schoolYear": school_year,
                        "selectionType": self._selection_type,
                        "selection": encrypted,
                        "showHeader": False, "periodText": "",
                        "week": __import__("datetime").date.today().isocalendar()[1],
                        "year": __import__("datetime").date.today().isocalendar()[0],
                        "privateFreeTextMode": False,
                        "privateSelectionMode": None, "customerKey": "",
                    },
                    authenticated=False,
                )
                lessons = (resp.get("data") or {}).get("lessonInfo") or []
                _LOGGER.debug(
                    "  Probe unit '%s': %d lessons",
                    unit.get("unitId", guid), len(lessons),
                )
                if lessons:
                    return guid
            except Exception as exc:  # noqa: BLE001
                _LOGGER.debug("  Probe unit '%s' error: %s", unit.get("unitId"), exc)

        _LOGGER.warning(
            "No unit returned lessons (may be summer break). Using first unit."
        )
        return units[0]["unitGuid"]

    async def _async_update_data(self) -> list[dict]:
        """Fetch fresh timetable data."""
        try:
            await self._ensure_authenticated()

            # Resolve unit GUID.
            # If unit_guid is already configured (from config entry), use it.
            # Otherwise: try ALL units and use whichever one yields lessons
            # (avoids always picking the wrong "first" unit in multi-school setups).
            from .const import SELECTION_TYPE_CLASS

            if not self._unit_guid:
                units = await self._api.get_units()
                _LOGGER.info(
                    "Available school units for %s (%d total):",
                    self._api._host, len(units),
                )
                for u in units:
                    _LOGGER.info(
                        "  unitId=%-35s  unitGuid=%s",
                        u.get("unitId", "?"), u.get("unitGuid", "?"),
                    )

                if not units:
                    raise Skola24ApiError("No school units returned for host")

                if len(units) == 1:
                    self._unit_guid = units[0]["unitGuid"]
                    _LOGGER.info("Single unit — using: %s", self._unit_guid)
                else:
                    # Multiple units: try each until one returns lessons.
                    # Cache the winning unit so we don't repeat this every update.
                    _LOGGER.info(
                        "Multiple units found — probing each to find the right school…"
                    )
                    self._unit_guid = await self._probe_best_unit(
                        units, school_year=None
                    )
                    _LOGGER.info("Best unit found: %s", self._unit_guid)

            # Resolve class name → class GUID for class-based selection
            selection_raw = self._selection_value
            if self._selection_type == SELECTION_TYPE_CLASS:
                try:
                    selection_raw = await self._api.get_class_guid(
                        self._unit_guid, self._selection_value
                    )
                    _LOGGER.debug(
                        "Class '%s' resolved to GUID: %s",
                        self._selection_value, selection_raw,
                    )
                except Exception as exc:  # noqa: BLE001
                    _LOGGER.warning(
                        "Could not resolve class '%s' to GUID: %s — "
                        "using raw value (may return 0 lessons)",
                        self._selection_value, exc,
                    )

            school_year = await self._api.get_school_year()

            raw_lessons = await self._api.fetch_lessons(
                selection_type=self._selection_type,
                selection_raw=selection_raw,
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
