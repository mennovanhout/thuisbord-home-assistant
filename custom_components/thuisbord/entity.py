"""What the Thuisbord entities share: the device they belong to."""

from __future__ import annotations

from typing import TYPE_CHECKING

from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .advice import BatteryAdviceCoordinator
from .const import DOMAIN

if TYPE_CHECKING:
    from . import ThuisbordConfigEntry


def device_info(entry: ThuisbordConfigEntry) -> DeviceInfo:
    """The one service device of an entry."""
    return DeviceInfo(
        identifiers={(DOMAIN, entry.unique_id or entry.entry_id)},
        name=entry.title,
        manufacturer="Thuisbord",
        entry_type=DeviceEntryType.SERVICE,
        configuration_url="https://thuisbord.app",
    )


class BatteryAdviceEntity(CoordinatorEntity[BatteryAdviceCoordinator]):
    """An entity that shows the battery charging advice. Read-only: nothing here switches a device."""

    _attr_has_entity_name = True

    def __init__(
        self, entry: ThuisbordConfigEntry, coordinator: BatteryAdviceCoordinator, key: str
    ) -> None:
        """Name the entity by its translation key."""
        super().__init__(coordinator)
        self._attr_translation_key = key
        self._attr_unique_id = f"{entry.unique_id}_{key}"
        self._attr_device_info = device_info(entry)
