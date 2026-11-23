"""Minimal async client for the Gemini Decisions API.

Uses Home Assistant's shared aiohttp session, so no extra Python packages are
needed and nothing can clash with the google-genai version core pins.
"""

from __future__ import annotations

import asyncio
from collections import deque
from dataclasses import dataclass
from datetime import date
from email.utils import parsedate_to_datetime
import json
import time
from typing import Any

import aiohttp

from . import const

def _retryable(status: int) -> bool:
    return status in (408, 429) or status >= 500
_BLOCK_CODES = frozenset(
    {
        "safety",
        "prohibited_content",
        "blocklist",
        "image_safety",
        "content_blocked",
        "other",
    }
)


class DecisionsError(Exception):
    """Base error. The message is safe to show to the user."""


class DecisionsAuthError(DecisionsError):
    """The API key was rejected."""


class DecisionsPermissionError(DecisionsError):
    """The key is valid but cannot use this API or model."""


class DecisionsBlockedError(DecisionsError):
    """Google's input filters blocked the request."""


class DecisionsRateLimitError(DecisionsError):
    """Google's quota or rate limit was hit."""


class DecisionsConnectionError(DecisionsError):
    """Google could not be reached in time."""


class DecisionsLimitReached(DecisionsError):
    """One of this integration's own safety caps was hit."""


def _error_details(body: Any) -> tuple[str, str, str, set[str]]:
    """Return (code, status, message, reasons) from a Google error body."""
    if not isinstance(body, dict) or not isinstance(body.get("error"), dict):
        return "", "", "", set()
    err = body["error"]
    reasons: set[str] = set()
    for detail in err.get("details") or []:
        if isinstance(detail, dict) and isinstance(detail.get("reason"), str):
            reasons.add(detail["reason"])
    message = err.get("message")
    return (
        str(err.get("code", "")).lower(),
        str(err.get("status", "")),
        message[:200] if isinstance(message, str) else "",
        reasons,
    )


def classify_error(http_status: int, body: Any) -> DecisionsError:
    """Turn a non-200 reply into the right exception."""
    code, status, message, reasons = _error_details(body)
    lowered = message.lower()
    if (
        "API_KEY_INVALID" in reasons
        or "api key not valid" in lowered
        or "api key expired" in lowered
        or http_status == 401
    ):
        return DecisionsAuthError("Google rejected the API key")
    if http_status == 403:
        if any(reason.startswith("API_KEY_") for reason in reasons):
            return DecisionsAuthError("Google rejected the API key")
        return DecisionsPermissionError(
            f"This API key can't use the Decisions API: {message or status}"
        )
    if http_status == 400 and code in _BLOCK_CODES:
        return DecisionsBlockedError(f"Gemini blocked this input ({code})")
    if http_status == 429:
        if "per day" in lowered or "daily" in lowered or "perday" in lowered:
            return DecisionsRateLimitError(
                "Google's daily quota for this key is used up; it resets at midnight Pacific time"
            )
        return DecisionsRateLimitError("Google's rate limit was hit, try again shortly")
    if http_status == 404:
        return DecisionsPermissionError(
            f"Google doesn't know this model or endpoint: {message or status}"
        )
    if 500 <= http_status < 600:
        return DecisionsConnectionError(f"Google had a server error ({http_status})")
    return DecisionsError(
        f"Google rejected the request ({http_status}): {message or status or 'no details'}"
    )


def _retry_after(headers: Any) -> float:
    """Return the Retry-After delay in seconds, 0 if absent."""
    value = headers.get("Retry-After") if headers else None
    if not value:
        return 0.0
    try:
        return max(0.0, float(value))
    except ValueError:
        pass
    try:
        return max(0.0, parsedate_to_datetime(value).timestamp() - time.time())
    except (TypeError, ValueError):
        return 0.0


async def _read_json(resp: aiohttp.ClientResponse) -> Any:
    """Read a JSON body, tolerating wrong content types and garbage."""
    try:
        return await resp.json(content_type=None)
    except (aiohttp.ContentTypeError, json.JSONDecodeError, ValueError):
        return None


