"""Config flow for Thuisbord: paste the connection key, then choose how readings are made."""

from __future__ import annotations

from collections.abc import Mapping
import ipaddress
import re
from typing import Any, Final
from urllib.parse import urlsplit

import voluptuous as vol

from homeassistant.components.sensor import SensorDeviceClass
from homeassistant.config_entries import (
    SOURCE_REAUTH,
    SOURCE_RECONFIGURE,
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlowWithReload,
)
from homeassistant.const import ATTR_UNIT_OF_MEASUREMENT
from homeassistant.core import HomeAssistant, callback
from homeassistant.data_entry_flow import section
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.selector import (
    EntityFilterSelectorConfig,
    EntitySelector,
    EntitySelectorConfig,
    TextSelector,
    TextSelectorConfig,
    TextSelectorType,
)
from homeassistant.loader import async_get_integration

from .api import CannotConnect, KeyRefused, ThuisbordApi, TooManyRequests, UnexpectedResponse
from .const import (
    ALL_SENSORS,
    CONF_ACTIVE_TARIFF,
    CONF_API_URL,
    CONF_CONNECTION_KEY,
    CONF_EXPORT_T1,
    CONF_EXPORT_T2,
    CONF_EXPORT_TOTAL,
    CONF_GAS_TOTAL,
    CONF_IMPORT_T1,
    CONF_IMPORT_T2,
    CONF_IMPORT_TOTAL,
    CONF_MODE,
    CONF_POWER,
    CONF_POWER_EXPORT,
    CONF_POWER_IMPORT,
    CONF_SOLAR_POWER,
    CONF_SOLAR_TOTAL,
    DEFAULT_API_URL,
    DOMAIN,
    MODE_ACTION,
    MODE_SENSORS,
    SENSOR_PAIRS,
)
from .reading import ENERGY_UNITS, GAS_UNITS, POWER_UNITS

# The contract's pattern for a connection key (api/openapi.yaml, connectionKey). The UUID part
# names the household and becomes the entry's unique ID.
KEY_PATTERN: Final = re.compile(
    r"^thb_([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})\.[A-Za-z0-9_-]{43,}$"
)

SECTION_ADVANCED: Final = "advanced"
SECTION_POWER: Final = "power_section"
SECTION_TOTALS: Final = "totals_section"
SECTION_EXTRA: Final = "extra_section"

# Which units each chosen sensor may report. The sensor selector already filters on device
# class; this catches a sensor whose unit Thuisbord cannot convert.
SENSOR_UNITS: Final = {
    CONF_POWER: POWER_UNITS,
    CONF_POWER_IMPORT: POWER_UNITS,
    CONF_POWER_EXPORT: POWER_UNITS,
    CONF_SOLAR_POWER: POWER_UNITS,
    CONF_IMPORT_TOTAL: ENERGY_UNITS,
    CONF_IMPORT_T1: ENERGY_UNITS,
    CONF_IMPORT_T2: ENERGY_UNITS,
    CONF_EXPORT_TOTAL: ENERGY_UNITS,
    CONF_EXPORT_T1: ENERGY_UNITS,
    CONF_EXPORT_T2: ENERGY_UNITS,
    CONF_SOLAR_TOTAL: ENERGY_UNITS,
    CONF_GAS_TOTAL: GAS_UNITS,
}

# The error shown for each refusal code of the contract.
REFUSAL_ERRORS: Final = {
    "unauthenticated": "invalid_key",
    "key_revoked": "key_revoked",
    "consent_not_recorded": "consent_not_recorded",
    "token_not_allowed": "token_not_allowed",
}

# Addresses on a home network: RFC 1918, link-local and IPv6 unique local addresses.
HOME_NETWORKS: Final = tuple(
    ipaddress.ip_network(network)
    for network in (
        "10.0.0.0/8",
        "172.16.0.0/12",
        "192.168.0.0/16",
        "169.254.0.0/16",
        "fc00::/7",
        "fe80::/10",
    )
)

KEY_SELECTOR: Final = TextSelector(
    TextSelectorConfig(type=TextSelectorType.PASSWORD, autocomplete="off")
)
URL_SELECTOR: Final = TextSelector(TextSelectorConfig(type=TextSelectorType.URL))


def household_id(key: str) -> str | None:
    """Return the household's UUID from a well-formed key, or None."""
    match = KEY_PATTERN.fullmatch(key)
    return match.group(1) if match else None


