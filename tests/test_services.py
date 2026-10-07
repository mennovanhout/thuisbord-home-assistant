"""The thuisbord.send_reading action."""

from __future__ import annotations

from freezegun.api import FrozenDateTimeFactory
import pytest

from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ServiceValidationError
from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.test_util.aiohttp import AiohttpClientMocker

from custom_components.thuisbord.const import DOMAIN, SERVICE_SEND_READING

from .conftest import READINGS_URL, accepted


async def test_action_sends_a_reading(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    aioclient_mock: AiohttpClientMocker,
    action_entry: MockConfigEntry,
) -> None:
    """The action sends one reading now, with only the fields given."""
    freezer.move_to("2026-10-06 21:15:40.7+00:00")
    aioclient_mock.post(READINGS_URL, status=202, json=accepted("2026-10-06T21:15:50Z"))
    assert await hass.config_entries.async_setup(action_entry.entry_id)

    response = await hass.services.async_call(
        DOMAIN,
        SERVICE_SEND_READING,
        {
            "config_entry_id": action_entry.entry_id,
            "active_power_w": -1250,
            "export_t1_kwh": 1022.301,
            "export_t2_kwh": "2410.871",
            "active_tariff": "2",
            "solar_power_w": 3400.4,
        },
        blocking=True,
        return_response=True,
    )
    await hass.async_block_till_done(wait_background_tasks=True)
    assert response == {"result": "queued"}
    _, _, data, _ = aioclient_mock.mock_calls[0]
    assert data == {
        "readings": [
            {
                "measured_at": "2026-10-06T21:15:40Z",
                "active_power_w": -1250,
                "export_t1_kwh": "1022.301",
                "export_t2_kwh": "2410.871",
                "active_tariff": 2,
                "solar_power_w": 3400,
            }
        ]
    }

    # Within 10 seconds of the previous reading: skipped, not an error.
    freezer.move_to("2026-10-06 21:15:45+00:00")
    response = await hass.services.async_call(
        DOMAIN,
        SERVICE_SEND_READING,
        {"config_entry_id": action_entry.entry_id, "active_power_w": 800},
        blocking=True,
        return_response=True,
    )
    assert response == {"result": "too_soon"}
    assert aioclient_mock.call_count == 1


@pytest.mark.parametrize(
    ("values", "key", "field"),
    [
        ({"active_power_w": 1, "import_t1_kwh": 1}, "reading_tariff_pair_incomplete", "import_t2_kwh"),
        ({"active_power_w": 70000}, "reading_out_of_range", "active_power_w"),
        ({"active_power_w": 1, "active_tariff": 3}, "reading_out_of_range", "active_tariff"),
    ],
)
async def test_action_refuses_what_thuisbord_would_refuse(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    action_entry: MockConfigEntry,
    values: dict,
    key: str,
    field: str,
) -> None:
    """A reading Thuisbord would refuse fails in the automation, naming the field."""
    assert await hass.config_entries.async_setup(action_entry.entry_id)
    with pytest.raises(ServiceValidationError) as err:
        await hass.services.async_call(
            DOMAIN,
            SERVICE_SEND_READING,
            {"config_entry_id": action_entry.entry_id, **values},
            blocking=True,
        )
    assert err.value.translation_key == key
    assert err.value.translation_placeholders == {"field": field}
    assert aioclient_mock.call_count == 0


async def test_action_refused_when_reading_sensors(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, sensors_entry: MockConfigEntry
) -> None:
    """An entry that reads sensors itself does not also take the action."""
    assert await hass.config_entries.async_setup(sensors_entry.entry_id)
    with pytest.raises(ServiceValidationError) as err:
        await hass.services.async_call(
            DOMAIN,
            SERVICE_SEND_READING,
            {"config_entry_id": sensors_entry.entry_id, "active_power_w": 1},
            blocking=True,
        )
    assert err.value.translation_key == "reads_sensors"


async def test_action_unknown_entry(
    hass: HomeAssistant, action_entry: MockConfigEntry
) -> None:
    """An entry ID that is not Thuisbord's is refused."""
    assert await hass.config_entries.async_setup(action_entry.entry_id)
    with pytest.raises(ServiceValidationError):
        await hass.services.async_call(
            DOMAIN,
            SERVICE_SEND_READING,
            {"config_entry_id": "nope", "active_power_w": 1},
            blocking=True,
        )
