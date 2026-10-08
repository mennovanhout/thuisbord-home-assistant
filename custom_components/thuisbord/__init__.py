"""The Thuisbord integration: sends this home's meter readings to Thuisbord.

Thuisbord is a household screen for energy and family. This integration is a bridge only: it
sends readings to the household's own Thuisbord, with the household's connection key. It reads
the sensors the household chose, or takes readings from the `thuisbord.send_reading` action. It
never controls a device, a home battery included.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.helpers.typing import ConfigType
from homeassistant.loader import async_get_integration

from .api import ThuisbordApi
from .collector import SensorCollector
from .const import (
    ALL_SENSORS,
    CONF_API_URL,
    CONF_BATTERY_CAPACITY,
    CONF_BATTERY_POWER_SIGN,
    CONF_CONNECTION_KEY,
    CONF_MODE,
    DEFAULT_API_URL,
    DOMAIN,
    MODE_SENSORS,
    SIGN_DISCHARGING_POSITIVE,
)
from .sender import ReadingSender
from .services import async_setup_services

PLATFORMS: list[Platform] = [Platform.SENSOR]

CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)


@dataclass(slots=True)
class ThuisbordData:
    """What a loaded entry keeps while it runs."""

    sender: ReadingSender
    reads_sensors: bool


type ThuisbordConfigEntry = ConfigEntry[ThuisbordData]


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Register the action, also before any entry is loaded."""
    async_setup_services(hass)
    return True


async def async_setup_entry(hass: HomeAssistant, entry: ThuisbordConfigEntry) -> bool:
    """Start sending readings.

    The key was checked when it was pasted. It is not checked again here, so a home without
    internet at start-up still buffers readings; a refused key shows at the first reading and
    starts the re-authentication flow.
    """
    integration = await async_get_integration(hass, DOMAIN)
    api = ThuisbordApi(
        async_get_clientsession(hass),
        entry.data.get(CONF_API_URL, DEFAULT_API_URL),
        entry.data[CONF_CONNECTION_KEY],
        f"Thuisbord-HomeAssistant/{integration.version}",
    )

    @callback
    def key_refused(code: str) -> None:
        entry.async_start_reauth(hass, data={"refusal_code": code})

    sender = ReadingSender(hass, entry, api, on_stopped=key_refused)
    entry.async_on_unload(sender.async_stop)

    @callback
    def refresh_status(_now: datetime) -> None:
        sender.async_refresh()

    # The status also changes when nothing happens: no new reading for 2 minutes.
    entry.async_on_unload(
        async_track_time_interval(hass, refresh_status, timedelta(seconds=30))
    )

    reads_sensors = entry.options.get(CONF_MODE) == MODE_SENSORS
    if reads_sensors:
        sensors = {key: entry.options[key] for key in ALL_SENSORS if entry.options.get(key)}
        collector = SensorCollector(
            hass,
            entry,
            sender,
            sensors,
            battery_sign=entry.options.get(CONF_BATTERY_POWER_SIGN, SIGN_DISCHARGING_POSITIVE),
            battery_capacity=entry.options.get(CONF_BATTERY_CAPACITY),
        )
        entry.async_on_unload(collector.async_start())

    entry.runtime_data = ThuisbordData(sender=sender, reads_sensors=reads_sensors)
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ThuisbordConfigEntry) -> bool:
    """Stop sending. Readings still waiting are dropped."""
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
