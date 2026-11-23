"""Fixtures for the Thuisbord tests.

The connection key below is a made-up test value in the contract's shape. It is not a key for
any household.
"""

from __future__ import annotations

from collections.abc import Generator
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.thuisbord.api import CannotConnect
from custom_components.thuisbord.const import (
    CONF_API_URL,
    CONF_CONNECTION_KEY,
    CONF_MODE,
    CONF_POWER,
    DEFAULT_API_URL,
    DOMAIN,
    MODE_ACTION,
    MODE_SENSORS,
)

HOUSEHOLD = "0199b6a2-7c22-7e10-8f4b-5a6c7d8e9f01"
SECRET = "T3st-only_secret-not-a-real-key-0123456789AB"
KEY = f"thb_{HOUSEHOLD}.{SECRET}"
OTHER_KEY = f"thb_{HOUSEHOLD}.{'R' * 43}"
OTHER_HOUSEHOLD_KEY = f"thb_0199b6a2-0000-7e10-8f4b-5a6c7d8e9f02.{SECRET}"

CONNECTION_URL = f"{DEFAULT_API_URL}/connection"
READINGS_URL = f"{DEFAULT_API_URL}/readings"
ADVICE_URL = f"{DEFAULT_API_URL}/battery-advice"
CONNECTION_OK = {"max_readings": 360, "min_interval_seconds": 10}

POWER_ENTITY = "sensor.p1_meter_power"


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations: None) -> Generator[None]:
    """Load the integration from custom_components."""
    yield


@pytest.fixture(autouse=True)
def no_advice_request(request: pytest.FixtureRequest) -> Generator[None]:
    """Reading the battery advice never reaches the network in tests that are not about it.

    Thuisbord cannot be reached there, which the integration must survive. tests/test_advice.py
    answers the request itself.
    """
    if request.module.__name__.endswith("test_advice"):
        yield
        return
    with patch(
        "custom_components.thuisbord.api.ThuisbordApi.get_battery_advice",
        AsyncMock(side_effect=CannotConnect),
    ):
        yield


def accepted(next_allowed_at: str, received: int = 1) -> dict[str, Any]:
    """A 202 body from POST /readings."""
    return {
        "received": received,
        "skipped": 0,
        "stored": received,
        "next_allowed_at": next_allowed_at,
    }


@pytest.fixture
def sensors_entry(hass: HomeAssistant) -> MockConfigEntry:
    """An entry that reads one net power sensor."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="Thuisbord",
        unique_id=HOUSEHOLD,
        data={CONF_CONNECTION_KEY: KEY, CONF_API_URL: DEFAULT_API_URL},
        options={CONF_MODE: MODE_SENSORS, CONF_POWER: POWER_ENTITY},
    )
    entry.add_to_hass(hass)
    return entry


@pytest.fixture
def action_entry(hass: HomeAssistant) -> MockConfigEntry:
    """An entry that takes readings from the action."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="Thuisbord",
        unique_id=HOUSEHOLD,
        data={CONF_CONNECTION_KEY: KEY, CONF_API_URL: DEFAULT_API_URL},
        options={CONF_MODE: MODE_ACTION},
    )
    entry.add_to_hass(hass)
    return entry
