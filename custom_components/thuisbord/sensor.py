"""Two diagnostic sensors: what the sender is doing, and when Thuisbord last took a reading."""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, Any

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .const import DOMAIN
from .sender import ReadingSender, State

if TYPE_CHECKING:
    from . import ThuisbordConfigEntry

# The sensors only report; nothing here can be changed or switched.
PARALLEL_UPDATES = 0


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ThuisbordConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Add the status sensors."""
    sender = entry.runtime_data.sender
    async_add_entities([StatusSensor(entry, sender), LastSentSensor(entry, sender)])


class _ThuisbordSensor(SensorEntity):
    _attr_has_entity_name = True
    _attr_should_poll = False
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, entry: ThuisbordConfigEntry, sender: ReadingSender, key: str) -> None:
        self._sender = sender
        self._attr_translation_key = key
        self._attr_unique_id = f"{entry.unique_id}_{key}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.unique_id or entry.entry_id)},
            name=entry.title,
            manufacturer="Thuisbord",
            entry_type=DeviceEntryType.SERVICE,
            configuration_url="https://thuisbord.app",
        )

    async def async_added_to_hass(self) -> None:
        """Follow the sender."""
        self.async_on_remove(self._sender.async_add_listener(self._async_changed))

    @callback
    def _async_changed(self) -> None:
        self.async_write_ha_state()


class StatusSensor(_ThuisbordSensor):
    """What the sender is doing, with the contract's code when something is wrong."""

    _attr_device_class = SensorDeviceClass.ENUM
    _attr_options = [state.value for state in State]

    def __init__(self, entry: ThuisbordConfigEntry, sender: ReadingSender) -> None:
        """Name the sensor `status`."""
        super().__init__(entry, sender, "status")

    @property
    def native_value(self) -> str:
        """The sender's state."""
        return self._sender.status.state.value

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """The reason, as a code, when the sender is retrying, refused or stopped."""
        status = self._sender.status
        return {"code": status.code}


class LastSentSensor(_ThuisbordSensor):
    """When Thuisbord last took readings, to the minute, so the state changes once a minute."""

    _attr_device_class = SensorDeviceClass.TIMESTAMP

    def __init__(self, entry: ThuisbordConfigEntry, sender: ReadingSender) -> None:
        """Name the sensor `last_sent`."""
        super().__init__(entry, sender, "last_sent")

    @property
    def native_value(self) -> datetime | None:
        """The minute of the last accepted request."""
        moment = self._sender.status.last_sent_at
        return None if moment is None else moment.replace(second=0, microsecond=0)
