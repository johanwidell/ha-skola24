"""Config flow for the Skola24 integration."""

from __future__ import annotations

import logging
from typing import Any

import aiohttp
import voluptuous as vol

from homeassistant import config_entries
from homeassistant.data_entry_flow import FlowResult
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import Skola24Api, Skola24AuthError, Skola24ApiError
from .const import (
    CONF_HOST,
    CONF_PASSWORD,
    CONF_SELECTION_TYPE,
    CONF_SELECTION_VALUE,
    CONF_UNIT_GUID,
    CONF_USERNAME,
    DOMAIN,
    SELECTION_TYPE_CLASS,
    SELECTION_TYPE_PIN,
    SELECTION_TYPE_LABELS,
)

_LOGGER = logging.getLogger(__name__)

# Fallback known good hosts — user can type any *.skola24.se subdomain
KNOWN_HOSTS = [
    "uppsala.skola24.se",
    "stockholm.skola24.se",
    "goteborg.skola24.se",
    "malmo.skola24.se",
    "linkoping.skola24.se",
    "vasteras.skola24.se",
    "orebro.skola24.se",
    "helsingborg.skola24.se",
    "norrköping.skola24.se",
    "jonkoping.skola24.se",
]

STEP_CREDENTIALS_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_HOST, default="uppsala.skola24.se"): str,
        vol.Required(CONF_USERNAME): str,
        vol.Required(CONF_PASSWORD): str,
    }
)

STEP_SELECTION_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_SELECTION_TYPE, default=SELECTION_TYPE_PIN): vol.In(
            list(SELECTION_TYPE_LABELS.keys())
        ),
        vol.Required(CONF_SELECTION_VALUE): str,
    }
)


class Skola24ConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Two-step config flow: credentials → selection."""

    VERSION = 1

    def __init__(self) -> None:
        self._credentials: dict[str, Any] = {}

    # ------------------------------------------------------------------
    # Step 1 — Credentials (host + username + password)
    # ------------------------------------------------------------------

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        errors: dict[str, str] = {}

        if user_input is not None:
            host = user_input[CONF_HOST].strip().lower()
            # Strip https:// if user pasted a full URL
            host = host.replace("https://", "").replace("http://", "").rstrip("/")
            user_input[CONF_HOST] = host

            # Validate credentials
            session = await _make_session(self.hass)
            api = Skola24Api(session, host)
            try:
                await api.login(user_input[CONF_USERNAME], user_input[CONF_PASSWORD])
            except Skola24AuthError as exc:
                _LOGGER.warning("Login failed: %s", exc)
                errors["base"] = "invalid_auth"
            except Skola24ApiError as exc:
                _LOGGER.warning("API error during login: %s", exc)
                errors["base"] = "cannot_connect"
            except Exception:  # noqa: BLE001
                _LOGGER.exception("Unexpected error during login")
                errors["base"] = "unknown"
            finally:
                await session.close()

            if not errors:
                self._credentials = user_input
                return await self.async_step_selection()

        return self.async_show_form(
            step_id="user",
            data_schema=STEP_CREDENTIALS_SCHEMA,
            errors=errors,
            description_placeholders={
                "host_hint": "T.ex. uppsala.skola24.se"
            },
        )

    # ------------------------------------------------------------------
    # Step 2 — Selection type (klass eller personnummer)
    # ------------------------------------------------------------------

    async def async_step_selection(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        errors: dict[str, str] = {}

        if user_input is not None:
            selection_type = user_input[CONF_SELECTION_TYPE]
            selection_value = user_input[CONF_SELECTION_VALUE].strip()

            # Quick sanity-check for PIN format
            if selection_type == SELECTION_TYPE_PIN:
                clean = selection_value.replace("-", "")
                if len(clean) not in (10, 12) or not clean.isdigit():
                    errors[CONF_SELECTION_VALUE] = "invalid_pin"

            if not errors:
                config = {
                    **self._credentials,
                    CONF_SELECTION_TYPE: selection_type,
                    CONF_SELECTION_VALUE: selection_value,
                }
                title = _make_title(self._credentials[CONF_HOST], selection_value)
                return self.async_create_entry(title=title, data=config)

        return self.async_show_form(
            step_id="selection",
            data_schema=STEP_SELECTION_SCHEMA,
            errors=errors,
            description_placeholders={
                "pin_hint": "Personnummer: ÅÅMMDD-XXXX",
                "class_hint": "Klassnamn exakt som i Skola24, t.ex. 9A",
            },
        )

    # ------------------------------------------------------------------
    # Re-auth (triggered when session expires and login fails repeatedly)
    # ------------------------------------------------------------------

    async def async_step_reauth(
        self, entry_data: dict[str, Any]
    ) -> FlowResult:
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        errors: dict[str, str] = {}
        existing_entry = self.hass.config_entries.async_get_entry(
            self.context["entry_id"]
        )

        if user_input is not None:
            session = await _make_session(self.hass)
            api = Skola24Api(session, existing_entry.data[CONF_HOST])
            try:
                await api.login(user_input[CONF_USERNAME], user_input[CONF_PASSWORD])
            except (Skola24AuthError, Skola24ApiError):
                errors["base"] = "invalid_auth"
            finally:
                await session.close()

            if not errors:
                self.hass.config_entries.async_update_entry(
                    existing_entry,
                    data={
                        **existing_entry.data,
                        CONF_USERNAME: user_input[CONF_USERNAME],
                        CONF_PASSWORD: user_input[CONF_PASSWORD],
                    },
                )
                await self.hass.config_entries.async_reload(existing_entry.entry_id)
                return self.async_abort(reason="reauth_successful")

        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_USERNAME): str,
                    vol.Required(CONF_PASSWORD): str,
                }
            ),
            errors=errors,
        )


# ------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------

async def _make_session(hass) -> aiohttp.ClientSession:
    """
    Create a private aiohttp session with a real CookieJar.

    Notes:
    - HA's shared session uses DummyCookieJar which discards all cookies;
      we need our own jar to maintain the ASP.NET session.
    - unsafe=True is NOT needed for named-domain cookies.  Skola24 sets
      Domain=.skola24.se so cookies from uppsala.skola24.se are
      automatically forwarded to web.skola24.se by the standard RFC jar.
    - We do NOT pass a connector here so aiohttp creates its own default
      TCPConnector (SSL enabled by default). Passing one from HA's
      event loop can cause "connector owner" errors on session close.
    """
    return aiohttp.ClientSession(
        cookie_jar=aiohttp.CookieJar(),
    )


def _make_title(host: str, selection: str) -> str:
    municipality = host.split(".")[0].capitalize()
    return f"Skola24 {municipality} — {selection}"
