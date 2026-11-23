"""The sensors: the battery charging advice, and two diagnostic ones for what the sender is doing
and when Thuisbord last took a reading."""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, Any

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .advice import BatteryAdviceCoordinator
from .api import ADVICE_STATES
from .entity import BatteryAdviceEntity, device_info
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
    async_add_entities(
        [
            StatusSensor(entry, sender),
            LastSentSensor(entry, sender),
            BatteryAdviceSensor(entry, entry.runtime_data.advice),
        ]
    )


class _ThuisbordSensor(SensorEntity):
    _attr_has_entity_name = True
    _attr_should_poll = False
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, entry: ThuisbordConfigEntry, sender: ReadingSender, key: str) -> None:
        self._sender = sender
        self._attr_translation_key = key
        self._attr_unique_id = f"{entry.unique_id}_{key}"
        self._attr_device_info = device_info(entry)

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


class BatteryAdviceSensor(BatteryAdviceEntity, SensorEntity):
    """Thuisbord's advice for the home battery: `charge`, `hold` or `normal`.

    `hold` means do not discharge now, the charge is worth more later. The sensor only reports;
    wiring it to a battery is the household's own automation, and by default nothing acts on it.
    """

    _attr_device_class = SensorDeviceClass.ENUM
    _attr_options = list(ADVICE_STATES)

    def __init__(self, entry: ThuisbordConfigEntry, coordinator: BatteryAdviceCoordinator) -> None:
        """Name the sensor `battery_advice`."""
        super().__init__(entry, coordinator, "battery_advice")

    @property
    def native_value(self) -> str:
        """The advice now."""
        return self.coordinator.data.state

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Until when it holds, the prices behind it, and why there is no plan."""
        advice = self.coordinator.data
        return {
            "recommend_charge": advice.recommend_charge,
            "until": _iso(advice.state_until),
            "price_now": None if advice.price_now is None else float(advice.price_now),
            "break_even_price": (
                None if advice.break_even_price is None else float(advice.break_even_price)
            ),
            "reason": advice.reason,
            "computed_at": _iso(advice.computed_at),
            "next_update_at": _iso(advice.next_update_at),
        }


def _iso(moment: datetime | None) -> str | None:
    return None if moment is None else moment.isoformat()
