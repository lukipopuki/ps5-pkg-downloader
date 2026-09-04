"""A shared token bucket that caps the aggregated download bandwidth."""

from __future__ import annotations

import asyncio
import time
from typing import Optional


class RateLimiter:
    """Global byte/second limit shared by every worker.

    ``rate <= 0`` disables limiting entirely (the default), in which case
    ``acquire`` returns immediately without touching the lock.
    """

    def __init__(self, rate: float = 0.0, burst_seconds: float = 1.0):
        self._rate = max(0.0, rate)
        self._burst_seconds = max(0.1, burst_seconds)
        self._tokens = self._capacity
        self._updated = time.monotonic()
        self._lock = asyncio.Lock()

    @property
    def _capacity(self) -> float:
        return self._rate * self._burst_seconds if self._rate > 0 else 0.0

    @property
    def rate(self) -> float:
        return self._rate

    def set_rate(self, rate: float) -> None:
        self._rate = max(0.0, rate)
        self._tokens = min(self._tokens, self._capacity)
        self._updated = time.monotonic()

    async def acquire(self, amount: int) -> None:
        if self._rate <= 0 or amount <= 0:
            return
        while True:
            async with self._lock:
                now = time.monotonic()
                elapsed = now - self._updated
                self._updated = now
                self._tokens = min(self._capacity, self._tokens + elapsed * self._rate)

                if self._tokens >= amount:
                    self._tokens -= amount
                    return

                if amount > self._capacity:
                    # A single chunk larger than the whole bucket can never be
                    # covered outright. Take it on credit once the balance is
                    # positive again; the debt is what enforces the average
                    # rate, so it must be repaid before the next chunk passes.
                    if self._tokens > 0:
                        self._tokens -= amount
                        return
                    wait_for = (-self._tokens + 1) / self._rate
                else:
                    wait_for = (amount - self._tokens) / self._rate
            await asyncio.sleep(min(max(wait_for, 0.01), 1.0))


class Stopwatch:
    """Exponentially smoothed transfer speed in bytes per second."""

    def __init__(self, half_life: float = 5.0):
        self.half_life = half_life
        self._speed = 0.0
        self._last = time.monotonic()
        self._pending = 0

    def add(self, amount: int) -> None:
        self._pending += amount

    def sample(self) -> float:
        now = time.monotonic()
        elapsed = now - self._last
        if elapsed < 0.2:
            return self._speed
        instant = self._pending / elapsed
        weight = 0.5 ** (elapsed / self.half_life)
        self._speed = self._speed * weight + instant * (1 - weight)
        self._pending = 0
        self._last = now
        return self._speed

    @property
    def speed(self) -> float:
        return self._speed

    def eta(self, remaining: int) -> Optional[int]:
        if self._speed <= 0 or remaining <= 0:
            return None
        return int(remaining / self._speed)
