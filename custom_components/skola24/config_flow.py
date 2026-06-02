"""Config flow for the Skola24 integration."""

from __future__ import annotations

import logging
from typing import Any

import aiohttp
import voluptuous as vol

from homeassistant import config_entries
from homeassistant.data_entry_flow import FlowResult

from .api import Skola24Api, Skola24AuthError, Skola24ApiError
from .const import (
    CONF_HOST,
    CONF_PASSWORD,
    CONF_SELECTION_TYPE,
    CONF_SELECTION_VALUE,
    CONF_UNIT_GUID,
    CONF_SCHOOL_NAME,
    CONF_USERNAME,
    DOMAIN,
    SELECTION_TYPE_CLASS,
    SELECTION_TYPE_PIN,
    SELECTION_TYPE_LABELS,
)

_LOGGER = logging.getLogger(__name__)

STEP_CREDENTIALS_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_HOST, default="Uppsala.skola24.se"): str,
        vol.Required(CONF_USERNAME): str,
        vol.Required(CONF_PASSWORD): str,
    }
)


class Skola24ConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Three-step config flow: credentials → school → selection."""

    VERSION = 1

    def __init__(self) -> None:
        self._credentials: dict[str, Any] = {}
        self._units: list[dict] = []          # raw unit dicts from API
        self._api: Skola24Api | None = None
        self._session: aiohttp.ClientSession | None = None

    # ------------------------------------------------------------------
    # Step 1 — Credentials
    # ------------------------------------------------------------------

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        errors: dict[str, str] = {}

        if user_input is not None:
            host = user_input[CONF_HOST].strip().lower()
            host = host.replace("https://", "").replace("http://", "").rstrip("/")
            user_input[CONF_HOST] = host

            # Close any previous session
            if self._session and not self._session.closed:
                await self._session.close()

            self._session = _make_session()
            self._api = Skola24Api(self._session, host)
            try:
                await self._api.login(user_input[CONF_USERNAME], user_input[CONF_PASSWORD])
                # Fetch units while session is warm
                self._units = await self._api.get_units()
            except Skola24AuthError as exc:
                _LOGGER.warning("Login failed: %s", exc)
                errors["base"] = "invalid_auth"
            except Skola24ApiError as exc:
                _LOGGER.warning("API error during login: %s", exc)
                errors["base"] = "cannot_connect"
            except Exception:  # noqa: BLE001
                _LOGGER.exception("Unexpected error during login")
                errors["base"] = "unknown"

            if not errors:
                self._credentials = user_input
                return await self.async_step_school()

        return self.async_show_form(
            step_id="user",
            data_schema=STEP_CREDENTIALS_SCHEMA,
            errors=errors,
            description_placeholders={"host_hint": "T.ex. Uppsala.skola24.se"},
        )

    # ------------------------------------------------------------------
    # Step 2 — School selection
    # ------------------------------------------------------------------

    async def async_step_school(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        if user_input is not None:
            self._credentials[CONF_UNIT_GUID]   = user_input[CONF_UNIT_GUID]
            self._credentials[CONF_SCHOOL_NAME] = user_input.get(CONF_SCHOOL_NAME, "")
            return await self.async_step_selection()

        if not self._units:
            # No units (shouldn't happen) — skip straight to selection
            return await self.async_step_selection()

        # Build dropdown: "Skolnamn (unitId)" → unitGuid
        unit_options: dict[str, str] = {
            u["unitGuid"]: f"{u.get('unitId', u['unitGuid'])}"
            for u in self._units
            if u.get("unitGuid")
        }

        schema = vol.Schema(
            {
                vol.Required(CONF_UNIT_GUID): vol.In(unit_options),
            }
        )

        return self.async_show_form(
            step_id="school",
            data_schema=schema,
            description_placeholders={
                "count": str(len(self._units)),
            },
        )

    # ------------------------------------------------------------------
    # Step 3 — Selection type + value
    # ------------------------------------------------------------------

    async def async_step_selection(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        errors: dict[str, str] = {}

        if user_input is not None:
            selection_type  = user_input[CONF_SELECTION_TYPE]
            selection_value = user_input[CONF_SELECTION_VALUE].strip()

            if selection_type == SELECTION_TYPE_PIN:
                clean = selection_value.replace("-", "")
                if len(clean) not in (10, 12) or not clean.isdigit():
                    errors[CONF_SELECTION_VALUE] = "invalid_pin"

            if not errors:
                # Close the temporary login session — runtime creates its own
                if self._session and not self._session.closed:
                    await self._session.close()

                config = {
                    **self._credentials,
                    CONF_SELECTION_TYPE:  selection_type,
                    CONF_SELECTION_VALUE: selection_value,
                }
                school = self._credentials.get(CONF_SCHOOL_NAME) or selection_value
                title = f"Skola24 {self._credentials[CONF_HOST].split('.')[0].capitalize()} — {school}"
                return self.async_create_entry(title=title, data=config)

        schema = vol.Schema(
            {
                vol.Required(CONF_SELECTION_TYPE, default=SELECTION_TYPE_PIN): vol.In(
                    SELECTION_TYPE_LABELS
                ),
                vol.Required(CONF_SELECTION_VALUE): str,
            }
        )

        return self.async_show_form(
            step_id="selection",
            data_schema=schema,
            errors=errors,
            description_placeholders={
                "pin_hint":   "Personnummer: ÅÅMMDD-XXXX",
                "class_hint": "Klassnamn exakt som i Skola24, t.ex. 9A",
            },
        )

    # ------------------------------------------------------------------
    # Re-auth
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
            session = _make_session()
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

def _make_session() -> aiohttp.ClientSession:
    """
    Create a private aiohttp session with a real CookieJar.

    quote_cookie=False: prevents Python's http.cookies from wrapping
    cookie values in double-quotes, which breaks ASP.NET tenant validation.
    """
    return aiohttp.ClientSession(
        cookie_jar=aiohttp.CookieJar(quote_cookie=False),
    )
