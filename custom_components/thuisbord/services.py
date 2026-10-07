"""The `thuisbord.send_reading` action, for households that build their own automation."""

from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import TYPE_CHECKING, Any

import voluptuous as vol

from homeassistant.core import (
    HomeAssistant,
    ServiceCall,
    ServiceResponse,
    SupportsResponse,
    callback,
)
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import config_validation as cv, service
from homeassistant.util import dt as dt_util

from .const import ATTR_CONFIG_ENTRY_ID, DOMAIN, SERVICE_SEND_READING
from .reading import READING_FIELDS, InvalidReading, reading_from_values
from .sender import Added

if TYPE_CHECKING:
    from . import ThuisbordConfigEntry


def _number(value: Any) -> Decimal:
    """Accept a number, or text holding one, as the automation editor and templates give."""
    if isinstance(value, bool):
        raise vol.Invalid("not a number")
    try:
        number = Decimal(str(value).strip())
    except InvalidOperation as err:
        raise vol.Invalid("not a number") from err
    if not number.is_finite():
        raise vol.Invalid("not a number")
    return number


NUMBER = vol.All(vol.Any(int, float, str), _number)

SEND_READING_SCHEMA = vol.Schema(
    {
        vol.Required(ATTR_CONFIG_ENTRY_ID): cv.string,
        vol.Required("active_power_w"): NUMBER,
        **{
            vol.Optional(field): NUMBER
            for field in READING_FIELDS
            if field not in ("measured_at", "active_power_w")
        },
    }
)


@callback
def async_setup_services(hass: HomeAssistant) -> None:
    """Register the action."""

    async def send_reading(call: ServiceCall) -> ServiceResponse:
        entry: ThuisbordConfigEntry = service.async_get_config_entry(
            call.hass, DOMAIN, call.data[ATTR_CONFIG_ENTRY_ID]
        )
        data = entry.runtime_data
        if data.reads_sensors:
            raise ServiceValidationError(
                translation_domain=DOMAIN, translation_key="reads_sensors"
            )
        now = dt_util.utcnow().replace(microsecond=0)
        values = {k: v for k, v in call.data.items() if k != ATTR_CONFIG_ENTRY_ID}
        try:
            reading = reading_from_values(now, values)
        except InvalidReading as err:
            raise ServiceValidationError(
                translation_domain=DOMAIN,
                translation_key=f"reading_{err.code}",
                translation_placeholders={"field": err.field},
            ) from err

        result = data.sender.async_add(reading, now)
        if result is Added.STOPPED:
            raise HomeAssistantError(translation_domain=DOMAIN, translation_key="sending_stopped")
        if call.return_response:
            return {"result": result.value}
        return None

    hass.services.async_register(
        DOMAIN,
        SERVICE_SEND_READING,
        send_reading,
        schema=SEND_READING_SCHEMA,
        supports_response=SupportsResponse.OPTIONAL,
    )
