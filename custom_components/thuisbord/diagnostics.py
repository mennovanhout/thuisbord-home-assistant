"""Diagnostics for Thuisbord. The connection key is redacted; no reading is included."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.core import HomeAssistant

from .const import CONF_CONNECTION_KEY

if TYPE_CHECKING:
    from . import ThuisbordConfigEntry

TO_REDACT = {CONF_CONNECTION_KEY}


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: ThuisbordConfigEntry
) -> dict[str, Any]:
    """Return the settings and what the sender is doing."""
    return {
        "data": async_redact_data(dict(entry.data), TO_REDACT),
        "options": dict(entry.options),
        "sender": entry.runtime_data.sender.status.as_dict(),
    }
