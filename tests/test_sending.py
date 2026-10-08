"""Reading the chosen sensors and sending readings: timing, the buffer and every answer."""

from __future__ import annotations

from collections.abc import Generator
import logging
from typing import Any
from unittest.mock import patch

from aiohttp import ClientError
from freezegun.api import FrozenDateTimeFactory
import pytest

from homeassistant.config_entries import SOURCE_REAUTH, ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.helpers import issue_registry as ir
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed,
)
from pytest_homeassistant_custom_component.test_util.aiohttp import AiohttpClientMocker

from custom_components.thuisbord.const import (
    CONF_MODE,
    CONF_POWER,
    CONF_SOLAR_TOTAL,
    DOMAIN,
    MODE_SENSORS,
)
from custom_components.thuisbord.diagnostics import async_get_config_entry_diagnostics

from .conftest import KEY, POWER_ENTITY, READINGS_URL, SECRET, accepted

STATUS = "sensor.thuisbord_status"
LAST_SENT = "sensor.thuisbord_last_reading_sent"
DAY = "2026-10-06"


@pytest.fixture(autouse=True)
def steady_backoff() -> Generator[None]:
    """Back-off without jitter: 10 s x 0.8, so waits never meet a whole 10 seconds."""
    with patch("custom_components.thuisbord.sender.random.random", return_value=0.0):
        yield


def meter(hass: HomeAssistant, freezer: FrozenDateTimeFactory, at: str, watts: str) -> None:
    """The meter reports power at `at`."""
    freezer.move_to(f"{DAY} {at}+00:00")
    hass.states.async_set(POWER_ENTITY, watts, {"unit_of_measurement": "W"})


async def at(hass: HomeAssistant, freezer: FrozenDateTimeFactory, moment: str) -> None:
    """Let the clock reach `moment` and run everything that is due."""
    freezer.move_to(f"{DAY} {moment}+00:00")
    async_fire_time_changed(hass)
    await hass.async_block_till_done(wait_background_tasks=True)


def sent(aioclient_mock: AiohttpClientMocker) -> list[list[dict[str, Any]]]:
    """The readings of every POST, in order."""
    return [data["readings"] for method, _, data, _ in aioclient_mock.mock_calls if method.upper() == "POST"]


async def start(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory, entry: MockConfigEntry
) -> None:
    """The meter reports 840 W, then Home Assistant starts the entry."""
    meter(hass, freezer, "21:15:35", "840")
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()


async def test_sends_power_every_10_seconds(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    aioclient_mock: AiohttpClientMocker,
    sensors_entry: MockConfigEntry,
) -> None:
    """At each whole 10 seconds with something new, exactly the minimal reading goes out."""
    aioclient_mock.post(READINGS_URL, status=202, json=accepted(f"{DAY}T21:15:50Z"))
    await start(hass, freezer, sensors_entry)
    assert hass.states.get(STATUS).state == "waiting"

    await at(hass, freezer, "21:15:40")
    assert sent(aioclient_mock) == [[{"measured_at": f"{DAY}T21:15:40Z", "active_power_w": 840}]]
    _, _, _, headers = aioclient_mock.mock_calls[0]
    assert headers["Authorization"] == f"Bearer {KEY}"
    assert headers["Accept"] == "application/json"
    assert headers["User-Agent"].startswith("Thuisbord-HomeAssistant/")
    assert hass.states.get(STATUS).state == "sending"
    assert hass.states.get(LAST_SENT).state == f"{DAY}T21:15:00+00:00"

    # Nothing new from the meter: nothing is sent.
    await at(hass, freezer, "21:15:50")
    assert len(sent(aioclient_mock)) == 1

    # The meter reports the same value again: that is new.
    meter(hass, freezer, "21:15:55", "840")
    await at(hass, freezer, "21:16:00")
    assert sent(aioclient_mock)[-1] == [{"measured_at": f"{DAY}T21:16:00Z", "active_power_w": 840}]


