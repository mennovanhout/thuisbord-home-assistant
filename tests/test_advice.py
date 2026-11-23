"""The battery charging advice: read with the connection key, shown as sensors, never acted on."""

from __future__ import annotations

from collections.abc import Generator
from datetime import timedelta
import json
import logging
from pathlib import Path
from typing import Any
from unittest.mock import patch

from aiohttp import ClientError
from freezegun.api import FrozenDateTimeFactory
import pytest

from homeassistant.config_entries import SOURCE_REAUTH, ConfigEntryState
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed,
)
from pytest_homeassistant_custom_component.test_util.aiohttp import AiohttpClientMocker

from custom_components.thuisbord.api import ADVICE_REASONS, ADVICE_STATES
from custom_components.thuisbord.diagnostics import async_get_config_entry_diagnostics

from .conftest import ADVICE_URL, KEY, READINGS_URL, SECRET

STATE = "sensor.thuisbord_battery_charging_advice"
RECOMMEND = "binary_sensor.thuisbord_recommend_to_charge"
START = "2026-11-10 22:00:10+00:00"

CHARGE = {
    "state": "charge",
    "recommend_charge": True,
    "reason": "cheap_now",
    "state_until": "2026-11-10T22:30:00Z",
    "price_now": "0.13505",
    "break_even_price": "0.15006",
    "slots": [
        {
            "starts_at": "2026-11-10T22:00:00Z",
            "ends_at": "2026-11-10T22:15:00Z",
            "state": "charge",
            "price": "0.13505",
        }
    ],
    "computed_at": "2026-11-10T22:00:04Z",
    "next_update_at": "2026-11-10T22:15:00Z",
}
HOLD = {
    **CHARGE,
    "state": "hold",
    "recommend_charge": False,
    "reason": "dearer_later",
    "state_until": None,
    "computed_at": "2026-11-10T22:15:03Z",
    "next_update_at": "2026-11-10T22:30:00Z",
}
NO_BATTERY = {
    "state": "normal",
    "recommend_charge": False,
    "reason": "no_battery",
    "state_until": None,
    "price_now": None,
    "break_even_price": None,
    "slots": [],
    "computed_at": "2026-11-10T22:00:04Z",
    "next_update_at": "2026-11-10T22:15:00Z",
}
ETAG = '"abc123"'


@pytest.fixture(autouse=True)
def steady_poll() -> Generator[None]:
    """No jitter on the poll time: the next request goes out 5 seconds after `next_update_at`."""
    with patch("custom_components.thuisbord.advice.random.random", return_value=0.0):
        yield


def advice_calls(mocker: AiohttpClientMocker) -> list[dict[str, str]]:
    """The headers of every request to /battery-advice, in order."""
    return [h for method, url, _, h in mocker.mock_calls if str(url) == ADVICE_URL]


def answer(mocker: AiohttpClientMocker, **kwargs: Any) -> None:
    """Replace what Thuisbord answers to /battery-advice."""
    calls = list(mocker.mock_calls)
    mocker.clear_requests()
    mocker.mock_calls.extend(calls)
    mocker.get(ADVICE_URL, **kwargs)


async def start(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory, entry: MockConfigEntry
) -> None:
    """Start the entry at a quarter's start."""
    freezer.move_to(START)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()


async def at(hass: HomeAssistant, freezer: FrozenDateTimeFactory, moment: str) -> None:
    """Let the clock reach `moment` and run everything that is due."""
    freezer.move_to(f"2026-11-10 {moment}+00:00")
    async_fire_time_changed(hass)
    await hass.async_block_till_done(wait_background_tasks=True)