def _is_local_host(host: str) -> bool:
    """Whether plain HTTP is acceptable: a server in the home or on this machine."""
    if host in ("localhost", "host.docker.internal") or host.endswith((".local", ".test")):
        return True
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return False
    return address.is_loopback or any(address in network for network in HOME_NETWORKS)


def check_api_url(url: str) -> str | None:
    """Return an error key for the server address, or None when it is usable.

    The key goes to this address, so it must be HTTPS, except for a server on the home network
    or this machine, used when testing a local Thuisbord back-end.
    """
    try:
        parts = urlsplit(url)
    except ValueError:
        return "invalid_url"
    if parts.scheme not in ("https", "http") or not parts.hostname:
        return "invalid_url"
    if parts.query or parts.fragment or parts.username or parts.password:
        return "invalid_url"
    if parts.scheme == "http" and not _is_local_host(parts.hostname):
        return "insecure_url"
    return None


async def async_check_key(hass: HomeAssistant, api_url: str, key: str) -> str | None:
    """Check the key with Thuisbord. Return an error key, or None when Thuisbord accepts it."""
    integration = await async_get_integration(hass, DOMAIN)
    api = ThuisbordApi(
        async_get_clientsession(hass),
        api_url,
        key,
        f"Thuisbord-HomeAssistant/{integration.version}",
    )
    try:
        await api.check_connection()
    except KeyRefused as err:
        return REFUSAL_ERRORS.get(err.code, "key_refused")
    except TooManyRequests:
        return "too_many_requests"
    except UnexpectedResponse:
        return "unexpected_response"
    except CannotConnect:
        return "cannot_connect"
    return None


def _key_schema(*, with_server: bool) -> vol.Schema:
    fields: dict[Any, Any] = {vol.Required(CONF_CONNECTION_KEY): KEY_SELECTOR}
    if with_server:
        fields[vol.Required(SECTION_ADVANCED)] = section(
            vol.Schema({vol.Required(CONF_API_URL, default=DEFAULT_API_URL): URL_SELECTOR}),
            {"collapsed": True},
        )
    return vol.Schema(fields)


def _entity(device_class: SensorDeviceClass | None = None) -> EntitySelector:
    if device_class is None:
        return EntitySelector(EntitySelectorConfig(filter=EntityFilterSelectorConfig(domain="sensor")))
    return EntitySelector(
        EntitySelectorConfig(
            filter=EntityFilterSelectorConfig(domain="sensor", device_class=device_class)
        )
    )


def sensors_schema() -> vol.Schema:
    """The form for choosing sensors: power first, then totals and tariff, then solar and gas."""
    power = _entity(SensorDeviceClass.POWER)
    energy = _entity(SensorDeviceClass.ENERGY)
    return vol.Schema(
        {
            vol.Required(SECTION_POWER): section(
                vol.Schema(
                    {
                        vol.Optional(CONF_POWER): power,
                        vol.Optional(CONF_POWER_IMPORT): power,
                        vol.Optional(CONF_POWER_EXPORT): power,
                    }
                )
            ),
            vol.Required(SECTION_TOTALS): section(
                vol.Schema(
                    {
                        vol.Optional(CONF_IMPORT_TOTAL): energy,
                        vol.Optional(CONF_IMPORT_T1): energy,
                        vol.Optional(CONF_IMPORT_T2): energy,
                        vol.Optional(CONF_EXPORT_TOTAL): energy,
                        vol.Optional(CONF_EXPORT_T1): energy,
                        vol.Optional(CONF_EXPORT_T2): energy,
                        vol.Optional(CONF_ACTIVE_TARIFF): _entity(),
                    }
                ),
                {"collapsed": True},
            ),
            vol.Required(SECTION_EXTRA): section(
                vol.Schema(
                    {
                        vol.Optional(CONF_SOLAR_POWER): power,
                        vol.Optional(CONF_SOLAR_TOTAL): energy,
                        vol.Optional(CONF_GAS_TOTAL): _entity(SensorDeviceClass.GAS),
                    }
                ),
                {"collapsed": True},
            ),
        }
    )