async def test_status_says_when_nothing_new_arrives(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    aioclient_mock: AiohttpClientMocker,
    sensors_entry: MockConfigEntry,
) -> None:
    """A meter that stops reporting is not shown as sending: after 2 minutes, waiting."""
    aioclient_mock.post(READINGS_URL, status=202, json=accepted(f"{DAY}T21:15:50Z"))
    await start(hass, freezer, sensors_entry)
    await at(hass, freezer, "21:15:40")
    assert hass.states.get(STATUS).state == "sending"

    hass.states.async_set(POWER_ENTITY, "unavailable")
    await at(hass, freezer, "21:16:40")
    assert hass.states.get(STATUS).state == "sending"
    await at(hass, freezer, "21:17:50")
    state = hass.states.get(STATUS)
    assert (state.state, state.attributes["code"]) == ("waiting", "no_new_reading")
    assert len(sent(aioclient_mock)) == 1

    meter(hass, freezer, "21:17:55", "800")
    await at(hass, freezer, "21:18:00")
    state = hass.states.get(STATUS)
    assert (state.state, state.attributes["code"]) == ("sending", None)


async def test_offline_buffers_one_per_minute(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    aioclient_mock: AiohttpClientMocker,
    sensors_entry: MockConfigEntry,
) -> None:
    """While Thuisbord is unreachable, readings wait: the first of each minute plus the newest."""
    aioclient_mock.post(READINGS_URL, exc=ClientError())
    await start(hass, freezer, sensors_entry)

    await at(hass, freezer, "21:15:40")  # A fails; next try after 8 s
    state = hass.states.get(STATUS)
    assert (state.state, state.attributes["code"]) == ("retrying", "offline")

    meter(hass, freezer, "21:15:45", "850")
    await at(hass, freezer, "21:15:48.6")  # A fails again; next try after 16 s
    meter(hass, freezer, "21:15:49", "860")
    await at(hass, freezer, "21:15:50")  # B waits
    meter(hass, freezer, "21:15:55", "870")
    await at(hass, freezer, "21:16:00")  # C, the first of a new minute, replaces B
    assert len(sent(aioclient_mock)) == 2

    aioclient_mock.clear_requests()
    aioclient_mock.post(READINGS_URL, status=202, json=accepted(f"{DAY}T21:16:10Z", 2))
    await at(hass, freezer, "21:16:04.6")
    assert sent(aioclient_mock) == [
        [
            {"measured_at": f"{DAY}T21:15:40Z", "active_power_w": 840},
            {"measured_at": f"{DAY}T21:16:00Z", "active_power_w": 870},
        ]
    ]
    assert hass.states.get(STATUS).state == "sending"

    meter(hass, freezer, "21:16:05", "880")
    await at(hass, freezer, "21:16:10")
    assert sent(aioclient_mock)[-1] == [{"measured_at": f"{DAY}T21:16:10Z", "active_power_w": 880}]


@pytest.mark.parametrize(
    ("status", "body", "code"),
    [
        (401, {"code": "unauthenticated"}, "unauthenticated"),
        (403, {"code": "key_revoked"}, "key_revoked"),
        (403, {"code": "consent_not_recorded"}, "consent_not_recorded"),
        (403, None, "forbidden"),
    ],
)
async def test_refused_key_stops_and_asks_for_a_new_one(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    aioclient_mock: AiohttpClientMocker,
    sensors_entry: MockConfigEntry,
    caplog: pytest.LogCaptureFixture,
    status: int,
    body: dict[str, Any] | None,
    code: str,
) -> None:
    """401 and 403 stop sending and open the reauth flow; the key never reaches the log."""
    caplog.set_level(logging.DEBUG)
    aioclient_mock.post(READINGS_URL, status=status, json=body)
    await start(hass, freezer, sensors_entry)
    await at(hass, freezer, "21:15:40")

    state = hass.states.get(STATUS)
    assert (state.state, state.attributes["code"]) == ("stopped", code)
    flows = hass.config_entries.flow.async_progress_by_handler(DOMAIN)
    assert [flow["context"]["source"] for flow in flows] == [SOURCE_REAUTH]

    meter(hass, freezer, "21:15:45", "900")
    await at(hass, freezer, "21:15:50")
    assert len(sent(aioclient_mock)) == 1

    assert KEY not in caplog.text
    assert SECRET not in caplog.text
    ours = [r.getMessage() for r in caplog.records if r.name.startswith("custom_components.thuisbord")]
    assert ours, "the refusal is logged, without the key"
    assert not any("840" in message or "900" in message for message in ours)