async def test_shows_the_advice(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    aioclient_mock: AiohttpClientMocker,
    action_entry: MockConfigEntry,
) -> None:
    """The sensor and the binary sensor show a `charge`, with the reasons for it as attributes."""
    aioclient_mock.get(ADVICE_URL, json=CHARGE, headers={"ETag": ETAG})
    await start(hass, freezer, action_entry)

    state = hass.states.get(STATE)
    assert state.state == "charge"
    assert state.attributes["device_class"] == "enum"
    assert state.attributes["options"] == ["charge", "hold", "normal"]
    assert state.attributes["recommend_charge"] is True
    assert state.attributes["until"] == "2026-11-10T22:30:00+00:00"
    assert state.attributes["price_now"] == 0.13505
    assert state.attributes["break_even_price"] == 0.15006
    assert state.attributes["reason"] == "cheap_now"
    assert state.attributes["next_update_at"] == "2026-11-10T22:15:00+00:00"
    # The plan per slot stays out of the attributes: they are kept in the history.
    assert "slots" not in state.attributes
    assert hass.states.get(RECOMMEND).state == "on"

    # A plain GET with the key and nothing else: no body, no reading.
    (_, url, data, headers) = aioclient_mock.mock_calls[0]
    assert (aioclient_mock.mock_calls[0][0], str(url), data) == ("GET", ADVICE_URL, None)
    assert headers["Authorization"] == f"Bearer {KEY}"
    assert headers["Accept"] == "application/json"
    assert headers["User-Agent"].startswith("Thuisbord-HomeAssistant/")
    assert "If-None-Match" not in headers
    assert all(method == "GET" for method, *_ in aioclient_mock.mock_calls)


async def test_hold_and_normal_without_a_plan(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    aioclient_mock: AiohttpClientMocker,
    action_entry: MockConfigEntry,
) -> None:
    """`hold` is not a recommendation to charge; a home without a battery gets `normal` and why."""
    aioclient_mock.get(ADVICE_URL, json=HOLD)
    await start(hass, freezer, action_entry)
    assert hass.states.get(STATE).state == "hold"
    assert hass.states.get(RECOMMEND).state == "off"
    assert hass.states.get(STATE).attributes["until"] is None

    answer(aioclient_mock, json=NO_BATTERY)
    await at(hass, freezer, "22:30:40")
    state = hass.states.get(STATE)
    assert state.state == "normal"
    assert state.attributes["reason"] == "no_battery"
    assert state.attributes["price_now"] is None
    assert state.attributes["break_even_price"] is None
    assert hass.states.get(RECOMMEND).state == "off"


async def test_unknown_reason_shows_the_state_alone(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    aioclient_mock: AiohttpClientMocker,
    action_entry: MockConfigEntry,
) -> None:
    """A reason a later contract version adds is not shown, and does not break the sensor."""
    aioclient_mock.get(ADVICE_URL, json={**CHARGE, "reason": "solar_surplus"})
    await start(hass, freezer, action_entry)
    assert hass.states.get(STATE).state == "charge"
    assert hass.states.get(STATE).attributes["reason"] is None


async def test_asks_again_after_the_quarter_with_the_etag(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    aioclient_mock: AiohttpClientMocker,
    action_entry: MockConfigEntry,
) -> None:
    """The next request goes out just after `next_update_at`, with If-None-Match; a 304 keeps it."""
    aioclient_mock.get(ADVICE_URL, json=CHARGE, headers={"ETag": ETAG})
    await start(hass, freezer, action_entry)
    assert len(advice_calls(aioclient_mock)) == 1

    # Not before the quarter ends, and not at the end itself.
    await at(hass, freezer, "22:10:00")
    await at(hass, freezer, "22:14:59")
    assert len(advice_calls(aioclient_mock)) == 1

    answer(aioclient_mock, status=304, headers={"ETag": ETAG})
    await at(hass, freezer, "22:15:06")
    calls = advice_calls(aioclient_mock)
    assert len(calls) == 2
    assert calls[1]["If-None-Match"] == ETAG
    assert hass.states.get(STATE).state == "charge"

    # The 304 says nothing new, so the next request is a quarter later: the old
    # `next_update_at` is past, and the poll never goes faster than once a minute.
    await at(hass, freezer, "22:15:30")
    assert len(advice_calls(aioclient_mock)) == 2
    await at(hass, freezer, "22:16:30")
    assert len(advice_calls(aioclient_mock)) == 3

    answer(aioclient_mock, json=HOLD, headers={"ETag": '"def456"'})
    await at(hass, freezer, "22:18:00")
    assert hass.states.get(STATE).state == "hold"
    # The new advice names the next quarter's end, and the new ETag goes along.
    answer(aioclient_mock, status=304)
    await at(hass, freezer, "22:30:06")
    assert advice_calls(aioclient_mock)[-1]["If-None-Match"] == '"def456"'


