"""Turning sensor states and action values into readings."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from custom_components.thuisbord.const import (
    CONF_ACTIVE_TARIFF,
    CONF_BATTERY_LEVEL,
    CONF_BATTERY_LIMIT,
    CONF_BATTERY_POWER,
    CONF_EXPORT_T1,
    CONF_EXPORT_T2,
    CONF_GAS_TOTAL,
    CONF_IMPORT_T1,
    CONF_IMPORT_T2,
    CONF_IMPORT_TOTAL,
    CONF_POWER,
    CONF_POWER_EXPORT,
    CONF_POWER_IMPORT,
    CONF_SOLAR_POWER,
    CONF_SOLAR_TOTAL,
    SIGN_CHARGING_POSITIVE,
    SIGN_DISCHARGING_POSITIVE,
)
from custom_components.thuisbord.reading import (
    READING_FIELDS,
    InvalidReading,
    Problem,
    Reason,
    SensorState,
    build_reading,
    reading_from_values,
)

POWER_840 = SensorState("840", "W")

AT = datetime(2026, 10, 6, 21, 15, 40, 250000, tzinfo=UTC)


def test_only_power() -> None:
    """Power alone is a reading; nothing else is added."""
    built = build_reading(AT, {CONF_POWER: SensorState("840", "W")})
    assert built.reading == {"measured_at": "2026-10-06T21:15:40Z", "active_power_w": 840}
    assert built.problems == ()


def test_full_reading_matches_the_contract_example() -> None:
    """Every field, written as the contract asks: totals as text with three decimals."""
    built = build_reading(
        AT,
        {
            CONF_POWER: SensorState("-1.25", "kW"),
            CONF_IMPORT_T1: SensorState("4182.517", "kWh"),
            CONF_IMPORT_T2: SensorState("3920044", "Wh"),
            CONF_EXPORT_T1: SensorState("1022.301", "kWh"),
            CONF_EXPORT_T2: SensorState("2.410871", "MWh"),
            CONF_ACTIVE_TARIFF: SensorState("2", None),
            CONF_SOLAR_POWER: SensorState("3400", "W"),
            CONF_SOLAR_TOTAL: SensorState("9875.21", "kWh"),
            CONF_GAS_TOTAL: SensorState("2911.408", "m³"),
        },
    )
    assert built.problems == ()
    assert built.reading == {
        "measured_at": "2026-10-06T21:15:40Z",
        "active_power_w": -1250,
        "import_t1_kwh": "4182.517",
        "import_t2_kwh": "3920.044",
        "export_t1_kwh": "1022.301",
        "export_t2_kwh": "2410.871",
        "active_tariff": 2,
        "solar_power_w": 3400,
        "solar_total_kwh": "9875.210",
        "gas_total_m3": "2911.408",
    }
    assert list(built.reading) == [f for f in READING_FIELDS if f in built.reading]


def test_import_minus_export() -> None:
    """Without a net sensor, power is import minus export, rounded to whole watts."""
    built = build_reading(
        AT,
        {
            CONF_POWER_IMPORT: SensorState("0.0", "kW"),
            CONF_POWER_EXPORT: SensorState("1.2345", "kW"),
        },
    )
    # 0 W - 1234.5 W = -1234.5 W, rounded half away from zero: -1235 W.
    assert built.reading is not None
    assert built.reading["active_power_w"] == -1235


@pytest.mark.parametrize("value", ["unknown", "unavailable", "", "abc", "nan", "inf"])
def test_power_without_a_value_sends_nothing(value: str) -> None:
    """Power is required: without it nothing is sent, and it is not a problem yet."""
    built = build_reading(AT, {CONF_POWER: SensorState(value, "W")})
    assert built.reading is None
    assert built.problems == ()


def test_power_sensor_gone() -> None:
    """A chosen power sensor that no longer exists is a problem and sends nothing."""
    built = build_reading(AT, {CONF_POWER: None})
    assert built.reading is None
    assert built.problems == (Problem(CONF_POWER, Reason.MISSING),)


def test_power_unit_unknown() -> None:
    """A power unit Thuisbord cannot convert is a problem."""
    built = build_reading(AT, {CONF_POWER: SensorState("840", "BTU/h")})
    assert built.reading is None
    assert built.problems == (Problem(CONF_POWER, Reason.UNIT),)


def test_power_out_of_range() -> None:
    """More than 60 kW is no household meter: nothing is sent."""
    built = build_reading(AT, {CONF_POWER: SensorState("60.001", "kW")})
    assert built.reading is None
    assert built.problems == (Problem(CONF_POWER, Reason.OUT_OF_RANGE),)


def test_half_a_tariff_pair_is_left_out() -> None:
    """When tariff 2 is unknown, tariff 1 is left out too; the combined total still goes."""
    built = build_reading(
        AT,
        {
            CONF_POWER: SensorState("840", "W"),
            CONF_IMPORT_T1: SensorState("4182.517", "kWh"),
            CONF_IMPORT_T2: SensorState("unavailable", "kWh"),
            CONF_IMPORT_TOTAL: SensorState("8102.561", "kWh"),
        },
    )
    assert built.reading == {
        "measured_at": "2026-10-06T21:15:40Z",
        "active_power_w": 840,
        "import_total_kwh": "8102.561",
    }


@pytest.mark.parametrize(
    ("raw", "tariff"),
    [("1", 1), ("2", 2), ("low", 1), ("Normal", 2), ("1.0", 1), ("2.0", 2)],
)
def test_tariff_words(raw: str, tariff: int) -> None:
    """The tariff as HomeWizard (1, 2) and DSMR (low, normal) report it."""
    built = build_reading(
        AT, {CONF_POWER: SensorState("1", "W"), CONF_ACTIVE_TARIFF: SensorState(raw, None)}
    )
    assert built.reading is not None
    assert built.reading["active_tariff"] == tariff


def test_tariff_unknown_value() -> None:
    """Another tariff value is left out and becomes a problem; the reading still goes."""
    built = build_reading(
        AT, {CONF_POWER: SensorState("1", "W"), CONF_ACTIVE_TARIFF: SensorState("3", None)}
    )
    assert built.reading is not None
    assert "active_tariff" not in built.reading
    assert built.problems == (Problem(CONF_ACTIVE_TARIFF, Reason.UNKNOWN_VALUE),)


def test_negative_solar_becomes_zero() -> None:
    """An inverter reporting its standby use as negative power sends 0."""
    built = build_reading(
        AT, {CONF_POWER: SensorState("1", "W"), CONF_SOLAR_POWER: SensorState("-3", "W")}
    )
    assert built.reading is not None
    assert built.reading["solar_power_w"] == 0


def test_gas_in_litres() -> None:
    """Gas in litres is converted to m³ exactly."""
    built = build_reading(
        AT, {CONF_POWER: SensorState("1", "W"), CONF_GAS_TOTAL: SensorState("2911408", "L")}
    )
    assert built.reading is not None
    assert built.reading["gas_total_m3"] == "2911.408"


def test_negative_total_is_a_problem() -> None:
    """A negative total cannot be a meter register: left out, and named."""
    built = build_reading(
        AT,
        {CONF_POWER: SensorState("1", "W"), CONF_SOLAR_TOTAL: SensorState("-1", "kWh")},
    )
    assert built.reading is not None
    assert "solar_total_kwh" not in built.reading
    assert built.problems == (Problem(CONF_SOLAR_TOTAL, Reason.OUT_OF_RANGE),)


def test_zero_total() -> None:
    """Zero is written as 0.000, never with a sign."""
    built = build_reading(
        AT,
        {
            CONF_POWER: SensorState("1", "W"),
            CONF_EXPORT_T1: SensorState("-0.0001", "kWh"),
            CONF_EXPORT_T2: SensorState("0", "kWh"),
        },
    )
    assert built.reading is not None
    assert built.reading["export_t1_kwh"] == "0.000"
    assert built.reading["export_t2_kwh"] == "0.000"


def test_reading_fields_end_with_the_battery() -> None:
    """The battery's four fields follow gas, in the contract's order (0.11.0)."""
    assert READING_FIELDS[-5:] == (
        "gas_total_m3",
        "battery_power_w",
        "battery_level_pct",
        "battery_limit_pct",
        "battery_capacity_kwh",
    )


def test_battery_in_the_energy_dashboard_sign() -> None:
    """A battery power sensor that is positive while discharging, the default, is turned.

    The contract's 'battery' example: the panels charge the battery at 2200 W, which such a sensor
    reports as -2.2 kW. Thuisbord's sign is positive while charging.
    """
    built = build_reading(
        AT,
        {
            CONF_POWER: SensorState("-420", "W"),
            CONF_BATTERY_POWER: SensorState("-2.2", "kW"),
            CONF_BATTERY_LEVEL: SensorState("64.4", "%"),
            CONF_BATTERY_LIMIT: SensorState("20", "%"),
        },
        battery_capacity=10.0,
    )
    assert built.problems == ()
    assert built.reading == {
        "measured_at": "2026-10-06T21:15:40Z",
        "active_power_w": -420,
        "battery_power_w": 2200,
        "battery_level_pct": 64,
        "battery_limit_pct": 20,
        "battery_capacity_kwh": "10.000",
    }
    assert list(built.reading) == [f for f in READING_FIELDS if f in built.reading]


@pytest.mark.parametrize(
    ("sign", "raw", "watts"),
    [
        (SIGN_DISCHARGING_POSITIVE, "1450", -1450),
        (SIGN_DISCHARGING_POSITIVE, "-2200", 2200),
        (SIGN_CHARGING_POSITIVE, "2200", 2200),
        (SIGN_CHARGING_POSITIVE, "-1450", -1450),
        (SIGN_DISCHARGING_POSITIVE, "0", 0),
        (SIGN_DISCHARGING_POSITIVE, "-0.5", 1),
        (SIGN_CHARGING_POSITIVE, "-0.5", -1),
    ],
)
def test_battery_power_sign(sign: str, raw: str, watts: int) -> None:
    """The sign is turned unless the sensor is positive while charging; rounding is symmetric."""
    built = build_reading(AT, {CONF_POWER: POWER_840, CONF_BATTERY_POWER: SensorState(raw, "W")}, battery_sign=sign)
    assert built.reading is not None
    assert built.reading["battery_power_w"] == watts


def test_battery_capacity_only_with_a_battery_value() -> None:
    """The capacity goes along with a measured battery value, never on its own."""
    states = {
        CONF_POWER: POWER_840,
        CONF_BATTERY_LEVEL: SensorState("unavailable", "%"),
        CONF_BATTERY_POWER: SensorState("unknown", "W"),
    }
    built = build_reading(AT, states, battery_capacity=13.5)
    assert built.reading == {"measured_at": "2026-10-06T21:15:40Z", "active_power_w": 840}
    assert built.problems == ()

    states[CONF_BATTERY_LEVEL] = SensorState("99.5", "%")
    built = build_reading(AT, states, battery_capacity=13.5)
    assert built.reading is not None
    assert built.reading["battery_level_pct"] == 100
    assert built.reading["battery_capacity_kwh"] == "13.500"


@pytest.mark.parametrize(
    ("sensor", "state", "reason"),
    [
        (CONF_BATTERY_LEVEL, SensorState("100.5", "%"), Reason.OUT_OF_RANGE),
        (CONF_BATTERY_LIMIT, SensorState("-1", "%"), Reason.OUT_OF_RANGE),
        (CONF_BATTERY_POWER, SensorState("60.001", "kW"), Reason.OUT_OF_RANGE),
        (CONF_BATTERY_LEVEL, SensorState("64", "kWh"), Reason.UNIT),
        (CONF_BATTERY_POWER, SensorState("2200", "VA"), Reason.UNIT),
    ],
)
def test_unusable_battery_value_is_left_out(sensor: str, state: SensorState, reason: Reason) -> None:
    """A battery value that cannot be right is left out and named; power still goes."""
    built = build_reading(AT, {CONF_POWER: POWER_840, sensor: state}, battery_capacity=10)
    assert built.reading == {"measured_at": "2026-10-06T21:15:40Z", "active_power_w": 840}
    assert built.problems == (Problem(sensor, reason),)


def test_action_battery_values() -> None:
    """The action takes the battery in Thuisbord's sign, rounded to whole watts and percent."""
    reading = reading_from_values(
        AT,
        {
            "active_power_w": 120,
            "battery_power_w": -1450.4,
            "battery_level_pct": "64.5",
            "battery_limit_pct": 20,
            "battery_capacity_kwh": Decimal("13.5"),
        },
    )
    assert reading == {
        "measured_at": "2026-10-06T21:15:40Z",
        "active_power_w": 120,
        "battery_power_w": -1450,
        "battery_level_pct": 65,
        "battery_limit_pct": 20,
        "battery_capacity_kwh": "13.500",
    }