async def test_refused_reading_is_dropped(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    aioclient_mock: AiohttpClientMocker,
    sensors_entry: MockConfigEntry,
) -> None:
    """422 drops the named reading, says which field, and never resends it."""
    aioclient_mock.post(
        READINGS_URL,
        status=422,
        json={
            "code": "validation_failed",
            "errors": [{"field": "readings.0.active_power_w", "code": "out_of_range"}],
        },
    )
    await start(hass, freezer, sensors_entry)
    await at(hass, freezer, "21:15:40")
    state = hass.states.get(STATUS)
    assert (state.state, state.attributes["code"]) == ("refused", "fields")
    assert len(sent(aioclient_mock)) == 1

    await at(hass, freezer, "21:15:50")
    assert len(sent(aioclient_mock)) == 1

    aioclient_mock.clear_requests()
    aioclient_mock.post(READINGS_URL, status=202, json=accepted(f"{DAY}T21:16:00Z"))
    meter(hass, freezer, "21:15:55", "910")
    await at(hass, freezer, "21:16:00")
    assert sent(aioclient_mock) == [[{"measured_at": f"{DAY}T21:16:00Z", "active_power_w": 910}]]


async def test_too_frequent_waits_and_skips(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    aioclient_mock: AiohttpClientMocker,
    sensors_entry: MockConfigEntry,
) -> None:
    """429 too_frequent: wait Retry-After, and skip readings before next_allowed_at."""
    aioclient_mock.post(
        READINGS_URL,
        status=429,
        json={"code": "too_frequent", "next_allowed_at": f"{DAY}T21:16:00Z"},
        headers={"Retry-After": "20"},
    )
    await start(hass, freezer, sensors_entry)
    await at(hass, freezer, "21:15:40")
    state = hass.states.get(STATUS)
    assert (state.state, state.attributes["code"]) == ("retrying", "rate_limited")

    meter(hass, freezer, "21:15:45", "920")
    await at(hass, freezer, "21:15:50")  # before next_allowed_at: skipped
    assert len(sent(aioclient_mock)) == 1

    aioclient_mock.clear_requests()
    aioclient_mock.post(READINGS_URL, status=202, json=accepted(f"{DAY}T21:16:10Z"))
    meter(hass, freezer, "21:15:55", "930")
    await at(hass, freezer, "21:16:00")
    assert sent(aioclient_mock) == [[{"measured_at": f"{DAY}T21:16:00Z", "active_power_w": 930}]]


async def test_server_error_then_recovers(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    aioclient_mock: AiohttpClientMocker,
    sensors_entry: MockConfigEntry,
) -> None:
    """A 5xx is tried again with the same reading."""
    aioclient_mock.post(READINGS_URL, status=500)
    await start(hass, freezer, sensors_entry)
    await at(hass, freezer, "21:15:40")
    state = hass.states.get(STATUS)
    assert (state.state, state.attributes["code"]) == ("retrying", "server_error")

    aioclient_mock.clear_requests()
    aioclient_mock.post(READINGS_URL, status=202, json=accepted(f"{DAY}T21:15:50Z"))
    await at(hass, freezer, "21:15:48.6")
    assert sent(aioclient_mock) == [[{"measured_at": f"{DAY}T21:15:40Z", "active_power_w": 840}]]