async def test_never_polls_faster_than_once_a_minute(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    aioclient_mock: AiohttpClientMocker,
    action_entry: MockConfigEntry,
) -> None:
    """An advice whose quarter has already ended is not asked for again in the same minute."""
    aioclient_mock.get(ADVICE_URL, json={**CHARGE, "next_update_at": "2026-11-10T21:59:00Z"})
    await start(hass, freezer, action_entry)
    await at(hass, freezer, "22:00:50")
    assert len(advice_calls(aioclient_mock)) == 1
    await at(hass, freezer, "22:01:15")
    assert len(advice_calls(aioclient_mock)) == 2


async def test_asks_at_most_every_15_minutes(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    aioclient_mock: AiohttpClientMocker,
    action_entry: MockConfigEntry,
) -> None:
    """A far away `next_update_at` does not make the integration wait for hours."""
    aioclient_mock.get(ADVICE_URL, json={**CHARGE, "next_update_at": "2026-11-11T08:00:00Z"})
    await start(hass, freezer, action_entry)
    await at(hass, freezer, "22:14:00")
    assert len(advice_calls(aioclient_mock)) == 1
    await at(hass, freezer, "22:15:30")
    assert len(advice_calls(aioclient_mock)) == 2


@pytest.mark.parametrize(
    ("status", "code", "shown"),
    [(401, "unauthenticated", "key_refused"), (403, "key_revoked", "key_revoked")],
)
async def test_refused_key_starts_reauthentication(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    aioclient_mock: AiohttpClientMocker,
    action_entry: MockConfigEntry,
    caplog: pytest.LogCaptureFixture,
    status: int,
    code: str,
    shown: str,
) -> None:
    """401 and 403 stop the polling, make the entities unavailable and ask for a new key."""
    aioclient_mock.get(ADVICE_URL, json=CHARGE)
    await start(hass, freezer, action_entry)
    assert hass.states.get(STATE).state == "charge"

    answer(aioclient_mock, status=status, json={"code": code})
    with caplog.at_level(logging.DEBUG):
        await at(hass, freezer, "22:15:30")

    assert hass.states.get(STATE).state == "unavailable"
    assert hass.states.get(RECOMMEND).state == "unavailable"
    flows = hass.config_entries.flow.async_progress_by_handler("thuisbord")
    assert [flow["context"]["source"] for flow in flows] == [SOURCE_REAUTH]
    calls = len(advice_calls(aioclient_mock))
    await at(hass, freezer, "23:00:00")
    assert len(advice_calls(aioclient_mock)) == calls
    assert SECRET not in caplog.text


async def test_offline_makes_the_entities_unavailable_and_recovers(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    aioclient_mock: AiohttpClientMocker,
    action_entry: MockConfigEntry,
) -> None:
    """While Thuisbord cannot be reached no stale advice is shown; it is tried again after 5 minutes."""
    aioclient_mock.get(ADVICE_URL, exc=ClientError())
    await start(hass, freezer, action_entry)
    assert action_entry.state is ConfigEntryState.LOADED
    assert hass.states.get(STATE).state == "unavailable"
    assert hass.states.get(RECOMMEND).state == "unavailable"
    assert hass.config_entries.flow.async_progress_by_handler("thuisbord") == []

    await at(hass, freezer, "22:04:00")
    assert len(advice_calls(aioclient_mock)) == 1
    answer(aioclient_mock, json=CHARGE)
    await at(hass, freezer, "22:05:30")
    assert len(advice_calls(aioclient_mock)) == 2
    assert hass.states.get(STATE).state == "charge"

    # And it goes unavailable again, rather than keeping the old advice, when it fails later.
    answer(aioclient_mock, status=503)
    await at(hass, freezer, "22:15:30")
    assert hass.states.get(STATE).state == "unavailable"


