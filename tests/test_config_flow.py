"""The config, reauth, reconfigure and options flows."""

from __future__ import annotations

from typing import Any
from unittest.mock import patch

from aiohttp import ClientError
import pytest

from homeassistant.config_entries import SOURCE_USER
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.test_util.aiohttp import AiohttpClientMocker

from custom_components.thuisbord.const import (
    CONF_API_URL,
    CONF_CONNECTION_KEY,
    CONF_EXPORT_T1,
    CONF_IMPORT_T1,
    CONF_IMPORT_T2,
    CONF_MODE,
    CONF_POWER,
    CONF_POWER_EXPORT,
    CONF_POWER_IMPORT,
    DEFAULT_API_URL,
    DOMAIN,
    MODE_ACTION,
    MODE_SENSORS,
)

from .conftest import (
    CONNECTION_OK,
    CONNECTION_URL,
    HOUSEHOLD,
    KEY,
    OTHER_HOUSEHOLD_KEY,
    OTHER_KEY,
    POWER_ENTITY,
)

ADVANCED = {"advanced": {CONF_API_URL: DEFAULT_API_URL}}


def sensors_input(**chosen: str) -> dict[str, Any]:
    """The sensors form as the frontend sends it, with its three sections."""
    power = {k: v for k, v in chosen.items() if k in (CONF_POWER, CONF_POWER_IMPORT, CONF_POWER_EXPORT)}
    extra = {k: v for k, v in chosen.items() if k in ("solar_power", "solar_total", "gas_total")}
    totals = {k: v for k, v in chosen.items() if k not in power and k not in extra}
    return {"power_section": power, "totals_section": totals, "extra_section": extra}


@pytest.fixture(autouse=True)
def power_sensor(hass: HomeAssistant) -> None:
    """A P1 meter's net power sensor and an energy total."""
    hass.states.async_set(POWER_ENTITY, "840", {"unit_of_measurement": "W"})
    hass.states.async_set("sensor.p1_import_t1", "4182.517", {"unit_of_measurement": "kWh"})
    hass.states.async_set("sensor.p1_import_t2", "3920.044", {"unit_of_measurement": "kWh"})


async def _to_menu(hass: HomeAssistant, aioclient_mock: AiohttpClientMocker) -> dict[str, Any]:
    aioclient_mock.get(CONNECTION_URL, json=CONNECTION_OK)
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "user"
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_CONNECTION_KEY: f"  {KEY}\n", **ADVANCED}
    )
    assert result["type"] is FlowResultType.MENU
    assert result["step_id"] == "mode"
    return result


async def test_sensors(hass: HomeAssistant, aioclient_mock: AiohttpClientMocker) -> None:
    """The key is checked with GET /connection, then the household picks a power sensor."""
    result = await _to_menu(hass, aioclient_mock)

    # The key went to Thuisbord as a bearer token, trimmed, and nothing else was sent.
    assert aioclient_mock.call_count == 1
    method, url, data, headers = aioclient_mock.mock_calls[0]
    assert (method.upper(), str(url), data) == ("GET", CONNECTION_URL, None)
    assert headers["Authorization"] == f"Bearer {KEY}"
    assert headers["Accept"] == "application/json"

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"next_step_id": MODE_SENSORS}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "sensors"

    with patch("custom_components.thuisbord.async_setup_entry", return_value=True):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            sensors_input(
                power=POWER_ENTITY, import_t1="sensor.p1_import_t1", import_t2="sensor.p1_import_t2"
            ),
        )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "Thuisbord"
    assert result["data"] == {CONF_CONNECTION_KEY: KEY, CONF_API_URL: DEFAULT_API_URL}
    assert result["options"] == {
        CONF_MODE: MODE_SENSORS,
        CONF_POWER: POWER_ENTITY,
        CONF_IMPORT_T1: "sensor.p1_import_t1",
        CONF_IMPORT_T2: "sensor.p1_import_t2",
    }
    assert result["result"].unique_id == HOUSEHOLD


async def test_action(hass: HomeAssistant, aioclient_mock: AiohttpClientMocker) -> None:
    """Choosing the action adds the household without sensors."""
    result = await _to_menu(hass, aioclient_mock)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"next_step_id": MODE_ACTION}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "action"
    with patch("custom_components.thuisbord.async_setup_entry", return_value=True):
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["options"] == {CONF_MODE: MODE_ACTION}


