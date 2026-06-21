import threading
import time
from collections.abc import Callable, Iterable

from .model import Host, NoHealthyHostError


class Router:
    """Thread-safe host cache + round-robin picker + local circuit breaker.

    Transport-agnostic: shared by both the sync and async clients. The lock is uncontended
    under asyncio (single thread) and cheap under threads.
    """

    def __init__(self, breaker_cooldown: float = 10.0, clock: Callable[[], float] = time.monotonic):
        self._cooldown = breaker_cooldown
        self._clock = clock
        self._lock = threading.Lock()
        self._hosts: tuple[Host, ...] = ()
        self._idx = 0
        self._dead: dict[Host, float] = {}

    def update(self, hosts: Iterable[Host]) -> None:
        with self._lock:
            self._hosts = tuple(hosts)
            live = set(self._hosts)
            self._dead = {h: t for h, t in self._dead.items() if h in live}

    def snapshot(self) -> tuple[Host, ...]:
        with self._lock:
            return self._hosts

    def mark_down(self, host: Host) -> None:
        with self._lock:
            self._dead[host] = self._clock() + self._cooldown

    def clear(self, host: Host) -> None:
        with self._lock:
            self._dead.pop(host, None)

    def pick(self) -> Host:
        with self._lock:
            hosts = self._hosts
            if not hosts:
                raise NoHealthyHostError("discovery cache is empty")
            now = self._clock()
            n = len(hosts)
            for _ in range(n):
                host = hosts[self._idx % n]
                self._idx += 1
                if self._dead.get(host, 0.0) <= now:
                    return host
            # every host is in cooldown: hand one back anyway rather than failing hard —
            # a possibly-degraded host beats refusing all traffic.
            host = hosts[self._idx % n]
            self._idx += 1
            return host
