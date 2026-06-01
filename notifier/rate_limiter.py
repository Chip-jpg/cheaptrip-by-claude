from __future__ import annotations

import asyncio
from collections import deque
from datetime import datetime, timedelta
from typing import Deque

from config import get_settings
from utils.logging_config import get_logger

log = get_logger(__name__)


class RateLimiter:
    """
    Token-bucket style rate limiter for Telegram alerts.

    Hard limits per spec:
    - Instant alerts: max 5/hour
    - Digest: 1/day
    - No duplicate alerts ever (handled by dedup layer)
    """

    def __init__(self) -> None:
        settings = get_settings()
        self._instant_max = settings.instant_alerts_per_hour
        self._instant_sent: Deque[datetime] = deque()
        self._digest_sent: Deque[datetime] = deque()
        self._lock = asyncio.Lock()

    def _prune_old(self, queue: Deque[datetime], window: timedelta) -> None:
        cutoff = datetime.utcnow() - window
        while queue and queue[0] < cutoff:
            queue.popleft()

    async def can_send_instant(self) -> bool:
        async with self._lock:
            self._prune_old(self._instant_sent, timedelta(hours=1))
            return len(self._instant_sent) < self._instant_max

    async def can_send_digest(self) -> bool:
        async with self._lock:
            self._prune_old(self._digest_sent, timedelta(hours=24))
            return len(self._digest_sent) == 0

    async def record_instant(self) -> None:
        async with self._lock:
            self._instant_sent.append(datetime.utcnow())

    async def record_digest(self) -> None:
        async with self._lock:
            self._digest_sent.append(datetime.utcnow())

    async def instant_remaining(self) -> int:
        async with self._lock:
            self._prune_old(self._instant_sent, timedelta(hours=1))
            return max(0, self._instant_max - len(self._instant_sent))
