"""Read the chosen sensors every 10 seconds and offer a reading to the sender.

A reading is made at every whole 10 seconds of the clock (:00, :10, ... :50), but only when a
power sensor has reported something since the previous reading, so a meter that stopped
reporting does not keep sending its last value. Its `measured_at` is that moment, in whole
seconds, so two readings are always at least 10 seconds apart.

A chosen sensor that cannot be used (it no longer exists, its unit is unknown, or its value is
impossible) becomes a repair issue after it has stayed that way for 5 minutes, so a sensor that
is still loading after a restart does not raise one. Values never reach the log.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from typing import Final

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import ATTR_UNIT_OF_MEASUREMENT
from homeassistant.core import CALLBACK_TYPE, HomeAssistant, callback
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.event import async_track_utc_time_change

from .const import (
    CONF_POWER,
    CONF_POWER_EXPORT,
    CONF_POWER_IMPORT,
    DOMAIN,
    SIGN_DISCHARGING_POSITIVE,
)
from .reading import Problem, SensorState, build_reading
from .sender import ReadingSender

ISSUE_GRACE_SECONDS: Final = 300


class SensorCollector:
    """Turns the chosen sensors' states into readings."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        sender: ReadingSender,
        sensors: Mapping[str, str],
        *,
        battery_sign: str = SIGN_DISCHARGING_POSITIVE,
        battery_capacity: float | None = None,
    ) -> None:
        """Read `sensors`, a map from the option name to the entity ID.

        `battery_sign` says how the battery power sensor reads, `battery_capacity` is the capacity
        in kWh the household entered; both only matter with battery sensors.
        """
        self._hass = hass
        self._entry = entry
        self._sender = sender
        self._sensors = dict(sensors)
        self._battery_sign = battery_sign
        self._battery_capacity = battery_capacity
        self._power = [
            entity_id
            for key, entity_id in self._sensors.items()
            if key in (CONF_POWER, CONF_POWER_IMPORT, CONF_POWER_EXPORT)
        ]
        self._last_seen: datetime | None = None
        self._problem_since: dict[Problem, datetime] = {}
        self._issues: set[str] = set()

    @callback
    def async_start(self) -> CALLBACK_TYPE:
        """Start reading. Returns a function that stops it and clears its issues."""
        unsubscribe = async_track_utc_time_change(self._hass, self._async_tick, second="/10")

        @callback
        def stop() -> None:
            unsubscribe()
            for issue_id in self._issues:
                ir.async_delete_issue(self._hass, DOMAIN, issue_id)
            self._issues.clear()

        return stop

    @callback
    def _async_tick(self, now: datetime) -> None:
        moment = now.replace(microsecond=0)
        states: dict[str, SensorState | None] = {}
        for key, entity_id in self._sensors.items():
            state = self._hass.states.get(entity_id)
            states[key] = (
                None
                if state is None
                else SensorState(state.state, state.attributes.get(ATTR_UNIT_OF_MEASUREMENT))
            )

        built = build_reading(
            moment,
            states,
            battery_sign=self._battery_sign,
            battery_capacity=self._battery_capacity,
        )
        self._update_issues(built.problems, moment)
        if built.reading is None:
            return

        reported = [
            state.last_reported
            for entity_id in self._power
            if (state := self._hass.states.get(entity_id)) is not None
        ]
        latest = max(reported) if reported else None
        if latest is not None and self._last_seen is not None and latest <= self._last_seen:
            return  # the meter reported nothing new since the previous reading
        self._sender.async_add(built.reading, moment)
        self._last_seen = latest

    def _update_issues(self, problems: tuple[Problem, ...], now: datetime) -> None:
        current = set(problems)
        for problem in list(self._problem_since):
            if problem not in current:
                del self._problem_since[problem]
        for problem in current:
            self._problem_since.setdefault(problem, now)

        wanted: dict[str, Problem] = {}
        for problem, since in self._problem_since.items():
            if (now - since).total_seconds() >= ISSUE_GRACE_SECONDS:
                wanted[f"{self._entry.entry_id}_{problem.sensor}_{problem.reason}"] = problem

        for issue_id in self._issues - wanted.keys():
            ir.async_delete_issue(self._hass, DOMAIN, issue_id)
        for issue_id in wanted.keys() - self._issues:
            problem = wanted[issue_id]
            ir.async_create_issue(
                self._hass,
                DOMAIN,
                issue_id,
                is_fixable=False,
                is_persistent=False,
                severity=ir.IssueSeverity.WARNING,
                translation_key=f"sensor_{problem.reason}",
                translation_placeholders={
                    "entity_id": self._sensors[problem.sensor],
                    "title": self._entry.title,
                },
            )
        self._issues = set(wanted)