def flatten_sensors(user_input: Mapping[str, Any]) -> dict[str, str]:
    """Return the chosen sensors from the form's sections, without empty ones."""
    chosen: dict[str, str] = {}
    for part in (SECTION_POWER, SECTION_TOTALS, SECTION_EXTRA):
        for key, value in (user_input.get(part) or {}).items():
            if key in ALL_SENSORS and isinstance(value, str) and value:
                chosen[key] = value
    return chosen


def nest_sensors(options: Mapping[str, Any]) -> dict[str, dict[str, str]]:
    """Return stored sensors in the form's sections, to suggest them again."""
    sections: dict[str, dict[str, str]] = {SECTION_POWER: {}, SECTION_TOTALS: {}, SECTION_EXTRA: {}}
    for key in ALL_SENSORS:
        value = options.get(key)
        if not value:
            continue
        if key in (CONF_POWER, CONF_POWER_IMPORT, CONF_POWER_EXPORT):
            sections[SECTION_POWER][key] = value
        elif key in (CONF_SOLAR_POWER, CONF_SOLAR_TOTAL, CONF_GAS_TOTAL):
            sections[SECTION_EXTRA][key] = value
        else:
            sections[SECTION_TOTALS][key] = value
    return sections


def check_sensors(hass: HomeAssistant, chosen: Mapping[str, str]) -> tuple[str | None, str]:
    """Return an error key and the entity it is about, or (None, "") when the choice is usable."""
    if CONF_POWER not in chosen and not (
        CONF_POWER_IMPORT in chosen and CONF_POWER_EXPORT in chosen
    ):
        if CONF_POWER_IMPORT in chosen or CONF_POWER_EXPORT in chosen:
            return "power_pair_incomplete", ""
        return "power_required", ""
    for first, second in SENSOR_PAIRS:
        if (first in chosen) != (second in chosen):
            return "pair_incomplete", chosen.get(first) or chosen.get(second, "")
    for key, entity_id in chosen.items():
        units = SENSOR_UNITS.get(key)
        state = hass.states.get(entity_id)
        if units is None or state is None:
            continue
        if state.attributes.get(ATTR_UNIT_OF_MEASUREMENT) not in units:
            return "unsupported_unit", entity_id
    return None, ""


