"""Reads the household's battery charging advice from Thuisbord.

`GET /battery-advice` (contract 0.28.0) answers `charge`, `hold` or `normal`. The integration
only reads it and shows it as sensors; it sends nothing for it and never switches a battery. Whether
anything acts on the advice is the household's own automation, and by default nothing does.

Timing. Thuisbord works the advice out once per quarter of an hour and says when in `next_update_at`,
and it allows 10 requests a minute per key. So the next request goes out just after `next_update_at`
(a few seconds of jitter, so homes do not all ask in the same second), never sooner than a minute
after this one, and never later than 15 minutes. A request that fails is tried again after 5
minutes, or after Retry-After on a 429. Each request carries `If-None-Match`, so an unchanged
advice costs a `304`.

While a request fails, the entities are unavailable rather than showing an advice that may be out of
date. A refused key (401, 403) stops the polling and starts the re-authentication flow.

Nothing here logs the key or the advice.
"""

from __future__ import annotations

from datetime import timedelta
import logging
import random
from typing import TYPE_CHECKING, Final

from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from .api import (
    BatteryAdvice,
    CannotConnect,
    KeyRefused,
    ThuisbordApi,
    TooManyRequests,
    UnexpectedResponse,
)
from .const import DOMAIN

if TYPE_CHECKING:
    from . import ThuisbordConfigEntry

_LOGGER = logging.getLogger(__name__)

MIN_POLL_INTERVAL: Final = timedelta(minutes=1)
MAX_POLL_INTERVAL: Final = timedelta(minutes=15)
RETRY_INTERVAL: Final = timedelta(minutes=5)
RETRY_AFTER_MAX: Final = timedelta(minutes=30)
# Just after `next_update_at`, so the new quarter's advice is there: 5 to 30 seconds.
AFTER_BOUNDARY_MIN_S: Final = 5.0
AFTER_BOUNDARY_SPREAD_S: Final = 25.0


def next_poll_in(advice: BatteryAdvice, now_utc) -> timedelta:
    """How long to wait before asking again, after an answer."""
    wait = advice.next_update_at - now_utc
    wait += timedelta(seconds=AFTER_BOUNDARY_MIN_S + random.random() * AFTER_BOUNDARY_SPREAD_S)
    return min(max(wait, MIN_POLL_INTERVAL), MAX_POLL_INTERVAL)


class BatteryAdviceCoordinator(DataUpdateCoordinator[BatteryAdvice]):
    """Fetches the advice when it is due."""

    config_entry: ThuisbordConfigEntry

    def __init__(
        self, hass: HomeAssistant, entry: ThuisbordConfigEntry, api: ThuisbordApi
    ) -> None:
        """Poll with the entry's key; the first request is made by the caller."""
        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name=f"{DOMAIN} battery advice",
            update_interval=RETRY_INTERVAL,
        )
        self._api = api
        self._etag: str | None = None

    async def _async_update_data(self) -> BatteryAdvice:
        try:
            result = await self._api.get_battery_advice(
                self._etag if self.data is not None else None
            )
        except KeyRefused as err:
            self.config_entry.async_start_reauth(
                self.hass, data={"refusal_code": err.code}
            )
            raise ConfigEntryAuthFailed(f"Thuisbord refused the key ({err.code})") from None
        except TooManyRequests as err:
            wait = timedelta(seconds=err.retry_after) if err.retry_after else RETRY_INTERVAL
            self.update_interval = min(max(wait, MIN_POLL_INTERVAL), RETRY_AFTER_MAX)
            raise UpdateFailed("Thuisbord asked to wait (HTTP 429)") from None
        except (CannotConnect, UnexpectedResponse) as err:
            self.update_interval = RETRY_INTERVAL
            raise UpdateFailed(f"Thuisbord could not be read ({err})") from None

        advice = result.advice if result.advice is not None else self.data
        if advice is None:
            # A 304 without anything to compare to cannot happen; ask again without the ETag.
            self._etag = None
            self.update_interval = MIN_POLL_INTERVAL
            raise UpdateFailed("Thuisbord answered 304 without an advice")
        self._etag = result.etag
        self.update_interval = next_poll_in(advice, dt_util.utcnow())
        return advice
