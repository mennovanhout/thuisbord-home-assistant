"""Constants for the Thuisbord integration."""

from __future__ import annotations

from typing import Final

DOMAIN: Final = "thuisbord"

DEFAULT_API_URL: Final = "https://thuisbord.app/api/v1"

# Config entry data. The key lives here and nowhere else.
CONF_API_URL: Final = "api_url"
CONF_CONNECTION_KEY: Final = "connection_key"

# Config entry options: how readings are made.
CONF_MODE: Final = "mode"
MODE_SENSORS: Final = "sensors"
MODE_ACTION: Final = "action"

# The sensors a household can choose, in the order the form shows them. Grid power is either
# one net sensor, or an import and an export sensor.
CONF_POWER: Final = "power"
CONF_POWER_IMPORT: Final = "power_import"
CONF_POWER_EXPORT: Final = "power_export"
CONF_IMPORT_TOTAL: Final = "import_total"
CONF_IMPORT_T1: Final = "import_t1"
CONF_IMPORT_T2: Final = "import_t2"
CONF_EXPORT_TOTAL: Final = "export_total"
CONF_EXPORT_T1: Final = "export_t1"
CONF_EXPORT_T2: Final = "export_t2"
CONF_ACTIVE_TARIFF: Final = "active_tariff"
CONF_SOLAR_POWER: Final = "solar_power"
CONF_SOLAR_TOTAL: Final = "solar_total"
CONF_GAS_TOTAL: Final = "gas_total"

POWER_SENSORS: Final = (CONF_POWER, CONF_POWER_IMPORT, CONF_POWER_EXPORT)
OPTIONAL_SENSORS: Final = (
    CONF_IMPORT_TOTAL,
    CONF_IMPORT_T1,
    CONF_IMPORT_T2,
    CONF_EXPORT_TOTAL,
    CONF_EXPORT_T1,
    CONF_EXPORT_T2,
    CONF_ACTIVE_TARIFF,
    CONF_SOLAR_POWER,
    CONF_SOLAR_TOTAL,
    CONF_GAS_TOTAL,
)
ALL_SENSORS: Final = POWER_SENSORS + OPTIONAL_SENSORS

# The sensors that only go together: a total per tariff needs the other tariff.
SENSOR_PAIRS: Final = (
    (CONF_IMPORT_T1, CONF_IMPORT_T2),
    (CONF_EXPORT_T1, CONF_EXPORT_T2),
    (CONF_POWER_IMPORT, CONF_POWER_EXPORT),
)

# The contract's limits (api/openapi.yaml, POST /readings).
MIN_INTERVAL_SECONDS: Final = 10
MAX_READINGS: Final = 360

# Service action.
SERVICE_SEND_READING: Final = "send_reading"
ATTR_CONFIG_ENTRY_ID: Final = "config_entry_id"
