"""Common interface for source-specific job collectors."""

from __future__ import annotations

import asyncio
import random
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any

from playwright.async_api import BrowserContext

from core.models import JobRecord


@dataclass(slots=True)
class CollectorResult:
    """Raw result and counters returned by one source adapter."""

    source: str
    queries: list[str] = field(default_factory=list)
    records: list[JobRecord] = field(default_factory=list)
    found: int = 0
    errors: int = 0
    error_messages: list[str] = field(default_factory=list)


class BaseCollector(ABC):
    """Contract shared by LinkedIn, WTTJ, and career-page adapters."""

    source_name: str

    @abstractmethod
    async def collect(
        self,
        context: BrowserContext,
        queries: list[dict[str, str]],
        limit: int,
    ) -> CollectorResult:
        """Collect records for the selected query/category pairs."""
        raise NotImplementedError


def setting(settings: dict[str, Any], *keys: str, default: Any = None) -> Any:
    """Read a nested setting without coupling adapters to a config class."""
    value: Any = settings
    for key in keys:
        if not isinstance(value, dict):
            return default
        value = value.get(key)
    return default if value is None else value


class HttpStatusError(RuntimeError):
    """A non-OK HTTP answer, kept with its status so a caller can tell a rate
    limit (429) from other failures."""

    def __init__(self, status: int) -> None:
        super().__init__(f"HTTP {status}")
        self.status = status


async def get_with_retry(
    context: BrowserContext,
    url: str,
    *,
    timeout_ms: int,
    retry_attempts: int = 0,
    delay_min_seconds: float = 1.0,
    delay_max_seconds: float = 1.0,
):
    """GET a URL through the browser's request context, retrying transient failures.

    `retry_attempts` and `delay_max_seconds` are configured in settings.yaml
    but were never read by any collector — a single dropped connection or a
    momentary non-2xx from an ATS/RSS/JSON endpoint permanently failed that
    board/query for the whole run. Retries a non-OK response or a raised
    exception (timeout, connection reset) up to `retry_attempts` extra times,
    waiting a jittered delay drawn from [delay_min_seconds, delay_max_seconds]
    between attempts.
    """
    attempts = max(1, retry_attempts + 1)
    last_error: Exception = RuntimeError(f"GET failed with no attempts made: {url}")
    for attempt in range(attempts):
        try:
            response = await context.request.get(url, timeout=timeout_ms)
            if response.ok:
                return response
            last_error = HttpStatusError(response.status)
        except Exception as error:  # noqa: BLE001 - surfaced to the caller after retries are exhausted
            last_error = error
        if attempt < attempts - 1:
            low, high = sorted((delay_min_seconds, delay_max_seconds))
            await asyncio.sleep(random.uniform(low, max(low, high)))
    raise last_error


def per_query_share(limit: int, query_count: int) -> int:
    """Fair per-query cap so one query can't exhaust a source's whole limit.

    Collecting query-by-query with only a shared total cap lets an early,
    high-volume query (e.g. "Data Analyst") consume the entire limit before
    later queries in the same run get a single result. Capping each query at
    its floor share still lets the overall limit go unfilled when a query
    has fewer matches than its share, but that's a fairer failure mode than
    silently starving whole categories.
    """
    if query_count <= 0:
        return max(limit, 0)
    return max(1, limit // query_count)