@pytest.mark.parametrize(
    ("mock", "error"),
    [
        ({"status": 401, "json": {"code": "unauthenticated"}}, "invalid_key"),
        ({"status": 403, "json": {"code": "key_revoked"}}, "key_revoked"),
        ({"status": 403, "json": {"code": "consent_not_recorded"}}, "consent_not_recorded"),
        ({"status": 403, "json": {"code": "token_not_allowed"}}, "token_not_allowed"),
        ({"status": 403, "text": "not json"}, "key_refused"),
        ({"status": 429, "json": {"code": "too_many_requests"}}, "too_many_requests"),
        ({"status": 503}, "cannot_connect"),
        ({"status": 404, "json": {"code": "not_found"}}, "unexpected_response"),
        ({"status": 302, "headers": {"Location": "https://elsewhere.example/"}}, "unexpected_response"),
        ({"exc": ClientError()}, "cannot_connect"),
        ({"exc": TimeoutError()}, "cannot_connect"),
    ],
)
async def test_key_refused_then_fixed(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, mock: dict[str, Any], error: str
) -> None:
    """Every answer to the check shows its own error, and the form recovers."""
    aioclient_mock.get(CONNECTION_URL, **mock)
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_CONNECTION_KEY: KEY, **ADVANCED}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": error}

    aioclient_mock.clear_requests()
    aioclient_mock.get(CONNECTION_URL, json=CONNECTION_OK)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_CONNECTION_KEY: KEY, **ADVANCED}
    )
    assert result["type"] is FlowResultType.MENU


@pytest.mark.parametrize(
    "key",
    [
        "",
        "thb_",
        f"thb_{HOUSEHOLD}",
        f"thb_{HOUSEHOLD}.short",
        f"thb_{HOUSEHOLD.upper()}.{'A' * 43}",
        f"tb_{HOUSEHOLD}.{'A' * 43}",
        f"thb_{HOUSEHOLD}.{'A' * 42}=",
    ],
)
async def test_key_format(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, key: str
) -> None:
    """A paste error shows at once, and the key never leaves Home Assistant."""
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_CONNECTION_KEY: key, **ADVANCED}
    )
    assert result["errors"] == {CONF_CONNECTION_KEY: "invalid_key_format"}
    assert aioclient_mock.call_count == 0


@pytest.mark.parametrize(
    ("url", "error"),
    [
        ("http://thuisbord.app/api/v1", "insecure_url"),
        ("http://203.0.113.5/api/v1", "insecure_url"),
        ("ftp://thuisbord.app/api/v1", "invalid_url"),
        ("https://thuisbord.app/api/v1?x=1", "invalid_url"),
        ("https://user:pw@thuisbord.app/api/v1", "invalid_url"),
        ("thuisbord.app", "invalid_url"),
    ],
)
async def test_server_address_refused(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, url: str, error: str
) -> None:
    """The key only goes over HTTPS, or plain HTTP to a server in the home."""
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_CONNECTION_KEY: KEY, "advanced": {CONF_API_URL: url}}
    )
    assert result["errors"] == {"base": error}
    assert aioclient_mock.call_count == 0


@pytest.mark.parametrize(
    "url",
    [
        "http://host.docker.internal:8000/api/v1",
        "http://localhost:8000/api/v1/",
        "http://192.168.1.20/api/v1",
        "http://thuisbord.test/api/v1",
    ],
)
async def test_local_server(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, url: str
) -> None:
    """A local Thuisbord back-end may use plain HTTP, for testing."""
    base = url.rstrip("/")
    aioclient_mock.get(f"{base}/connection", json=CONNECTION_OK)
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_CONNECTION_KEY: KEY, "advanced": {CONF_API_URL: url}}
    )
    assert result["type"] is FlowResultType.MENU
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"next_step_id": MODE_ACTION}
    )
    with patch("custom_components.thuisbord.async_setup_entry", return_value=True):
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    assert result["data"][CONF_API_URL] == base


async def test_household_already_added(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, action_entry: MockConfigEntry
) -> None:
    """The same household cannot be added twice, also not with a renewed key."""
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_CONNECTION_KEY: OTHER_KEY, **ADVANCED}
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"
    assert aioclient_mock.call_count == 0