def test_action_values() -> None:
    """Numbers from the automation editor become the contract's fields."""
    reading = reading_from_values(
        AT,
        {
            "active_power_w": 840.4,
            "import_t1_kwh": 4183.102,
            "import_t2_kwh": "3921.95",
            "active_tariff": "1",
            "solar_power_w": -2,
            "gas_total_m3": Decimal("2911.408"),
        },
    )
    assert reading == {
        "measured_at": "2026-10-06T21:15:40Z",
        "active_power_w": 840,
        "import_t1_kwh": "4183.102",
        "import_t2_kwh": "3921.950",
        "active_tariff": 1,
        "solar_power_w": 0,
        "gas_total_m3": "2911.408",
    }


@pytest.mark.parametrize(
    ("values", "field", "code"),
    [
        ({}, "active_power_w", "required"),
        ({"active_power_w": 60001}, "active_power_w", "out_of_range"),
        ({"active_power_w": 1, "export_t2_kwh": 1}, "export_t1_kwh", "tariff_pair_incomplete"),
        ({"active_power_w": 1, "import_t1_kwh": 1}, "import_t2_kwh", "tariff_pair_incomplete"),
        ({"active_power_w": 1, "gas_total_m3": -1}, "gas_total_m3", "out_of_range"),
        ({"active_power_w": 1, "active_tariff": 3}, "active_tariff", "out_of_range"),
        ({"active_power_w": 1, "battery_power_w": 60001}, "battery_power_w", "out_of_range"),
        ({"active_power_w": 1, "battery_level_pct": 100.5}, "battery_level_pct", "out_of_range"),
        ({"active_power_w": 1, "battery_limit_pct": -1}, "battery_limit_pct", "out_of_range"),
        ({"active_power_w": 1, "battery_capacity_kwh": 0}, "battery_capacity_kwh", "out_of_range"),
        ({"active_power_w": 1, "battery_capacity_kwh": "0.0004"}, "battery_capacity_kwh", "out_of_range"),
        ({"active_power_w": 1, "battery_capacity_kwh": 1000.001}, "battery_capacity_kwh", "out_of_range"),
    ],
)
def test_action_values_refused(values: dict, field: str, code: str) -> None:
    """An action call that cannot become a reading names the field, never the value."""
    with pytest.raises(InvalidReading) as err:
        reading_from_values(AT, values)
    assert (err.value.field, err.value.code) == (field, code)
