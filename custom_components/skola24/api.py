"""Async Skola24 API client using aiohttp with session-cookie auth."""

from __future__ import annotations

import json
import logging
import re
from datetime import date, datetime, time as dtime, timedelta
from zoneinfo import ZoneInfo

import aiohttp

from .const import (
    BASE_URL,
    EP_ENCRYPT,
    EP_RENDER,
    EP_RENDER_KEY,
    EP_SCHOOL_YEARS,
    EP_SELECTION,
    EP_UNITS,
    EP_USER_INFO,
    LOGIN_PATH,
    X_SCOPE_AUTH,
    X_SCOPE_PUBLIC,
)

_LOGGER = logging.getLogger(__name__)

TZ = ZoneInfo("Europe/Stockholm")


class Skola24AuthError(Exception):
    """Raised when authentication fails."""


class Skola24ApiError(Exception):
    """Raised when an API call fails unexpectedly."""


class Skola24Api:
    """
    Async client for the (reverse-engineered) Skola24 web API.

    Authentication flow:
      1. GET the ASP.NET login page → extract hidden form tokens
      2. POST credentials → server sets ASP.NET_SessionId + tenant cookies
      3. Use those cookies + X_SCOPE_AUTH for all subsequent API calls

    The aiohttp.ClientSession passed in must have an aiohttp.CookieJar
    (not a DummyCookieJar) so that cookies survive between requests.
    """

    def __init__(self, session: aiohttp.ClientSession, host: str) -> None:
        """
        Args:
            session: aiohttp session with a real CookieJar.
            host:    Municipality hostname, e.g. "uppsala.skola24.se".
        """
        self._session = session
        self._host = host
        self._authenticated = False
        # Parsed from /api/get/user/info → sessionExpires (root-level field).
        # Confirmed by DevTools: session lives ~1 hour from login.
        self._session_expires: datetime | None = None

    # ------------------------------------------------------------------
    # Session expiry helpers
    # ------------------------------------------------------------------

    def session_about_to_expire(self, margin_minutes: int = 10) -> bool:
        """
        Return True if the current session expires within `margin_minutes`.

        Use this in the coordinator before every data fetch so we
        re-login proactively rather than getting a mid-fetch 401.
        """
        if self._session_expires is None:
            return True  # Unknown → treat as expired
        threshold = datetime.now(tz=self._session_expires.tzinfo) + timedelta(
            minutes=margin_minutes
        )
        return self._session_expires <= threshold

    @property
    def session_expires_at(self) -> datetime | None:
        """Expose session expiry for coordinator logging."""
        return self._session_expires

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    @property
    def _auth_headers(self) -> dict[str, str]:
        """Headers required for authenticated portal API calls."""
        return {
            "Content-Type": "application/json",
            "X-Scope": X_SCOPE_AUTH,
            "X-Requested-With": "XMLHttpRequest",
            "Origin": BASE_URL,
            "Referer": f"{BASE_URL}/portal/start",
            "User-Agent": (
                "Mozilla/5.0 (compatible; HomeAssistant/Skola24)"
            ),
        }

    @property
    def _public_headers(self) -> dict[str, str]:
        """Headers for unauthenticated public timetable viewer endpoints."""
        return {
            "Content-Type": "application/json",
            "X-Scope": X_SCOPE_PUBLIC,
        }

    async def _post(
        self,
        path: str,
        body: object = None,
        *,
        authenticated: bool = True,
    ) -> dict:
        """
        POST to BASE_URL + path.

        body=None sends JSON `null` (Content-Length: 4), which several
        Skola24 endpoints expect for no-payload calls.
        """
        headers = self._auth_headers if authenticated else self._public_headers
        raw = json.dumps(body)          # None → "null", {} → "{}"
        url = f"{BASE_URL}{path}"
        try:
            async with self._session.post(
                url,
                data=raw.encode(),
                headers=headers,
            ) as resp:
                if resp.status == 401:
                    self._authenticated = False
                    raise Skola24AuthError(f"Session expired (401) on {path}")
                if resp.status != 200:
                    raise Skola24ApiError(
                        f"HTTP {resp.status} from {path}"
                    )
                return await resp.json(content_type=None)
        except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
            raise Skola24ApiError(f"Network error on {path}: {exc}") from exc

    # ------------------------------------------------------------------
    # Authentication
    # ------------------------------------------------------------------

    async def login(self, username: str, password: str) -> None:
        """
        Authenticate via ASP.NET WebForms login page.

        The login form lives on the *municipality* subdomain, e.g.:
          https://uppsala.skola24.se/Applications/Authentication/login.aspx
                                                      ?host=uppsala.skola24.se

        NOT on web.skola24.se — confirmed by DevTools sniffing.

        Flow (confirmed):
          GET  → server sets ASP.NET_SessionId + s24_tenant + legacyuicookiestd
                 (cookies are Domain=.skola24.se → also valid on web.skola24.se)
          POST → authenticates; server redirects to web.skola24.se/portal/start
          GET /api/get/user/info → validates session

        Raises Skola24AuthError on failure.
        """
        # Login lives on the MUNICIPALITY host, not web.skola24.se
        municipality_origin = f"https://{self._host}"
        login_url = f"{municipality_origin}{LOGIN_PATH}?host={self._host}"

        # ---- Step 0: GET municipality root to obtain ASP.NET_SessionId ---
        # Confirmed by curl tracing: the ROOT of https://uppsala.skola24.se/
        # returns a 302 that sets ASP.NET_SessionId with domain=.skola24.se.
        # This cookie is required for the login POST to succeed.
        # Skipping straight to the login page bypasses this step entirely.
        _LOGGER.debug(
            "Fetching municipality root to establish ASP.NET session: %s/",
            municipality_origin,
        )
        try:
            # allow_redirects=False is intentional!
            #
            # The root 302 from https://uppsala.skola24.se/ sets
            # ASP.NET_SessionId and legacyuicookiestd with domain=.skola24.se.
            # That first response is all we need.
            #
            # If we follow redirects the chain continues to www.skola24.se
            # which sets a NEW ASP.NET_SessionId for a different application.
            # That overwrites the Uppsala session ID, causing a ViewState MAC
            # mismatch when we later POST the login form → DefaultErrorPage.
            async with self._session.get(
                f"{municipality_origin}/",
                headers={
                    "User-Agent": (
                        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) "
                        "Chrome/124.0.0.0 Safari/537.36"
                    ),
                    "Accept": "text/html,application/xhtml+xml,*/*;q=0.8",
                },
                allow_redirects=False,
            ) as resp:
                _LOGGER.debug(
                    "Step 0 done — HTTP %s, cookies: %s",
                    resp.status,
                    _cookie_names(self._session),
                )
        except aiohttp.ClientError as exc:
            raise Skola24AuthError(
                f"Could not reach municipality host {self._host}: {exc}"
            ) from exc

        # ---- Step 1: GET login page → sets session + tenant cookies -----
        _LOGGER.debug("Fetching login page: %s", login_url)
        try:
            async with self._session.get(
                login_url,
                headers={
                    "User-Agent": (
                        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) "
                        "Chrome/124.0.0.0 Safari/537.36"
                    ),
                    "Accept": (
                        "text/html,application/xhtml+xml,application/xml;"
                        "q=0.9,image/avif,image/webp,*/*;q=0.8"
                    ),
                },
            ) as resp:
                if resp.status != 200:
                    raise Skola24AuthError(
                        f"Login page returned HTTP {resp.status}"
                    )
                html = await resp.text()
                _LOGGER.debug(
                    "Step 1 done — login page OK | cookies: %s",
                    _cookie_names(self._session),
                )
        except aiohttp.ClientError as exc:
            raise Skola24AuthError(f"Could not reach login page: {exc}") from exc

        viewstate = _extract_hidden(html, "__VIEWSTATE")
        viewstate_gen = _extract_hidden(html, "__VIEWSTATEGENERATOR")
        event_validation = _extract_hidden(html, "__EVENTVALIDATION")

        if not viewstate:
            # Dump first 500 chars to help diagnose page structure changes
            _LOGGER.error(
                "Could not find __VIEWSTATE in login page. "
                "First 500 chars: %s",
                html[:500],
            )
            raise Skola24AuthError(
                "Could not parse login page — page structure may have changed"
            )

        # Find actual field names by searching for known partials.
        # Confirmed field names from live page (2026-06-01):
        #   LoginUC$UserNameTB, LoginUC$PwdTB, LoginUC$LoginButton
        user_field = (
            _find_input_name(html, "UserNameTB")
            or _find_input_name(html, "txtUserName")
            or "LoginUC$UserNameTB"
        )
        pass_field = (
            _find_input_name(html, "PwdTB")
            or _find_input_name(html, "txtPassword")
            or "LoginUC$PwdTB"
        )
        submit_field = (
            _find_submit_name(html)
            or "LoginUC$LoginButton"
        )
        # The login page's OnSubmit() JS increments __POST_ID before the form
        # is submitted (window.onload sets it to 0, onsubmit adds 1 → sends 1).
        # Sending the raw HTML value (0) triggers an ASP.NET exception because
        # the server uses this as an anti-bot / double-submit guard.
        raw_post_id = int(_extract_hidden(html, "__POST_ID") or "0")
        post_id = str(raw_post_id + 1)

        _LOGGER.debug(
            "Login form fields — user: %s  pass: %s  submit: %s  __POST_ID: %s→%s",
            user_field, pass_field, submit_field, raw_post_id, post_id,
        )

        # Skola24 appears to have a bot-detection timing check:
        # form submissions arriving < ~1 s after page load throw DefaultErrorPage.
        # A real user takes 2–10 s to enter credentials. We wait 2 s to pass the check.
        _LOGGER.debug("Waiting 2 s before POST (bot-detection timing workaround)")
        await asyncio.sleep(2)

        # Build form as ordered list to match HTML field order exactly.
        # Bypass aiohttp's FormData encoder — use urllib.parse.urlencode directly
        # so encoding is identical to what browser sends (quote_plus for values,
        # $ left unencoded in names to match browser behavior).
        import urllib.parse as _urlparse
        form_pairs: list[tuple[str, str]] = [
            ("__EVENTTARGET", ""),
            ("__EVENTARGUMENT", ""),
            ("__LASTFOCUS", ""),
            ("__POST_ID", post_id),
            ("__VIEWSTATE", viewstate),
            ("LoginUC$UserNameTB", username),
            ("LoginUC$PwdTB", password),
            ("LoginUC$LoginButton", "Logga in"),
            ("__VIEWSTATEGENERATOR", viewstate_gen or ""),
            ("__EVENTVALIDATION", event_validation or ""),
        ]
        # Encode: field names keep $ unencoded (safe='$'), values use quote_plus
        encoded_parts = []
        for k, v in form_pairs:
            ek = _urlparse.quote(k, safe="$")
            ev = _urlparse.quote_plus(v)
            encoded_parts.append(f"{ek}={ev}")
        encoded_body = "&".join(encoded_parts).encode("ascii")

        # ---- Step 2: POST credentials ------------------------------------
        # Log what cookies aiohttp will inject and the body size.
        from yarl import URL as _URL
        _filtered = self._session.cookie_jar.filter_cookies(_URL(login_url))
        _LOGGER.debug(
            "POST body: %d bytes | Cookies: %s",
            len(encoded_body),
            list(_filtered.keys()),
        )

        _LOGGER.debug("Posting login credentials for user '%s'", username)
        try:
            async with self._session.post(
                login_url,
                data=encoded_body,
                headers={
                    "Content-Type": "application/x-www-form-urlencoded",
                    "Content-Length": str(len(encoded_body)),
                    "Origin": municipality_origin,
                    "Referer": login_url,
                    "Upgrade-Insecure-Requests": "1",
                    "Cache-Control": "max-age=0",
                    "User-Agent": (
                        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) "
                        "Chrome/148.0.0.0 Safari/537.36"
                    ),
                    "Accept": (
                        "text/html,application/xhtml+xml,application/xml;"
                        "q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8,"
                        "application/signed-exchange;v=b3;q=0.7"
                    ),
                    "Accept-Language": "sv-SE,sv;q=0.9,en;q=0.8",
                    "sec-ch-ua": (
                        '"Chromium";v="148", "Google Chrome";v="148", '
                        '"Not/A)Brand";v="99"'
                    ),
                    "sec-ch-ua-mobile": "?0",
                    "sec-ch-ua-platform": '"macOS"',
                    "Sec-Fetch-Dest": "document",
                    "Sec-Fetch-Mode": "navigate",
                    "Sec-Fetch-Site": "same-origin",
                    "Sec-Fetch-User": "?1",
                },
                allow_redirects=True,
                max_redirects=10,
            ) as resp:
                final_status = resp.status
                final_url = str(resp.url)
        except aiohttp.ClientError as exc:
            raise Skola24AuthError(f"Login POST failed: {exc}") from exc

        _LOGGER.debug(
            "Step 2 done — POST landed on %s (HTTP %s) | cookies: %s",
            final_url,
            final_status,
            _cookie_names(self._session),
        )

        # Detect login failure.
        # Confirmed by testing: Skola24 redirects to DefaultErrorPage for
        # WRONG CREDENTIALS (not a genuine ASP.NET crash). A totally fake
        # username/password gives the same DefaultErrorPage as a wrong-password
        # attempt for a real user. The login page never shows an inline error.
        if "defaulterrorpage" in final_url.lower():
            raise Skola24AuthError(
                "Fel användarnamn eller lösenord "
                "(wrong username or password — Skola24 returns DefaultErrorPage "
                "for invalid credentials)."
            )
        elif "login.aspx" in final_url.lower():
            raise Skola24AuthError(
                "Login failed — server redirected back to login page. "
                "Check username and password."
            )

        # ---- Step 3: Validate session by calling /api/get/user/info ------
        info = await self._get_user_info_raw()
        if info is None:
            raise Skola24AuthError(
                f"Login POST succeeded (landed on {final_url}) but "
                "/api/get/user/info returned no valid session. "
                "This is unexpected — please report with debug logs."
            )

        self._authenticated = True
        _LOGGER.info("Skola24 login successful for host %s", self._host)

    async def _get_user_info_raw(self) -> dict | None:
        """
        Call POST /api/get/user/info; parse and cache session expiry.
        Logs the full response on failure for diagnostics.

        Confirmed response structure (from DevTools):
        {
          "error": null,
          "data": {
            "userMustChangePassword": false,
            "hasAcceptedAgreement": true
          },
          "exception": null,
          "validation": [],
          "sessionExpires": "2026-06-01T08:57:19.7393913+02:00",  ← ROOT level
          "needSessionRefresh": false                              ← ROOT level
        }

        Note: sessionExpires is NOT inside "data" — it's at the root of
        the response envelope.  Session duration is ~1 hour.
        """
        try:
            url = f"{BASE_URL}{EP_USER_INFO}"
            async with self._session.post(
                url,
                data=b"null",
                headers=self._auth_headers,
            ) as resp:
                _LOGGER.debug(
                    "user/info → HTTP %s | cookies sent: %s",
                    resp.status,
                    _cookie_names(self._session),
                )
                if resp.status != 200:
                    body = await resp.text()
                    _LOGGER.error(
                        "user/info returned HTTP %s — body: %s",
                        resp.status,
                        body[:300],
                    )
                    return None
                resp_json = await resp.json(content_type=None)
                _LOGGER.debug("user/info response: %s", resp_json)
        except Exception as exc:  # noqa: BLE001
            _LOGGER.debug("user/info exception: %s", exc)
            return None

        # Parse session expiry from root-level field
        expires_str = resp_json.get("sessionExpires")
        if expires_str:
            try:
                self._session_expires = datetime.fromisoformat(expires_str)
                _LOGGER.debug(
                    "Session expires at %s (needRefresh=%s)",
                    self._session_expires.isoformat(),
                    resp_json.get("needSessionRefresh"),
                )
            except ValueError:
                _LOGGER.warning("Could not parse sessionExpires: %s", expires_str)

        return resp_json.get("data") or {}

    async def get_user_info(self) -> dict:
        """
        Return logged-in user details.

        Note: the /api/get/user/info endpoint only returns:
          - userMustChangePassword (bool)
          - hasAcceptedAgreement (bool)
        It does NOT expose personGuid or student info.
        Use PIN or class-based selection for timetable queries.
        """
        data = await self._get_user_info_raw()
        if data is None:
            raise Skola24AuthError("Not authenticated")
        return data

    # ------------------------------------------------------------------
    # Timetable API
    # ------------------------------------------------------------------

    async def get_units(self) -> list[dict]:
        """Return list of school units for this host."""
        resp = await self._post(
            EP_UNITS,
            {"getTimetableViewerUnitsRequest": {"hostName": self._host}},
        )
        try:
            return resp["data"]["getTimetableViewerUnitsResponse"]["units"]
        except (KeyError, TypeError) as exc:
            raise Skola24ApiError(f"Unexpected units response: {exc}") from exc

    async def get_unit_guid(self, unit_id: str | None = None) -> str:
        """
        Resolve unitGuid.

        If unit_id is given, match by unitId; otherwise return the first unit.
        """
        units = await self.get_units()
        if not units:
            raise Skola24ApiError("No school units returned for host")
        if unit_id:
            for u in units:
                if u.get("unitId") == unit_id:
                    return u["unitGuid"]
            _LOGGER.warning(
                "unitId '%s' not found; available: %s. Using first.",
                unit_id,
                [u.get("unitId") for u in units],
            )
        return units[0]["unitGuid"]

    async def get_school_year(self) -> str:
        """Return GUID of the currently active school year."""
        resp = await self._post(
            EP_SCHOOL_YEARS,
            {"hostName": self._host, "checkSchoolYearsFeature": False},
        )
        try:
            return resp["data"]["activeSchoolYears"][0]["guid"]
        except (KeyError, IndexError, TypeError) as exc:
            raise Skola24ApiError(f"Could not get school year: {exc}") from exc

    async def get_render_key(self) -> str:
        """Fetch a one-time render key required by /api/render/timetable."""
        resp = await self._post(EP_RENDER_KEY, None)
        try:
            return resp["data"]["key"]
        except (KeyError, TypeError) as exc:
            raise Skola24ApiError(f"Could not get render key: {exc}") from exc

    async def get_encrypted_signature(self, raw_signature: str) -> str:
        """
        Encrypt a signature (class GUID or PIN) for use in render/timetable.

        Skola24 requires all selection values to be encrypted before
        passing them to the render endpoint.
        """
        resp = await self._post(EP_ENCRYPT, {"signature": raw_signature})
        try:
            return resp["data"]["signature"]
        except (KeyError, TypeError) as exc:
            raise Skola24ApiError(f"Could not encrypt signature: {exc}") from exc

    async def get_classes(self, unit_guid: str) -> list[dict]:
        """Return list of classes for a school unit."""
        resp = await self._post(
            EP_SELECTION,
            {
                "hostname": self._host,
                "unitGuid": unit_guid,
                "filters": {"class": True},
            },
        )
        try:
            return resp["data"]["classes"]
        except (KeyError, TypeError):
            return []

    async def get_class_guid(self, unit_guid: str, class_name: str) -> str:
        """Resolve a class name (e.g. '9A') to its GUID."""
        classes = await self.get_classes(unit_guid)
        for cls in classes:
            if cls.get("groupName") == class_name:
                return cls["groupGuid"]
        available = [c.get("groupName") for c in classes]
        raise Skola24ApiError(
            f"Class '{class_name}' not found. Available: {available}"
        )

    async def fetch_lessons(
        self,
        selection_type: int,
        selection_raw: str,
        unit_guid: str,
        school_year: str,
        *,
        weeks_past: int = 1,
        weeks_future: int = 4,
    ) -> list[dict]:
        """
        Fetch lessons across a window of ISO weeks.

        Returns list of lesson dicts (raw API data, enriched with
        `_iso_year` and `_iso_week` keys).
        """
        render_key = await self.get_render_key()
        encrypted = await self.get_encrypted_signature(selection_raw)

        week_range = _week_window(weeks_past, weeks_future)
        lessons: list[dict] = []

        for iso_year, iso_week in week_range:
            resp = await self._post(
                EP_RENDER,
                {
                    "renderKey": render_key,
                    "host": self._host,
                    "unitGuid": unit_guid,
                    "startDate": None,
                    "endDate": None,
                    "scheduleDay": 0,
                    "blackAndWhite": False,
                    "width": 1223,
                    "height": 550,
                    "schoolYear": school_year,
                    "selectionType": selection_type,
                    "selection": encrypted,
                    "showHeader": False,
                    "periodText": "",
                    "week": iso_week,
                    "year": iso_year,
                    "privateFreeTextMode": False,
                    "privateSelectionMode": None,
                    "customerKey": "",
                },
            )
            lesson_info = (resp.get("data") or {}).get("lessonInfo") or []
            for lesson in lesson_info:
                lesson["_iso_year"] = iso_year
                lesson["_iso_week"] = iso_week
            lessons.extend(lesson_info)
            _LOGGER.debug(
                "Week %s/%s: got %d lessons", iso_year, iso_week, len(lesson_info)
            )

        return lessons