class DecisionsClient:
    """Talks to the Decisions endpoint."""

    def __init__(self, session: aiohttp.ClientSession, api_key: str) -> None:
        """Store the session and key."""
        self._session = session
        self._api_key = api_key

    @property
    def _headers(self) -> dict[str, str]:
        # The key only ever travels in this header, never in a URL, so it
        # cannot leak into aiohttp exception text.
        return {"x-goog-api-key": self._api_key, "Content-Type": "application/json"}

    async def async_validate_key(self, timeout: float = 10) -> None:
        """Check the key with the free model list. 429 counts as valid."""
        try:
            async with asyncio.timeout(timeout):
                async with self._session.get(
                    f"{const.BASE_URL}/models",
                    params={"pageSize": "1"},
                    headers=self._headers,
                    allow_redirects=False,
                ) as resp:
                    if resp.status in (200, 429):
                        return
                    raise classify_error(resp.status, await _read_json(resp))
        except TimeoutError as err:
            raise DecisionsConnectionError("Google didn't answer in time") from err
        except ValueError as err:
            raise DecisionsAuthError("The API key contains characters Google can't accept") from err
        except aiohttp.ClientError as err:
            raise DecisionsConnectionError("Google couldn't be reached") from err

    async def async_create(self, body: bytes, budget: float) -> dict[str, Any]:
        """POST a prepared JSON body, with one retry inside one time budget."""
        deadline = time.monotonic() + budget
        attempt = 0
        while True:
            attempt += 1
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise DecisionsConnectionError("Google didn't answer in time")
            retry_wait: float | None = None
            last_error: DecisionsError | None = None
            try:
                async with asyncio.timeout(remaining):
                    async with self._session.post(
                        f"{const.BASE_URL}/decisions",
                        data=body,
                        headers=self._headers,
                        allow_redirects=False,
                    ) as resp:
                        payload = await _read_json(resp)
                        if resp.status == 200:
                            if not isinstance(payload, dict):
                                raise DecisionsError("Google sent a reply that isn't JSON")
                            return payload
                        error = classify_error(resp.status, payload)
                        daily = isinstance(error, DecisionsRateLimitError) and "daily" in str(error)
                        if not _retryable(resp.status) or daily or attempt > 1:
                            raise error
                        wait = _retry_after(resp.headers)
                        if wait > const.MAX_RETRY_AFTER:
                            raise error
                        retry_wait = max(wait, 0.5)
                        last_error = error
            except TimeoutError as err:
                # A timed-out request may still be billed, so never retry it.
                raise DecisionsConnectionError("Google didn't answer in time") from err
            except (
                aiohttp.ClientConnectionError,
                aiohttp.ClientPayloadError,
            ) as err:
                if attempt > 1:
                    raise DecisionsConnectionError("Google couldn't be reached") from err
                retry_wait = 0.5
                last_error = DecisionsConnectionError("Google couldn't be reached")
            except ValueError as err:
                raise DecisionsAuthError("The API key contains characters Google can't accept") from err
            except aiohttp.ClientError as err:
                raise DecisionsConnectionError("Google couldn't be reached") from err
            if retry_wait is None or deadline - time.monotonic() <= retry_wait:
                raise last_error or DecisionsConnectionError("Google didn't answer in time")
            await asyncio.sleep(retry_wait)


@dataclass
class UsageSnapshot:
    """Counters persisted across restarts."""

    day: str
    requests: int = 0
    tokens: int = 0


class RequestLimiter:
    """Concurrency, per-minute and per-day caps so a runaway automation can't run up a bill."""

    def __init__(self, usage: UsageSnapshot) -> None:
        """Start from persisted usage."""
        self.usage = usage
        self._semaphore = asyncio.Semaphore(const.MAX_CONCURRENT)
        self._recent: deque[float] = deque()

    def _roll_day(self, today: date) -> None:
        if self.usage.day != today.isoformat():
            self.usage = UsageSnapshot(day=today.isoformat())

    def check(self, today: date, per_minute: int, per_day: int) -> None:
        """Raise if a cap is already hit, without counting anything."""
        self._roll_day(today)
        if self.usage.requests >= per_day:
            raise DecisionsLimitReached(
                f"Gemini Decisions hit its daily cap of {per_day} requests; "
                "it resets at midnight, or raise the cap in its options"
            )
        now = time.monotonic()
        recent = sum(1 for stamp in self._recent if now - stamp <= 60)
        if recent >= per_minute:
            raise DecisionsLimitReached(
                f"Gemini Decisions hit its limit of {per_minute} requests per minute"
            )

    def reserve(self, today: date, per_minute: int, per_day: int) -> None:
        """Count one request or raise if a cap is hit."""
        self._roll_day(today)
        if self.usage.requests >= per_day:
            raise DecisionsLimitReached(
                f"Gemini Decisions hit its daily cap of {per_day} requests; "
                "it resets at midnight, or raise the cap in its options"
            )
        now = time.monotonic()
        while self._recent and now - self._recent[0] > 60:
            self._recent.popleft()
        if len(self._recent) >= per_minute:
            raise DecisionsLimitReached(
                f"Gemini Decisions hit its limit of {per_minute} requests per minute"
            )
        self._recent.append(now)
        self.usage.requests += 1

    def add_tokens(self, tokens: int) -> None:
        """Record billed input tokens."""
        self.usage.tokens += max(0, tokens)

    @property
    def semaphore(self) -> asyncio.Semaphore:
        """Limit concurrent requests."""
        return self._semaphore
