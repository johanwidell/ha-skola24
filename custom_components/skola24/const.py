"""Constants for the Skola24 integration."""

DOMAIN = "skola24"

# Configuration keys
CONF_HOST = "host"          # e.g. "uppsala.skola24.se"
CONF_USERNAME = "username"
CONF_PASSWORD = "password"
CONF_SELECTION_TYPE = "selection_type"
CONF_SELECTION_VALUE = "selection_value"
CONF_UNIT_GUID   = "unit_guid"
CONF_SCHOOL_NAME = "school_name"

# Selection types (matches Skola24 API selectionType)
SELECTION_TYPE_CLASS = 0    # Class name, e.g. "9A"
SELECTION_TYPE_STUDENT = 1  # Student GUID (from user info)
SELECTION_TYPE_PIN = 4      # Personal ID (personnummer)

SELECTION_TYPE_LABELS = {
    SELECTION_TYPE_CLASS: "Klass (t.ex. 9A)",
    SELECTION_TYPE_PIN: "Personnummer (ÅÅMMDD-XXXX)",
}

# X-Scope headers — two different scopes exist in Skola24
# The public scope is used by the unauthenticated timetable viewer
# The auth scope is used by the logged-in portal (sniffed from DevTools)
X_SCOPE_PUBLIC = "8a22163c-8662-4535-9050-bc5e1923df48"
X_SCOPE_AUTH = "a0b6c9c4-11d7-4a52-a030-a55a15058eef"

# URLs
# All JSON API endpoints live on web.skola24.se
BASE_URL = "https://web.skola24.se"

# Login page lives on the MUNICIPALITY subdomain, e.g. https://uppsala.skola24.se
# Full URL: https://{host}/Applications/Authentication/login.aspx?host={host}
LOGIN_PATH = "/Applications/Authentication/login.aspx"

# API endpoints (all POST to BASE_URL)
EP_USER_INFO = "/api/get/user/info"
EP_RENDER_KEY = "/api/get/timetable/render/key"
EP_SCHOOL_YEARS = "/api/get/active/school/years"
EP_UNITS = "/api/services/skola24/get/timetable/viewer/units"
EP_SELECTION = "/api/get/timetable/selection"
EP_ENCRYPT = "/api/encrypt/signature"
EP_RENDER = "/api/render/timetable"

# Timing
UPDATE_INTERVAL_HOURS = 6
WEEKS_PAST = 1
WEEKS_FUTURE = 4

# HA storage
STORAGE_VERSION = 1