# ------------------------------------------------------------------
# Helpers (module-level, no state)
# ------------------------------------------------------------------

def _extract_hidden(html: str, field_name: str) -> str | None:
    """Extract value of an ASP.NET hidden input field."""
    # Match both name="..." and id="..." variants
    for attr in ("name", "id"):
        pattern = (
            rf'<input[^>]+{attr}="{re.escape(field_name)}"'
            r'[^>]+value="([^"]*)"'
            r'|'
            rf'<input[^>]+value="([^"]*)"'
            rf'[^>]+{attr}="{re.escape(field_name)}"'
        )
        m = re.search(pattern, html, re.IGNORECASE | re.DOTALL)
        if m:
            return m.group(1) or m.group(2)
    return None


def _find_input_name(html: str, partial: str) -> str | None:
    """Find the full `name` attribute of an input whose name contains `partial`."""
    pattern = rf'<input[^>]+name="([^"]*{re.escape(partial)}[^"]*)"'
    m = re.search(pattern, html, re.IGNORECASE)
    return m.group(1) if m else None


def _cookie_names(session: aiohttp.ClientSession) -> list[str]:
    """Return list of cookie names currently in the session jar (no values)."""
    try:
        return sorted({m.key for m in session.cookie_jar})
    except Exception:  # noqa: BLE001
        return ["(could not read jar)"]


