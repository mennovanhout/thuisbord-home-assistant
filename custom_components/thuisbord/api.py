"""The two calls this integration makes to the Thuisbord API.

- `GET /connection` checks a connection key without sending meter data.
- `POST /readings` sends a batch of readings.

Both carry the household's connection key as a bearer token. The key goes to the configured
address only: redirects are never followed. Nothing here logs the key, a reading or a response
body.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Final

import aiohttp

from .const import MAX_READINGS, MIN_INTERVAL_SECONDS
from .reading import Reading

# Home Assistant's shared session has no timeout of its own. A reading is due every 10 seconds;
# one that waits longer stays in the buffer.
REQUEST_TIMEOUT: Final = aiohttp.ClientTimeout(total=15)


class ThuisbordError(Exception):
    """Thuisbord could not be reached, or answered unexpectedly."""


class CannotConnect(ThuisbordError):
    """No answer, or a server error. Trying again later may work."""


class UnexpectedResponse(ThuisbordError):
    """An answer the contract does not list, such as a 404 from an older back-end."""

    def __init__(self, status: int) -> None:
        """Keep the status, never the body."""
        super().__init__(f"HTTP {status}")
        self.status = status


class KeyRefused(ThuisbordError):
    """Thuisbord refused the key: 401 or 403, with the contract's code."""

    def __init__(self, status: int, code: str) -> None:
        """Keep the status and the code, never the key."""
        super().__init__(f"HTTP {status} {code}")
        self.status = status
        self.code = code


class TooManyRequests(ThuisbordError):
    """429: try again later."""


@dataclass(frozen=True, slots=True)
class ConnectionInfo:
    """What `GET /connection` tells a bridge."""

    max_readings: int = MAX_READINGS
    min_interval_seconds: int = MIN_INTERVAL_SECONDS
    # False when the key is valid but the household has not agreed yet, so readings would be
    # refused with `consent_not_recorded`. Absent means the back-end refuses such a key itself.
    consent_given: bool = True


@dataclass(frozen=True, slots=True)
class Answer:
    """An answer to `POST /readings`. Status 0 means there was no answer."""

    status: int
    body: dict[str, Any] | None
    retry_after: str | None

    @property
    def code(self) -> str | None:
        """The contract's error code, when the body has one."""
        code = self.body.get("code") if self.body else None
        return code if isinstance(code, str) else None


def refusal_code(status: int, body: dict[str, Any] | None) -> str:
    """Return the code of a 401 or 403, or a generic one when the body has none."""
    code = body.get("code") if body else None
    if isinstance(code, str) and code:
        return code
    return "unauthenticated" if status == 401 else "forbidden"


class ThuisbordApi:
    """Calls the Thuisbord API with one household's connection key."""

    def __init__(
        self, session: aiohttp.ClientSession, base_url: str, key: str, user_agent: str
    ) -> None:
        """Use Home Assistant's shared session."""
        self._session = session
        self._base_url = base_url.rstrip("/")
        self._key = key
        self._user_agent = user_agent

    def __repr__(self) -> str:
        """Never show the key."""
        return f"ThuisbordApi({self._base_url!r})"

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._key}",
            "Accept": "application/json",
            "User-Agent": self._user_agent,
        }

    async def check_connection(self) -> ConnectionInfo:
        """Check the key. Raises KeyRefused, TooManyRequests, CannotConnect or UnexpectedResponse."""
        try:
            async with self._session.get(
                f"{self._base_url}/connection",
                headers=self._headers(),
                allow_redirects=False,
                timeout=REQUEST_TIMEOUT,
            ) as response:
                status = response.status
                body = await _json(response)
        except (aiohttp.ClientError, TimeoutError) as err:
            raise CannotConnect from err

        if status == 200 and body is not None:
            return ConnectionInfo(
                max_readings=_positive_int(body.get("max_readings"), MAX_READINGS),
                min_interval_seconds=_positive_int(
                    body.get("min_interval_seconds"), MIN_INTERVAL_SECONDS
                ),
                consent_given=body.get("consent_given") is not False,
            )
        if status in (401, 403):
            raise KeyRefused(status, refusal_code(status, body))
        if status == 429:
            raise TooManyRequests
        if status >= 500:
            raise CannotConnect
        raise UnexpectedResponse(status)

    async def send_readings(self, readings: Sequence[Reading]) -> Answer:
        """Send a batch. Never raises: a failure is an Answer with status 0."""
        try:
            async with self._session.post(
                f"{self._base_url}/readings",
                headers=self._headers(),
                json={"readings": list(readings)},
                allow_redirects=False,
                timeout=REQUEST_TIMEOUT,
            ) as response:
                return Answer(
                    status=response.status,
                    body=await _json(response),
                    retry_after=response.headers.get("Retry-After"),
                )
        except (aiohttp.ClientError, TimeoutError):
            # Offline, DNS, TLS or a timeout. The error itself is not kept or logged.
            return Answer(status=0, body=None, retry_after=None)


async def _json(response: aiohttp.ClientResponse) -> dict[str, Any] | None:
    """Return the body as a JSON object, or None."""
    try:
        body = await response.json(content_type=None)
    except (aiohttp.ClientError, ValueError):
        return None
    return body if isinstance(body, dict) else None


def _positive_int(value: object, default: int) -> int:
    if isinstance(value, int) and not isinstance(value, bool) and value > 0:
        return value
    return default
