"""A binary sensor that is on while Thuisbord recommends charging the home battery from the grid.

It only reports the advice. Wiring it to a battery is the household's own automation; by default
nothing acts on it.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from homeassistant.components.binary_sensor import BinarySensorEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .entity import BatteryAdviceEntity

if TYPE_CHECKING:
    from . import ThuisbordConfigEntry

# The coordinator does the polling; nothing here can be switched.
PARALLEL_UPDATES = 0


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ThuisbordConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Add the recommendation."""
    async_add_entities([RecommendChargeSensor(entry, entry.runtime_data.advice)])


class RecommendChargeSensor(BatteryAdviceEntity, BinarySensorEntity):
    """On exactly when the advice is `charge`."""

    def __init__(self, entry: ThuisbordConfigEntry, coordinator) -> None:
        """Name the sensor `recommend_charge`."""
        super().__init__(entry, coordinator, "recommend_charge")

    @property
    def is_on(self) -> bool:
        """Whether charging is recommended now."""
        return self.coordinator.data.recommend_charge