async def test_missing_sensor_becomes_a_repair_issue(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    aioclient_mock: AiohttpClientMocker,
    sensors_entry: MockConfigEntry,
) -> None:
    """A chosen sensor that stays gone for 5 minutes becomes an issue, and clears when back."""
    aioclient_mock.post(READINGS_URL, status=202, json=accepted(f"{DAY}T21:15:50Z"))
    await start(hass, freezer, sensors_entry)
    hass.states.async_remove(POWER_ENTITY)
    issue_id = f"{sensors_entry.entry_id}_{CONF_POWER}_missing"
    issues = ir.async_get(hass)

    await at(hass, freezer, "21:15:40")
    assert issues.async_get_issue(DOMAIN, issue_id) is None
    await at(hass, freezer, "21:20:40")
    issue = issues.async_get_issue(DOMAIN, issue_id)
    assert issue is not None
    assert issue.translation_key == "sensor_missing"
    assert issue.translation_placeholders == {"entity_id": POWER_ENTITY, "title": "Thuisbord"}
    assert sent(aioclient_mock) == []

    meter(hass, freezer, "21:20:45", "940")
    await at(hass, freezer, "21:20:50")
    assert issues.async_get_issue(DOMAIN, issue_id) is None
    assert len(sent(aioclient_mock)) == 1


async def test_unusable_optional_sensor_is_left_out(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    aioclient_mock: AiohttpClientMocker,
) -> None:
    """An optional sensor in a unit Thuisbord does not know is left out; power still goes."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="Thuisbord",
        unique_id="0199b6a2-7c22-7e10-8f4b-5a6c7d8e9f01",
        data={"connection_key": KEY, "api_url": "https://thuisbord.app/api/v1"},
        options={
            CONF_MODE: MODE_SENSORS,
            CONF_POWER: POWER_ENTITY,
            CONF_SOLAR_TOTAL: "sensor.solar_total",
        },
    )
    entry.add_to_hass(hass)
    aioclient_mock.post(READINGS_URL, status=202, json=accepted(f"{DAY}T21:15:50Z"))
    freezer.move_to(f"{DAY} 21:15:35+00:00")
    hass.states.async_set("sensor.solar_total", "35.6", {"unit_of_measurement": "GJ"})
    await start(hass, freezer, entry)
    await at(hass, freezer, "21:15:40")
    assert sent(aioclient_mock) == [[{"measured_at": f"{DAY}T21:15:40Z", "active_power_w": 840}]]


async def test_unload_stops_sending(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    aioclient_mock: AiohttpClientMocker,
    sensors_entry: MockConfigEntry,
) -> None:
    """After unloading, nothing is read or sent."""
    aioclient_mock.post(READINGS_URL, exc=ClientError())
    await start(hass, freezer, sensors_entry)
    await at(hass, freezer, "21:15:40")
    assert await hass.config_entries.async_unload(sensors_entry.entry_id)
    assert sensors_entry.state is ConfigEntryState.NOT_LOADED

    meter(hass, freezer, "21:15:45", "950")
    await at(hass, freezer, "21:16:40")
    assert len(sent(aioclient_mock)) == 1


async def test_diagnostics_hide_the_key(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    aioclient_mock: AiohttpClientMocker,
    sensors_entry: MockConfigEntry,
) -> None:
    """Diagnostics show the settings and the sender's state, never the key or a reading."""
    aioclient_mock.post(READINGS_URL, exc=ClientError())
    await start(hass, freezer, sensors_entry)
    await at(hass, freezer, "21:15:40")

    diagnostics = await async_get_config_entry_diagnostics(hass, sensors_entry)
    assert diagnostics["data"] == {
        "connection_key": "**REDACTED**",
        "api_url": "https://thuisbord.app/api/v1",
    }
    assert diagnostics["options"] == {CONF_MODE: MODE_SENSORS, CONF_POWER: POWER_ENTITY}
    assert diagnostics["sender"]["state"] == "retrying"
    assert diagnostics["sender"]["buffered"] == 1
    assert SECRET not in repr(diagnostics)
    assert "840" not in repr(diagnostics)
