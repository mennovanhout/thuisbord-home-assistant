"""Turn sensor states, or the values of an action call, into one Thuisbord reading.

A reading holds the fields of `Reading` in the Thuisbord API contract and nothing more:

- `measured_at`: UTC, whole seconds, with a `Z`.
- `active_power_w`, the only required value: whole watts, positive for import and negative for
  export. From one net power sensor, or from an import sensor minus an export sensor.
- every total (per tariff, combined, solar and gas): a decimal string with exactly three
  decimals, such as "4182.517", so the meter's value arrives exactly. All optional.
- totals per tariff come in pairs: tariff 1 with tariff 2, for import and for export separately.
  When one half is unknown, both halves are left out, because the contract refuses half a pair.
- `active_tariff`: 1 or 2, as the meter numbers it. Optional.
- `solar_power_w`: whole watts, 0 when an inverter reports its standby use as negative.
- a home battery, which Thuisbord shows and never controls, each field optional on its own:
  `battery_power_w` in whole watts, positive while the battery charges and negative while it
  discharges (Thuisbord's sign); `battery_level_pct` and `battery_limit_pct` in whole percent from
  0 to 100; `battery_capacity_kwh` as text with three decimals, above 0 and at most 1000. A battery
  power sensor reads, by Home Assistant's energy dashboard convention, positive while discharging,
  so its sign is turned unless the household says it is positive while charging. The capacity is
  a number the household enters, and goes along only with another battery value.

A value that is unknown or unavailable is left out, never sent as null or 0. Nothing in this
module logs: values never reach the log.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from enum import StrEnum
from typing import Final

from .const import (
    CONF_ACTIVE_TARIFF,
    CONF_BATTERY_LEVEL,
    CONF_BATTERY_LIMIT,
    CONF_BATTERY_POWER,
    CONF_EXPORT_T1,
    CONF_EXPORT_T2,
    CONF_EXPORT_TOTAL,
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

# GridPowerW, SolarPowerW and BatteryPowerW in the contract.
POWER_LIMIT_W: Final = 60000
# EnergyTotalKwh and GasTotalM3: from 0 to 999999.999.
TOTAL_LIMIT: Final = Decimal("999999.999")
THOUSANDTH: Final = Decimal("0.001")
# BatteryPercent: whole percent from 0 to 100.
PERCENT_LIMIT: Final = 100
# BatteryCapacityKwh: above 0 and at most 1000 kWh.
CAPACITY_LIMIT: Final = Decimal(1000)

# The units each kind of sensor may report, and the factor to the contract's unit.
# Decimal factors, not Home Assistant's float converters, so a meter's three decimals arrive
# exactly.
POWER_UNITS: Final = {
    "mW": Decimal("0.001"),
    "W": Decimal(1),
    "kW": Decimal(1000),
    "MW": Decimal(1000000),
}
ENERGY_UNITS: Final = {"Wh": Decimal("0.001"), "kWh": Decimal(1), "MWh": Decimal(1000)}
GAS_UNITS: Final = {"L": Decimal("0.001"), "m³": Decimal(1)}
PERCENT_UNITS: Final = {"%": Decimal(1)}

# The battery's percentages, each from its own sensor.
BATTERY_PERCENTS: Final = (
    (CONF_BATTERY_LEVEL, "battery_level_pct"),
    (CONF_BATTERY_LIMIT, "battery_limit_pct"),
)
# The fields of a battery that are measured; the capacity goes along only with one of them.
BATTERY_MEASURED: Final = ("battery_power_w", "battery_level_pct", "battery_limit_pct")

# How each total sensor maps to a field of the reading.
ENERGY_TOTALS: Final = (
    (CONF_IMPORT_T1, "import_t1_kwh"),
    (CONF_IMPORT_T2, "import_t2_kwh"),
    (CONF_EXPORT_T1, "export_t1_kwh"),
    (CONF_EXPORT_T2, "export_t2_kwh"),
    (CONF_IMPORT_TOTAL, "import_total_kwh"),
    (CONF_EXPORT_TOTAL, "export_total_kwh"),
    (CONF_SOLAR_TOTAL, "solar_total_kwh"),
)
TARIFF_PAIRS: Final = (
    ("import_t1_kwh", "import_t2_kwh"),
    ("export_t1_kwh", "export_t2_kwh"),
)

# The fields of `Reading`, in the contract's order. A reading never holds anything else.
READING_FIELDS: Final = (
    "measured_at",
    "active_power_w",
    "import_t1_kwh",
    "import_t2_kwh",
    "export_t1_kwh",
    "export_t2_kwh",
    "import_total_kwh",
    "export_total_kwh",
    "active_tariff",
    "solar_power_w",
    "solar_total_kwh",
    "gas_total_m3",
    "battery_power_w",
    "battery_level_pct",
    "battery_limit_pct",
    "battery_capacity_kwh",
)

TARIFF_WORDS: Final = {"1": 1, "low": 1, "2": 2, "normal": 2}

type Reading = dict[str, str | int]


class Reason(StrEnum):
    """Why a chosen sensor cannot be used."""

    MISSING = "missing"
    UNIT = "unit"
    OUT_OF_RANGE = "out_of_range"
    UNKNOWN_VALUE = "unknown_value"


@dataclass(frozen=True, slots=True)
class SensorState:
    """The state of a chosen sensor: its value as text and its unit."""

    value: str
    unit: str | None


@dataclass(frozen=True, slots=True)
class Problem:
    """A chosen sensor the household needs to look at."""

    sensor: str
    reason: Reason


@dataclass(frozen=True, slots=True)
class Built:
    """The result of reading the sensors.

    `reading` is None when nothing can be sent now: power is unknown, or a power sensor has a
    problem. `problems` names the sensors that need attention; an optional sensor with a problem
    is left out of the reading.
    """

    reading: Reading | None
    problems: tuple[Problem, ...]


class InvalidReading(ValueError):
    """An action call that cannot become a reading. Names the field, never the value."""

    def __init__(self, field: str, code: str) -> None:
        """Name the field and the reason."""
        super().__init__(f"{field}: {code}")
        self.field = field
        self.code = code


def format_measured_at(moment: datetime) -> str:
    """Return the moment in UTC, whole seconds, with a `Z`."""
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _number(text: str) -> Decimal | None:
    """Return the state as a finite number, or None when it is not one."""
    try:
        number = Decimal(text.strip())
    except InvalidOperation:
        return None
    return number if number.is_finite() else None


def _format_total(value: Decimal) -> str | None:
    """Return a total as text with exactly three decimals, or None when out of range."""
    rounded = value.quantize(THOUSANDTH, rounding=ROUND_HALF_UP)
    if rounded < 0 or rounded > TOTAL_LIMIT:
        return None
    if rounded == 0:
        rounded = Decimal("0.000")
    return f"{rounded:.3f}"


def _watts(value: Decimal) -> int:
    return int(value.quantize(Decimal(1), rounding=ROUND_HALF_UP))


def _percent(value: Decimal) -> int | None:
    """Return a percentage in whole percent, or None when it is not from 0 to 100 once rounded."""
    rounded = _watts(value)
    return rounded if 0 <= rounded <= PERCENT_LIMIT else None


def format_capacity(value: Decimal) -> str | None:
    """Return a capacity as text with three decimals, or None unless above 0 and at most 1000."""
    rounded = value.quantize(THOUSANDTH, rounding=ROUND_HALF_UP)
    if rounded <= 0 or rounded > CAPACITY_LIMIT:
        return None
    return f"{rounded:.3f}"


def battery_watts(value: Decimal, sign: str) -> int:
    """Return a battery sensor's power in Thuisbord's sign: positive while charging.

    `sign` says how the sensor reads: SIGN_DISCHARGING_POSITIVE, the energy dashboard's standard
    and the default, is turned; SIGN_CHARGING_POSITIVE is sent as it is.
    """
    watts = _watts(value)
    return watts if sign == SIGN_CHARGING_POSITIVE else -watts


class _Reader:
    """Reads the chosen sensors and collects problems."""

    def __init__(self, states: Mapping[str, SensorState | None]) -> None:
        self.states = states
        self.problems: list[Problem] = []

    def value(self, sensor: str, units: Mapping[str, Decimal]) -> Decimal | None:
        """Return the sensor's value in the contract's unit, or None when it has none now.

        A sensor that is not chosen is absent from `states`; one that no longer exists maps to
        None.
        """
        if sensor not in self.states:
            return None
        state = self.states[sensor]
        if state is None:
            self.problems.append(Problem(sensor, Reason.MISSING))
            return None
        number = _number(state.value)
        if number is None:
            return None
        factor = units.get(state.unit or "")
        if factor is None:
            self.problems.append(Problem(sensor, Reason.UNIT))
            return None
        return number * factor

    def tariff(self) -> int | None:
        if CONF_ACTIVE_TARIFF not in self.states:
            return None
        state = self.states[CONF_ACTIVE_TARIFF]
        if state is None:
            self.problems.append(Problem(CONF_ACTIVE_TARIFF, Reason.MISSING))
            return None
        raw = state.value.strip().lower()
        if raw in ("unknown", "unavailable", ""):
            return None
        if raw in TARIFF_WORDS:
            return TARIFF_WORDS[raw]
        number = _number(raw)
        if number is not None and number in (1, 2):
            return int(number)
        self.problems.append(Problem(CONF_ACTIVE_TARIFF, Reason.UNKNOWN_VALUE))
        return None


def build_reading(
    measured_at: datetime,
    states: Mapping[str, SensorState | None],
    *,
    battery_sign: str = SIGN_DISCHARGING_POSITIVE,
    battery_capacity: float | Decimal | None = None,
) -> Built:
    """Build one reading from the states of the chosen sensors.

    `states` holds an entry for every chosen sensor: its state, or None when the entity no longer
    exists. Sensors that were not chosen are absent. `battery_sign` says how the battery power
    sensor reads; `battery_capacity` is the capacity in kWh the household entered, if any.
    """
    reader = _Reader(states)

    # Grid power: one net sensor, or import minus export.
    if CONF_POWER in states:
        parts = ((CONF_POWER, 1),)
    else:
        parts = ((CONF_POWER_IMPORT, 1), (CONF_POWER_EXPORT, -1))
    watts = Decimal(0)
    power_known = True
    for sensor, sign in parts:
        value = reader.value(sensor, POWER_UNITS)
        if value is None:
            power_known = False
            continue
        watts += sign * value
    power: int | None = None
    if power_known:
        power = _watts(watts)
        if abs(power) > POWER_LIMIT_W:
            reader.problems.append(Problem(parts[0][0], Reason.OUT_OF_RANGE))
            power = None

    reading: Reading = {}
    for sensor, field in ENERGY_TOTALS:
        value = reader.value(sensor, ENERGY_UNITS)
        if value is None:
            continue
        text = _format_total(value)
        if text is None:
            reader.problems.append(Problem(sensor, Reason.OUT_OF_RANGE))
            continue
        reading[field] = text
    for first, second in TARIFF_PAIRS:
        if (first in reading) != (second in reading):
            reading.pop(first, None)
            reading.pop(second, None)

    gas = reader.value(CONF_GAS_TOTAL, GAS_UNITS)
    if gas is not None:
        text = _format_total(gas)
        if text is None:
            reader.problems.append(Problem(CONF_GAS_TOTAL, Reason.OUT_OF_RANGE))
        else:
            reading["gas_total_m3"] = text

    tariff = reader.tariff()
    if tariff is not None:
        reading["active_tariff"] = tariff

    solar = reader.value(CONF_SOLAR_POWER, POWER_UNITS)
    if solar is not None:
        solar_w = max(_watts(solar), 0)
        if solar_w > POWER_LIMIT_W:
            reader.problems.append(Problem(CONF_SOLAR_POWER, Reason.OUT_OF_RANGE))
        else:
            reading["solar_power_w"] = solar_w

    battery = reader.value(CONF_BATTERY_POWER, POWER_UNITS)
    if battery is not None:
        battery_w = battery_watts(battery, battery_sign)
        if abs(battery_w) > POWER_LIMIT_W:
            reader.problems.append(Problem(CONF_BATTERY_POWER, Reason.OUT_OF_RANGE))
        else:
            reading["battery_power_w"] = battery_w
    for sensor, field in BATTERY_PERCENTS:
        value = reader.value(sensor, PERCENT_UNITS)
        if value is None:
            continue
        percent = _percent(value)
        if percent is None:
            reader.problems.append(Problem(sensor, Reason.OUT_OF_RANGE))
        else:
            reading[field] = percent
    if battery_capacity is not None and any(field in reading for field in BATTERY_MEASURED):
        capacity = _number(str(battery_capacity))
        text = None if capacity is None else format_capacity(capacity)
        if text is not None:
            reading["battery_capacity_kwh"] = text

    problems = tuple(reader.problems)
    if power is None:
        return Built(None, problems)
    reading["measured_at"] = format_measured_at(measured_at)
    reading["active_power_w"] = power
    return Built(_ordered(reading), problems)


def reading_from_values(measured_at: datetime, values: Mapping[str, object]) -> Reading:
    """Build one reading from the fields of an action call.

    `values` uses the reading's own field names, and the battery's power is already in Thuisbord's
    sign, positive while charging. Raises InvalidReading, naming the field, when a value cannot be
    sent; never silently changes what the household asked to send, except that totals and the
    capacity are written with three decimals, percentages are rounded to whole percent, and a
    negative solar power becomes 0.
    """
    reading: Reading = {"measured_at": format_measured_at(measured_at)}

    power = _number(str(values.get("active_power_w", "")))
    if power is None:
        raise InvalidReading("active_power_w", "required")
    watts = _watts(power)
    if abs(watts) > POWER_LIMIT_W:
        raise InvalidReading("active_power_w", "out_of_range")
    reading["active_power_w"] = watts

    for field in (*(field for _, field in ENERGY_TOTALS), "gas_total_m3"):
        if values.get(field) is None:
            continue
        number = _number(str(values[field]))
        text = None if number is None else _format_total(number)
        if text is None:
            raise InvalidReading(field, "out_of_range")
        reading[field] = text
    for first, second in TARIFF_PAIRS:
        if first in reading and second not in reading:
            raise InvalidReading(second, "tariff_pair_incomplete")
        if second in reading and first not in reading:
            raise InvalidReading(first, "tariff_pair_incomplete")

    if values.get("active_tariff") is not None:
        tariff = _number(str(values["active_tariff"]))
        if tariff not in (1, 2):
            raise InvalidReading("active_tariff", "out_of_range")
        reading["active_tariff"] = int(tariff)

    if values.get("solar_power_w") is not None:
        solar = _number(str(values["solar_power_w"]))
        if solar is None:
            raise InvalidReading("solar_power_w", "out_of_range")
        solar_w = max(_watts(solar), 0)
        if solar_w > POWER_LIMIT_W:
            raise InvalidReading("solar_power_w", "out_of_range")
        reading["solar_power_w"] = solar_w

    if values.get("battery_power_w") is not None:
        battery = _number(str(values["battery_power_w"]))
        if battery is None or abs(_watts(battery)) > POWER_LIMIT_W:
            raise InvalidReading("battery_power_w", "out_of_range")
        reading["battery_power_w"] = _watts(battery)
    for field in ("battery_level_pct", "battery_limit_pct"):
        if values.get(field) is None:
            continue
        number = _number(str(values[field]))
        percent = None if number is None else _percent(number)
        if percent is None:
            raise InvalidReading(field, "out_of_range")
        reading[field] = percent
    if values.get("battery_capacity_kwh") is not None:
        number = _number(str(values["battery_capacity_kwh"]))
        text = None if number is None else format_capacity(number)
        if text is None:
            raise InvalidReading("battery_capacity_kwh", "out_of_range")
        reading["battery_capacity_kwh"] = text

    return _ordered(reading)


def _ordered(reading: Reading) -> Reading:
    """Return the reading with its fields in the contract's order, and only those."""
    return {field: reading[field] for field in READING_FIELDS if field in reading}