async def test_rate_limit_waits_retry_after(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    aioclient_mock: AiohttpClientMocker,
    action_entry: MockConfigEntry,
) -> None:
    """A 429 is not asked again before Retry-After."""
    aioclient_mock.get(
        ADVICE_URL,
        status=429,
        json={"code": "too_many_requests"},
        headers={"Retry-After": "120"},
    )
    await start(hass, freezer, action_entry)
    assert hass.states.get(STATE).state == "unavailable"
    assert len(advice_calls(aioclient_mock)) == 1

    answer(aioclient_mock, json=CHARGE)
    await at(hass, freezer, "22:01:30")
    assert len(advice_calls(aioclient_mock)) == 1
    await at(hass, freezer, "22:02:30")
    assert len(advice_calls(aioclient_mock)) == 2
    assert hass.states.get(STATE).state == "charge"


@pytest.mark.parametrize(
    "body",
    [
        {**CHARGE, "state": "boost"},
        {**CHARGE, "recommend_charge": "yes"},
        {**CHARGE, "next_update_at": "tomorrow"},
        {**CHARGE, "price_now": 0.13},
        {"unexpected": True},
    ],
)
async def test_an_answer_the_contract_does_not_describe_is_a_failure(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    aioclient_mock: AiohttpClientMocker,
    action_entry: MockConfigEntry,
    body: dict[str, Any],
) -> None:
    """An invented state is never shown as an advice."""
    aioclient_mock.get(ADVICE_URL, json=body)
    await start(hass, freezer, action_entry)
    assert hass.states.get(STATE).state == "unavailable"
    assert hass.states.get(RECOMMEND).state == "unavailable"


async def test_works_next_to_sending_and_acts_on_nothing(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    aioclient_mock: AiohttpClientMocker,
    sensors_entry: MockConfigEntry,
) -> None:
    """Reading the advice sends nothing, and the entities offer no way to switch anything."""
    aioclient_mock.get(ADVICE_URL, json=CHARGE)
    aioclient_mock.post(READINGS_URL, status=202, json={})
    await start(hass, freezer, sensors_entry)

    assert [
        method for method, url, *_ in aioclient_mock.mock_calls if str(url) == ADVICE_URL
    ] == ["GET"]
    for domain in ("switch", "button", "number", "select", "climate"):
        assert hass.states.async_entity_ids(domain) == []
    assert not hass.services.has_service("binary_sensor", "turn_on")
    assert set(hass.services.async_services_for_domain("thuisbord")) == {"send_reading"}


async def test_diagnostics_show_the_advice_but_not_the_key(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    aioclient_mock: AiohttpClientMocker,
    action_entry: MockConfigEntry,
) -> None:
    """Only the state and reason are included, never a price or the key."""
    aioclient_mock.get(ADVICE_URL, json=CHARGE)
    await start(hass, freezer, action_entry)
    diagnostics = await async_get_config_entry_diagnostics(hass, action_entry)
    assert diagnostics["battery_advice"] == {
        "available": True,
        "state": "charge",
        "reason": "cheap_now",
    }
    assert SECRET not in repr(diagnostics)
    assert "0.13505" not in repr(diagnostics)


@pytest.mark.parametrize("language", ["nl", "en"])
def test_every_state_and_reason_is_translated(language: str) -> None:
    """The state sensor, its reason attribute and the binary sensor have texts in both languages."""
    root = Path(__file__).parent.parent / "custom_components" / "thuisbord"
    entity = json.loads((root / "translations" / f"{language}.json").read_text())["entity"]
    sensor = entity["sensor"]["battery_advice"]
    assert set(sensor["state"]) == set(ADVICE_STATES)
    assert set(sensor["state_attributes"]["reason"]["state"]) == set(ADVICE_REASONS)
    assert entity["binary_sensor"]["recommend_charge"]["name"]
