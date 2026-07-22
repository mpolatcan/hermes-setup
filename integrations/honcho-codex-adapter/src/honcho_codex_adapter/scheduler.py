from __future__ import annotations

import asyncio
import time
from collections import deque
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass, field
from typing import Callable


_QUEUE_CLASSES = ("dialectic", "summary", "deriver", "dream")
_WEIGHTED_CYCLE = (
    *("dialectic",) * 8,
    *("summary",) * 4,
    *("deriver",) * 2,
    "dream",
)


def queue_class_for_model(model: str) -> str:
    if model.startswith("honcho-dialectic-"):
        return "dialectic"
    if model == "honcho-summary-luna":
        return "summary"
    if model.startswith("honcho-dream-"):
        return "dream"
    return "deriver"


@dataclass(slots=True)
class _Waiter:
    sequence: int
    queue_class: str
    enqueued_at: float
    event: asyncio.Event = field(default_factory=asyncio.Event)
    granted: bool = False


class QueueLease(AbstractAsyncContextManager["QueueLease"]):
    def __init__(
        self,
        scheduler: "WeightedPriorityScheduler",
        *,
        queue_class: str,
        enqueued_at: float,
        granted_at: float,
    ) -> None:
        self._scheduler = scheduler
        self._released = False
        self.queue_class = queue_class
        self.wait_seconds = max(0.0, granted_at - enqueued_at)

    async def __aenter__(self) -> "QueueLease":
        return self

    async def __aexit__(self, exc_type, exc, traceback) -> None:
        await self.release()

    async def release(self) -> None:
        if self._released:
            return
        self._released = True
        await self._scheduler._release()


class WeightedPriorityScheduler:
    """Single-worker bounded scheduler with weighted priority and age boosting."""

    def __init__(
        self,
        capacity: int,
        *,
        aging_seconds: float = 30.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if capacity < 1:
            raise ValueError("capacity must be at least 1")
        if aging_seconds <= 0:
            raise ValueError("aging_seconds must be greater than 0")
        self.capacity = capacity
        self.aging_seconds = aging_seconds
        self._clock = clock
        self._lock = asyncio.Lock()
        self._active = False
        self._admitted = 0
        self._sequence = 0
        self._cycle_index = 0
        self._waiters: dict[str, deque[_Waiter]] = {
            queue_class: deque() for queue_class in _QUEUE_CLASSES
        }

    async def try_acquire(self, model: str) -> QueueLease | None:
        queue_class = queue_class_for_model(model)
        enqueued_at = self._clock()
        async with self._lock:
            if self._admitted >= self.capacity:
                return None
            self._admitted += 1
            if not self._active and not self._has_waiters_locked():
                self._active = True
                return QueueLease(
                    self,
                    queue_class=queue_class,
                    enqueued_at=enqueued_at,
                    granted_at=self._clock(),
                )
            waiter = _Waiter(
                sequence=self._sequence,
                queue_class=queue_class,
                enqueued_at=enqueued_at,
            )
            self._sequence += 1
            self._waiters[queue_class].append(waiter)

        try:
            await waiter.event.wait()
        except asyncio.CancelledError:
            async with self._lock:
                if waiter.granted:
                    self._active = False
                    self._admitted -= 1
                    self._grant_next_locked()
                else:
                    self._waiters[queue_class].remove(waiter)
                    self._admitted -= 1
            raise

        return QueueLease(
            self,
            queue_class=queue_class,
            enqueued_at=enqueued_at,
            granted_at=self._clock(),
        )

    async def _release(self) -> None:
        async with self._lock:
            if not self._active:
                raise RuntimeError("scheduler release without an active lease")
            self._active = False
            self._admitted -= 1
            self._grant_next_locked()

    def _has_waiters_locked(self) -> bool:
        return any(self._waiters.values())

    def _grant_next_locked(self) -> None:
        waiter = self._select_next_locked()
        if waiter is None:
            return
        self._active = True
        waiter.granted = True
        waiter.event.set()

    def _select_next_locked(self) -> _Waiter | None:
        heads = [queue[0] for queue in self._waiters.values() if queue]
        if not heads:
            return None

        now = self._clock()
        aged = [waiter for waiter in heads if now - waiter.enqueued_at >= self.aging_seconds]
        if aged:
            selected = min(aged, key=lambda waiter: (waiter.enqueued_at, waiter.sequence))
            return self._waiters[selected.queue_class].popleft()

        for offset in range(len(_WEIGHTED_CYCLE)):
            index = (self._cycle_index + offset) % len(_WEIGHTED_CYCLE)
            queue_class = _WEIGHTED_CYCLE[index]
            queue = self._waiters[queue_class]
            if queue:
                self._cycle_index = (index + 1) % len(_WEIGHTED_CYCLE)
                return queue.popleft()
        raise RuntimeError("scheduler waiters exist outside the weighted cycle")
