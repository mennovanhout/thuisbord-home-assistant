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
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
    TextSelector,
    TextSelectorConfig,
    TextSelectorType,
)
from homeassistant.loader import async_get_integration

from .api import CannotConnect, KeyRefused, ThuisbordApi, TooManyRequests, UnexpectedResponse
from .const import (
    ALL_SENSORS,
    BATTERY_POWER_SIGNS,
    BATTERY_SENSORS,
    CONF_ACTIVE_TARIFF,
    CONF_API_URL,
    CONF_BATTERY_CAPACITY,
    CONF_BATTERY_LEVEL,
    CONF_BATTERY_LIMIT,
    CONF_BATTERY_POWER,
    CONF_BATTERY_POWER_SIGN,
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
    SIGN_DISCHARGING_POSITIVE,
)
from .reading import ENERGY_UNITS, GAS_UNITS, PERCENT_UNITS, POWER_UNITS

# The contract's pattern for a connection key (api/openapi.yaml, connectionKey). The UUID part
# names the household and becomes the entry's unique ID.
KEY_PATTERN: Final = re.compile(
    r"^thb_([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})\.[A-Za-z0-9_-]{43,}$"
)

SECTION_ADVANCED: Final = "advanced"
SECTION_POWER: Final = "power_section"
SECTION_TOTALS: Final = "totals_section"
SECTION_EXTRA: Final = "extra_section"
SECTION_BATTERY: Final = "battery_section"
SENSOR_SECTIONS: Final = (SECTION_POWER, SECTION_TOTALS, SECTION_EXTRA, SECTION_BATTERY)

# Which units each chosen sensor may report. The sensor selector already filters on device
# class; this catches a sensor whose unit Thuisbord cannot convert.
SENSOR_UNITS: Final = {
    CONF_POWER: POWER_UNITS,
    CONF_POWER_IMPORT: POWER_UNITS,
    CONF_POWER_EXPORT: POWER_UNITS,
    CONF_SOLAR_POWER: POWER_UNITS,
    CONF_BATTERY_POWER: POWER_UNITS,
    CONF_BATTERY_LEVEL: PERCENT_UNITS,
    CONF_BATTERY_LIMIT: PERCENT_UNITS,
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

# Not an error: the key is valid, but the household has not agreed in the app yet.
CONSENT_PENDING: Final = "consent_pending"

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

# How the battery power sensor reads: positive while discharging, the energy dashboard's standard
# and so the default, or positive while charging.
SIGN_SELECTOR: Final = SelectSelector(
    SelectSelectorConfig(
        options=list(BATTERY_POWER_SIGNS),
        translation_key=CONF_BATTERY_POWER_SIGN,
        mode=SelectSelectorMode.LIST,
    )
)
# The battery's limit is often a setting of the battery's integration (a number entity, such as
# a minimum state of charge), and sometimes a sensor.
LIMIT_SELECTOR: Final = EntitySelector(
    EntitySelectorConfig(
        filter=EntityFilterSelectorConfig(domain=["sensor", "number", "input_number"])
    )
)
# The usable capacity in kWh, a number like the energy dashboard's battery `capacity`: above 0
# and at most 1000, as BatteryCapacityKwh in the contract, with up to three decimals.
CAPACITY_SELECTOR: Final = NumberSelector(
    NumberSelectorConfig(
        min=0.001, max=1000, step=0.001, unit_of_measurement="kWh", mode=NumberSelectorMode.BOX
    )
)


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
    """Check the key with Thuisbord.

    Return None when Thuisbord takes readings with it, CONSENT_PENDING when the key is valid but
    the household has not agreed in the app yet, or an error key.
    """
    integration = await async_get_integration(hass, DOMAIN)
    api = ThuisbordApi(
        async_get_clientsession(hass),
        api_url,
        key,
        f"Thuisbord-HomeAssistant/{integration.version}",
    )
    try:
        info = await api.check_connection()
    except KeyRefused as err:
        if err.code == "consent_not_recorded":
            return CONSENT_PENDING
        return REFUSAL_ERRORS.get(err.code, "key_refused")
    except TooManyRequests:
        return "too_many_requests"
    except UnexpectedResponse:
        return "unexpected_response"
    except CannotConnect:
        return "cannot_connect"
    if not info.consent_given:
        return CONSENT_PENDING
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
    """The form for choosing sensors: power, totals and tariff, solar and gas, a home battery."""
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
            vol.Required(SECTION_BATTERY): section(
                vol.Schema(
                    {
                        vol.Optional(CONF_BATTERY_LEVEL): _entity(SensorDeviceClass.BATTERY),
                        vol.Optional(CONF_BATTERY_POWER): power,
                        vol.Optional(
                            CONF_BATTERY_POWER_SIGN, default=SIGN_DISCHARGING_POSITIVE
                        ): SIGN_SELECTOR,
                        vol.Optional(CONF_BATTERY_LIMIT): LIMIT_SELECTOR,
                        vol.Optional(CONF_BATTERY_CAPACITY): CAPACITY_SELECTOR,
                    }
                ),
                {"collapsed": True},
            ),
        }
    )


def flatten_sensors(user_input: Mapping[str, Any]) -> dict[str, Any]:
    """Return the choice from the form's sections, without empty fields.

    The chosen sensors' entity IDs, and for a home battery how its power sensor reads (only with a
    battery power sensor) and the capacity in kWh, if entered.
    """
    chosen: dict[str, Any] = {}
    for part in SENSOR_SECTIONS:
        for key, value in (user_input.get(part) or {}).items():
            if key in ALL_SENSORS and isinstance(value, str) and value:
                chosen[key] = value
    battery = user_input.get(SECTION_BATTERY) or {}
    if CONF_BATTERY_POWER in chosen:
        sign = battery.get(CONF_BATTERY_POWER_SIGN)
        chosen[CONF_BATTERY_POWER_SIGN] = (
            sign if sign in BATTERY_POWER_SIGNS else SIGN_DISCHARGING_POSITIVE
        )
    capacity = battery.get(CONF_BATTERY_CAPACITY)
    if isinstance(capacity, int | float) and not isinstance(capacity, bool):
        chosen[CONF_BATTERY_CAPACITY] = float(capacity)
    return chosen


