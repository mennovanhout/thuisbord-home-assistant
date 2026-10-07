"""Send readings to `POST /readings` and do what the contract asks of a bridge with each answer.

- At most one reading every 10 seconds: a reading less than 10 seconds after the previous one
  that was queued is refused here, so every `measured_at` is at least 10 seconds apart, also
  within a batch.
- Normally each reading goes out on its own as soon as it is queued. Readings that wait, while
  Thuisbord is unreachable, sit in a small bounded buffer: one per minute (the first, which is
  the one the back-end stores) plus the newest (for the live view), at most 1440, so 24 hours.
  When it is full, the oldest is dropped. Readings older than the contract's 24 hours are
  dropped before they are sent.
- Waiting readings go out in batches of at most 360, oldest first.
- 202: the sent readings leave the buffer.
- No answer, 5xx, or an answer the contract does not list: the batch is sent again after a
  growing wait (10 s, 20 s, 40 s ... at most 5 minutes, plus or minus 20 %).
- 429: waits Retry-After seconds. On `too_frequent`, readings measured before `next_allowed_at`
  are dropped first.
- 413: smaller batches.
- 422: drops the readings named in `errors`, or the whole batch when `readings` itself is named,
  and sends the rest once. A batch is never sent again unchanged.
- 401 and 403: stops, drops the buffer, and asks the household for a new key.
- Requests are at least 5 seconds apart, so a key never makes more than 12 a minute.

The readings stay in memory only and are never logged. The status holds no key and no reading.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from email.utils import parsedate_to_datetime
from enum import StrEnum
import logging
import math
import random
import re
from typing import Any, Final

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import CALLBACK_TYPE, HomeAssistant, callback
from homeassistant.helpers.event import async_call_later
from homeassistant.util import dt as dt_util

from .api import Answer, ThuisbordApi, refusal_code
from .const import MAX_READINGS, MIN_INTERVAL_SECONDS
from .reading import Reading

_LOGGER = logging.getLogger(__name__)

MIN_REQUEST_GAP: Final = 5.0
MAX_BUFFER: Final = 1440
# The back-end refuses readings older than 24 hours by its clock; keep 10 minutes for skew.
MAX_AGE: Final = 24 * 3600.0 - 600.0
BACKOFF_BASE: Final = 10.0
BACKOFF_MAX: Final = 300.0
RETRY_AFTER_MAX: Final = 3600.0

CLOCK_CODES: Final = frozenset({"in_future", "too_old"})
READINGS_FIELD: Final = re.compile(r"^readings\.(\d+)(?:\.([a-z0-9_]+))?")


class State(StrEnum):
    """What the sender is doing. These are the options of the status sensor."""

    WAITING = "waiting"
    SENDING = "sending"
    RETRYING = "retrying"
    REFUSED = "refused"
    STOPPED = "stopped"


class Added(StrEnum):
    """What happened to a reading offered to the sender."""

    QUEUED = "queued"
    TOO_SOON = "too_soon"
    STOPPED = "stopped"


@dataclass(slots=True, eq=False)
class _Entry:
    reading: Reading
    at: float
    minute: int
    first: bool = True
    sending: bool = False
    after_422: bool = False


@dataclass(frozen=True, slots=True)
class Status:
    """What the status sensor and diagnostics show. Holds no key and no reading."""

    state: State
    code: str | None = None
    http: int | None = None
    fields: tuple[str, ...] = ()
    buffered: int = 0
    last_sent_at: datetime | None = None
    next_attempt_at: datetime | None = None

    def as_dict(self) -> dict[str, Any]:
        """Return the status for diagnostics."""
        return {
            "state": self.state.value,
            "code": self.code,
            "http": self.http,
            "fields": list(self.fields),
            "buffered": self.buffered,
            "last_sent_at": _iso(self.last_sent_at),
            "next_attempt_at": _iso(self.next_attempt_at),
        }


def _iso(moment: datetime | None) -> str | None:
    return None if moment is None else moment.isoformat()


def parse_retry_after(header: str | None, now: float) -> float | None:
    """Return the wait in seconds from a Retry-After header, or None."""
    if header is None or not header.strip():
        return None
    text = header.strip()
    if text.isdigit():
        wait = float(text)
    else:
        try:
            wait = parsedate_to_datetime(text).timestamp() - now
        except (TypeError, ValueError):
            return None
    return min(max(wait, 1.0), RETRY_AFTER_MAX)


def _parse_time(value: object) -> float | None:
    if not isinstance(value, str):
        return None
    moment = dt_util.parse_datetime(value)
    return None if moment is None else moment.timestamp()


class ReadingSender:
    """Buffers and sends one household's readings."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        api: ThuisbordApi,
        *,
        max_readings: int = MAX_READINGS,
        min_interval: int = MIN_INTERVAL_SECONDS,
        on_stopped: Callable[[str], None],
    ) -> None:
        """Send with `api`; call `on_stopped` with the code when the key is refused."""
        self._hass = hass
        self._entry = entry
        self._api = api
        self._max_batch = max(1, min(max_readings, MAX_READINGS))
        self._min_spacing = max(min_interval, MIN_INTERVAL_SECONDS)
        self._on_stopped = on_stopped
        self._listeners: list[CALLBACK_TYPE] = []

        self._queue: list[_Entry] = []
        self._in_flight = False
        self._cancel_timer: CALLBACK_TYPE | None = None
        self._next_attempt_at: float | None = None
        self._failures = 0
        self._batch_limit = self._max_batch
        self._last_queued_at: float | None = None
        self._not_before: float | None = None
        self._last_request_at: float | None = None
        self._last_sent_at: float | None = None
        self._stopped_code: str | None = None
        self._disposed = False

        self._state = State.WAITING
        self._code: str | None = None
        self._http: int | None = None
        self._fields: tuple[str, ...] = ()

    # --- public ---------------------------------------------------------------------------

    @callback
    def async_add(self, reading: Reading, measured_at: datetime) -> Added:
        """Offer one reading, measured at `measured_at`."""
        if self._disposed or self._stopped_code is not None:
            return Added.STOPPED
        at = float(math.floor(measured_at.timestamp()))
        if self._last_queued_at is not None and at - self._last_queued_at < self._min_spacing:
            return Added.TOO_SOON
        if self._not_before is not None and at < self._not_before:
            return Added.TOO_SOON

        self._last_queued_at = at
        self._enqueue(_Entry(reading, at, int(at // 60)))
        self._notify()
        self._pump()
        return Added.QUEUED

    @property
    def status(self) -> Status:
        """The current status."""
        return Status(
            state=self._state,
            code=self._code,
            http=self._http,
            fields=self._fields,
            buffered=len(self._queue),
            last_sent_at=_moment(self._last_sent_at),
            next_attempt_at=_moment(self._next_attempt_at)
            if self._state is State.RETRYING
            else None,
        )

    @callback
    def async_add_listener(self, listener: CALLBACK_TYPE) -> CALLBACK_TYPE:
        """Call `listener` whenever the status changes. Returns a function that removes it."""
        self._listeners.append(listener)

        @callback
        def remove() -> None:
            self._listeners.remove(listener)

        return remove

    @callback
    def async_stop(self) -> None:
        """Stop for good: drop the buffer and cancel any wait."""
        self._disposed = True
        self._clear_timer()
        self._queue = []

    # --- the buffer -----------------------------------------------------------------------

    def _enqueue(self, entry: _Entry) -> None:
        last = self._queue[-1] if self._queue else None
        # A waiting reading that is not the first of its minute was kept only as the newest
        # one, for the live view. The new reading is newer, so it takes that place.
        if last is not None and not last.sending and not last.first:
            self._queue.pop()
            last = self._queue[-1] if self._queue else None
        entry.first = not (last is not None and last.minute == entry.minute)
        self._queue.append(entry)

        while len(self._queue) > MAX_BUFFER:
            oldest = next((e for e in self._queue if not e.sending), None)
            if oldest is None:
                break
            self._queue.remove(oldest)

    def _remove(self, entries: list[_Entry] | set[_Entry]) -> None:
        gone = {id(e) for e in entries}
        self._queue = [e for e in self._queue if id(e) not in gone]

    def _drop_before(self, at: float) -> None:
        self._queue = [e for e in self._queue if e.sending or e.at >= at]

    def _raise_not_before(self, at: float) -> None:
        self._not_before = at if self._not_before is None else max(self._not_before, at)
        self._drop_before(self._not_before)

    # --- sending --------------------------------------------------------------------------

    def _pump(self) -> None:
        if (
            self._disposed
            or self._in_flight
            or self._cancel_timer is not None
            or self._stopped_code is not None
        ):
            return
        now = _now()
        self._drop_before(now - MAX_AGE)
        if not self._queue:
            return
        if self._last_request_at is not None:
            wait = self._last_request_at + MIN_REQUEST_GAP - now
            if wait > 0:
                self._schedule(wait)
                return
        self._in_flight = True
        self._entry.async_create_background_task(
            self._hass, self._send(), "thuisbord_send_readings"
        )

    async def _send(self) -> None:
        batch = self._queue[: self._batch_limit]
        for entry in batch:
            entry.sending = True
        self._last_request_at = _now()

        answer = await self._api.send_readings([e.reading for e in batch])

        if self._disposed:
            return
        self._in_flight = False
        for entry in batch:
            entry.sending = False
        self._handle(answer, batch)

    def _handle(self, answer: Answer, batch: list[_Entry]) -> None:
        status, body, code = answer.status, answer.body, answer.code
        now = _now()

        if 200 <= status < 300:
            self._remove(batch)
            self._failures = 0
            next_allowed = _parse_time(body.get("next_allowed_at") if body else None)
            if next_allowed is not None:
                self._raise_not_before(next_allowed)
            self._last_sent_at = now
            self._set_state(State.SENDING)
            self._pump()
            return

        if status in (401, 403):
            stop = refusal_code(status, body)
            self._stopped_code = stop
            self._queue = []
            self._clear_timer()
            _LOGGER.warning("Thuisbord refused the connection key (%s); sending stopped", stop)
            self._set_state(State.STOPPED, code=stop, http=status)
            self._on_stopped(stop)
            return

        if status == 413:
            max_readings = body.get("max_readings") if body else None
            if isinstance(max_readings, int) and 1 <= max_readings < len(batch):
                limit = max_readings
            else:
                limit = len(batch) // 2
            if limit < 1:
                self._remove(batch)  # one reading too large: it can never be sent
            else:
                self._batch_limit = min(limit, self._max_batch)
            self._pump()
            return

        if status == 422:
            drop, fields, clock = _refused_entries(body, batch)
            drop.update(e for e in batch if e.after_422)
            self._remove(drop)
            for entry in batch:
                if entry not in drop:
                    entry.after_422 = True
            _LOGGER.warning(
                "Thuisbord refused %d of %d readings (%s)",
                len(drop),
                len(batch),
                "clock" if clock else ", ".join(fields) or "readings",
            )
            self._set_state(State.REFUSED, code="clock" if clock else "fields", http=status,
                            fields=fields)
            self._pump()
            return

        if status == 429:
            if code == "too_frequent":
                next_allowed = _parse_time(body.get("next_allowed_at") if body else None)
                if next_allowed is not None:
                    self._raise_not_before(next_allowed)
            self._failures += 1
            wait = parse_retry_after(answer.retry_after, now)
            self._retry_in(
                wait if wait is not None else self._backoff(), "rate_limited", status
            )
            return

        self._failures += 1
        if status == 0:
            reason = "offline"
        elif status >= 500:
            reason = "server_error"
        else:
            reason = "unexpected_response"
        wait = parse_retry_after(answer.retry_after, now) if status == 503 else None
        self._retry_in(wait or self._backoff(), reason, status or None)

    def _backoff(self) -> float:
        base = min(BACKOFF_BASE * 2 ** (self._failures - 1), BACKOFF_MAX)
        return base * (0.8 + 0.4 * random.random())

    def _retry_in(self, wait: float, reason: str, http: int | None) -> None:
        if self._state is not State.RETRYING or self._code != reason:
            _LOGGER.info("Thuisbord could not take the readings (%s); trying again", reason)
        self._schedule(wait)
        self._set_state(State.RETRYING, code=reason, http=http)

    def _schedule(self, wait: float) -> None:
        self._clear_timer()
        self._next_attempt_at = _now() + wait
        self._cancel_timer = async_call_later(self._hass, wait, self._on_timer)

    @callback
    def _on_timer(self, _now: datetime) -> None:
        self._cancel_timer = None
        self._next_attempt_at = None
        self._pump()

    def _clear_timer(self) -> None:
        if self._cancel_timer is not None:
            self._cancel_timer()
        self._cancel_timer = None
        self._next_attempt_at = None

    # --- status ---------------------------------------------------------------------------

    def _set_state(
        self,
        state: State,
        *,
        code: str | None = None,
        http: int | None = None,
        fields: tuple[str, ...] = (),
    ) -> None:
        self._state = state
        self._code = code
        self._http = http
        self._fields = fields
        self._notify()

    def _notify(self) -> None:
        for listener in list(self._listeners):
            listener()


def _now() -> float:
    return dt_util.utcnow().timestamp()


def _moment(at: float | None) -> datetime | None:
    return None if at is None else dt_util.utc_from_timestamp(at)


def _refused_entries(
    body: dict[str, Any] | None, batch: list[_Entry]
) -> tuple[set[_Entry], tuple[str, ...], bool]:
    """The entries a 422 names, the fields it names, and whether the refusal was about time.

    A refusal of `readings` itself, or of something this bridge cannot place, drops the batch.
    """
    drop: set[_Entry] = set()
    fields: set[str] = set()
    clock = False
    errors = body.get("errors") if body else None
    if not isinstance(errors, list) or not errors:
        return set(batch), (), False
    for error in errors:
        name = error.get("field") if isinstance(error, dict) else None
        match = READINGS_FIELD.match(name) if isinstance(name, str) else None
        index = int(match.group(1)) if match else -1
        if match is None or index >= len(batch):
            drop.update(batch)
            continue
        drop.add(batch[index])
        part = match.group(2)
        if part == "measured_at" and error.get("code") in CLOCK_CODES:
            clock = True
        elif part:
            fields.add(part)
    return drop, tuple(sorted(fields)), clock