class ThuisbordConfigFlow(ConfigFlow, domain=DOMAIN):
    """Add Thuisbord: the key first, then sensors or the action."""

    VERSION = 1
    MINOR_VERSION = 1

    def __init__(self) -> None:
        """Start without a key."""
        self._data: dict[str, Any] = {}
        self._refusal_code: str | None = None

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> ThuisbordOptionsFlow:
        """Change the sensors, or switch between sensors and the action."""
        return ThuisbordOptionsFlow()

    async def _async_validate_key(
        self, user_input: dict[str, Any], api_url: str
    ) -> dict[str, str]:
        """Check the pasted key's shape, its household, and then with Thuisbord."""
        key = user_input[CONF_CONNECTION_KEY].strip()
        household = household_id(key)
        if household is None:
            return {CONF_CONNECTION_KEY: "invalid_key_format"}
        if (url_error := check_api_url(api_url)) is not None:
            return {"base": url_error}

        await self.async_set_unique_id(household)
        if self.source in (SOURCE_REAUTH, SOURCE_RECONFIGURE):
            # A renewed key keeps the household's UUID; another household is another entry.
            self._abort_if_unique_id_mismatch(reason="wrong_household")
        else:
            self._abort_if_unique_id_configured()

        if (error := await async_check_key(self.hass, api_url, key)) is not None:
            return {"base": error}
        self._data = {CONF_CONNECTION_KEY: key, CONF_API_URL: api_url.rstrip("/")}
        return {}

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Ask for the connection key the Thuisbord app shows."""
        errors: dict[str, str] = {}
        if user_input is not None:
            api_url = (user_input.get(SECTION_ADVANCED) or {}).get(CONF_API_URL, DEFAULT_API_URL)
            errors = await self._async_validate_key(user_input, api_url.strip())
            if not errors:
                return await self.async_step_mode()
        return self.async_show_form(
            step_id="user",
            data_schema=self.add_suggested_values_to_schema(
                _key_schema(with_server=True), user_input or {}
            ),
            errors=errors,
            description_placeholders={"default_url": DEFAULT_API_URL},
        )

    async def async_step_mode(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Let Thuisbord read sensors by itself, or use the action in an automation."""
        return self.async_show_menu(step_id="mode", menu_options=[MODE_SENSORS, MODE_ACTION])

    async def async_step_sensors(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Choose the sensors Thuisbord reads."""
        errors: dict[str, str] = {}
        placeholders = {"entity_id": ""}
        if user_input is not None:
            chosen = flatten_sensors(user_input)
            error, entity_id = check_sensors(self.hass, chosen)
            if error is None:
                return self.async_create_entry(
                    title="Thuisbord", data=self._data, options={CONF_MODE: MODE_SENSORS, **chosen}
                )
            errors["base"] = error
            placeholders["entity_id"] = entity_id
        return self.async_show_form(
            step_id="sensors",
            data_schema=self.add_suggested_values_to_schema(sensors_schema(), user_input or {}),
            errors=errors,
            description_placeholders=placeholders,
        )

    async def async_step_action(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Explain the action, then add Thuisbord without sensors."""
        if user_input is not None:
            return self.async_create_entry(
                title="Thuisbord", data=self._data, options={CONF_MODE: MODE_ACTION}
            )
        return self.async_show_form(step_id="action", data_schema=vol.Schema({}))

    async def async_step_reauth(
        self, entry_data: Mapping[str, Any]
    ) -> ConfigFlowResult:
        """Thuisbord refused the key: ask for the current one.

        `entry_data` holds only `refusal_code`, the contract's code for the refusal, when the
        integration started this flow itself. It never holds the key.
        """
        code = entry_data.get("refusal_code")
        self._refusal_code = code if isinstance(code, str) else None
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Ask for the current key and check it."""
        entry = self._get_reauth_entry()
        errors: dict[str, str] = {}
        if user_input is not None:
            errors = await self._async_validate_key(
                user_input, entry.data.get(CONF_API_URL, DEFAULT_API_URL)
            )
            if not errors:
                return self.async_update_reload_and_abort(
                    entry, data_updates={CONF_CONNECTION_KEY: self._data[CONF_CONNECTION_KEY]}
                )
        elif self._refusal_code is not None:
            # Show at once why Thuisbord stopped taking readings.
            errors = {"base": REFUSAL_ERRORS.get(self._refusal_code, "key_refused")}
        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=_key_schema(with_server=False),
            errors=errors,
        )

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Paste a renewed key, or change the server address."""
        entry = self._get_reconfigure_entry()
        errors: dict[str, str] = {}
        if user_input is not None:
            api_url = (user_input.get(SECTION_ADVANCED) or {}).get(
                CONF_API_URL, entry.data.get(CONF_API_URL, DEFAULT_API_URL)
            )
            errors = await self._async_validate_key(user_input, api_url.strip())
            if not errors:
                return self.async_update_reload_and_abort(entry, data_updates=self._data)
        suggested = {
            SECTION_ADVANCED: {CONF_API_URL: entry.data.get(CONF_API_URL, DEFAULT_API_URL)}
        }
        return self.async_show_form(
            step_id="reconfigure",
            data_schema=self.add_suggested_values_to_schema(
                _key_schema(with_server=True), suggested
            ),
            errors=errors,
            description_placeholders={"default_url": DEFAULT_API_URL},
        )


class ThuisbordOptionsFlow(OptionsFlowWithReload):
    """Change the sensors, or switch between sensors and the action."""

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Choose between sensors and the action."""
        return self.async_show_menu(step_id="init", menu_options=[MODE_SENSORS, MODE_ACTION])

    async def async_step_sensors(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Choose the sensors Thuisbord reads."""
        errors: dict[str, str] = {}
        placeholders = {"entity_id": ""}
        if user_input is not None:
            chosen = flatten_sensors(user_input)
            error, entity_id = check_sensors(self.hass, chosen)
            if error is None:
                return self.async_create_entry(data={CONF_MODE: MODE_SENSORS, **chosen})
            errors["base"] = error
            placeholders["entity_id"] = entity_id
            suggested: Mapping[str, Any] = user_input
        else:
            suggested = nest_sensors(self.config_entry.options)
        return self.async_show_form(
            step_id="sensors",
            data_schema=self.add_suggested_values_to_schema(sensors_schema(), suggested),
            errors=errors,
            description_placeholders=placeholders,
        )

    async def async_step_action(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Stop reading sensors; readings come from the action."""
        if user_input is not None:
            return self.async_create_entry(data={CONF_MODE: MODE_ACTION})
        return self.async_show_form(step_id="action", data_schema=vol.Schema({}))