def _find_submit_name(html: str) -> str | None:
    """Find the `name` of the first submit button/input on the page."""
    pattern = r'<input[^>]+type=["\']submit["\'][^>]+name="([^"]+)"'
    m = re.search(pattern, html, re.IGNORECASE)
    if m:
        return m.group(1)
    # Also try name-before-type ordering
    pattern2 = r'<input[^>]+name="([^"]+)"[^>]+type=["\']submit["\']'
    m = re.search(pattern2, html, re.IGNORECASE)
    return m.group(1) if m else None


def _week_window(past: int, future: int) -> list[tuple[int, int]]:
    """Return list of (iso_year, iso_week) tuples around today."""
    today = date.today()
    iso_year, iso_week, _ = today.isocalendar()
    result = []
    for offset in range(-past, future + 1):
        monday = date.fromisocalendar(iso_year, iso_week, 1) + timedelta(weeks=offset)
        y, w, _ = monday.isocalendar()
        result.append((y, w))
    return result


def lesson_to_datetimes(
    lesson: dict,
) -> tuple[datetime, datetime] | tuple[None, None]:
    """
    Convert a raw lesson dict to (start, end) aware datetimes.

    Lesson keys used: _iso_year, _iso_week, dayOfWeekNumber,
                      timeStart ("HH:MM:SS"), timeEnd ("HH:MM:SS")
    """
    try:
        iso_year = int(lesson["_iso_year"])
        iso_week = int(lesson["_iso_week"])
        dow = int(lesson["dayOfWeekNumber"])  # 1=Mon … 7=Sun (Skola24 convention)
        # Skola24 uses 1=Mon based on observed data; clamp to valid range
        iso_dow = max(1, min(7, dow))

        day = date.fromisocalendar(iso_year, iso_week, iso_dow)

        def _parse_time(t: str) -> dtime:
            h, m, s = (int(x) for x in t.split(":"))
            return dtime(h, m, s)

        start_dt = datetime.combine(day, _parse_time(lesson["timeStart"]), tzinfo=TZ)
        end_dt = datetime.combine(day, _parse_time(lesson["timeEnd"]), tzinfo=TZ)
        return start_dt, end_dt
    except (KeyError, ValueError, TypeError) as exc:
        _LOGGER.debug("Could not parse lesson datetimes: %s — %s", exc, lesson)
        return None, None


import asyncio  # noqa: E402  (needed for TimeoutError reference above)
