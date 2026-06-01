"""Skola24 custom integration for Home Assistant."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

import aiohttp
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryNotReady

from .const import DOMAIN
from .coordinator import Skola24Coordinator

if TYPE_CHECKING:
    pass

_LOGGER = logging.getLogger(__name__)

PLATFORMS = [Platform.CALENDAR]

# Type alias used in calendar.py for IDE support (compatible with Python 3.11+)
Skola24ConfigEntry = ConfigEntry


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Skola24 from a config entry."""

    # Create our own session with a real CookieJar.
    # HA's shared session uses DummyCookieJar (discards cookies),
    # but we need cookies for ASP.NET session-based auth.
    #
    # Skola24 login is on uppsala.skola24.se; the API is on web.skola24.se.
    # The server sets Domain=.skola24.se so the standard RFC CookieJar
    # forwards cookies across subdomains — no unsafe=True needed.
    session = aiohttp.ClientSession(
        cookie_jar=aiohttp.CookieJar(),
    )

    coordinator = Skola24Coordinator(hass, session, dict(entry.data))

    # Initial refresh — raises ConfigEntryNotReady on failure so HA retries
    try:
        await coordinator.async_config_entry_first_refresh()
    except Exception as exc:
        await session.close()
        raise ConfigEntryNotReady(
            f"Could not connect to Skola24: {exc}"
        ) from exc

    # Store coordinator and session so we can clean up on unload
    hass.data.setdefault(DOMAIN, {})
    hass.data[DOMAIN][entry.entry_id] = coordinator
    hass.data[DOMAIN][f"{entry.entry_id}_session"] = session

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.async_on_unload(entry.add_update_listener(_async_update_listener))

    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload Skola24 config entry."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)

    if unload_ok:
        # Close the private aiohttp session
        session: aiohttp.ClientSession = hass.data[DOMAIN].pop(
            f"{entry.entry_id}_session", None
        )
        if session and not session.closed:
            await session.close()

        hass.data[DOMAIN].pop(entry.entry_id, None)

    return unload_ok


async def _async_update_listener(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Handle config entry updates (e.g. options changed)."""
    await hass.config_entries.async_reload(entry.entry_id)