def nest_sensors(options: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    """Return the stored choice in the form's sections, to suggest it again."""
    sections: dict[str, dict[str, Any]] = {part: {} for part in SENSOR_SECTIONS}
    for key in ALL_SENSORS:
        value = options.get(key)
        if not value:
            continue
        if key in (CONF_POWER, CONF_POWER_IMPORT, CONF_POWER_EXPORT):
            sections[SECTION_POWER][key] = value
        elif key in (CONF_SOLAR_POWER, CONF_SOLAR_TOTAL, CONF_GAS_TOTAL):
            sections[SECTION_EXTRA][key] = value
        elif key in BATTERY_SENSORS:
            sections[SECTION_BATTERY][key] = value
        else:
            sections[SECTION_TOTALS][key] = value
    for key in (CONF_BATTERY_POWER_SIGN, CONF_BATTERY_CAPACITY):
        if options.get(key) is not None:
            sections[SECTION_BATTERY][key] = options[key]
    return sections


def check_sensors(hass: HomeAssistant, chosen: Mapping[str, Any]) -> tuple[str | None, str]:
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
    if CONF_BATTERY_CAPACITY in chosen and not any(key in chosen for key in BATTERY_SENSORS):
        # The capacity goes along only with the battery's charge level, power or limit.
        return "battery_capacity_alone", ""
    for key in ALL_SENSORS:
        entity_id = chosen.get(key)
        units = SENSOR_UNITS.get(key)
        if entity_id is None or units is None:
            continue
        state = hass.states.get(entity_id)
        if state is None:
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
        self._awaiting_consent = False
        # A refusal found while waiting for consent, shown on the key form it returns to.
        self._key_error: str | None = None

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

        error = await async_check_key(self.hass, api_url, key)
        if error not in (None, CONSENT_PENDING):
            return {"base": error}
        self._data = {CONF_CONNECTION_KEY: key, CONF_API_URL: api_url.rstrip("/")}
        self._awaiting_consent = error == CONSENT_PENDING
        return {}

    async def _async_key_accepted(self) -> ConfigFlowResult:
        """Go on once Thuisbord accepts the key: wait for consent first when it is missing."""
        if self._awaiting_consent:
            return await self.async_step_consent()
        if self.source == SOURCE_REAUTH:
            return self.async_update_reload_and_abort(
                self._get_reauth_entry(),
                data_updates={CONF_CONNECTION_KEY: self._data[CONF_CONNECTION_KEY]},
            )
        if self.source == SOURCE_RECONFIGURE:
            return self.async_update_reload_and_abort(
                self._get_reconfigure_entry(), data_updates=self._data
            )
        return await self.async_step_mode()

    async def async_step_consent(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """The key works, but the household has not agreed yet. Check again on Submit."""
        errors: dict[str, str] = {}
        if user_input is not None:
            error = await async_check_key(
                self.hass, self._data[CONF_API_URL], self._data[CONF_CONNECTION_KEY]
            )
            if error is None:
                self._awaiting_consent = False
                return await self._async_key_accepted()
            if error != CONSENT_PENDING:
                # The key itself is refused now, for example because it was renewed: ask for it.
                self._awaiting_consent = False
                self._key_error = error
                if self.source == SOURCE_REAUTH:
                    return await self.async_step_reauth_confirm()
                if self.source == SOURCE_RECONFIGURE:
                    return await self.async_step_reconfigure()
                return await self.async_step_user()
            errors["base"] = "consent_still_missing"
        return self.async_show_form(step_id="consent", data_schema=vol.Schema({}), errors=errors)

    def _shown_errors(self, errors: dict[str, str]) -> dict[str, str]:
        """The form's errors, or the refusal found while waiting for consent."""
        if not errors and self._key_error is not None:
            errors = {"base": self._key_error}
            self._key_error = None
        return errors

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Ask for the connection key the Thuisbord app shows."""
        errors: dict[str, str] = {}
        if user_input is not None:
            api_url = (user_input.get(SECTION_ADVANCED) or {}).get(CONF_API_URL, DEFAULT_API_URL)
            errors = await self._async_validate_key(user_input, api_url.strip())
            if not errors:
                return await self._async_key_accepted()
        return self.async_show_form(
            step_id="user",
            data_schema=self.add_suggested_values_to_schema(
                _key_schema(with_server=True), user_input or {}
            ),
            errors=self._shown_errors(errors),
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
                return await self._async_key_accepted()
        elif self._refusal_code is not None:
            # Show at once why Thuisbord stopped taking readings.
            errors = {"base": REFUSAL_ERRORS.get(self._refusal_code, "key_refused")}
            self._refusal_code = None
        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=_key_schema(with_server=False),
            errors=self._shown_errors(errors),
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
                return await self._async_key_accepted()
        suggested = {
            SECTION_ADVANCED: {CONF_API_URL: entry.data.get(CONF_API_URL, DEFAULT_API_URL)}
        }
        return self.async_show_form(
            step_id="reconfigure",
            data_schema=self.add_suggested_values_to_schema(
                _key_schema(with_server=True), suggested
            ),
            errors=self._shown_errors(errors),
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