@pytest.mark.parametrize(
    ("chosen", "error"),
    [
        ({}, "power_required"),
        ({CONF_POWER_IMPORT: POWER_ENTITY}, "power_pair_incomplete"),
        ({CONF_POWER: POWER_ENTITY, CONF_IMPORT_T1: "sensor.p1_import_t1"}, "pair_incomplete"),
        ({CONF_POWER: POWER_ENTITY, CONF_EXPORT_T1: "sensor.p1_import_t1"}, "pair_incomplete"),
        ({CONF_POWER: "sensor.p1_import_t1"}, "unsupported_unit"),
    ],
)
async def test_sensor_choice_refused(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    chosen: dict[str, str],
    error: str,
) -> None:
    """Power is required, pairs go together, and units must be ones Thuisbord converts."""
    result = await _to_menu(hass, aioclient_mock)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"next_step_id": MODE_SENSORS}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], sensors_input(**chosen)
    )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": error}


async def test_reauth(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, sensors_entry: MockConfigEntry
) -> None:
    """The reauth form says why at once, and a renewed key for the same household works."""
    result = await sensors_entry.start_reauth_flow(hass, data={"refusal_code": "key_revoked"})
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "reauth_confirm"
    assert result["errors"] == {"base": "key_revoked"}

    aioclient_mock.get(CONNECTION_URL, json=CONNECTION_OK)
    with patch("custom_components.thuisbord.async_setup_entry", return_value=True):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_CONNECTION_KEY: OTHER_KEY}
        )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reauth_successful"
    assert sensors_entry.data[CONF_CONNECTION_KEY] == OTHER_KEY
    assert aioclient_mock.mock_calls[0][3]["Authorization"] == f"Bearer {OTHER_KEY}"


async def test_reauth_still_refused(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, sensors_entry: MockConfigEntry
) -> None:
    """Consent still missing keeps the form open with that reason."""
    aioclient_mock.get(CONNECTION_URL, status=403, json={"code": "consent_not_recorded"})
    result = await sensors_entry.start_reauth_flow(hass)
    assert result["errors"] == {}
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_CONNECTION_KEY: KEY}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "consent_not_recorded"}


async def test_reauth_other_household(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, sensors_entry: MockConfigEntry
) -> None:
    """A key of another household does not replace this one's."""
    result = await sensors_entry.start_reauth_flow(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_CONNECTION_KEY: OTHER_HOUSEHOLD_KEY}
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "wrong_household"
    assert sensors_entry.data[CONF_CONNECTION_KEY] == KEY
    assert aioclient_mock.call_count == 0


async def test_reconfigure(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, sensors_entry: MockConfigEntry
) -> None:
    """A renewed key and another server address can be pasted at any time."""
    local = "http://host.docker.internal:8000/api/v1"
    aioclient_mock.get(f"{local}/connection", json=CONNECTION_OK)
    result = await sensors_entry.start_reconfigure_flow(hass)
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "reconfigure"
    with patch("custom_components.thuisbord.async_setup_entry", return_value=True):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_CONNECTION_KEY: OTHER_KEY, "advanced": {CONF_API_URL: local}}
        )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reconfigure_successful"
    assert sensors_entry.data == {CONF_CONNECTION_KEY: OTHER_KEY, CONF_API_URL: local}
    assert sensors_entry.options[CONF_POWER] == POWER_ENTITY


async def test_options_change_sensors(
    hass: HomeAssistant, sensors_entry: MockConfigEntry
) -> None:
    """The options suggest the current sensors and store the new choice."""
    result = await hass.config_entries.options.async_init(sensors_entry.entry_id)
    assert result["type"] is FlowResultType.MENU
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": MODE_SENSORS}
    )
    assert result["type"] is FlowResultType.FORM
    schema = result["data_schema"].schema
    power_section = next(v for k, v in schema.items() if k == "power_section")
    suggested = {
        str(key): key.description.get("suggested_value")
        for key in power_section.schema.schema
        if key.description
    }
    assert suggested == {CONF_POWER: POWER_ENTITY}

    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        sensors_input(
            power=POWER_ENTITY, import_t1="sensor.p1_import_t1", import_t2="sensor.p1_import_t2"
        ),
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert sensors_entry.options == {
        CONF_MODE: MODE_SENSORS,
        CONF_POWER: POWER_ENTITY,
        CONF_IMPORT_T1: "sensor.p1_import_t1",
        CONF_IMPORT_T2: "sensor.p1_import_t2",
    }


async def test_options_switch_to_action(
    hass: HomeAssistant, sensors_entry: MockConfigEntry
) -> None:
    """Switching to the action forgets the sensors."""
    result = await hass.config_entries.options.async_init(sensors_entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": MODE_ACTION}
    )
    result = await hass.config_entries.options.async_configure(result["flow_id"], {})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert sensors_entry.options == {CONF_MODE: MODE_ACTION}
